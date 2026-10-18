"""Checkpoint detection: CAPTCHA, verification, OTP prompts, block pages and refused documents.

Pure functions over what a page showed (final URL, main-document status, title, visible text and
which marker elements were visible). The rules are data in ``site_profile.json``. When any rule
matches, the server stops, leaves the window open for the user, records a cooldown and refuses
every tool except ``naukri_status`` and ``naukri_login`` until the user signs in again.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

# Kinds that mean "a human must look at this" vs "the site refused us".
CHECKPOINT_KINDS = ('captcha', 'verification', 'otp')
BLOCKED_KINDS = ('block_page', 'http_403', 'http_429', 'restricted')
KINDS = CHECKPOINT_KINDS + BLOCKED_KINDS
COOLDOWN_HOURS = 24


@dataclass(frozen=True)
class Checkpoint:
    """One detected checkpoint. ``rule`` names the matching rule (never page text)."""

    kind: str
    rule: str

    @property
    def code(self) -> str:
        return 'checkpoint' if self.kind in CHECKPOINT_KINDS else 'blocked'

    def message(self) -> str:
        if self.kind == 'otp':
            return 'Naukri is asking for a one-time code outside the login page.'
        if self.kind == 'captcha':
            return 'Naukri is showing a CAPTCHA.'
        if self.kind == 'verification':
            return 'Naukri is showing a verification or security check.'
        if self.kind == 'restricted':
            return 'Naukri says the account is restricted.'
        if self.kind == 'http_429':
            return 'Naukri refused the page as too many requests (HTTP 429).'
        if self.kind == 'http_403':
            return 'Naukri refused the page (HTTP 403).'
        return 'Naukri is showing a block page.'


def _compile(rules: Iterable[Mapping[str, Any]]) -> list[tuple[str, str, re.Pattern[str]]]:
    out = []
    for rule in rules or ():
        kind = rule.get('kind')
        if kind not in KINDS:
            raise ValueError('Unknown checkpoint kind in the site profile: %r' % (kind,))
        out.append((str(rule.get('id') or kind), kind, re.compile(rule['regex'], re.I)))
    return out


class Rules:
    """Compiled checkpoint rules from the ``checkpoints`` section of the site profile."""

    def __init__(self, section: Mapping[str, Any]) -> None:
        self.urls = _compile(section.get('url_patterns', ()))
        self.titles = _compile(section.get('title_patterns', ()))
        self.texts = _compile(section.get('text_patterns', ()))
        self.elements = [(str(e.get('id')), e['kind'], e['selector']) for e in section.get('element_markers', ())]
        for _, kind, _ in self.elements:
            if kind not in KINDS:
                raise ValueError('Unknown checkpoint kind in the site profile: %r' % (kind,))
        self.statuses = {int(code): kind for code, kind in (section.get('statuses') or {}).items()}
        self.login_only = frozenset(section.get('allowed_in_login_flow', ('otp',)))
        self.text_limit = int(section.get('text_scan_chars', 20000))

    def element_selectors(self) -> list[tuple[str, str]]:
        """(rule id, CSS selector) pairs the browser should test for visibility."""
        return [(rule_id, selector) for rule_id, _, selector in self.elements]

    def detect(self, *, url: str = '', status: int | None = None, title: str = '', text: str = '',
               visible: Iterable[str] = (), in_login_flow: bool = False) -> Checkpoint | None:
        """The first matching checkpoint, or None. ``visible`` holds ids of visible element markers."""
        found = self._detect(url, status, title, text, frozenset(visible))
        if found is not None and in_login_flow and found.kind in self.login_only:
            return None  # an OTP prompt on the login page is the user's own login, not a checkpoint
        return found

    def _detect(self, url: str, status: int | None, title: str, text: str, visible: frozenset[str]) -> Checkpoint | None:
        if status is not None and status in self.statuses:
            return Checkpoint(self.statuses[status], 'status_%d' % status)
        for rule_id, kind, pattern in self.urls:
            if pattern.search(url or ''):
                return Checkpoint(kind, rule_id)
        for rule_id, kind, pattern in self.titles:
            if pattern.search(title or ''):
                return Checkpoint(kind, rule_id)
        for rule_id, kind, _ in self.elements:
            if rule_id in visible:
                return Checkpoint(kind, rule_id)
        sample = (text or '')[:self.text_limit]
        for rule_id, kind, pattern in self.texts:
            if pattern.search(sample):
                return Checkpoint(kind, rule_id)
        return None
