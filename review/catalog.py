"""MinIO 이벤트 카탈로그.

현장 MinIO 의 오브젝트 키가 곧 이벤트 ID 다:
    {bct}/{yyyymmdd_HHMMSS}/{hook|ppe}.mp4      1280, 박스 없음 (학습용)
    _tg/{bct}/{yyyymmdd_HHMMSS}/{hook|ppe}.mp4  640, 박스 있음 (검수용)
이벤트 ID = {SITE}-{bct}-{yyyymmdd_HHMMSS}
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from minio import Minio

TS_RE = re.compile(r"^\d{8}_\d{6}$")
BCT_RE = re.compile(r"^bct\d+$")


def minio_client(endpoint: str, access: str, secret: str, secure: bool = False) -> Minio:
    return Minio(endpoint, access_key=access, secret_key=secret, secure=secure)


@dataclass
class Event:
    site: str
    bct: str
    ts: str                                   # yyyymmdd_HHMMSS (현장 로컬 시각)
    train: dict[str, str] = field(default_factory=dict)   # role -> object key
    tg: dict[str, str] = field(default_factory=dict)      # role -> object key
    size: int = 0

    @property
    def id(self) -> str:
        return f"{self.site}-{self.bct}-{self.ts}"

    @property
    def date(self) -> str:                    # yyyy-mm-dd
        return f"{self.ts[0:4]}-{self.ts[4:6]}-{self.ts[6:8]}"

    @property
    def hhmm(self) -> str:
        return self.ts[9:13]

    @property
    def time_str(self) -> str:
        return f"{self.ts[9:11]}:{self.ts[11:13]}:{self.ts[13:15]}"

    def local_dt(self, tz_offset: str = "+09:00") -> datetime:
        sign = 1 if tz_offset.startswith("+") else -1
        hh, mm = tz_offset[1:].split(":")
        tz = timezone(sign * timedelta(hours=int(hh), minutes=int(mm)))
        return datetime.strptime(self.ts, "%Y%m%d_%H%M%S").replace(tzinfo=tz)

    def key_for(self, role: str, tg: bool = False) -> str | None:
        return (self.tg if tg else self.train).get(role)


def parse_event_id(event_id: str) -> tuple[str, str, str]:
    """'HANIL-bct10-20260907_130950' -> (site, bct, ts)"""
    parts = event_id.strip().split("-")
    if len(parts) != 3 or not BCT_RE.match(parts[1]) or not TS_RE.match(parts[2]):
        raise ValueError(f"이벤트 ID 형식 오류: {event_id}  (예: HANIL-bct10-20260907_130950)")
    return parts[0].upper(), parts[1], parts[2]


# ── 나열 ──────────────────────────────────────────────────────────────────
def list_bcts(client: Minio, bucket: str) -> list[str]:
    out = []
    for o in client.list_objects(bucket, recursive=False):
        name = o.object_name.rstrip("/")
        if o.object_name.endswith("/") and BCT_RE.match(name):
            out.append(name)
    return sorted(out, key=lambda b: int(b[3:]))


def list_days(client: Minio, bucket: str, bcts: list[str] | None = None) -> dict[str, dict[str, int]]:
    """{yyyy-mm-dd: {bct: 이벤트수}}  — {bct}/{ts}/ prefix 만 나열하므로 오브젝트 본문은 읽지 않는다."""
    bcts = bcts or list_bcts(client, bucket)
    days: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for bct in bcts:
        for o in client.list_objects(bucket, prefix=f"{bct}/", recursive=False):
            ts = o.object_name[len(bct) + 1:].rstrip("/")
            if TS_RE.match(ts):
                days[f"{ts[0:4]}-{ts[4:6]}-{ts[6:8]}"][bct] += 1
    return {d: dict(v) for d, v in sorted(days.items())}


def list_events(client: Minio, bucket: str, site: str, date: str,
                bcts: list[str] | None = None, tg_prefix: str = "_tg/") -> list[Event]:
    """하루치 이벤트. prefix 가 {bct}/{yyyymmdd} 라 날짜 필터가 서버에서 끝난다."""
    ymd = date.replace("-", "")
    bcts = bcts or list_bcts(client, bucket)
    events: dict[tuple[str, str], Event] = {}

    def _put(key: str, is_tg: bool) -> None:
        parts = key.split("/")
        if is_tg:
            parts = parts[1:]
        if len(parts) != 3:
            return
        bct, ts, fname = parts
        if not (BCT_RE.match(bct) and TS_RE.match(ts) and fname.endswith(".mp4")):
            return
        role = fname[:-4]
        ev = events.setdefault((bct, ts), Event(site=site, bct=bct, ts=ts))
        (ev.tg if is_tg else ev.train)[role] = key

    for bct in bcts:
        for o in client.list_objects(bucket, prefix=f"{bct}/{ymd}", recursive=True):
            _put(o.object_name, False)
            ev = events.get(tuple(o.object_name.split("/")[:2]))
            if ev:
                ev.size += o.size or 0
        for o in client.list_objects(bucket, prefix=f"{tg_prefix}{bct}/{ymd}", recursive=True):
            _put(o.object_name, True)

    return sorted(events.values(), key=lambda e: (e.ts, e.bct))
