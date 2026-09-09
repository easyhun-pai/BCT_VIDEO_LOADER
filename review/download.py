"""이벤트 클립 다운로드 → {out}/{site}/{yyyy-mm-dd}/{event_id}/{role}.mp4

기본은 학습용(박스 없음)만. --tg 를 주면 검수용(박스 있음)도 {role}_tg.mp4 로 같이 받는다.
이미 있는 파일은 건너뛴다(재실행 안전).
"""
from __future__ import annotations

import json
import os
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from minio import Minio

from .catalog import Event


@dataclass
class FetchResult:
    event_id: str
    dir: Path
    downloaded: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)


def fetch_events(client: Minio, bucket: str, events: list[Event], out_root: Path,
                 roles: list[str], include_tg: bool = False, progress=None) -> list[FetchResult]:
    results: list[FetchResult] = []
    for i, ev in enumerate(events, 1):
        d = out_root / ev.site / ev.date / ev.id
        d.mkdir(parents=True, exist_ok=True)
        r = FetchResult(event_id=ev.id, dir=d)
        wants: list[tuple[str, str | None, Path]] = []
        for role in roles:
            wants.append((f"{role}.mp4", ev.key_for(role), d / f"{role}.mp4"))
            if include_tg:
                wants.append((f"{role}_tg.mp4", ev.key_for(role, tg=True), d / f"{role}_tg.mp4"))
        for label, key, dst in wants:
            if key is None:
                r.missing.append(label)
                continue
            if dst.exists() and dst.stat().st_size > 0:
                r.skipped.append(label)
                continue
            # minio 는 dst 옆에 '<dst>.<hash>.part.minio' 임시파일을 만드는데, NAS/깊은 경로에서는
            # Windows 260자 한계를 넘기 쉽다. 임시파일은 짧은 temp 디렉터리에 두고 완료 시 dst 로 옮긴다.
            tmp = os.path.join(tempfile.gettempdir(), f"bctrv_{uuid.uuid4().hex}.part")
            client.fget_object(bucket, key, str(dst), tmp_file_path=tmp)
            r.downloaded.append(label)
        results.append(r)
        if progress:
            progress(i, len(events), r)
    return results


def write_fetch_log(out_root: Path, site: str, date: str, results: list[FetchResult], extra: dict | None = None) -> Path:
    """세션 파일의 전신. P1 에서 session.json 으로 확장한다."""
    d = out_root / site / date
    d.mkdir(parents=True, exist_ok=True)
    p = d / "fetch.json"
    prev = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {"fetches": []}
    prev["fetches"].append({
        "at": datetime.now().isoformat(timespec="seconds"),
        "events": [r.event_id for r in results],
        "downloaded": sum(len(r.downloaded) for r in results),
        "skipped": sum(len(r.skipped) for r in results),
        "missing": {r.event_id: r.missing for r in results if r.missing},
        **(extra or {}),
    })
    p.write_text(json.dumps(prev, ensure_ascii=False, indent=2), encoding="utf-8")
    return p
