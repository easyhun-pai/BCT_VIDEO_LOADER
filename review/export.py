"""오탐 영상 NAS 반영 — session.json 의 판정·오탐 항목과 NAS 폴더를 맞춘다.

    {저장루트}/{site}/{date}/hook/{yyyymmdd_HHMMSS}_{bct}_hook.mp4     학습용(박스 없음) 영상
    {저장루트}/{site}/{date}/ppe/{yyyymmdd_HHMMSS}_{bct}_ppe.mp4
    (2026-09-15 까지의 예전 구조 {date}/{event_id}/{role}.mp4 는 반영할 때 새 구조로 옮긴다)

규칙 (2026-09-15)
  · 오탐 항목이 안전고리면 hook 영상, 안전모·하네스면 ppe 영상만 둔다. 둘 다 있으면 둘 다.
  · 오탐 항목이 아직 없는 오탐(분류 전)은 카메라 영상 전부를 둔다 — 분류하면 그때 정리된다.
  · 오탐이 아닌(정탐으로 바뀐) 이벤트의 영상은 지운다. 판정 기록에 없는 파일·폴더는 건드리지 않는다.
  · 필요한데 없는 영상은 (예전 구조에 있으면 옮기고, 없으면) 현장 MinIO 에서 받고, 필요 없는 영상은 지운다.
    그 결과를 session.json 의 exported 에 적는다.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .catalog import parse_event_id
from .download import _download

CLASS_CAMERA = {"hook": "hook", "helmet": "ppe", "harness": "ppe"}
EXPORT_VERDICTS = ("fp", "unsure")                     # unsure 는 예전 세션 파일 호환


def need_roles(v: dict | None, cameras: list[str]) -> list[str]:
    """판정 한 건 → NAS 에 있어야 할 카메라 영상."""
    if not v or v.get("verdict") not in EXPORT_VERDICTS:
        return []
    want = {CLASS_CAMERA.get(c) for c in (v.get("classes") or [])} & set(cameras)
    return [r for r in cameras if r in want] if want else list(cameras)


def clip_name(event_id: str, role: str) -> str:
    """'HANIL-bct6-20260915_134650' + hook → '20260915_134650_bct6_hook.mp4'"""
    _, bct, ts = parse_event_id(event_id)
    return f"{ts}_{bct}_{role}.mp4"


def clip_path(date_dir: Path, event_id: str, role: str) -> Path:
    return Path(date_dir) / role / clip_name(event_id, role)


def legacy_path(date_dir: Path, event_id: str, role: str) -> Path:
    return Path(date_dir) / event_id / f"{role}.mp4"


@dataclass
class DayPlan:
    site: str
    date: str
    dir: Path
    download: dict[str, list[str]] = field(default_factory=dict)    # event_id -> 받을 role
    move: dict[str, list[str]] = field(default_factory=dict)        # event_id -> 예전 구조에서 옮길 role
    remove: dict[str, list[str]] = field(default_factory=dict)      # event_id -> 지울 role (새·예전 구조 모두)
    keep: dict[str, list[str]] = field(default_factory=dict)        # event_id -> 있어야 할 role (반영 후 exported 기록용)
    stale_exported: list[str] = field(default_factory=list)         # exported 기록이 디스크와 다른 이벤트

    @property
    def n_events(self) -> int:
        return len(set(self.download) | set(self.move) | set(self.remove))

    @property
    def empty(self) -> bool:
        return not (self.download or self.move or self.remove or self.stale_exported)


def _has(p: Path) -> bool:
    try:
        return p.is_file() and p.stat().st_size > 0
    except OSError:
        return False


def _exported_files(date_dir: Path, event_id: str, roles: list[str]) -> list[str]:
    return [f"{r}/{clip_name(event_id, r)}" for r in roles if _has(clip_path(date_dir, event_id, r))]


def plan_day(root: Path, site_code: str, date: str, cameras: list[str]) -> DayPlan | None:
    d = Path(root) / site_code / date
    try:
        data = json.loads((d / "session.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    plan = DayPlan(site_code, date, d)
    verdicts = data.get("verdicts") or {}
    exported = data.get("exported") or {}
    for eid, v in verdicts.items():
        try:
            parse_event_id(eid)
        except ValueError:
            continue
        need = need_roles(v, cameras)
        if need:
            plan.keep[eid] = need
        for r in cameras:
            new, old = clip_path(d, eid, r), legacy_path(d, eid, r)
            if r in need:
                if _has(new):
                    if old.exists():
                        plan.remove.setdefault(eid, []).append(r)          # 새 구조에 이미 있으면 예전 것은 정리
                elif _has(old):
                    plan.move.setdefault(eid, []).append(r)
                else:
                    plan.download.setdefault(eid, []).append(r)
            elif new.exists() or old.exists():
                plan.remove.setdefault(eid, []).append(r)
        if need and eid not in plan.download and eid not in plan.move:
            if sorted((exported.get(eid) or {}).get("files") or []) != sorted(_exported_files(d, eid, need)):
                plan.stale_exported.append(eid)
    plan.stale_exported += [eid for eid in exported if eid not in plan.keep]
    return plan


def list_days(root: Path, site_code: str) -> list[str]:
    d = Path(root) / site_code
    if not d.is_dir():
        return []
    return sorted(p.name for p in d.iterdir() if p.is_dir() and (p / "session.json").exists())


def plan_all(root: Path, sites) -> list[DayPlan]:
    """모든 현장·일자의 반영 계획 중 할 일이 있는 것만."""
    out = []
    for site in sites:
        for date in list_days(root, site.code):
            p = plan_day(root, site.code, date, site.cameras)
            if p and not p.empty:
                out.append(p)
    return out


@dataclass
class ApplyResult:
    downloaded: int = 0
    moved: int = 0
    removed: int = 0
    missing: list[str] = field(default_factory=list)     # MinIO 에 없어 못 받은 "event_id/role"


def apply_plan(plan: DayPlan, client, bucket: str, cameras: list[str], progress=None) -> ApplyResult:
    """계획대로 옮기고 받고 지운 뒤 session.json 의 exported 를 디스크 상태에 맞춘다. progress(i, n, event_id)."""
    from minio.error import S3Error

    res = ApplyResult()
    targets = sorted(set(plan.download) | set(plan.move) | set(plan.remove))
    for i, eid in enumerate(targets, 1):
        for role in plan.remove.get(eid, []):
            for p in (clip_path(plan.dir, eid, role), legacy_path(plan.dir, eid, role)):
                if p.exists() and not (role in plan.keep.get(eid, []) and p == clip_path(plan.dir, eid, role)):
                    p.unlink()
                    res.removed += 1
        for role in plan.move.get(eid, []):
            dst = clip_path(plan.dir, eid, role)
            dst.parent.mkdir(parents=True, exist_ok=True)
            os.replace(legacy_path(plan.dir, eid, role), dst)
            res.moved += 1
        if plan.download.get(eid):
            _, bct, ts = parse_event_id(eid)
            for role in plan.download[eid]:
                dst = clip_path(plan.dir, eid, role)
                dst.parent.mkdir(parents=True, exist_ok=True)
                try:
                    _download(client, bucket, f"{bct}/{ts}/{role}.mp4", dst)
                    res.downloaded += 1
                except S3Error as e:
                    if e.code not in ("NoSuchKey", "NoSuchObject"):
                        raise
                    res.missing.append(f"{eid}/{role}")
        old_dir = plan.dir / eid                           # 예전 구조 폴더가 비었으면 지운다
        if old_dir.is_dir() and not any(old_dir.iterdir()):
            old_dir.rmdir()
        if progress:
            progress(i, len(targets), eid)
    for role in cameras:                                   # 영상이 하나도 안 남은 카메라 폴더도 정리
        rd = plan.dir / role
        if rd.is_dir() and not any(rd.iterdir()):
            rd.rmdir()
    _record_exported(plan, cameras)
    return res


def _record_exported(plan: DayPlan, cameras: list[str]) -> None:
    """exported = 실제로 NAS 에 있는 대상 영상. 앱이 들고 있는 세션과 겹쳐 쓰지 않도록 저장 직전에 다시 읽는다."""
    from .session import Session

    p = plan.dir / "session.json"
    s = Session(p, json.loads(p.read_text(encoding="utf-8")))
    s.data.setdefault("exported", {})
    now = datetime.now().isoformat(timespec="seconds")
    verdicts = s.data.get("verdicts") or {}
    for eid in list(s.data["exported"]):
        if not need_roles(verdicts.get(eid), cameras):
            s.data["exported"].pop(eid)
    for eid, v in verdicts.items():
        need = need_roles(v, cameras)
        if not need:
            continue
        have = _exported_files(plan.dir, eid, need)
        old = s.data["exported"].get(eid) or {}
        if have and old.get("files") != have:
            s.data["exported"][eid] = {"at": now, "files": have}
        elif not have:
            s.data["exported"].pop(eid, None)
    s.save()
