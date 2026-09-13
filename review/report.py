"""기간 리포트 — 이벤트를 '시도 세션' 으로 묶어 BCT 별 출입 승인/거부 통계를 낸다.

세션 규칙 (2026-09-14 합의):
  · 같은 BCT 에서 직전 이벤트가 '거부' 이고, 그로부터 gap_sec(기본 300초=5분) 안에 다음 이벤트가 오면 같은 세션(재시도).
  · '승인' 이 나오면 그 세션은 거기서 끝난다(출입 성공). 다음 이벤트는 새 세션.
  · 세션 결과: 승인이 한 번이라도 있으면 '승인', 끝까지 없으면 '거부' (거부는 몇 번을 반복해도 1회).
  · 거부 세션의 사유는 마지막 이벤트의 사유(포기 직전 상태).

300초 근거 (영월 9/7~9/13): 거부 뒤 다음 이벤트의 초당 밀도가 승인 뒤(=다른 사람 기준선) 대비 120초까지 23~29배,
120~300초 1.4~6.7배(재시도 우세), 300초부터 0.8~1.8배로 기준선과 구분되지 않는다. 사용자 상한 10분.

python -m review report HANIL 2026-09-07 2026-09-13 [--gap 300] [--out DIR]
"""
from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

REASON_ORDER = ["안전고리 미체결", "하네스 미착용", "안전모 미착용"]


# ══════════════════════════════════════════════════════════════════════════
# 수집
# ══════════════════════════════════════════════════════════════════════════
def pull_events(site, tz: str, day_from: str, day_to: str, acc, log=print) -> tuple[list[dict], dict]:
    """[day_from, day_to] 의 이벤트를 Influx 판정과 조인해 행 목록으로. 반환: (rows, 품질정보)"""
    from .catalog import list_events, minio_client
    from .influx import join_events, query_day, reasons_from

    c = minio_client(acc.minio_endpoint, site.minio_access, site.minio_secret, site.minio_secure)
    inf = site.influx
    d0, d1 = date.fromisoformat(day_from), date.fromisoformat(day_to)
    rows, quality = [], {"days": {}, "join_total": 0, "events_total": 0, "influx_rows_total": 0}
    d = d0
    while d <= d1:
        ds = d.isoformat()
        evs = list_events(c, site.minio_bucket, site.code, ds, tg_prefix=site.tg_prefix)
        irows = query_day(acc.influx_url, site.influx_token, inf.get("org", "mithril"), inf.get("bucket", "gate_events"),
                          inf.get("measurement", "gate_event"), ds, tz)
        matched, stats = join_events(evs, irows, tz, int(inf.get("join_tolerance_sec", 90)))
        for ev in evs:
            r = matched.get(ev.id)
            rows.append({
                "event_id": ev.id, "date": ds, "ts": ev.ts, "time": ev.time_str, "bct": ev.bct,
                "verdict": (r or {}).get("verdict", ""),
                "hook": (r or {}).get("hook_score"), "helmet": (r or {}).get("helmet_score"), "harness": (r or {}).get("harness_score"),
                "analysis_sec": (r or {}).get("analysis_sec"),
                "reasons": "|".join(reasons_from(r, site.thresholds)) if r else "",
                "has_tg": bool(ev.tg), "has_train": bool(ev.train),
            })
        quality["days"][ds] = {"events": len(evs), "influx_rows": len(irows), "joined": stats["matched"],
                               "delta_med": stats.get("delta_med")}
        quality["events_total"] += len(evs); quality["influx_rows_total"] += len(irows); quality["join_total"] += stats["matched"]
        log(f"  {ds}: 이벤트 {len(evs):4d} · Influx {len(irows):4d} · 조인 {stats['matched']:4d}")
        d += timedelta(days=1)
    return rows, quality


# ══════════════════════════════════════════════════════════════════════════
# 세션
# ══════════════════════════════════════════════════════════════════════════
@dataclass
class Session:
    bct: str
    events: list[dict] = field(default_factory=list)

    @property
    def start(self) -> datetime: return self.events[0]["dt"]
    @property
    def end(self) -> datetime: return self.events[-1]["dt"]
    @property
    def attempts(self) -> int: return len(self.events)
    @property
    def outcome(self) -> str: return "allowed" if any(e["verdict"] == "allowed" for e in self.events) else "denied"
    @property
    def first_try_ok(self) -> bool: return self.events[0]["verdict"] == "allowed"
    @property
    def final_reasons(self) -> str: return self.events[-1]["reasons"]
    @property
    def duration_sec(self) -> float: return (self.end - self.start).total_seconds()


def build_sessions(rows: list[dict], gap_sec: int = 300) -> list[Session]:
    for r in rows:
        if "dt" not in r:
            r["dt"] = datetime.strptime(r["ts"], "%Y%m%d_%H%M%S")
    by_bct: dict[str, list[dict]] = defaultdict(list)
    for r in sorted(rows, key=lambda r: (r["bct"], r["dt"])):
        by_bct[r["bct"]].append(r)
    sessions: list[Session] = []
    for bct, lst in by_bct.items():
        cur: Session | None = None
        for r in lst:
            if cur is not None and cur.events[-1]["verdict"] == "denied" \
                    and (r["dt"] - cur.events[-1]["dt"]).total_seconds() <= gap_sec:
                cur.events.append(r)
            else:
                cur = Session(bct=bct, events=[r]); sessions.append(cur)
    sessions.sort(key=lambda s: s.start)
    return sessions


# ══════════════════════════════════════════════════════════════════════════
# 집계
# ══════════════════════════════════════════════════════════════════════════
def _rate(n: int, d: int) -> float | None:
    return round(100.0 * n / d, 1) if d else None


def _bct_key(b: str) -> int:
    return int(b[3:]) if b.startswith("bct") and b[3:].isdigit() else 999


def summarize(rows: list[dict], sessions: list[Session], gap_sec: int) -> dict:
    ev_n = len(rows)
    allowed = [s for s in sessions if s.outcome == "allowed"]
    denied = [s for s in sessions if s.outcome == "denied"]
    retried = [s for s in sessions if s.attempts > 1]
    recovered = [s for s in retried if s.outcome == "allowed"]
    first_ok = [s for s in sessions if s.first_try_ok]

    overview = {
        "events": ev_n, "sessions": len(sessions),
        "allowed": len(allowed), "denied": len(denied), "success_rate": _rate(len(allowed), len(sessions)),
        "first_try_ok": len(first_ok), "first_try_rate": _rate(len(first_ok), len(sessions)),
        "retried_sessions": len(retried), "recovered": len(recovered), "recovery_rate": _rate(len(recovered), len(retried)),
        "denied_events": sum(1 for r in rows if r["verdict"] == "denied"),
        "allowed_events": sum(1 for r in rows if r["verdict"] == "allowed"),
        "avg_attempts_recovered": round(sum(s.attempts for s in recovered) / len(recovered), 2) if recovered else None,
        "avg_attempts_denied": round(sum(s.attempts for s in denied) / len(denied), 2) if denied else None,
        "gap_sec": gap_sec,
    }

    # BCT 별
    per_bct = []
    for bct in sorted({s.bct for s in sessions}, key=_bct_key):
        ss = [s for s in sessions if s.bct == bct]
        al = [s for s in ss if s.outcome == "allowed"]; de = [s for s in ss if s.outcome == "denied"]
        rt = [s for s in ss if s.attempts > 1]; rc = [s for s in rt if s.outcome == "allowed"]
        reasons = Counter(s.final_reasons for s in de)
        per_bct.append({
            "bct": bct, "events": sum(1 for r in rows if r["bct"] == bct), "sessions": len(ss),
            "allowed": len(al), "denied": len(de), "success_rate": _rate(len(al), len(ss)),
            "first_try_rate": _rate(sum(1 for s in ss if s.first_try_ok), len(ss)),
            "retried": len(rt), "recovery_rate": _rate(len(rc), len(rt)),
            "avg_attempts": round(sum(s.attempts for s in ss) / len(ss), 2) if ss else None,
            "max_attempts": max((s.attempts for s in ss), default=0),
            "top_reason": reasons.most_common(1)[0][0] if reasons else "",
            "reasons": dict(reasons),
        })

    # 일별
    daily = []
    for d in sorted({s.start.date().isoformat() for s in sessions}):
        ss = [s for s in sessions if s.start.date().isoformat() == d]
        al = sum(1 for s in ss if s.outcome == "allowed")
        daily.append({"date": d, "weekday": "월화수목금토일"[date.fromisoformat(d).weekday()],
                      "events": sum(1 for r in rows if r["date"] == d), "sessions": len(ss),
                      "allowed": al, "denied": len(ss) - al, "success_rate": _rate(al, len(ss))})

    # 시간대별
    hourly = []
    for h in range(24):
        ss = [s for s in sessions if s.start.hour == h]
        al = sum(1 for s in ss if s.outcome == "allowed")
        hourly.append({"hour": h, "sessions": len(ss), "allowed": al, "denied": len(ss) - al, "success_rate": _rate(al, len(ss))})

    # 시도 횟수 분포
    att = Counter()
    for s in sessions:
        k = s.attempts if s.attempts < 6 else 6
        att[(k, s.outcome)] += 1
    attempts_dist = [{"attempts": (str(k) if k < 6 else "6+"), "allowed": att[(k, "allowed")], "denied": att[(k, "denied")]} for k in range(1, 7)]

    # 거부 사유 (거부 세션의 최종 사유)
    reason_sessions = Counter(s.final_reasons for s in denied)
    reason_single = Counter()
    for s in denied:
        for x in s.final_reasons.split("|"):
            if x: reason_single[x] += 1
    # 거부 이벤트 전체 기준(재시도 포함)
    reason_events = Counter()
    for r in rows:
        if r["verdict"] == "denied":
            for x in r["reasons"].split("|"):
                if x: reason_events[x] += 1

    # 이상 징후
    bct_days = defaultdict(Counter)
    for r in rows: bct_days[r["bct"]][r["date"]] += 1
    all_days = sorted({r["date"] for r in rows})
    zero_days = [{"bct": b, "date": d} for b in sorted(bct_days, key=_bct_key) for d in all_days if bct_days[b][d] == 0]
    longest = sorted(sessions, key=lambda s: -s.attempts)[:10]
    long_sessions = [{"bct": s.bct, "start": s.start.strftime("%m-%d %H:%M:%S"), "attempts": s.attempts,
                      "duration_min": round(s.duration_sec / 60, 1), "outcome": s.outcome, "final_reasons": s.final_reasons}
                     for s in longest]
    no_tg = sum(1 for r in rows if not r["has_tg"]); no_train = sum(1 for r in rows if not r["has_train"])
    no_verdict = sum(1 for r in rows if not r["verdict"])

    # 민감도: gap 을 바꾸면 세션 수/성공률이 얼마나 변하나
    sensitivity = []
    for g in sorted({60, 120, 180, 300, 420, 600, gap_sec}):
        ss = build_sessions([dict(r) for r in rows], g)
        al = sum(1 for s in ss if s.outcome == "allowed")
        sensitivity.append({"gap_sec": g, "sessions": len(ss), "allowed": al, "denied": len(ss) - al, "success_rate": _rate(al, len(ss))})

    return {
        "overview": overview, "per_bct": per_bct, "daily": daily, "hourly": hourly, "attempts_dist": attempts_dist,
        "reason_sessions": dict(reason_sessions.most_common()), "reason_single": dict(reason_single.most_common()),
        "reason_events": dict(reason_events.most_common()),
        "anomalies": {"zero_days": zero_days, "long_sessions": long_sessions, "no_tg": no_tg, "no_train": no_train, "no_verdict": no_verdict},
        "sensitivity": sensitivity,
    }


# ══════════════════════════════════════════════════════════════════════════
# 파일 출력
# ══════════════════════════════════════════════════════════════════════════
def write_outputs(out: Path, rows: list[dict], sessions: list[Session], summary: dict, quality: dict) -> None:
    out.mkdir(parents=True, exist_ok=True)
    def wcsv(name, recs, fields=None):
        if not recs: return
        fields = fields or [k for k in recs[0].keys() if k != "dt"]
        with open(out / name, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore"); w.writeheader(); w.writerows(recs)
    wcsv("events_raw.csv", rows)
    wcsv("sessions.csv", [{
        "session_no": i + 1, "bct": s.bct, "date": s.start.date().isoformat(), "start": s.start.strftime("%H:%M:%S"),
        "end": s.end.strftime("%H:%M:%S"), "attempts": s.attempts, "outcome": s.outcome, "first_try_ok": s.first_try_ok,
        "duration_sec": int(s.duration_sec), "final_reasons": s.final_reasons, "event_ids": " ".join(e["event_id"] for e in s.events),
    } for i, s in enumerate(sessions)])
    wcsv("per_bct.csv", [{k: v for k, v in b.items() if k != "reasons"} for b in summary["per_bct"]])
    wcsv("daily.csv", summary["daily"])
    wcsv("hourly.csv", summary["hourly"])
    (out / "summary.json").write_text(json.dumps({"summary": summary, "quality": quality}, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
