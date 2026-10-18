"""Source import: copies and index, text per format, LinkedIn export parsing and draft entries.

Every fixture is built here at test time from fictional text; no binary files are committed.
"""
import io
import json
import os
import subprocess
import sys
import unittest
from unittest import mock
import zipfile

from support import SCRIPTS, DemoWorkspace

import evidence
import importer
import layout
import managed
import pdf_tools
from local import read_json, read_text

W = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
MC = 'http://schemas.openxmlformats.org/markup-compatibility/2006'


def tiny_pdf(lines):
    """A one-page PDF with a Helvetica text layer, offsets computed so any reader accepts it."""
    escape = lambda t: t.replace('\\', '\\\\').replace('(', '\\(').replace(')', '\\)')
    content = 'BT /F1 12 Tf 72 720 Td 16 TL ' + ' '.join('(%s) Tj T*' % escape(l) for l in lines) + ' ET'
    objects = ['<< /Type /Catalog /Pages 2 0 R >>', '<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
               '<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> '
               '/Contents 5 0 R >>',
               '<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
               '<< /Length %d >>\nstream\n%s\nendstream' % (len(content), content)]
    out, offsets = b'%PDF-1.4\n', []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += ('%d 0 obj\n%s\nendobj\n' % (number, body)).encode('latin-1')
    start = len(out)
    out += ('xref\n0 %d\n0000000000 65535 f \n' % (len(objects) + 1)).encode()
    out += b''.join(('%010d 00000 n \n' % offset).encode() for offset in offsets)
    out += ('trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n' % (len(objects) + 1, start)).encode()
    return out


def zip_bytes(files):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, text in files.items():
            archive.writestr(name, text.encode('utf-8') if isinstance(text, str) else text)
    return buffer.getvalue()


def docx_bytes(doctype=''):
    body = (
        '<w:p><w:pPr><w:tabs><w:tab w:val="right" w:pos="9000"/></w:tabs></w:pPr>'
        '<w:r><w:rPr><w:b/></w:rPr><w:t>Alex Rivera</w:t></w:r></w:p>'
        '<w:p><w:r><w:t xml:space="preserve">Senior Data Analyst</w:t><w:tab/><w:t>Toronto, ON</w:t>'
        '<w:br/><w:t>Second line of the same paragraph</w:t></w:r></w:p>'
        '<w:p><w:pPr><w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr></w:pPr>'
        '<w:r><w:t>Built a churn-risk model</w:t></w:r></w:p>'
        '<w:p><w:r><w:t>Kept</w:t></w:r><w:del><w:r><w:delText>tracked deletion</w:delText></w:r></w:del>'
        '<w:ins><w:r><w:t xml:space="preserve"> and inserted</w:t></w:r></w:ins></w:p>'
        '<w:tbl><w:tblPr/><w:tr><w:tc><w:p><w:r><w:t>2023-04</w:t></w:r></w:p></w:tc>'
        '<w:tc><w:p><w:r><w:t>Lakeshore Metrics</w:t></w:r></w:p></w:tc></w:tr>'
        '<w:tr><w:tc><w:p><w:r><w:t>Tools</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>SQL</w:t></w:r></w:p>'
        '<w:p><w:r><w:t>Python</w:t></w:r></w:p></w:tc></w:tr></w:tbl>'
        '<mc:AlternateContent xmlns:mc="%s"><mc:Choice Requires="wps"><w:p><w:r><w:t>Sidebar text</w:t></w:r></w:p>'
        '</mc:Choice><mc:Fallback><w:p><w:r><w:t>Sidebar text</w:t></w:r></w:p></mc:Fallback></mc:AlternateContent>'
        '<w:sectPr/>' % MC)
    document = '<?xml version="1.0" encoding="UTF-8"?>%s<w:document xmlns:w="%s"><w:body>%s</w:body></w:document>' % (
        doctype, W, body)
    header = '<?xml version="1.0"?><w:hdr xmlns:w="%s"><w:p><w:r><w:t>alex.rivera@mail.example</w:t></w:r></w:p></w:hdr>' % W
    return zip_bytes({'[Content_Types].xml': '<?xml version="1.0"?><Types/>', 'word/document.xml': document,
                      'word/header1.xml': header, 'word/vbaProject.bin': b'\x00not run\x00'})


LINKEDIN = {
    'Profile.csv': ('﻿First Name,Last Name,Maiden Name,Address,Birth Date,Headline,Summary,Industry,Zip Code,'
                    'Geo Location,Twitter Handles,Websites,Instant Messengers\n'
                    'Alex,Rivera,,1 Fictional Lane,Jan 1 1990,Senior Data Analyst,"I turn messy data into decisions.\n'
                    'Second summary line.",Analytics,A1A 1A1,"Toronto, Ontario, Canada",,'
                    '"[PORTFOLIO:https://alexrivera.example]",\n'),
    'Positions.csv': ('﻿Company Name,Title,Description,Location,Started On,Finished On\n'
                      'Lakeshore Metrics,Senior Data Analyst,"Built a churn-risk model in Python and SQL.\n'
                      '- Rebuilt the revenue dashboards on dbt and Tableau.","Toronto, Ontario, Canada",Apr 2023,\n'
                      'Alpine Peak Partners,Analyst,Ran weekly | reporting for 30 branches,Remote,Sept 2021,Mar 2023\n'
                      'Idea,Junior Analyst,,Toronto,Jan 2020,Aug 2021\n'
                      'Ivory Sun Optics,Intern,Wrote audit checklist ISO-12 for lens samples,Toronto,Spring 2019,Dec 2019\n'
                      'Example Venture,Co-op Analyst,Built survey dashboards,Toronto,2018,2019\n'),
    'Education.csv': ('School Name,Start Date,End Date,Notes,Degree Name,Activities\n'
                      'Northbridge University,2014,2018,,"BSc Statistics, minor in Economics",Statistics society\n'
                      'Example College,2012,2014,Evening courses,,\n'),
    'Skills.csv': 'Name\nSQL\nPython\nStakeholder management\n',
    'Certifications.csv': ('Name,Url,Authority,Started On,Finished On,License Number\n'
                           'Example Cloud Analyst,https://cert.example/abc,Example Cloud,Jan 2022,Jan 2025,LIC-0000\n'),
    'Projects.csv': 'Title,Description,Url,Started On,Finished On\nSurvey dashboards,Built dashboards for two non-profits.,,2016,2018\n',
    'Languages.csv': 'Name,Proficiency\nEnglish,Native or bilingual proficiency\n',
    'Connections.csv': ('Notes:\n"Exported connections"\n\nFirst Name,Last Name,URL,Email Address,Company,Position,'
                        'Connected On\nSam,Lee,https://example.com/sam,sam.lee@example.com,Example Corp,Recruiter,1 Oct 2026\n'),
    'messages.csv': 'CONVERSATION ID,FROM,TO,CONTENT\n1,Sam Lee,Alex Rivera,A private message body\n',
}

TEX = r"""\documentclass[11pt]{article}
\usepackage{hyperref} % preamble note
\newcommand{\resumeItem}[1]{\item\small{#1}}
% \begin{document} inside a comment does not start the body
\begin{document}
\begin{center}
  {\Huge \scshape Alex Rivera} \\ \vspace{1pt}
  \small 555-0142 $|$ \href{mailto:alex.rivera@mail.example}{alex.rivera@mail.example} $|$
  \href{https://alexrivera.example/work}{Portfolio}
\end{center}
\section{Experience}
  \resumeSubheading
    {Lakeshore Metrics}{Apr 2023 -- Present}
    {Senior Data Analyst}{Toronto, ON}
  \begin{itemize}[leftmargin=*]
    \resumeItem{Built a \textbf{churn-risk model} that cut churn 18\%} % private aside
    \item Ran readouts for \emph{14 pricing experiments}
  \end{itemize}
\section*{Skills}
\begin{tabular}{@{}l l@{}}
SQL & Python \\
\end{tabular}
Caf\'e r\'esum\'e~line \textcolor{blue}{Blue words}
\input{other-part}
\end{document}
"""


class ImporterTests(unittest.TestCase):
    def setUp(self):
        self.demo = DemoWorkspace(init=False)
        self.ws = self.demo.root
        self.incoming = self.demo.base / 'incoming'
        self.incoming.mkdir()
        self.seed = managed.raw_revision(self.ws)

    def tearDown(self):
        self.demo.close()

    def put(self, name, data):
        path = self.incoming / name
        path.write_bytes(data.encode('utf-8') if isinstance(data, str) else data)
        return path

    def add(self, name, data, kind=None):
        result = importer.add([self.put(name, data)], kind, ws=self.ws)[0]
        self.assertIn(result['status'], ('added', 'already-imported', 're-extracted'), result)
        return result

    def test_copy_index_and_idempotent_readd(self):
        source = self.put('My CV (2026).md', '# Alex Rivera\r\n\r\nSenior Data Analyst  \r\n')
        first = importer.add([source], 'cv', 'fictional CV', ws=self.ws)[0]
        self.assertEqual(first['status'], 'added')
        self.assertTrue(source.is_file())  # copied, never moved
        self.assertEqual(first['id'], 'SRC-' + first['sha256'][:16])
        self.assertEqual(first['stored_path'], 'profile/sources/%s-My-CV-2026.md' % first['sha256'][:16])
        self.assertEqual((self.ws / first['stored_path']).read_bytes(), source.read_bytes())
        self.assertEqual(first['text_path'], 'profile/sources/%s.txt' % first['id'])
        self.assertEqual(importer.text(first['id'], self.ws), '# Alex Rivera\n\nSenior Data Analyst\n')
        index = read_json(self.ws / 'profile/sources/index.json')
        entry = index['sources'][0]
        for key in ('id', 'original_name', 'kind', 'sha256', 'bytes', 'added_at', 'text_path', 'extraction'):
            self.assertIn(key, entry)
        self.assertEqual((entry['kind'], entry['note'], entry['bytes']), ('cv', 'fictional CV', len(source.read_bytes())))
        self.assertEqual(entry['extraction']['method'], 'plain')
        self.assertTrue(entry['extraction']['ok'])
        again = importer.add([self.put('renamed.md', source.read_bytes())], 'writing-sample', ws=self.ws)[0]
        self.assertEqual((again['status'], again['id']), ('already-imported', first['id']))
        self.assertEqual(len(read_json(self.ws / 'profile/sources/index.json')['sources']), 1)
        (self.ws / first['stored_path']).unlink()
        importer.add([source], ws=self.ws)
        self.assertEqual((self.ws / first['stored_path']).read_bytes(), source.read_bytes())  # restored copy
        self.assertEqual(importer.find(first['id'][4:12], self.ws)['id'], first['id'])
        self.assertEqual(managed.raw_revision(self.ws), self.seed)  # no profile file was written

    def test_refuses_large_files_folders_links_and_missing_files(self):
        big = self.put('big.txt', 'x' * 200)
        folder = self.incoming / 'folder'
        folder.mkdir()
        link = self.incoming / 'link.txt'
        os.symlink(str(self.put('target.txt', 'fictional')), str(link))
        with mock.patch.object(importer, 'MAX_BYTES', 100):
            results = importer.add([big, folder, link, self.incoming / 'absent.txt'], ws=self.ws)
        self.assertEqual([r['status'] for r in results], ['refused'] * 4)
        self.assertIn('20 MB', results[0]['reason'])
        self.assertFalse((self.ws / 'profile/sources/index.json').exists())

    @unittest.skipUnless(pdf_tools.resolve().get('pdftotext'), 'pdftotext (Poppler) not installed')
    def test_pdf_text_via_pdftotext(self):
        result = self.add('cv.pdf', tiny_pdf(['Alex Rivera', 'Senior Data Analyst (Toronto)']))
        self.assertEqual(result['extraction']['method'], 'pdftotext')
        self.assertTrue(result['extraction']['ok'])
        text = importer.text(result['id'], self.ws)
        self.assertIn('Alex Rivera', text)
        self.assertIn('Senior Data Analyst (Toronto)', text)

    def test_pdf_without_pdftotext_is_recorded_with_advice_then_reextracted(self):
        data = tiny_pdf(['Alex Rivera'])
        with mock.patch.object(pdf_tools, 'resolve', return_value={'pdftotext': None}):
            result = self.add('cv.pdf', data, 'cv')
        self.assertFalse(result['extraction']['ok'])
        self.assertIsNone(result['text_path'])
        self.assertTrue(any('Read the PDF yourself' in w for w in result['extraction']['warnings']))
        with self.assertRaises(ValueError) as caught:
            importer.text(result['id'], self.ws)
        self.assertIn(result['stored_path'].split('/')[-1], str(caught.exception))
        if pdf_tools.resolve().get('pdftotext'):
            again = importer.add([self.incoming / 'cv.pdf'], ws=self.ws)[0]
            self.assertEqual(again['status'], 're-extracted')
            self.assertIn('Alex Rivera', importer.text(result['id'], self.ws))

    def test_docx_paragraphs_tables_breaks_and_header(self):
        result = self.add('cv.docx', docx_bytes(), 'cv')
        self.assertEqual(result['extraction']['method'], 'docx-xml')
        text = importer.text(result['id'], self.ws)
        self.assertTrue(text.startswith('alex.rivera@mail.example\n\nAlex Rivera\n'))
        self.assertIn('Senior Data Analyst\tToronto, ON\nSecond line of the same paragraph', text)
        self.assertIn('- Built a churn-risk model', text)
        self.assertIn('Kept and inserted', text)
        self.assertNotIn('tracked deletion', text)
        self.assertIn('2023-04 | Lakeshore Metrics', text)
        self.assertIn('Tools\nSQL\nPython', text)
        self.assertEqual(text.count('Sidebar text'), 1)  # the compatibility fallback copy is skipped
        self.assertNotIn('not run', text)

    def test_docx_with_a_dtd_is_refused(self):
        result = importer.add([self.put('bomb.docx', docx_bytes('<!DOCTYPE d [<!ENTITY a "aaaa">]>'))], ws=self.ws)[0]
        self.assertFalse(result['extraction']['ok'])
        self.assertIn('DTD', ' '.join(result['extraction']['warnings']))

    def test_tex_comments_macros_and_no_includes(self):
        self.put('other-part.tex', 'Text that must not be pulled in')
        result = self.add('cv.tex', TEX, 'cv')
        text = importer.text(result['id'], self.ws)
        self.assertEqual(result['extraction']['method'], 'tex-strip')
        for expected in ('Alex Rivera', '555-0142 | alex.rivera@mail.example', 'Portfolio (https://alexrivera.example/work)',
                         'Experience', 'Lakeshore Metrics | Apr 2023 – Present | Senior Data Analyst | Toronto, ON',
                         '- Built a churn-risk model that cut churn 18%', '- Ran readouts for 14 pricing experiments',
                         'SQL | Python', 'Café résumé line Blue words'):
            self.assertIn(expected, text)
        for hidden in ('preamble note', 'private aside', 'hyperref', 'newcommand', 'must not be pulled', 'leftmargin',
                       '\\', '{', '}'):
            self.assertNotIn(hidden, text)
        self.assertTrue(any('not followed' in w for w in result['extraction']['warnings']))

    def test_html_is_stripped_and_text_kept(self):
        page = ('<html><head><title>Hidden title</title><style>p {color: red}</style></head><body><h1>Alex Rivera</h1>'
                '<p>Data <b>analyst</b> &amp; SQL</p><ul><li>One</li><li>Two</li></ul><script>var x = 1;</script>'
                '<table><tr><td>2023</td><td>Lakeshore Metrics</td></tr></table></body></html>')
        text = importer.text(self.add('page.html', page)['id'], self.ws)
        self.assertEqual(text, 'Alex Rivera\n\nData analyst & SQL\n\n- One\n- Two\n\n2023 | Lakeshore Metrics\n')
        self.assertEqual(importer.extract('notes.txt', b'<p>kept as typed</p>')['text'], '<p>kept as typed</p>')

    def test_unsupported_and_binary_files_are_indexed_without_text(self):
        for name, data in (('old.doc', b'\xd0\xcf\x11\xe0binary'), ('photo.png', b'\x89PNG\r\n'), ('blob.bin', b'\x00\x01')):
            result = importer.add([self.put(name, data)], ws=self.ws)[0]
            self.assertEqual(result['status'], 'added')
            self.assertFalse(result['extraction']['ok'])
            self.assertTrue(result['extraction']['warnings'])

    def test_linkedin_export_parsed_without_other_peoples_data(self):
        result = self.add('Basic_LinkedInDataExport.zip',
                          zip_bytes({'Basic_LinkedInDataExport/' + k: v for k, v in LINKEDIN.items()}))
        self.assertEqual((result['kind'], result['extraction']['method']), ('linkedin-export', 'linkedin-export'))
        data = read_json(self.ws / result['linkedin_path'])
        self.assertEqual(data['profile']['first_name'], 'Alex')  # BOM stripped from the first header
        self.assertEqual(data['profile']['summary'], 'I turn messy data into decisions.\nSecond summary line.')
        self.assertEqual(data['profile']['websites'], [{'label': 'Portfolio', 'url': 'https://alexrivera.example'}])
        first = data['positions'][0]
        self.assertEqual((first['company'], first['start'], first['end']), ('Lakeshore Metrics', '2023-04', 'present'))
        self.assertIn('\n- Rebuilt the revenue dashboards', first['description'])  # quoted multi-line field
        self.assertEqual((data['positions'][1]['start'], data['positions'][1]['end']), ('2021-09', '2023-03'))
        self.assertIsNone(data['positions'][3]['start'])
        self.assertEqual(data['positions'][3]['start_raw'], 'Spring 2019')
        self.assertEqual(data['education'][0]['degree'], 'BSc Statistics, minor in Economics')
        self.assertEqual(data['skills'], ['SQL', 'Python', 'Stakeholder management'])
        self.assertEqual(data['certifications'][0]['start'], '2022-01')
        self.assertEqual(data['languages'][0]['name'], 'English')
        stored = read_text(self.ws / result['linkedin_path']) + importer.text(result['id'], self.ws)
        for private in ('Sam', 'sam.lee@example.com', 'A private message body', 'Fictional Lane', '1990', 'A1A',
                        'LIC-0000'):
            self.assertNotIn(private, stored)
        self.assertIn('Positions\n- Senior Data Analyst, Lakeshore Metrics', importer.text(result['id'], self.ws))

    def test_draft_entries_follow_the_convention_and_never_write_experience(self):
        result = self.add('export.zip', zip_bytes(LINKEDIN))
        before = read_text(self.ws / layout.EXPERIENCE)
        output = importer.linkedin(result['id'], self.ws)
        draft = output['draft']
        prefixes = [e['prefix'] for e in draft['entries']]
        self.assertEqual(len(prefixes), len(set(prefixes)))
        existing = set(evidence.parse(before).prefixes)
        for prefix in prefixes:
            self.assertRegex(prefix, r'^[A-Z][A-Z0-9]{1,7}$')
            self.assertNotIn(prefix, evidence.RESERVED | existing)
        by_org = {e['org']: e for e in draft['entries']}
        self.assertNotIn(by_org['Lakeshore Metrics']['prefix'], ('LKM',))  # already used in EXPERIENCE.md
        self.assertNotEqual(by_org['Alpine Peak Partners']['prefix'], 'APP')
        self.assertNotEqual(by_org['Idea']['prefix'], 'ID')
        self.assertNotEqual(by_org['Ivory Sun Optics']['prefix'], 'ISO')  # "ISO-12" in the export would read as a reference
        self.assertEqual((by_org['Lakeshore Metrics']['start'], by_org['Lakeshore Metrics']['end']), ('2023-04', 'present'))
        self.assertEqual(draft['parse_check'], 'ok')
        catalog = evidence.parse('# Draft\n\n' + draft['markdown'])
        self.assertTrue(all(e['cv'] == 'locked' for e in catalog.entries))
        merged = evidence.parse(before + '\n' + draft['markdown'])  # fits beside the existing entries
        self.assertEqual(len(merged.prefixes), len(existing) + len(prefixes))
        export_text = '\n'.join(LINKEDIN.values()).replace('|', '/')
        for section in catalog.sections.values():
            for claim in section['claims'].values():
                self.assertIn(claim, export_text)  # claims are copied, never written
        self.assertIn('<<ask: degree title>>', draft['markdown'])
        questions = ' '.join(draft['questions'])
        for expected in ('Example College', 'Spring 2019', 'Idea: no description'):
            self.assertIn(expected, questions)
        self.assertEqual(read_text(self.ws / layout.EXPERIENCE), before)
        self.assertEqual(managed.raw_revision(self.ws), self.seed)

    def test_cli_add_list_text_and_linkedin(self):
        cv = self.put('cv.md', '# Fictional CV\n\nData analyst.\n')
        export = self.put('export.zip', zip_bytes(LINKEDIN))
        script = str(SCRIPTS / 'importer.py')
        run = lambda *args: subprocess.run([sys.executable, script] + list(args), capture_output=True, text=True,
                                           timeout=60)
        added = run('add', str(cv), str(export), '--kind', 'cv')
        self.assertEqual(added.returncode, 0, added.stderr)
        ids = [r['id'] for r in json.loads(added.stdout)]
        self.assertEqual(len(json.loads(run('list').stdout)), 2)
        self.assertEqual(run('text', ids[0]).stdout, '# Fictional CV\n\nData analyst.\n')
        parsed = json.loads(run('linkedin', ids[1]).stdout)
        self.assertEqual(parsed['export']['positions'][0]['company'], 'Lakeshore Metrics')
        self.assertIn('```jobfather-entry', run('linkedin', ids[1], '--markdown').stdout)
        refused = run('add', str(self.incoming / 'absent.md'))
        self.assertEqual(refused.returncode, 1)
        self.assertNotEqual(run('linkedin', ids[0]).returncode, 0)


if __name__ == '__main__':
    unittest.main()
