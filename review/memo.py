"""텔레그램 수기 메모 → 이벤트 ID 자동 매칭.

메모 형식 (지금 쓰는 그대로):
    * 26/08/26          ← 날짜 (yy/mm/dd 또는 yyyy-mm-dd)
    0946 7              ← HHMM BCT번호
    0915 9
    * 26/08/25
    2244 9
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .catalog import Event

DATE_RE = re.compile(r"^\*?\s*(\d{2,4})[/\-.](\d{1,2})[/\-.](\d{1,2})\s*$")
ENTRY_RE = re.compile(r"^(\d{4})\s+(?:bct)?\s*0*(\d{1,2})\s*$", re.IGNORECASE)


@dataclass
class MemoEntry:
    date: str      # yyyy-mm-dd
    hhmm: str
    bct: str       # bct7
    line_no: int


@dataclass
class Resolution:
    entry: MemoEntry
    matched: list[Event] = field(default_factory=list)

    @property
    def status(self) -> str:
        n = len(self.matched)
        return "OK" if n == 1 else ("MISS" if n == 0 else f"AMBIGUOUS({n})")


def parse_memo(text: str) -> list[MemoEntry]:
    entries: list[MemoEntry] = []
    cur: str | None = None
    for i, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("---"):
            continue
        m = DATE_RE.match(line)
        if m:
            y, mo, d = m.groups()
            if len(y) == 2:
                y = "20" + y
            cur = f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"
            continue
        m = ENTRY_RE.match(line)
        if m:
            if cur is None:
                raise ValueError(f"{i}행: 날짜 줄(* yy/mm/dd) 없이 항목이 나왔습니다: {raw!r}")
            entries.append(MemoEntry(date=cur, hhmm=m.group(1), bct=f"bct{int(m.group(2))}", line_no=i))
            continue
        raise ValueError(f"{i}행: 해석 못 함: {raw!r}")
    return entries


def resolve(entries: list[MemoEntry], events_by_date: dict[str, list[Event]]) -> list[Resolution]:
    out = []
    for e in entries:
        cands = [ev for ev in events_by_date.get(e.date, []) if ev.bct == e.bct and ev.hhmm == e.hhmm]
        out.append(Resolution(entry=e, matched=cands))
    return out


def nearby(entry: MemoEntry, events_by_date: dict[str, list[Event]], minutes: int = 10) -> list[Event]:
    """MISS 일 때 힌트용: 같은 BCT 의 ±minutes 이벤트."""
    h, m = int(entry.hhmm[:2]), int(entry.hhmm[2:])
    t0 = h * 60 + m
    res = []
    for ev in events_by_date.get(entry.date, []):
        if ev.bct != entry.bct:
            continue
        t = int(ev.ts[9:11]) * 60 + int(ev.ts[11:13])
        if abs(t - t0) <= minutes:
            res.append(ev)
    return res
