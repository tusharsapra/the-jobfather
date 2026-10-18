"""CV engine: deterministic rendering, marker segmentation, content contract, PDF build and promotion."""
import copy
import json
from pathlib import Path
import unittest

from support import DemoWorkspace, REPO

import cv_content
import cv_init
import cv_template
import layout
import memory
import pdf_tools
from applications import Store
from local import digest, read_json, read_text, sha_text

A4 = REPO / 'tests' / 'fixtures' / 'a4'


def pdf_ready():
    return all(pdf_tools.resolve().values())


class CvWorkspace(DemoWorkspace):
    """Demo workspace with an adopted master CV and registered setup (requires TeX for the PDF parts)."""

    def __init__(self, persona=None, with_pdf=True):
        super().__init__(init=False)
        if persona:
            for name in ('identity.json', 'EXPERIENCE.md'):
                (self.root / 'profile' / name).write_bytes((persona / name).read_bytes())
        self.render = cv_init.render(self.root)
        self.render_dir = self.root / layout.RENDERS / self.render['render_id']
        if with_pdf:
            report = cv_init.check(self.root, self.render['render_id'])
            self.report = report
            receipt = {'passed': True, 'reviewed_by': 'test', 'notes': 'Fixture render reviewed.',
                       'render_sha256': {p: digest(self.root / p) for p in report['renders']}}
            cv_init.adopt(self.root, self.render['render_id'], receipt, 'claude-code')
        else:
            (self.root / layout.CV_MASTER).write_bytes((self.render_dir / 'master-cv.tex').read_bytes())
            (self.root / layout.CV_MANIFEST).write_bytes((self.render_dir / 'cv-manifest.json').read_bytes())
        self.init_event = self.initialize()


class TemplateTests(unittest.TestCase):
    def setUp(self):
        self.ws = CvWorkspace(with_pdf=False)
        self.root = self.ws.root

    def tearDown(self):
        self.ws.close()

    def test_render_is_deterministic_and_marks_editable_regions(self):
        identity = read_text(self.root / layout.IDENTITY)
        experience = read_text(self.root / layout.EXPERIENCE)
        a = cv_init.render_texts(identity, experience)
        b = cv_init.render_texts(identity, experience)
        self.assertEqual(a, b)
        source, manifest = a
        self.assertEqual(source, read_text(self.root / layout.CV_MASTER))
        self.assertEqual(sorted(manifest['slots']), ['core_skills', 'hbh', 'lkm', 'summary', 'tools'])
        self.assertEqual(manifest['slots']['lkm']['count'], 4)
        self.assertEqual(manifest['page']['size'], 'Letter')
        self.assertEqual([e['id'] for e in manifest['entries']], ['LKM', 'HBH', 'MFC', 'COOP', 'NBU'])
        self.assertNotIn('<<', source[:source.index(r'\begin{document}')])
        self.assertIn('Apr 2023 – Present', source)

    def test_byte_exact_baseline_and_protected_spans(self):
        data = cv_template.template(self.root)
        self.assertEqual(len(data['slots']), 1 + 4 + 3 + 2)
        assembled = cv_template.assemble(data['baseline_patch'], ws=self.root, data=data)
        self.assertEqual(assembled['source'], read_text(self.root / layout.CV_MASTER))
        changed = assembled['source'].replace('Lakeshore Metrics', 'Other Org', 1)
        self.assertFalse(cv_template.check_source(changed, assembled)['ok'])

    def test_master_edited_outside_is_refused(self):
        with open(self.root / layout.CV_MASTER, 'a', encoding='utf-8') as fh:
            fh.write('% sneaky edit\n')
        with self.assertRaises(ValueError):
            cv_template.template(self.root)

    def test_escape_fragment_round_trip(self):
        text = 'Cut costs 30% & saved $5k for R&D_ops #1 {fast} ~20 a^2 back\\slash'
        spans = [{'start': 4, 'end': 9}]
        decoded = cv_template.fragment(cv_template.render(text, spans))
        self.assertEqual((decoded['text'], decoded['bold_spans']), (text, spans))

    def test_content_contract(self):
        base = cv_content.baseline(self.root)
        self.assertTrue(cv_content.validate(base, self.root)['ok'])
        cases = {
            'missing bullet': lambda p: p['lkm'].pop(),
            'extra field': lambda p: p.update(education='Changed'),
            'tex command': lambda p: p.update(summary='\\input{secret}'),
            'markdown bold': lambda p: p['lkm'].__setitem__(0, 'Built **things**'),
            'evidence id': lambda p: p['hbh'].__setitem__(0, 'Forecasts (HBH-01)'),
            'unknown tool': lambda p: p['tools'].append('Spark'),
            'unsupported script': lambda p: p.update(summary='数据分析师'),
            'too few skills': lambda p: p.update(core_skills=['SQL']),
        }
        for name, mutate in cases.items():
            with self.subTest(name):
                patch = copy.deepcopy(base)
                mutate(patch)
                self.assertFalse(cv_content.validate(patch, self.root)['ok'])
        patch = copy.deepcopy(base)
        patch['tools'].append('BigQuery')  # listed in the tools vocabulary
        self.assertTrue(cv_content.validate(patch, self.root)['ok'])

    def test_emphasis_rules(self):
        data = cv_template.template(self.root)
        patch = copy.deepcopy(data['baseline_patch'])
        patch['lkm'][0] = 'Built a churn model that cut churn 18% for the 2024 cohort.'
        assembled = cv_template.assemble(patch, {'lkm.0': [{'start': 8, 'end': 19}]}, self.root, data)
        self.assertIn(r'\textbf{churn model}', assembled['source'])
        for bad in ({'summary': [{'start': 0, 'end': 4}]},
                    {'lkm.0': [{'start': 0, 'end': len(patch['lkm'][0])}]},
                    {'lkm.0': [{'start': 0, 'end': 5}, {'start': 3, 'end': 8}]}):
            with self.assertRaises(ValueError):
                cv_template.assemble(patch, bad, self.root, data)

    def test_cv_master_cannot_drift_from_experience(self):
        base = dict(memory.current(self.root))
        exp = read_text(self.root / layout.EXPERIENCE)
        dependents = {'profile/PROFILE.md': 'unchanged', 'profile/STORY-BANK.md': 'unchanged',
                      'cv/master-cv.tex': 'unchanged', 'cv/cv-manifest.json': 'unchanged'}

        def change(old, new, suffix, review=None):
            return {'transaction_id': 'TX-cv-binding-' + suffix, 'intent': 'explicit-update',
                    'user_request': 'Correct this claim.', 'platform': 'claude-code', 'scope': 'global',
                    'change_type': 'factual-correction', 'expected_revision': memory.current(self.root)['revision'],
                    'summary': {'before': 'Old wording.', 'after': 'New wording.'}, 'reason': 'Fixture correction.',
                    'review': {'user_authorized': True, 'facts_checked': True, 'scope_checked': True},
                    'dependency_review': review or dependents,
                    'changes': [{'path': layout.EXPERIENCE, 'before_sha256': sha_text(read_text(self.root / layout.EXPERIENCE)),
                                 'replace': {'old': old, 'new': new}}]}
        # A claim-table edit does not touch the CV: "unchanged" is verified by re-rendering.
        memory.apply(change('Informal mentoring; no direct reports.', 'Informal mentoring only.', 'claim'), self.root)
        # A CV bullet edit while claiming "unchanged" is refused.
        with self.assertRaises(ValueError):
            memory.apply(change('cut churn **18%** for the 2024 cohort', 'cut churn **18%** for 2024', 'bullet'), self.root)
        self.assertNotEqual(base['revision'], memory.current(self.root)['revision'])


@unittest.skipUnless(pdf_ready(), 'pdflatex and Poppler are not installed')
class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.ws = CvWorkspace()
        self.root = self.ws.root
        self.store = Store(self.root, test=True)
        app = self.store.create('Example Corp', 'Senior Data Analyst',
                                'Example Corp is hiring a senior data analyst for its in-house team. '
                                'You will own forecasting and dashboards in SQL and Tableau.', 'fixture', 'claude-code')
        self.app = app['application_id']
        self.store.save_analysis(self.app, {
            'employer_type': 'in_house', 'role_track': 'analytics',
            'supporting_excerpts': ['in-house team', 'forecasting and dashboards'],
            'requirements': [{'requirement': 'Forecasting', 'evidence_ids': ['HBH-01'], 'assessment': 'supported'}],
            'fit_summary': 'Strong forecasting evidence.', 'selected_evidence_ids': ['HBH-01', 'LKM-02'],
            'gaps': [], 'emphasis': 'Forecasting'}, 'claude-code')

    def tearDown(self):
        self.ws.close()

    def review(self):
        return {'facts_preserved': True, 'voice_checked': True, 'context_revision': memory.current(self.root)['revision']}

    def compile(self, patch, **kw):
        import cv_pdf
        return cv_pdf.compile_cv(patch, self.root, self.app, True, 'claude-code', review=self.review(), **kw)

    def receipt(self, build):
        return {'passed': True, 'reviewed_by': 'test', 'notes': 'Reviewed the tailored page.',
                'render_sha256': {p: digest(self.root / p) for p in build['render_paths']}}

    def test_baseline_master_was_one_page_letter(self):
        self.assertEqual(self.ws.report['pages'], 1)
        self.assertTrue(self.ws.report['mechanical_ok'], self.ws.report['errors'])
        self.assertTrue((self.root / layout.CV_BASELINE).is_file())

    def test_tailored_compile_and_promote(self):
        import cv_pdf
        patch = cv_content.baseline(self.root)
        patch['lkm'][2] = 'Built forecasting dashboards in Tableau that product leads used for weekly launch decisions.'
        build = self.compile(patch)
        self.assertEqual(build['status'], 'awaiting-visual-review', build.get('error'))
        directory = self.root / build['directory']
        with self.assertRaises(ValueError):
            cv_pdf.promote(directory, dict(self.receipt(build), render_sha256={}), self.root)
        result = cv_pdf.promote(directory, self.receipt(build), self.root)
        self.assertEqual(result['status'], 'promoted')
        self.assertTrue(Path(result['pdf_path']).name.startswith('Alex_Rivera_Example_Corp_Senior_Data_Analyst_'))
        self.assertEqual(cv_pdf.promote(directory, self.receipt(build), self.root)['pdf_path'], result['pdf_path'])
        outputs = [o for o in self.store.load(self.app)['outputs'] if o['kind'] == 'cv-pdf']
        self.assertEqual(outputs[0]['path_root'], 'workspace')
        self.assertEqual(self.store.output_path(self.app, outputs[0]['output_id'])['validation_status'], 'passed')

    def test_overflow_is_measured_and_retries_are_limited(self):
        patch = cv_content.baseline(self.root)
        long_text = ('Built a long-running analytics programme covering forecasting, experimentation, dashboards '
                     'and data quality across product, finance and operations teams with weekly reviews. ')
        patch['lkm'] = [long_text * 3] * 4
        patch['hbh'] = [long_text * 3] * 3
        build = self.compile(patch)
        self.assertEqual(build['status'], 'validation-failed')
        validation = read_json(self.root / build['directory'] / 'validation.json')
        self.assertGreater(validation['pages'], 1)
        self.assertIn('region_feedback', validation['overflow_feedback'])
        retry = self.compile(patch, retry_of=self.root / build['directory'])
        self.assertEqual(retry['retry_count'], 1)
        ok = self.compile(cv_content.baseline(self.root))
        with self.assertRaises(ValueError):
            self.compile(cv_content.baseline(self.root), retry_of=self.root / ok['directory'])

    def test_a4_persona_switches_paper_and_language(self):
        a4 = CvWorkspace(persona=A4)
        try:
            manifest = read_json(a4.root / layout.CV_MANIFEST)
            self.assertEqual((manifest['page']['size'], manifest['language']['babel']), ('A4', 'british'))
            self.assertEqual(a4.report['pages'], 1)
            self.assertTrue(a4.report['mechanical_ok'], a4.report['errors'])
        finally:
            a4.close()


if __name__ == '__main__':
    unittest.main()
