"""현장 InfluxDB(gate_events) 조회와 이벤트 조인.

엣지 influx_writer.py 가 쓰는 포인트:
    measurement gate_event
    tags   gate_id (= bct4~bct13), verdict (allowed|denied)
    fields allowed(0/1), hook_score, helmet_score, harness_score, analysis_sec, *_yolo, *_dino
포인트 시각은 분석이 끝난 뒤 서버 수신 시각(UTC). 이벤트 폴더의 ts 는 트리거 시각(현장 로컬).
→ 같은 gate_id 에서 트리거 이후 tolerance 초 안에 가장 가까운 포인트를 붙인다.
사유(reasons)는 Influx 에 없으므로 점수와 임계값으로 유도한다.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .catalog import Event

FIELDS = ("allowed", "hook_score", "helmet_score", "harness_score", "analysis_sec")


def query_day(url: str, token: str, org: str, bucket: str, measurement: str,
              date: str, tz_offset: str = "+09:00", timeout_ms: int = 30_000) -> list[dict]:
    """하루치 gate_event 포인트를 [{time, gate_id, verdict, allowed, hook_score, ...}] 로."""
    from influxdb_client import InfluxDBClient

    d0 = datetime.strptime(date, "%Y-%m-%d")
    start = f"{d0:%Y-%m-%d}T00:00:00{tz_offset}"
    stop = f"{(d0 + timedelta(days=1)):%Y-%m-%d}T00:00:00{tz_offset}"
    flux = f'''
from(bucket: "{bucket}")
  |> range(start: {start}, stop: {stop})
  |> filter(fn: (r) => r._measurement == "{measurement}")
  |> pivot(rowKey: ["_time", "gate_id", "verdict"], columnKey: ["_field"], valueColumn: "_value")
  |> sort(columns: ["_time"])
'''
    rows: list[dict] = []
    with InfluxDBClient(url=url, token=token, org=org, timeout=timeout_ms) as c:
        for table in c.query_api().query(flux, org=org):
            for rec in table.records:
                v = rec.values
                row = {
                    "time": rec.get_time(),                 # tz-aware UTC
                    "gate_id": v.get("gate_id", ""),
                    "verdict": v.get("verdict", ""),
                }
                for k, val in v.items():
                    if k.startswith("_") or k in ("result", "table", "gate_id", "verdict"):
                        continue
                    row[k] = val
                rows.append(row)
    return rows


def join_events(events: list[Event], rows: list[dict], tz_offset: str = "+09:00",
                tolerance_sec: int = 90) -> tuple[dict[str, dict], dict]:
    """이벤트 → Influx 행 매칭. (event_id -> row, 통계) 반환.

    같은 gate_id 의 행 중, 트리거 시각 이후 [-5s, +tolerance] 안에서 가장 이른 행을 고른다.
    한 행은 한 이벤트에만 붙인다.
    """
    by_gate: dict[str, list[dict]] = {}
    for r in rows:
        by_gate.setdefault(r["gate_id"], []).append(r)
    for lst in by_gate.values():
        lst.sort(key=lambda r: r["time"])

    used: set[int] = set()
    matched: dict[str, dict] = {}
    deltas: list[float] = []
    for ev in sorted(events, key=lambda e: e.ts):
        trig = ev.local_dt(tz_offset).astimezone(timezone.utc)
        best = None
        for r in by_gate.get(ev.bct, []):
            if id(r) in used:
                continue
            delta = (r["time"] - trig).total_seconds()
            if -5 <= delta <= tolerance_sec:
                best = r
                break               # 정렬돼 있으므로 첫 후보가 가장 이른 것
            if delta > tolerance_sec:
                break
        if best is not None:
            used.add(id(best))
            matched[ev.id] = best
            deltas.append((best["time"] - trig).total_seconds())

    stats = {
        "events": len(events),
        "rows": len(rows),
        "matched": len(matched),
        "delta_min": min(deltas) if deltas else None,
        "delta_med": sorted(deltas)[len(deltas) // 2] if deltas else None,
        "delta_max": max(deltas) if deltas else None,
    }
    return matched, stats


def reasons_from(row: dict | None, thresholds: dict) -> list[str]:
    """Influx 행 + 임계값 → 사유. 엣지 decision_engine 과 같은 규칙이어야 한다."""
    if row is None:
        return []
    if row.get("verdict") == "allowed" or row.get("allowed") == 1:
        return ["ALL_CLEAR"]
    out = []
    if row.get("helmet_score", 1.0) < thresholds.get("helmet_score", 0.5):
        out.append("안전모 미착용")
    if row.get("harness_score", 1.0) < thresholds.get("harness_score", 0.5):
        out.append("하네스 미착용")
    if row.get("hook_score", 1.0) < thresholds.get("hook_score", 0.5):
        out.append("안전고리 미체결")
    return out or ["거부(사유 미상)"]
