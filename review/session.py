"""검수 세션 파일 — {out_root}/{site}/{yyyy-mm-dd}/session.json

NAS 공유폴더(SMB) 위에 SQLite 를 두면 동시 쓰기에서 잠금이 깨지므로,
판정은 세션마다 JSON 한 파일로 쓴다. 버튼을 누를 때마다 바로 저장해 중간에 꺼도 이어서 한다.
쓰기는 임시파일 → os.replace 로 원자적으로.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

VERDICTS = {"tp": "정탐", "fp": "오탐"}          # 2026-09-14 '애매' 제거 — 정탐/오탐 이분법
LEGACY_VERDICTS = {"unsure": "애매"}           # 예전 세션 파일에 남아 있을 수 있는 값 (읽기만)
FP_CLASSES = ("helmet", "harness", "hook")    # 오탐 클래스 (2026-09-15 추가). 예전 판정에는 classes 가 없다


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _hist_ids(h) -> list[str]:
    """history 항목 → event_id 목록. 형식: 'id'(예전 1개씩) · ['id', ...](예전 묶음) · {"ids": [...], "prev": {...}}(현재)."""
    if isinstance(h, dict):
        return list(h.get("ids") or [])
    return list(h) if isinstance(h, list) else [h]


@dataclass
class Session:
    path: Path
    data: dict

    # ── 열기/저장 ──
    @classmethod
    def open(cls, out_root: Path, site: str, date: str, source: dict | None = None) -> "Session":
        d = Path(out_root) / site / date
        d.mkdir(parents=True, exist_ok=True)
        p = d / "session.json"
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            data.setdefault("verdicts", {}); data.setdefault("history", []); data.setdefault("exported", {})
            if source:
                data["source"] = source
            return cls(p, data)
        data = {
            "site": site, "date": date, "reviewer": "",
            "created_at": _now(), "updated_at": _now(),
            "source": source or {},
            "filters_last": {},
            "verdicts": {},      # event_id -> {verdict, at, by, memo}
            "history": [],       # 판정 순서 (되돌리기용)
            "exported": {},      # event_id -> {at, files}
        }
        s = cls(p, data)
        s.save()
        return s

    def save(self) -> None:
        self.data["updated_at"] = _now()
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    # ── 판정 ──
    def verdict(self, event_id: str) -> dict | None:
        return self.data["verdicts"].get(event_id)

    def note_reviewer(self, reviewer: str, ip: str = "") -> None:
        """이 세션을 연 검수자(계정·접속 IP)를 기록. 공통 계정이라 IP 로 사람을 구분한다."""
        lst = self.data.setdefault("reviewers", [])
        if not any(r.get("by") == reviewer and r.get("ip") == ip for r in lst):
            lst.append({"by": reviewer, "ip": ip, "at": _now()})
            self.save()

    def set(self, event_id: str, verdict: str, reviewer: str, memo: str = "", ip: str = "", classes: list[str] | None = None) -> None:
        self.set_many({event_id: verdict}, reviewer, ip=ip, memos={event_id: memo}, classes={event_id: classes or []})

    def set_many(self, verdicts: dict[str, str], reviewer: str, ip: str = "", memos: dict[str, str] | None = None,
                 classes: dict[str, list[str]] | None = None) -> None:
        """여러 이벤트를 한 번에 판정 (그리드 모드). history 에는 묶음 하나로 들어가 되돌리기가 묶음 단위.

        classes: 오탐 이벤트의 오탐 클래스 {event_id: ["helmet"|"harness"|"hook", ...]}. 정탐이면 무시(빈 목록).
        """
        for v in verdicts.values():
            if v not in VERDICTS:
                raise ValueError(v)
        if not verdicts:
            return
        memos, classes = memos or {}, classes or {}
        now = _now()
        prev = {eid: self.data["verdicts"].get(eid) for eid in verdicts}
        for eid, v in verdicts.items():
            cls = [k for k in FP_CLASSES if k in (classes.get(eid) or [])] if v == "fp" else []
            memo = memos.get(eid, (prev[eid] or {}).get("memo", ""))
            self.data["verdicts"][eid] = {"verdict": v, "classes": cls, "at": now, "by": reviewer, "ip": ip, "memo": memo}
        # 같은 이벤트가 이미 history 에 있으면(재판정) 옛 항목에서 제거
        hist = []
        for h in self.data["history"]:
            ids_h = _hist_ids(h)
            keep = [x for x in ids_h if x not in verdicts]
            if not keep:
                continue
            if isinstance(h, dict):
                hist.append({"ids": keep, "prev": {k: v for k, v in (h.get("prev") or {}).items() if k in keep}})
            else:
                hist.append(keep if isinstance(h, list) else keep[0])
        # 되돌리기가 '판정 삭제'가 아니라 '이전 판정 복원'이 되도록 이전 값을 같이 남긴다 (2026-09-15, 재판정·항목 분류 대비)
        hist.append({"ids": list(verdicts), "prev": prev})
        self.data["history"] = hist
        if reviewer:
            self.data["reviewer"] = reviewer
        self.save()

    def set_classes(self, event_id: str, classes: list[str]) -> None:
        """이미 오탐으로 판정한 이벤트의 오탐 클래스만 고친다 (history 에는 안 남김)."""
        v = self.data["verdicts"].get(event_id)
        cls = [k for k in FP_CLASSES if k in classes]
        if v is not None and v.get("verdict") == "fp" and v.get("classes", []) != cls:
            v["classes"] = cls
            self.save()

    def set_memo(self, event_id: str, memo: str) -> None:
        v = self.data["verdicts"].get(event_id)
        if v is not None and v.get("memo", "") != memo:
            v["memo"] = memo
            self.save()

    def undo(self) -> list[str]:
        """마지막 판정(또는 묶음)을 되돌리고 그 event_id 목록을 돌려준다. 재판정이었으면 이전 판정으로 복원."""
        hist = self.data["history"]
        if not hist:
            return []
        last = hist.pop()
        prev = (last.get("prev") or {}) if isinstance(last, dict) else {}
        ids = _hist_ids(last)
        for eid in ids:
            if prev.get(eid):
                self.data["verdicts"][eid] = prev[eid]
            else:
                self.data["verdicts"].pop(eid, None)
        self.save()
        return ids

    def last_judged_id(self) -> str | None:
        """이어하기용: 마지막으로 판정한 이벤트 ID (묶음이면 그 마지막)."""
        hist = self.data["history"]
        return _hist_ids(hist[-1])[-1] if hist else None

    def unclassified_fp_ids(self) -> list[str]:
        """오탐인데 오탐 항목(classes)이 없는 이벤트 — 2026-09-15 이전 판정."""
        return [k for k, v in self.data["verdicts"].items() if v.get("verdict") == "fp" and not v.get("classes")]

    # ── 내보내기 ──
    def mark_exported(self, event_id: str, files: list[str]) -> None:
        self.data["exported"][event_id] = {"at": _now(), "files": files}
        self.save()

    # 영상을 내보내는 판정. 정탐은 기록만. 'unsure' 는 예전 세션 파일 호환용(있으면 같이 내보냄).
    EXPORT_VERDICTS = ("fp", "unsure")

    def ids_by_verdict(self, verdict: str) -> list[str]:
        return [k for k, v in self.data["verdicts"].items() if v.get("verdict") == verdict]

    def fp_ids(self) -> list[str]:
        return self.ids_by_verdict("fp")

    def unexported_ids(self) -> dict[str, list[str]]:
        """{verdict: [event_id...]} — 오탐(및 예전 애매) 중 아직 영상을 안 받은 것."""
        return {v: [k for k in self.ids_by_verdict(v) if k not in self.data["exported"]] for v in self.EXPORT_VERDICTS}

    def unexported_fp_ids(self) -> list[str]:
        return self.unexported_ids()["fp"]

    # ── 집계 ──
    def counts(self) -> dict[str, int]:
        c = {k: 0 for k in VERDICTS}
        c["unsure"] = 0
        for v in self.data["verdicts"].values():
            c[v.get("verdict", "")] = c.get(v.get("verdict", ""), 0) + 1
        c["total"] = len(self.data["verdicts"])
        c["exported"] = len(self.data["exported"])
        return c
