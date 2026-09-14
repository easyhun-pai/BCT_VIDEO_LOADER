"""월간 성능 검수 — BCT 당 N건(기본 100) 랜덤 표본을 사람이 클래스별로 채점해 정확도·오탐율을 낸다.

저장 (NAS 저장 루트 아래, 오탐 검수와 섞이지 않게 _perf/):
    {root}/_perf/{site}/{yyyy-mm}/pool.json     표본 후보 전체 (한 번 만들고 고정). BCT 별로 시드 셔플된 순서
    {root}/_perf/{site}/{yyyy-mm}/review.json   검수 대기열 · 채점 · 되돌리기 이력 (누를 때마다 원자적 저장)
    {root}/_perf/{site}/{yyyy-mm}/report/       리포트 산출물 (HTML · PDF · CSV · summary.json)

표본 규칙
  · 후보 = 그 달 이벤트 중 판정(Influx)이 조인되고 세 클래스 점수가 모두 있으며 영상이 있는 것.
  · BCT 별로 후보를 시드 고정 셔플해 앞에서부터 N건. 후보가 N건 미만이면 전부.
  · '제외'(판단 불가: 사람 안 보임·영상 깨짐 등)로 채점하면 같은 BCT 셔플 순서의 다음 후보로 자동 보충.
    → 표본은 시드와 제외 기록만으로 재현된다.

채점 (클래스별)
  · 모델 판정: 클래스 점수 ≥ 현장 임계값이면 ⭕(착용/체결), 아니면 ❌. 출입 판정은 Influx verdict.
  · 검수자는 모델 판정이 틀린 클래스만 표시한다. 표시 안 한 클래스는 모델이 맞은 것.
  · 실제 = 모델 판정을 틀린 클래스만 뒤집은 것. 실제 출입 = 세 클래스 모두 실제 ⭕ 이면 승인.
  · 오탐 = 모델 ❌ · 실제 ⭕ (착용했는데 미착용으로 잡음, 부당 거부의 원인)
    미탐 = 모델 ⭕ · 실제 ❌ (미착용인데 착용으로 잡음, 부당 승인의 원인)
"""
from __future__ import annotations

import json
import math
import os
import random
from calendar import monthrange
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

CLASS_KEYS = ("helmet", "harness", "hook")
CLASS_KO = {"helmet": "안전모", "harness": "하네스", "hook": "안전고리"}
DEFAULT_PER_BCT = 100


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _atomic_write(p: Path, data: dict, compact: bool = False) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    text = json.dumps(data, ensure_ascii=False, separators=(",", ":")) if compact else json.dumps(data, ensure_ascii=False, indent=1)
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, p)


def _bct_key(b: str) -> int:
    return int(b[3:]) if b.startswith("bct") and b[3:].isdigit() else 999


def month_dir(root: Path, site_code: str, month: str) -> Path:
    return Path(root) / "_perf" / site_code / month


def month_days(month: str, until: date | None = None) -> list[str]:
    y, m = int(month[:4]), int(month[5:7])
    last = date(y, m, monthrange(y, m)[1])
    if until and until < last:
        last = until
    d, out = date(y, m, 1), []
    while d <= last:
        out.append(d.isoformat()); d += timedelta(days=1)
    return out


# ══════════════════════════════════════════════════════════════════════════
# 후보 수집 (MinIO 이벤트 × Influx 판정)
# ══════════════════════════════════════════════════════════════════════════
def collect_candidates(site, acc, month: str, tz: str, progress=None) -> tuple[list[dict], dict]:
    """그 달 표본 후보 행 목록과 수집 통계. progress(i, n, day, n_events) 로 진행 알림."""
    from .catalog import list_events, minio_client
    from .influx import join_events, query_day

    c = minio_client(acc.minio_endpoint, site.minio_access, site.minio_secret, site.minio_secure)
    inf = site.influx
    days = month_days(month, until=date.today())
    rows, stats = [], {"days": len(days), "events": 0, "joined": 0, "eligible": 0}
    for i, ds in enumerate(days, 1):
        evs = list_events(c, site.minio_bucket, site.code, ds, tg_prefix=site.tg_prefix)
        matched = {}
        if evs:
            irows = query_day(acc.influx_url, site.influx_token, inf.get("org", "mithril"), inf.get("bucket", "gate_events"),
                              inf.get("measurement", "gate_event"), ds, tz)
            matched, _ = join_events(evs, irows, tz, int(inf.get("join_tolerance_sec", 90)))
        stats["events"] += len(evs); stats["joined"] += len(matched)
        for ev in evs:
            r = matched.get(ev.id)
            if not r or r.get("verdict") not in ("allowed", "denied"):
                continue
            scores = {k: r.get(f"{k}_score") for k in CLASS_KEYS}
            if any(v is None for v in scores.values()) or not (ev.tg or ev.train):
                continue
            rows.append({"id": ev.id, "bct": ev.bct, "ts": ev.ts, "verdict": r["verdict"],
                         **{k: round(float(v), 4) for k, v in scores.items()},
                         "tg": sorted(ev.tg), "train": sorted(ev.train)})
        if progress:
            progress(i, len(days), ds, len(evs))
    stats["eligible"] = len(rows)
    return rows, stats


def build_pool(site, month: str, rows: list[dict], stats: dict, per_bct: int, seed: int, by: str) -> dict:
    by_bct: dict[str, list[dict]] = {}
    for r in sorted(rows, key=lambda r: (r["bct"], r["ts"])):
        by_bct.setdefault(r["bct"], []).append(r)
    rng = random.Random(seed)
    for bct in sorted(by_bct, key=_bct_key):
        rng.shuffle(by_bct[bct])
    return {
        "site": site.code, "site_name": site.name, "month": month, "seed": seed, "per_bct": per_bct,
        "thresholds": {f"{k}_score": site.thresholds.get(f"{k}_score", 0.5) for k in CLASS_KEYS},
        "tg_prefix": site.tg_prefix, "created_at": _now(), "created_by": by, "stats": stats,
        "bcts": {b: by_bct[b] for b in sorted(by_bct, key=_bct_key)},
    }


# ══════════════════════════════════════════════════════════════════════════
# 저장소
# ══════════════════════════════════════════════════════════════════════════
@dataclass
class PerfStore:
    dir: Path
    pool: dict
    data: dict

    # ── 열기 · 만들기 ──
    @classmethod
    def exists(cls, root: Path, site_code: str, month: str) -> bool:
        d = month_dir(root, site_code, month)
        return (d / "pool.json").exists() and (d / "review.json").exists()

    @classmethod
    def open(cls, root: Path, site_code: str, month: str) -> "PerfStore":
        d = month_dir(root, site_code, month)
        pool = json.loads((d / "pool.json").read_text(encoding="utf-8"))
        data = json.loads((d / "review.json").read_text(encoding="utf-8"))
        data.setdefault("judgments", {}); data.setdefault("history", []); data.setdefault("model", "")
        return cls(d, pool, data)

    @classmethod
    def create(cls, root: Path, pool: dict, model: str = "") -> "PerfStore":
        d = month_dir(root, pool["site"], pool["month"])
        n = int(pool["per_bct"])
        queue = [r for rows in pool["bcts"].values() for r in rows[:n]]
        queue.sort(key=lambda r: (r["ts"], _bct_key(r["bct"])))          # 검수는 시간순 (BCT 가 섞여 나온다)
        data = {"site": pool["site"], "month": pool["month"], "model": model, "created_at": _now(), "updated_at": _now(),
                "queue": [r["id"] for r in queue], "judgments": {}, "history": [], "reviewers": []}
        _atomic_write(d / "pool.json", pool, compact=True)       # 한 달 후보 수만 건 — 한 번 쓰고 읽기만 하므로 압축
        s = cls(d, pool, data)
        s.save()
        return s

    def save(self) -> None:
        self.data["updated_at"] = _now()
        # 월 목록 화면이 큰 pool.json 을 열지 않고도 상태를 보이게 요약을 같이 적어 둔다
        tot = self.progress()["total"]
        self.data["status"] = {**tot, "accuracy": _verdict_block(graded_rows(self))["accuracy"]}
        _atomic_write(self.dir / "review.json", self.data)

    def refresh(self) -> None:
        """다른 검수자가 같은 달을 같이 채점할 수 있으므로, 쓰기 직전에 디스크의 최신 review.json 을 다시 읽는다."""
        try:
            data = json.loads((self.dir / "review.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        data.setdefault("judgments", {}); data.setdefault("history", []); data.setdefault("model", "")
        self.data = data

    @staticmethod
    def read_status(root: Path, site_code: str, month: str) -> dict | None:
        try:
            d = json.loads((month_dir(root, site_code, month) / "review.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return {**(d.get("status") or {}), "model": d.get("model", "")}

    # ── 조회 ──
    @property
    def rows(self) -> dict[str, dict]:
        if not hasattr(self, "_rows"):
            self._rows = {r["id"]: r for rows in self.pool["bcts"].values() for r in rows}
        return self._rows

    @property
    def queue(self) -> list[str]:
        return self.data["queue"]

    def judgment(self, eid: str) -> dict | None:
        return self.data["judgments"].get(eid)

    def thresholds(self) -> dict:
        return self.pool["thresholds"]

    def pred(self, row: dict) -> dict[str, bool]:
        """클래스별 모델 판정 (True = ⭕ 착용/체결)."""
        th = self.thresholds()
        return {k: row[k] >= th.get(f"{k}_score", 0.5) for k in CLASS_KEYS}

    def note_reviewer(self, by: str, ip: str = "") -> None:
        if any(r.get("by") == by and r.get("ip") == ip for r in self.data.get("reviewers", [])):
            return
        self.refresh()
        self.data.setdefault("reviewers", []).append({"by": by, "ip": ip, "at": _now()})
        self.save()

    def set_model(self, model: str) -> None:
        if model != self.data.get("model", ""):
            self.refresh(); self.data["model"] = model; self.save()

    def first_pending_index(self, ids: list[str]) -> int:
        """이어하기: 목록에서 첫 미채점 위치 (없으면 0)."""
        return next((i for i, e in enumerate(ids) if not self.judgment(e)), 0)

    def progress(self) -> dict:
        """전체·BCT 별 진행: 목표(target) · 채점(done) · 유효(valid) · 제외(excluded) · 대기(pending)."""
        n = int(self.pool["per_bct"])
        per = {}
        for bct, rows in self.pool["bcts"].items():
            per[bct] = {"target": min(n, len(rows)), "candidates": len(rows), "done": 0, "valid": 0, "excluded": 0, "pending": 0}
        for eid in self.queue:
            b = self.rows[eid]["bct"]; j = self.judgment(eid)
            if not j:
                per[b]["pending"] += 1
            else:
                per[b]["done"] += 1
                per[b]["excluded" if j.get("excluded") else "valid"] += 1
        tot = {k: sum(v[k] for v in per.values()) for k in ("target", "candidates", "done", "valid", "excluded", "pending")}
        return {"total": tot, "per_bct": per}

    # ── 채점 ──
    def _next_candidate(self, bct: str) -> str | None:
        inq = set(self.queue)
        return next((r["id"] for r in self.pool["bcts"].get(bct, []) if r["id"] not in inq), None)

    def commit(self, batch: dict[str, dict], by: str, ip: str = "") -> list[str]:
        """batch = {event_id: {"wrong": [class...], "excluded": bool}}. 새로 제외된 이벤트마다 같은 BCT 에서 보충.

        되돌리기는 묶음 단위. 반환: 이번에 보충으로 대기열 끝에 붙은 이벤트 ID.
        """
        if not batch:
            return []
        self.refresh()
        now, added, prev = _now(), [], {}
        for eid, v in batch.items():
            wrong = [k for k in CLASS_KEYS if k in (v.get("wrong") or [])]
            excluded = bool(v.get("excluded"))
            old = self.judgment(eid)
            prev[eid] = old
            self.data["judgments"][eid] = {"wrong": [] if excluded else wrong, "excluded": excluded, "at": now, "by": by, "ip": ip}
            if excluded and not (old and old.get("excluded")):
                nid = self._next_candidate(self.rows[eid]["bct"])
                if nid:
                    self.queue.append(nid); added.append(nid)
        self.data["history"].append({"ids": list(batch), "prev": prev, "added": added, "at": now})
        self.save()
        return added

    def undo(self) -> list[str]:
        """마지막 묶음을 되돌린다: 이전 채점으로 복원, 보충된 이벤트 중 아직 채점 안 된 것은 대기열에서 뺀다."""
        self.refresh()
        hist = self.data["history"]
        if not hist:
            return []
        h = hist.pop()
        for eid in h.get("ids", []):
            old = (h.get("prev") or {}).get(eid)
            if old:
                self.data["judgments"][eid] = old
            else:
                self.data["judgments"].pop(eid, None)
        for nid in h.get("added", []):
            if nid not in self.data["judgments"] and nid in self.queue:
                self.queue.remove(nid)
        self.save()
        return list(h.get("ids", []))


def list_months(root: Path, site_code: str) -> list[str]:
    d = Path(root) / "_perf" / site_code
    if not d.is_dir():
        return []
    return sorted(p.name for p in d.iterdir() if p.is_dir() and (p / "review.json").exists())


# ══════════════════════════════════════════════════════════════════════════
# 집계
# ══════════════════════════════════════════════════════════════════════════
def _rate(n: int, d: int) -> float | None:
    return round(100.0 * n / d, 1) if d else None


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float] | None:
    """이항 비율 95% 신뢰구간 (Wilson, %)."""
    if not n:
        return None
    p = k / n
    den = 1 + z * z / n
    c = (p + z * z / (2 * n)) / den
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return round(100 * max(0.0, c - h), 1), round(100 * min(1.0, c + h), 1)


def graded_rows(store: PerfStore) -> list[dict]:
    """채점된(제외 아님) 이벤트마다 모델·실제 판정을 펼친 행."""
    out = []
    for eid in store.queue:
        j = store.judgment(eid)
        if not j or j.get("excluded"):
            continue
        r = store.rows[eid]
        pred = store.pred(r)
        wrong = set(j.get("wrong") or [])
        gt = {k: (not pred[k]) if k in wrong else pred[k] for k in CLASS_KEYS}
        sys_allow = r["verdict"] == "allowed"
        gt_allow = all(gt.values())
        out.append({
            "event_id": eid, "bct": r["bct"], "date": f"{r['ts'][0:4]}-{r['ts'][4:6]}-{r['ts'][6:8]}",
            "time": f"{r['ts'][9:11]}:{r['ts'][11:13]}:{r['ts'][13:15]}",
            "verdict": r["verdict"], "gt_verdict": "allowed" if gt_allow else "denied",
            "verdict_ok": sys_allow == gt_allow, "all_ok": not wrong,
            **{f"{k}_score": r[k] for k in CLASS_KEYS},
            **{f"{k}_pred": pred[k] for k in CLASS_KEYS}, **{f"{k}_gt": gt[k] for k in CLASS_KEYS},
            "wrong": [k for k in CLASS_KEYS if k in wrong], "by": j.get("by", ""), "at": j.get("at", ""),
        })
    return out


def _verdict_block(rows: list[dict]) -> dict:
    n = len(rows)
    ok = sum(1 for r in rows if r["verdict_ok"])
    sys_deny = [r for r in rows if r["verdict"] == "denied"]
    sys_allow = [r for r in rows if r["verdict"] == "allowed"]
    wrong_deny = sum(1 for r in sys_deny if r["gt_verdict"] == "allowed")       # 부당 거부 (출입 오탐)
    wrong_allow = sum(1 for r in sys_allow if r["gt_verdict"] == "denied")      # 부당 승인 (출입 미탐)
    return {
        "n": n, "correct": ok, "accuracy": _rate(ok, n), "ci": wilson(ok, n),
        "all_ok": sum(1 for r in rows if r["all_ok"]), "all_ok_rate": _rate(sum(1 for r in rows if r["all_ok"]), n),
        "sys_denied": len(sys_deny), "sys_allowed": len(sys_allow),
        "wrong_deny": wrong_deny, "wrong_deny_rate": _rate(wrong_deny, len(sys_deny)),
        "wrong_allow": wrong_allow, "wrong_allow_rate": _rate(wrong_allow, len(sys_allow)),
        "gt_denied": sum(1 for r in rows if r["gt_verdict"] == "denied"),
    }


def _class_block(rows: list[dict], k: str) -> dict:
    n = len(rows)
    pred_x = [r for r in rows if not r[f"{k}_pred"]]
    pred_o = [r for r in rows if r[f"{k}_pred"]]
    fp = sum(1 for r in pred_x if r[f"{k}_gt"])            # 오탐: 모델 ❌, 실제 ⭕
    fn = sum(1 for r in pred_o if not r[f"{k}_gt"])        # 미탐: 모델 ⭕, 실제 ❌
    ok = n - fp - fn
    return {"class": k, "name": CLASS_KO[k], "n": n, "correct": ok, "accuracy": _rate(ok, n),
            "pred_x": len(pred_x), "fp": fp, "fp_rate": _rate(fp, len(pred_x)),
            "pred_o": len(pred_o), "fn": fn, "fn_rate": _rate(fn, len(pred_o)),
            "gt_x": sum(1 for r in rows if not r[f"{k}_gt"])}


def summarize(store: PerfStore) -> dict:
    rows = graded_rows(store)
    prog = store.progress()
    overview = _verdict_block(rows)

    per_bct = []
    for bct in sorted(store.pool["bcts"], key=_bct_key):
        br = [r for r in rows if r["bct"] == bct]
        pb = prog["per_bct"][bct]
        per_bct.append({"bct": bct, **_verdict_block(br), "target": pb["target"], "excluded": pb["excluded"],
                        "pending": pb["pending"], "candidates": pb["candidates"],
                        "classes": {k: _class_block(br, k) for k in CLASS_KEYS}})

    per_class = [_class_block(rows, k) for k in CLASS_KEYS]

    # 이용량 가중 정확도: BCT 표본은 같은 수(100)라 단순 합산은 BCT 를 동일 가중한다. 실제 이벤트 비중으로 다시 가중한 값.
    cand = {b: len(v) for b, v in store.pool["bcts"].items()}
    tot_c = sum(cand[b["bct"]] for b in per_bct if b["accuracy"] is not None)
    weighted = round(sum(b["accuracy"] * cand[b["bct"]] for b in per_bct if b["accuracy"] is not None) / tot_c, 1) if tot_c else None

    errors = [r for r in rows if not r["all_ok"]]
    errors.sort(key=lambda r: (_bct_key(r["bct"]), r["date"], r["time"]))
    excluded = [{"event_id": eid, "bct": store.rows[eid]["bct"], "by": store.judgment(eid).get("by", "")}
                for eid in store.queue if (store.judgment(eid) or {}).get("excluded")]

    return {
        "site": store.pool["site"], "site_name": store.pool.get("site_name", store.pool["site"]), "month": store.pool["month"],
        "model": store.data.get("model", ""), "seed": store.pool["seed"], "per_bct_target": store.pool["per_bct"],
        "thresholds": store.pool["thresholds"], "pool_stats": store.pool.get("stats", {}), "pool_created_at": store.pool.get("created_at", ""),
        "progress": prog["total"], "complete": prog["total"]["pending"] == 0,
        "overview": {**overview, "weighted_accuracy": weighted},
        "per_bct": per_bct, "per_class": per_class,
        "errors": errors, "excluded": excluded,
        "reviewers": sorted({r["by"] for r in rows if r["by"]}),
    }


def compare(cur: dict, base: dict | None) -> dict | None:
    """개선 비교: 기준(이전 달·이전 모델) 대비 지표 변화. 기준이 없으면 None. 값은 %p 차이."""
    if not base:
        return None

    def d(a, b):
        return None if a is None or b is None else round(a - b, 1)

    co, bo = cur["overview"], base["overview"]
    metrics = [("출입 판정 정확도", co["accuracy"], bo["accuracy"], True),
               ("전 항목 정답률", co["all_ok_rate"], bo["all_ok_rate"], True),
               ("부당 거부율", co["wrong_deny_rate"], bo["wrong_deny_rate"], False),
               ("부당 승인율", co["wrong_allow_rate"], bo["wrong_allow_rate"], False)]
    for c, b in zip(cur["per_class"], base["per_class"]):
        metrics.append((f"{c['name']} 오탐율", c["fp_rate"], b["fp_rate"], False))
        metrics.append((f"{c['name']} 미탐율", c["fn_rate"], b["fn_rate"], False))
    bb = {b["bct"]: b for b in base["per_bct"]}
    per_bct = [{"bct": b["bct"], "cur": b["accuracy"], "base": (bb.get(b["bct"]) or {}).get("accuracy"),
                "delta": d(b["accuracy"], (bb.get(b["bct"]) or {}).get("accuracy"))} for b in cur["per_bct"]]
    return {"base_month": base["month"], "base_model": base.get("model", ""), "cur_model": cur.get("model", ""),
            "metrics": [{"name": n, "cur": a, "base": b, "delta": d(a, b), "higher_better": hb} for n, a, b, hb in metrics],
            "per_bct": per_bct}
