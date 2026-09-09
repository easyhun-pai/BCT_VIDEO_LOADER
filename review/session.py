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

VERDICTS = {"tp": "정탐", "fp": "오탐", "unsure": "애매"}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


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

    def set(self, event_id: str, verdict: str, reviewer: str, memo: str = "") -> None:
        if verdict not in VERDICTS:
            raise ValueError(verdict)
        self.data["verdicts"][event_id] = {"verdict": verdict, "at": _now(), "by": reviewer, "memo": memo}
        hist = self.data["history"]
        if event_id in hist:
            hist.remove(event_id)
        hist.append(event_id)
        if reviewer:
            self.data["reviewer"] = reviewer
        self.save()

    def set_memo(self, event_id: str, memo: str) -> None:
        v = self.data["verdicts"].get(event_id)
        if v is not None and v.get("memo", "") != memo:
            v["memo"] = memo
            self.save()

    def undo(self) -> str | None:
        """마지막 판정을 지우고 그 event_id 를 돌려준다."""
        hist = self.data["history"]
        if not hist:
            return None
        eid = hist.pop()
        self.data["verdicts"].pop(eid, None)
        self.save()
        return eid

    # ── 내보내기 ──
    def mark_exported(self, event_id: str, files: list[str]) -> None:
        self.data["exported"][event_id] = {"at": _now(), "files": files}
        self.save()

    def fp_ids(self) -> list[str]:
        return [k for k, v in self.data["verdicts"].items() if v.get("verdict") == "fp"]

    def unexported_fp_ids(self) -> list[str]:
        return [k for k in self.fp_ids() if k not in self.data["exported"]]

    # ── 집계 ──
    def counts(self) -> dict[str, int]:
        c = {k: 0 for k in VERDICTS}
        for v in self.data["verdicts"].values():
            c[v.get("verdict", "")] = c.get(v.get("verdict", ""), 0) + 1
        c["total"] = len(self.data["verdicts"])
        c["exported"] = len(self.data["exported"])
        return c
