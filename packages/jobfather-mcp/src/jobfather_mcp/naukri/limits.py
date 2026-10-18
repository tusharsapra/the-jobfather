"""Fixed spacing between requests and daily caps, shared by every host using the workspace.

Every page load counts as ``page``; each tool also counts its own action. Applications count at
the moment the user confirms the submit. The ledger is ``<ws>/.state/naukri/ledger.jsonl`` (action
names and times only), read and appended under a file lock and bucketed by local calendar day.
Waits of up to 30 seconds are slept; longer waits and exhausted caps raise ``rate_limited``.
There is no random jitter: spacing is fixed.

Defaults can be changed in ``config/platforms.json`` → ``naukri.limits``, e.g.
``{"search": {"spacing_s": 30, "daily_cap": 20}}``. Values are kept within hard bounds (an apply
cap never exceeds 25); the bounds are relaxed only in test mode.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from ..errors import JFError
from . import site as site_mod
from .store import folder, locked

DEFAULTS: dict[str, tuple[float, int]] = {
    'page': (6, 200),
    'search': (20, 30),
    'recommended': (20, 30),
    'saved': (20, 30),
    'similar': (20, 30),
    'job': (8, 100),
    'apply': (120, 10),
}
# (minimum spacing seconds, maximum daily cap) a user override may reach
BOUNDS: dict[str, tuple[float, int]] = {
    'page': (3, 400),
    'search': (10, 60),
    'recommended': (10, 60),
    'saved': (10, 60),
    'similar': (10, 60),
    'job': (4, 200),
    'apply': (60, 25),
}
MAX_SLEEP_S = 30
COMPACT_BYTES = 256_000


def effective_limits(overrides: Mapping[str, Any] | None) -> tuple[dict[str, tuple[float, int]], list[str]]:
    """(limits, notes) after applying ``naukri.limits`` within the hard bounds."""
    limits = dict(DEFAULTS)
    notes: list[str] = []
    relaxed = site_mod.test_mode()
    for action, value in (overrides or {}).items():
        if action not in DEFAULTS or not isinstance(value, Mapping):
            notes.append('ignored unknown limit %r' % (action,))
            continue
        spacing, cap = limits[action]
        floor, ceiling = BOUNDS[action]
        if relaxed:
            floor = 0
        try:
            if 'spacing_s' in value:
                spacing = float(value['spacing_s'])
            if 'daily_cap' in value:
                cap = int(value['daily_cap'])
        except (TypeError, ValueError):
            notes.append('ignored invalid limit for %s' % action)
            continue
        if spacing < floor:
            notes.append('%s spacing raised to the minimum %ss' % (action, floor))
            spacing = floor
        if cap > ceiling:
            notes.append('%s daily cap lowered to the maximum %d' % (action, ceiling))
            cap = ceiling
        limits[action] = (max(0.0, spacing), max(0, cap))
    return limits, notes


class Limiter:
    def __init__(self, ws: str | os.PathLike[str], overrides: Mapping[str, Any] | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.ws = Path(ws)
        self.limits, self.notes = effective_limits(overrides)
        self.clock = clock

    # ------------------------------------------------------------------ ledger
    @property
    def path(self) -> Path:
        return folder(self.ws) / 'ledger.jsonl'

    def _entries(self) -> list[dict[str, Any]]:
        if not self.path.is_file():
            return []
        out = []
        for line in self.path.read_bytes().decode('utf-8', 'replace').splitlines():
            try:
                item = json.loads(line)
            except ValueError:
                continue  # a torn last line after a crash; pacing is not a security boundary
            if isinstance(item, dict) and isinstance(item.get('t'), (int, float)):
                out.append(item)
        return out

    def _append(self, entries: Iterable[dict[str, Any]]) -> None:
        with self.path.open('ab') as fh:
            for entry in entries:
                fh.write((json.dumps(entry, sort_keys=True, separators=(',', ':')) + '\n').encode('utf-8'))
            fh.flush()
            os.fsync(fh.fileno())

    def _compact(self, entries: list[dict[str, Any]], today: str) -> None:
        if not self.path.is_file() or self.path.stat().st_size < COMPACT_BYTES:
            return
        last: dict[str, dict[str, Any]] = {}
        for entry in entries:
            if entry.get('event') == 'use':
                last[entry.get('action', '')] = entry
        keep = [e for e in entries if e.get('day') == today]
        keep += [e for e in last.values() if e.get('day') != today]
        keep.sort(key=lambda e: e['t'])
        temp = self.path.with_name(self.path.name + '.tmp')
        temp.write_bytes(''.join(json.dumps(e, sort_keys=True, separators=(',', ':')) + '\n'
                                 for e in keep).encode('utf-8'))
        os.replace(temp, self.path)

    def _day(self, moment: float) -> str:
        return datetime.fromtimestamp(moment).astimezone().date().isoformat()

    def _midnight_in(self, moment: float) -> int:
        local = datetime.fromtimestamp(moment).astimezone()
        midnight = (local + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        return max(1, int(math.ceil((midnight - local).total_seconds())))

    # ------------------------------------------------------------------ queries
    def _state(self, entries: list[dict[str, Any]], action: str, moment: float) -> tuple[int, float | None]:
        today = self._day(moment)
        uses = [e for e in entries if e.get('action') == action and e.get('event') == 'use']
        used = sum(1 for e in uses if e.get('day') == today)
        last = max((e['t'] for e in uses), default=None)
        return used, last

    def usage(self, action: str, entries: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        moment = self.clock()
        entries = self._entries() if entries is None else entries
        spacing, cap = self.limits[action]
        used, last = self._state(entries, action, moment)
        next_at = None
        if last is not None and last + spacing > moment:
            next_at = datetime.fromtimestamp(last + spacing, timezone.utc).isoformat(timespec='seconds')
        if used >= cap:
            midnight = moment + self._midnight_in(moment)
            next_at = datetime.fromtimestamp(midnight, timezone.utc).isoformat(timespec='seconds')
        return {'action': action, 'used_today': used, 'daily_cap': cap, 'next_allowed_at': next_at}

    def all_usage(self) -> list[dict[str, Any]]:
        entries = self._entries()
        return [self.usage(action, entries) for action in DEFAULTS]

    def _wait(self, entries: list[dict[str, Any]], actions: Iterable[str], moment: float) -> float:
        wait = 0.0
        for action in actions:
            spacing, cap = self.limits[action]
            used, last = self._state(entries, action, moment)
            if used >= cap:
                raise JFError('rate_limited', 'Daily Naukri limit reached for %s (%d per day); it resets at local '
                              'midnight.' % (action, cap), True, retry_after_s=self._midnight_in(moment),
                              user_action='Wait until tomorrow. Limits can only be changed by the user in '
                                          'config/platforms.json (naukri.limits).')
            if last is not None:
                wait = max(wait, last + spacing - moment)
        return wait

    def check(self, actions: Iterable[str]) -> float:
        """Seconds to wait before ``actions`` may run (0 when allowed now); raises on an exhausted cap."""
        with locked(folder(self.ws) / 'ledger.lock'):
            return max(0.0, self._wait(self._entries(), list(actions), self.clock()))

    def try_reserve(self, actions: Iterable[str]) -> float:
        """Record one use of every action and return 0, or return the wait without recording."""
        actions = list(dict.fromkeys(actions))
        with locked(folder(self.ws) / 'ledger.lock'):
            entries = self._entries()
            moment = self.clock()
            wait = self._wait(entries, actions, moment)
            if wait > 0:
                return wait
            day = self._day(moment)
            stamp = datetime.fromtimestamp(moment, timezone.utc).isoformat(timespec='seconds')
            self._append({'t': round(moment, 3), 'at': stamp, 'day': day, 'action': a, 'event': 'use'}
                         for a in actions)
            self._compact(entries, day)
            return 0.0

    async def acquire(self, actions: Iterable[str], max_sleep: float = MAX_SLEEP_S) -> None:
        """Wait (up to ``max_sleep`` seconds in total) for the spacing, then record the actions."""
        actions = list(actions)
        slept = 0.0
        while True:
            wait = await asyncio.to_thread(self.try_reserve, actions)
            if wait <= 0:
                return
            if slept + wait > max_sleep:
                raise JFError('rate_limited', 'Naukri requests are spaced out; the next %s is allowed in %d s.'
                              % ('/'.join(a for a in actions if a != 'page') or 'page load', math.ceil(wait)),
                              True, retry_after_s=math.ceil(wait))
            await asyncio.sleep(wait)
            slept += wait

    async def wait_allowed(self, actions: Iterable[str], max_sleep: float = MAX_SLEEP_S) -> None:
        """Like :meth:`acquire` but records nothing (used before an apply is confirmed)."""
        actions = list(actions)
        wait = await asyncio.to_thread(self.check, actions)
        if wait > max_sleep:
            raise JFError('rate_limited', 'Applications are spaced out; the next one is allowed in %d s.'
                          % math.ceil(wait), True, retry_after_s=math.ceil(wait))
        if wait > 0:
            await asyncio.sleep(wait)

    def record(self, actions: Iterable[str]) -> None:
        """Record actions now regardless of spacing (the user already confirmed; caps were checked)."""
        moment = self.clock()
        with locked(folder(self.ws) / 'ledger.lock'):
            day = self._day(moment)
            stamp = datetime.fromtimestamp(moment, timezone.utc).isoformat(timespec='seconds')
            self._append({'t': round(moment, 3), 'at': stamp, 'day': day, 'action': a, 'event': 'use'}
                         for a in actions)
