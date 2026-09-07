"""python -m review <command> ...

  sites                          등록된 현장
  days SITE [--month YYYY-MM]    MinIO 에 데이터가 있는 날짜와 건수
  list SITE DATE [필터…]         하루치 이벤트 (Influx 조인: 판정·점수·사유)
  check-influx SITE DATE         Influx 필드·조인율·시각차 검증
  resolve SITE MEMO.txt          텔레그램 메모(HHMM BCT) → 이벤트 ID
  fetch SITE (--ids-file F | --ids a,b | DATE --bct.. --verdict..)  클립 다운로드
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from tabulate import tabulate

from . import config as cfgmod
from .access import SiteAccess
from .catalog import Event, list_bcts, list_days, list_events, minio_client, parse_event_id
from .download import fetch_events, write_fetch_log
from .influx import join_events, query_day, reasons_from
from .memo import nearby, parse_memo, resolve


# ── 공통 ─────────────────────────────────────────────────────────────────
def _client(site, acc: SiteAccess):
    site.require_secrets()
    return minio_client(acc.minio_endpoint, site.minio_access, site.minio_secret, site.minio_secure)


def _influx_rows(site, acc: SiteAccess, date: str, tz: str) -> list[dict]:
    inf = site.influx
    return query_day(acc.influx_url, site.influx_token, inf.get("org", "mithril"),
                     inf.get("bucket", "gate_events"), inf.get("measurement", "gate_event"), date, tz)


def _bct_arg(v: str | None) -> list[str] | None:
    if not v:
        return None
    out = []
    for x in v.split(","):
        x = x.strip().lower()
        if not x:
            continue
        out.append(x if x.startswith("bct") else f"bct{int(x)}")
    return out


def _in_window(ev: Event, t_from: str | None, t_to: str | None) -> bool:
    hhmm = ev.hhmm
    if t_from and hhmm < t_from.replace(":", ""):
        return False
    if t_to and hhmm > t_to.replace(":", ""):
        return False
    return True


# ── 명령 ─────────────────────────────────────────────────────────────────
def cmd_sites(a, st):
    rows = [(s.code, s.name, s.access, ",".join(s.cameras), len(s.bcts),
             "OK" if s.minio_access else "없음") for s in st.sites.values()]
    print(tabulate(rows, headers=["code", "name", "access", "cameras", "bcts", "secrets"]))
    root, src = st.resolve_out_root()
    print(f"\n저장 루트: {root}  ({src})")
    return 0


def cmd_days(a, st):
    site = st.site(a.site)
    with SiteAccess(site) as acc:
        c = _client(site, acc)
        days = list_days(c, site.minio_bucket)
    rows = []
    for d, per in days.items():
        if a.month and not d.startswith(a.month):
            continue
        rows.append([d, sum(per.values())] + [per.get(b, 0) for b in site.bcts])
    print(tabulate(rows, headers=["date", "total"] + list(site.bcts)))
    return 0


def _list_with_influx(site, acc, date, tz, use_influx: bool):
    c = _client(site, acc)
    events = list_events(c, site.minio_bucket, site.code, date, tg_prefix=site.tg_prefix)
    matched, stats = {}, {}
    if use_influx and site.influx_token:
        try:
            rows = _influx_rows(site, acc, date, tz)
            matched, stats = join_events(events, rows, tz, int(site.influx.get("join_tolerance_sec", 90)))
        except Exception as e:  # Influx 가 죽어도 목록은 나오게
            print(f"[influx] 조회 실패, 판정 없이 진행: {e}", file=sys.stderr)
    return c, events, matched, stats


def cmd_list(a, st):
    site = st.site(a.site)
    bcts = _bct_arg(a.bct)
    with SiteAccess(site) as acc:
        c, events, matched, stats = _list_with_influx(site, acc, a.date, st.timezone, not a.no_influx)
    rows = []
    for ev in events:
        if bcts and ev.bct not in bcts:
            continue
        if not _in_window(ev, a.time_from, a.time_to):
            continue
        r = matched.get(ev.id)
        verdict = (r or {}).get("verdict", "")
        if a.verdict and verdict != a.verdict:
            continue
        reasons = reasons_from(r, site.thresholds)
        if a.reason and not any(a.reason in x for x in reasons):
            continue
        if a.min_hook is not None and r and r.get("hook_score", 0) < a.min_hook:
            continue
        if a.max_hook is not None and r and r.get("hook_score", 0) > a.max_hook:
            continue
        rows.append([
            ev.id, ev.time_str, verdict or "-",
            f"{r['hook_score']:.2f}" if r else "-",
            f"{r['helmet_score']:.2f}" if r else "-",
            f"{r['harness_score']:.2f}" if r else "-",
            ",".join(reasons) if reasons else "-",
            "".join(k[0] for k in sorted(ev.train)), "".join(k[0] for k in sorted(ev.tg)),
        ])
    hdr = ["event_id", "time", "verdict", "hook", "helmet", "harness", "reasons", "train", "tg"]
    if a.csv:
        import csv
        with open(a.csv, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f); w.writerow(hdr); w.writerows(rows)
        print(f"{len(rows)}건 → {a.csv}")
    else:
        print(tabulate(rows, headers=hdr))
    if stats:
        print(f"\n{len(events)}건 중 표시 {len(rows)}건 · Influx 조인 {stats['matched']}/{stats['events']}"
              + (f" · 시각차 중앙값 {stats['delta_med']:.1f}s" if stats.get("delta_med") is not None else ""))
    else:
        print(f"\n{len(events)}건 중 표시 {len(rows)}건 (Influx 미조인)")
    return 0


def cmd_check_influx(a, st):
    site = st.site(a.site)
    if not site.influx_token:
        print(f"[{site.code}] influx_token 이 secrets.local.json 에 없습니다."); return 2
    with SiteAccess(site) as acc:
        c = _client(site, acc)
        events = list_events(c, site.minio_bucket, site.code, a.date, tg_prefix=site.tg_prefix)
        rows = _influx_rows(site, acc, a.date, st.timezone)
        matched, stats = join_events(events, rows, st.timezone, int(site.influx.get("join_tolerance_sec", 90)))

    print(f"=== {site.code} {a.date} ===")
    print(f"MinIO 이벤트 {len(events)}건 · Influx 행 {len(rows)}건 · 조인 {stats['matched']}건")
    if stats.get("delta_med") is not None:
        print(f"트리거→Influx 시각차(s): min {stats['delta_min']:.1f} / med {stats['delta_med']:.1f} / max {stats['delta_max']:.1f}")
    fields = sorted({k for r in rows for k in r if k not in ("time", "gate_id", "verdict")})
    print(f"필드: {', '.join(fields) or '(없음)'}")
    have = {f: sum(1 for r in rows if r.get(f) is not None) for f in ("allowed", "hook_score", "helmet_score", "harness_score")}
    print("필수 필드 채움율: " + " · ".join(f"{k} {v}/{len(rows)}" for k, v in have.items()))
    print("사유(reasons) 필드: 없음 → 점수·임계값으로 유도 (thresholds 참고)")
    from collections import Counter
    print("verdict 분포:", dict(Counter(r.get("verdict", "") for r in rows)))
    print("\n샘플 (앞 5건):")
    sample = [[ev.id, ev.time_str, (matched.get(ev.id) or {}).get("verdict", "-"),
               (matched.get(ev.id) or {}).get("hook_score", "-"),
               ",".join(reasons_from(matched.get(ev.id), site.thresholds)) or "-"] for ev in events[:5]]
    print(tabulate(sample, headers=["event_id", "time", "verdict", "hook", "reasons(유도)"]))
    unmatched = [ev for ev in events if ev.id not in matched]
    if unmatched:
        print(f"\n조인 안 된 이벤트 {len(unmatched)}건 (앞 5): " + ", ".join(ev.id for ev in unmatched[:5]))
    return 0


def cmd_resolve(a, st):
    site = st.site(a.site)
    entries = parse_memo(Path(a.memo).read_text(encoding="utf-8"))
    dates = sorted({e.date for e in entries})
    with SiteAccess(site) as acc:
        c = _client(site, acc)
        by_date = {d: list_events(c, site.minio_bucket, site.code, d, tg_prefix=site.tg_prefix) for d in dates}
    res = resolve(entries, by_date)
    ok = [r for r in res if r.status == "OK"]
    bad = [r for r in res if r.status != "OK"]
    print(tabulate([[r.entry.date, r.entry.hhmm, r.entry.bct, r.status,
                     r.matched[0].id if len(r.matched) == 1 else " | ".join(m.time_str for m in r.matched)] for r in res],
                   headers=["date", "hhmm", "bct", "status", "event_id / 후보"]))
    print(f"\n{len(res)}건 중 매칭 {len(ok)} · 문제 {len(bad)}")
    for r in bad:
        if r.status == "MISS":
            hint = nearby(r.entry, by_date)
            print(f"  MISS  {r.entry.date} {r.entry.hhmm} {r.entry.bct}  ±10분 후보: "
                  + (", ".join(ev.time_str for ev in hint) or "없음"))
        else:
            print(f"  {r.status}  {r.entry.date} {r.entry.hhmm} {r.entry.bct}: " + ", ".join(m.id for m in r.matched))
    if a.out:
        Path(a.out).write_text("\n".join(r.matched[0].id for r in ok) + "\n", encoding="utf-8")
        print(f"매칭된 ID {len(ok)}개 → {a.out}")
    return 0 if not bad else 1


def cmd_fetch(a, st):
    site = st.site(a.site)
    roles = [r.strip() for r in (a.roles or ",".join(site.cameras)).split(",") if r.strip()]
    out_root, src = st.resolve_out_root(a.out)
    print(f"[fetch] 저장 루트 {out_root} ({src}) · roles {roles} · tg={'on' if a.tg else 'off'}", file=sys.stderr)

    ids: list[str] = []
    if a.ids_file:
        ids += [l.strip() for l in Path(a.ids_file).read_text(encoding="utf-8").splitlines() if l.strip() and not l.startswith("#")]
    if a.ids:
        ids += [x.strip() for x in a.ids.split(",") if x.strip()]

    with SiteAccess(site) as acc:
        c = _client(site, acc)
        targets: list[Event] = []
        if ids:
            wanted = {}
            for i in ids:
                s, bct, ts = parse_event_id(i)
                if s != site.code:
                    raise SystemExit(f"현장 불일치: {i} (현재 {site.code})")
                wanted.setdefault(f"{ts[0:4]}-{ts[4:6]}-{ts[6:8]}", set()).add((bct, ts))
            for d, keys in wanted.items():
                evs = list_events(c, site.minio_bucket, site.code, d, tg_prefix=site.tg_prefix)
                idx = {(e.bct, e.ts): e for e in evs}
                for k in sorted(keys):
                    if k in idx:
                        targets.append(idx[k])
                    else:
                        print(f"  없음: {site.code}-{k[0]}-{k[1]}", file=sys.stderr)
        elif a.date:
            _, events, matched, _ = _list_with_influx(site, acc, a.date, st.timezone, bool(a.verdict or a.reason))
            bcts = _bct_arg(a.bct)
            for ev in events:
                if bcts and ev.bct not in bcts:
                    continue
                if not _in_window(ev, a.time_from, a.time_to):
                    continue
                r = matched.get(ev.id)
                if a.verdict and (r or {}).get("verdict") != a.verdict:
                    continue
                if a.reason and not any(a.reason in x for x in reasons_from(r, site.thresholds)):
                    continue
                targets.append(ev)
        else:
            raise SystemExit("--ids/--ids-file 또는 DATE 를 주세요.")

        if not targets:
            print("대상 이벤트 없음"); return 1
        if a.dry_run:
            print(f"[dry-run] {len(targets)}건:"); [print(" ", e.id) for e in targets]; return 0

        def prog(i, n, r):
            print(f"  {i:4d}/{n}  {r.event_id}  ↓{len(r.downloaded)} ={len(r.skipped)} ✗{len(r.missing)}", file=sys.stderr)
        results = fetch_events(c, site.minio_bucket, targets, out_root, roles, a.tg, prog)

    by_date: dict[str, list] = {}
    for ev, r in zip(targets, results):
        by_date.setdefault(ev.date, []).append(r)
    for d, rs in by_date.items():
        p = write_fetch_log(out_root, site.code, d, rs, {"source": "ids" if ids else "filter"})
        print(f"{d}: {len(rs)}건 · 다운로드 {sum(len(r.downloaded) for r in rs)} · 건너뜀 {sum(len(r.skipped) for r in rs)} · 결손 {sum(len(r.missing) for r in rs)}  → {p.parent}")
    return 0


# ── argparse ─────────────────────────────────────────────────────────────
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m review", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", help="설정 디렉터리 (기본 config/, 또는 BCT_REVIEW_CONFIG)")
    sp = p.add_subparsers(dest="cmd", required=True)

    sp.add_parser("sites").set_defaults(fn=cmd_sites)

    q = sp.add_parser("days"); q.add_argument("site"); q.add_argument("--month"); q.set_defaults(fn=cmd_days)

    q = sp.add_parser("list"); q.add_argument("site"); q.add_argument("date")
    q.add_argument("--bct", help="bct7,bct8 또는 7,8"); q.add_argument("--verdict", choices=["allowed", "denied"])
    q.add_argument("--reason", help="사유 부분 문자열 (예: 안전고리)")
    q.add_argument("--from", dest="time_from", help="HH:MM"); q.add_argument("--to", dest="time_to", help="HH:MM")
    q.add_argument("--min-hook", type=float); q.add_argument("--max-hook", type=float)
    q.add_argument("--no-influx", action="store_true"); q.add_argument("--csv")
    q.set_defaults(fn=cmd_list)

    q = sp.add_parser("check-influx"); q.add_argument("site"); q.add_argument("date"); q.set_defaults(fn=cmd_check_influx)

    q = sp.add_parser("resolve"); q.add_argument("site"); q.add_argument("memo"); q.add_argument("--out", help="매칭된 ID 목록 파일")
    q.set_defaults(fn=cmd_resolve)

    q = sp.add_parser("fetch"); q.add_argument("site"); q.add_argument("date", nargs="?")
    q.add_argument("--ids"); q.add_argument("--ids-file")
    q.add_argument("--bct"); q.add_argument("--verdict", choices=["allowed", "denied"]); q.add_argument("--reason")
    q.add_argument("--from", dest="time_from"); q.add_argument("--to", dest="time_to")
    q.add_argument("--roles", help="hook,ppe (기본: 현장 cameras)"); q.add_argument("--tg", action="store_true", help="검수용(박스) 영상도")
    q.add_argument("--out", help="저장 루트 (기본: NAS → 로컬 폴백)"); q.add_argument("--dry-run", action="store_true")
    q.set_defaults(fn=cmd_fetch)
    return p


def main(argv=None) -> int:
    a = build_parser().parse_args(argv)
    st = cfgmod.load(a.config)
    return a.fn(a, st)
