"""월간 성능 검수 통합 리포트 — 여러 현장의 채점 결과를 PDF 한 부로 묶는다.

    1장   검수 환경·기준, 종합 정확도(통합 · 현장별)
    2장~  통합 상세 → 현장별 상세 (BCT 별 · 항목 별 · BCT×항목)
    부록  검수한 이벤트 전체 목록 (클립 · 시스템/실제 판정 · 항목별 결과 · 오탐/미탐) · 제외 목록

python -m review perf combined all 2026-09 [--out-dir data/_reports/perf_2026-09]
산출: report.pdf · report.html · graded_all.csv · summary.json (+ 차트 PNG)

python -m review perf retrain all 2026-09
산출: retrain_clips.json (오탐·미탐 이벤트의 클립 키 · 받을 카메라 · NAS 경로) · retrain_event_ids.txt (fetch --ids-file 용)
"""
from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path

from .perf import CLASS_KEYS, CLASS_KO, PerfStore, _bct_key, _class_block, _verdict_block, graded_rows
from .perf_render import _ci, make_charts
from .report_render import CSS, _img, _n, _pct, html_to_pdf

VK = {"allowed": "승인", "denied": "거부"}


# ══════════════════════════════════════════════════════════════════════════
# 집계
# ══════════════════════════════════════════════════════════════════════════
def load_sites(settings, root: Path, month: str) -> list[dict]:
    """표본이 있는 현장마다 {site, store, rows(채점 유효), excluded}."""
    out = []
    for site in settings.sites.values():
        if not PerfStore.exists(root, site.code, month):
            continue
        store = PerfStore.open(root, site.code, month)
        rows = graded_rows(store)
        for r in rows:
            r["site"] = site.code
            r["site_name"] = site.name
            r["ts"] = store.rows[r["event_id"]]["ts"]
        excluded = [{"site": site.code, "site_name": site.name, "event_id": eid, "bct": store.rows[eid]["bct"],
                     "ts": store.rows[eid]["ts"], "by": (store.judgment(eid) or {}).get("by", "")}
                    for eid in store.queue if (store.judgment(eid) or {}).get("excluded")]
        out.append({"site": site, "store": store, "rows": rows, "excluded": excluded})
    return out


def section(name: str, rows: list[dict], bct_label) -> dict:
    """perf_render.make_charts 가 읽는 모양(overview · per_bct · per_class)으로 한 구간을 집계한다."""
    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(bct_label(r), []).append(r)
    per_bct = [{"bct": lab, **_verdict_block(br), "classes": {k: _class_block(br, k) for k in CLASS_KEYS}}
               for lab, br in groups.items()]
    return {"name": name, "overview": _verdict_block(rows), "per_bct": per_bct,
            "per_class": [_class_block(rows, k) for k in CLASS_KEYS],
            "errors": [r for r in rows if not r["all_ok"]], "n_rows": len(rows)}


def _bct_sort_key(label: str):
    parts = label.split()
    return (parts[0] if len(parts) > 1 else "", _bct_key(parts[-1].lower()))


def build_summary(sites: list[dict], month: str) -> dict:
    all_rows = [r for s in sites for r in s["rows"]]
    name_of = {s["site"].code: s["site"].name for s in sites}
    sections = [section("통합", all_rows, lambda r: f"{name_of[r['site']].split()[-1]} {r['bct'].upper()}")]
    for s in sites:
        sections.append(section(s["site"].name, s["rows"], lambda r: r["bct"].upper()))
    order = {name.split()[-1]: i for i, name in enumerate(name_of.values())}       # 통합 라벨 "영월 BCT4" → 현장 등록 순
    for sec in sections:
        sec["per_bct"].sort(key=lambda b: (order.get(_bct_sort_key(b["bct"])[0], 0), _bct_sort_key(b["bct"])[1]))
    sections[0]["chart_opts"] = {"figsize": (9, 3.8), "rotate": 35, "fontsize": 8.5}
    meta = {
        "month": month,
        "sites": [{"code": s["site"].code, "name": s["site"].name, "bcts": sorted(s["store"].pool["bcts"], key=_bct_key),
                   "events": s["store"].pool.get("stats", {}).get("events", 0),
                   "eligible": s["store"].pool.get("stats", {}).get("eligible", 0),
                   "per_bct": s["store"].pool["per_bct"], "seed": s["store"].pool["seed"],
                   "thresholds": s["store"].pool["thresholds"], "model": s["store"].data.get("model", ""),
                   "n": len(s["rows"]), "excluded": len(s["excluded"]), "sampling": s["store"].pool.get("sampling"),
                   "days": sorted({r["date"] for r in s["rows"]})} for s in sites],
        "reviewers": sorted({r["by"] for r in all_rows if r["by"]}),
        "graded_from": min(r["at"] for r in all_rows)[:16].replace("T", " ") if all_rows else "",
        "graded_to": max(r["at"] for r in all_rows)[:16].replace("T", " ") if all_rows else "",
        "n": len(all_rows), "excluded": sum(len(s["excluded"]) for s in sites),
    }
    return {"meta": meta, "sections": sections}


# ══════════════════════════════════════════════════════════════════════════
# HTML 조각
# ══════════════════════════════════════════════════════════════════════════
def _mark(pred: bool, gt: bool) -> str:
    if pred == gt:
        return "<span class=good>O</span>" if pred else "<span class=bad>X</span>"
    return "<span class=bad>X→O 오탐</span>" if not pred else "<span class=bad>O→X 미탐</span>"


def _result(r: dict) -> str:
    fp = any(not r[f"{k}_pred"] and r[f"{k}_gt"] for k in CLASS_KEYS)
    fn = any(r[f"{k}_pred"] and not r[f"{k}_gt"] for k in CLASS_KEYS)
    return "오탐·미탐" if fp and fn else ("오탐" if fp else ("미탐" if fn else "정상"))


def _tiles(o: dict) -> str:
    return ("<div class='tiles'>"
            f"<div class='tile accent'><div class='k'>출입 판정 정확도</div><div class='v'>{_pct(o['accuracy'])}</div>"
            f"<div class='s'>95% CI {_ci(o['ci'])}% · 표본 {_n(o['n'])}</div></div>"
            f"<div class='tile allow'><div class='k'>전 항목 정답률</div><div class='v'>{_pct(o['all_ok_rate'])}</div>"
            f"<div class='s'>세 항목 모두 맞힘 {_n(o['all_ok'])}건</div></div>"
            f"<div class='tile deny'><div class='k'>부당 거부</div><div class='v'>{_n(o['wrong_deny'])}</div>"
            f"<div class='s'>거부 판정 {_n(o['sys_denied'])}건의 {_pct(o['wrong_deny_rate'])}</div></div>"
            f"<div class='tile deny'><div class='k'>부당 승인</div><div class='v'>{_n(o['wrong_allow'])}</div>"
            f"<div class='s'>승인 판정 {_n(o['sys_allowed'])}건의 {_pct(o['wrong_allow_rate'])}</div></div>"
            "</div>")


def _overview_table(sections: list[dict]) -> str:
    def row(sec):
        o = sec["overview"]
        pc = {c["class"]: c for c in sec["per_class"]}
        cells = "".join(f"<td class=n>{_pct(pc[k]['fp_rate'])} / {_pct(pc[k]['fn_rate'])}</td>" for k in CLASS_KEYS)
        return (f"<tr><td><b>{sec['name']}</b></td><td class=n>{_n(o['n'])}</td><td class=n>{_pct(o['accuracy'])}</td>"
                f"<td class=n>{_ci(o['ci'])}</td><td class=n>{_pct(o['all_ok_rate'])}</td>"
                f"<td class=n>{_n(o['wrong_deny'])} <span class=muted>({_pct(o['wrong_deny_rate'])})</span></td>"
                f"<td class=n>{_n(o['wrong_allow'])} <span class=muted>({_pct(o['wrong_allow_rate'])})</span></td>{cells}</tr>")
    heads = "".join(f"<th class=n>{CLASS_KO[k]}<br>오탐율 / 미탐율</th>" for k in CLASS_KEYS)
    body = "".join(row(s) for s in sections)
    return ("<table class=ov><thead><tr><th>구분</th><th class=n>표본</th><th class=n>출입 판정 정확도</th><th class=n>95% CI</th>"
            f"<th class=n>전 항목 정답률</th><th class=n>부당 거부</th><th class=n>부당 승인</th>{heads}</tr></thead>"
            f"<tbody>{body}</tbody></table>")


def _bct_table(sec: dict) -> str:
    rows = []
    for b in sec["per_bct"]:
        cls = "bad" if b["accuracy"] < 85 else ("good" if b["accuracy"] >= 95 else "")
        hi = " class=hi" if b["accuracy"] < 85 else ""
        fps = "".join(f"<td class=n>{_pct(b['classes'][k]['fp_rate'])}</td>" for k in CLASS_KEYS)
        rows.append(f"<tr{hi}><td><b>{b['bct']}</b></td><td class=n>{_n(b['n'])}</td>"
                    f"<td class=n>{_n(b['sys_allowed'])} / {_n(b['sys_denied'])}</td>"
                    f"<td class='n {cls}'>{_pct(b['accuracy'])}</td><td class=n>{_ci(b['ci'])}</td>"
                    f"<td class=n>{_n(b['wrong_deny'])} <span class=muted>({_pct(b['wrong_deny_rate'])})</span></td>"
                    f"<td class=n>{_n(b['wrong_allow'])} <span class=muted>({_pct(b['wrong_allow_rate'])})</span></td>"
                    f"<td class=n>{_pct(b['all_ok_rate'])}</td>{fps}</tr>")
    heads = "".join(f"<th class=n>{CLASS_KO[k]} 오탐율</th>" for k in CLASS_KEYS)
    return ("<table><thead><tr><th>BCT</th><th class=n>표본</th><th class=n>승인 / 거부</th><th class=n>정확도</th>"
            f"<th class=n>95% CI</th><th class=n>부당 거부</th><th class=n>부당 승인</th><th class=n>전 항목</th>{heads}</tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table>")


def _class_table(sec: dict) -> str:
    rows = []
    for c in sec["per_class"]:
        fp_cls = "bad" if (c["fp_rate"] or 0) >= 10 else ""
        fn_cls = "bad" if (c["fn_rate"] or 0) >= 5 else ""
        rows.append(f"<tr><td><b>{c['name']}</b></td><td class=n>{_n(c['n'])}</td><td class=n>{_pct(c['accuracy'])}</td>"
                    f"<td class=n>{_n(c['pred_x'])}</td><td class=n>{_n(c['fp'])}</td><td class='n {fp_cls}'>{_pct(c['fp_rate'])}</td>"
                    f"<td class=n>{_n(c['pred_o'])}</td><td class=n>{_n(c['fn'])}</td><td class='n {fn_cls}'>{_pct(c['fn_rate'])}</td></tr>")
    return ("<table><thead><tr><th>항목</th><th class=n>표본</th><th class=n>정확도</th><th class=n>미착용 판정</th><th class=n>오탐</th>"
            "<th class=n>오탐율</th><th class=n>착용 판정</th><th class=n>미탐</th><th class=n>미탐율</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table>")


def _matrix_table(sec: dict) -> str:
    rows = []
    for b in sec["per_bct"]:
        cells = "".join(f"<td class=n>{b['classes'][k]['fp']}</td><td class=n>{b['classes'][k]['fn']}</td>" for k in CLASS_KEYS)
        rows.append(f"<tr><td><b>{b['bct']}</b></td><td class=n>{_n(b['n'])}</td>{cells}</tr>")
    heads = "".join(f"<th class=n>{CLASS_KO[k]} 오탐</th><th class=n>{CLASS_KO[k]} 미탐</th>" for k in CLASS_KEYS)
    return f"<table><thead><tr><th>BCT</th><th class=n>표본</th>{heads}</tr></thead><tbody>{''.join(rows)}</tbody></table>"


def _detail(sec: dict, charts: dict[str, Path]) -> str:
    def img(key):
        return f"<div class='chart'><img src='{_img(charts[key])}'></div>" if key in charts else ""
    return (f"<div class='pb'></div><h2>{sec['name']} 상세</h2>{_tiles(sec['overview'])}"
            f"<h3>BCT 별</h3>{img('bct')}{_bct_table(sec)}"
            f"<h3>항목 별</h3><div class='two'><div>{img('class')}</div><div></div></div>{_class_table(sec)}"
            f"<h3>BCT × 항목 오류 건수</h3>{_matrix_table(sec)}")


def _env_table(meta: dict) -> str:
    sites = meta["sites"]
    site_line = " · ".join(
        f"{s['name']} {s['bcts'][0].upper()}~{s['bcts'][-1].upper()} ({len(s['bcts'])}개소)" if len(s["bcts"]) > 1
        else f"{s['name']} {s['bcts'][0].upper()}" for s in sites)
    days = sorted({d for s in sites for d in s["days"]})
    ev_line = " · ".join(f"{s['name']} {_n(s['events'])}건 (후보 {_n(s['eligible'])})" for s in sites)
    samp = " · ".join(f"{s['name']} {_n(s['n'])}건" for s in sites)
    th = sites[0]["thresholds"] if sites else {}
    same_th = all(s["thresholds"] == th for s in sites)
    th_line = (f"안전모 {th.get('helmet_score')} · 하네스 {th.get('harness_score')} · 안전고리 {th.get('hook_score')}"
               + ("" if same_th else " (현장별 상이)")
               + " — 점수가 임계값 이상이면 착용·체결(O), 미만이면 미착용·미체결(X). 엣지 판정 규칙과 동일")
    models = {s["model"] for s in sites if s["model"]}
    period = f"{meta['month']} ({days[0]} ~ {days[-1]}, 표본에 포함된 날짜 {len(days)}일)" if days else meta["month"]
    rows = [
        ("대상 기간", period),
        ("현장 · BCT", site_line),
        ("데이터", f"현장 서버 MinIO 트리거 클립(hook · ppe 카메라)과 InfluxDB gate_event(승인/거부, 항목별 점수). {ev_line}"),
        ("표본 설계", f"BCT 당 {sites[0]['per_bct']}건. 시스템 승인·거부를 반반으로 하되 한쪽이 부족하면 전부 넣고 나머지를 다른 쪽에서 채움. "
                   "승인·거부 각각 안에서 일자 → 시간대(2시간) 순으로 번갈아 추출. "
                   f"판단 불가(사람 안 보임·영상 손상)는 제외하고 같은 BCT·같은 판정의 다음 후보로 보충. {samp}"),
        ("채점", "검수자가 hook · ppe 영상을 보고 모델 판정이 틀린 항목(안전모 · 하네스 · 안전고리)만 표시. 표시하지 않은 항목은 모델이 맞은 것. "
               "실제 출입 = 세 항목 모두 실제 착용·체결이면 승인, 아니면 거부."),
        ("판정 기준", th_line),
        ("지표", "출입 판정 정확도 = 시스템 승인/거부가 실제와 같은 비율(Wilson 95% 신뢰구간). "
               "부당 거부 = 시스템 거부 · 실제 승인 대상. 부당 승인 = 시스템 승인 · 실제 거부 대상. "
               "오탐율 = 미착용(X) 판정 중 실제 착용 비율. 미탐율 = 착용(O) 판정 중 실제 미착용 비율."),
    ]
    if models:
        rows.insert(5, ("모델", " · ".join(sorted(models))))
    body = "".join(f"<tr><th>{k}</th><td>{v}</td></tr>" for k, v in rows)
    return f"<table class=env><tbody>{body}</tbody></table>"


def _event_list(sites: list[dict]) -> str:
    parts = []
    for s in sites:
        rows = sorted(s["rows"], key=lambda r: (_bct_key(r["bct"]), r["ts"]))
        trs = []
        for i, r in enumerate(rows, 1):
            marks = "".join(f"<td>{_mark(r[f'{k}_pred'], r[f'{k}_gt'])}</td>" for k in CLASS_KEYS)
            hi = "" if r["verdict_ok"] else " class=hi"
            trs.append(f"<tr{hi}><td class=n>{i}</td><td>{r['bct'].upper()}</td><td>{r['date'][5:]} {r['time']}</td>"
                       f"<td class=small>{r['bct']}/{r['ts']}/</td><td>{VK[r['verdict']]}</td><td>{VK[r['gt_verdict']]}</td>"
                       f"{marks}<td>{_result(r)}</td></tr>")
        ex_rows = []
        for i, e in enumerate(s["excluded"], 1):
            t = e["ts"]
            ex_rows.append(f"<tr><td class=n>{i}</td><td>{e['bct'].upper()}</td><td>{t[4:6]}-{t[6:8]} {t[9:11]}:{t[11:13]}:{t[13:15]}</td>"
                           f"<td class=small>{e['bct']}/{t}/</td><td colspan=7>판단 불가 → 제외 (같은 BCT 후보로 보충)</td></tr>")
        heads = "".join(f"<th>{CLASS_KO[k]}</th>" for k in CLASS_KEYS)
        ex_html = ""                                   # 제외 목록은 리포트에 싣지 않는다 (graded_all.csv 와 summary.json 에는 남음)
        parts.append(
            f"<div class='pb'></div><h2>부록 · {s['site'].name} 검수 이벤트 목록</h2>"
            f"<p class='small muted'>유효 {_n(len(rows))}건, BCT · 시각 순. 클립 = MinIO 버킷 bct-clips 의 폴더(hook.mp4 · ppe.mp4, "
            "박스 표시 영상은 _tg/ 아래 같은 경로). 항목 칸: O 착용·체결, X 미착용·미체결. X→O 오탐 = 모델 X · 실제 O, "
            "O→X 미탐 = 모델 O · 실제 X. 출입 판정이 틀린 행은 붉게 표시.</p>"
            f"<table class=list><thead><tr><th class=n>#</th><th>BCT</th><th>시각</th><th>클립</th><th>시스템</th><th>실제</th>{heads}"
            f"<th>결과</th></tr></thead><tbody>{''.join(trs)}</tbody></table>{ex_html}")
    return "".join(parts)


EXTRA_CSS = """
.good{color:var(--allow);font-weight:600} .bad{color:var(--deny);font-weight:600}
.pb{break-before:page;page-break-before:always}
table.env th{width:110px;text-align:left;vertical-align:top;background:var(--soft);text-transform:none;letter-spacing:0;font-size:12.5px;color:var(--ink)}
table.env td{font-size:12.5px} table.list{font-size:11px} table.list th,table.list td{padding:3px 6px} table.list tr{break-inside:avoid}
thead{display:table-header-group} table.ov td:first-child{white-space:nowrap}
table.ov{font-size:11.5px} table.ov th{white-space:normal;line-height:1.3} table.ov th,table.ov td{padding:5px 6px}
table.env{font-size:12px}
@media print{.tiles .tile .v{font-size:20px} .site-tiles .tile .k{font-size:11px} table{break-inside:auto} tr{break-inside:avoid} h3{break-after:avoid}} .site-tiles .tile .k{font-size:12px;text-transform:none;letter-spacing:0;color:var(--ink)}
"""


def make_html(summary: dict, sites: list[dict], charts: dict[str, dict[str, Path]], out: Path) -> Path:
    meta, secs = summary["meta"], summary["sections"]
    generated = datetime.now().strftime("%Y-%m-%d %H:%M")
    site_names = " · ".join(s["name"] for s in meta["sites"])
    top = "".join(
        f"<div class='tile{' accent' if i == 0 else ''}'><div class='k'>{sec['name']} 출입 판정 정확도</div>"
        f"<div class='v'>{_pct(sec['overview']['accuracy'])}</div>"
        f"<div class='s'>95% CI {_ci(sec['overview']['ci'])}% · 표본 {_n(sec['overview']['n'])}건</div></div>"
        for i, sec in enumerate(secs))
    details = "".join(_detail(sec, charts[sec["name"]]) for sec in secs)
    html = (f"<!doctype html><html lang='ko'><head><meta charset='utf-8'><title>BCT 모델 성능 검수 리포트 {meta['month']}</title>"
            f"<style>{CSS}{EXTRA_CSS}</style></head><body><div class='page'>"
            f"<div class='head'><span class='eyebrow'>Paimedialab · 성능 검수</span><h1>BCT 출입 통제 모델 성능 검수 리포트</h1>"
            f"<span class='meta'>{meta['month']} · {site_names}</span></div>"
            f"<h2>검수 환경 및 기준</h2>{_env_table(meta)}"
            f"<h2>종합 정확도</h2><div class='tiles site-tiles' style='grid-template-columns:repeat({len(secs)},1fr)'>{top}</div>"
            f"{_overview_table(secs)}"
            f"{details}{_event_list(sites)}</div></body></html>")
    p = out / "report.html"
    p.write_text(html, encoding="utf-8")
    return p


def build(settings, root: Path, month: str, out: Path) -> tuple[Path, Path | None, dict]:
    sites = load_sites(settings, root, month)
    if not sites:
        raise SystemExit(f"{month}: 표본이 있는 현장이 없습니다 ({root})")
    summary = build_summary(sites, month)
    out.mkdir(parents=True, exist_ok=True)
    charts = {}
    for i, sec in enumerate(summary["sections"]):
        d = out / f"charts_{i}"
        d.mkdir(exist_ok=True)
        charts[sec["name"]] = make_charts({"per_bct": sec["per_bct"], "overview": sec["overview"], "per_class": sec["per_class"],
                                           "chart_opts": sec.get("chart_opts")}, d)
    html = make_html(summary, sites, charts, out)
    flat = [{**r, "wrong": "|".join(r["wrong"]), "result": _result(r)} for s in sites for r in s["rows"]]
    with open(out / "graded_all.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(flat[0].keys()))
        w.writeheader()
        w.writerows(flat)
    slim = [{k: v for k, v in s.items() if k != "errors"} for s in summary["sections"]]
    (out / "summary.json").write_text(json.dumps({"meta": summary["meta"], "sections": slim}, ensure_ascii=False, indent=2, default=str),
                                      encoding="utf-8")
    pdf = out / "report.pdf"
    return html, (pdf if html_to_pdf(html, pdf) else None), summary


# ══════════════════════════════════════════════════════════════════════════
# 추가 학습용 클립 목록 (오탐·미탐 이벤트)
# ══════════════════════════════════════════════════════════════════════════
ROLE_OF = {"hook": "hook", "helmet": "ppe", "harness": "ppe"}      # 틀린 항목 → 학습 카메라 (export.py 규칙과 동일)
BS = chr(92)


def build_retrain_list(settings, root: Path, month: str, out: Path) -> tuple[Path, Path, dict]:
    """항목이 하나라도 틀린 이벤트를 모아 retrain_clips.json 과 retrain_event_ids.txt 를 쓴다."""
    sites = load_sites(settings, root, month)
    if not sites:
        raise SystemExit(f"{month}: 표본이 있는 현장이 없습니다 ({root})")
    items, per_site = [], {}
    for s in sites:
        site, store = s["site"], s["store"]
        th = store.thresholds()
        mine = []
        for r in s["rows"]:
            if r["all_ok"]:
                continue
            row = store.rows[r["event_id"]]
            errors = []
            for k in CLASS_KEYS:
                if r[f"{k}_pred"] == r[f"{k}_gt"]:
                    continue
                kind = "fp" if not r[f"{k}_pred"] else "fn"
                errors.append({"class": k, "class_ko": CLASS_KO[k], "type": kind,
                               "type_ko": "오탐(모델 X · 실제 O)" if kind == "fp" else "미탐(모델 O · 실제 X)",
                               "score": r[f"{k}_score"], "threshold": th.get(f"{k}_score"), "camera": ROLE_OF[k]})
            cams = sorted({e["camera"] for e in errors}, key=lambda c: c != "hook")
            mine.append({
                "site": site.code, "site_name": site.name, "event_id": r["event_id"], "bct": row["bct"], "ts": row["ts"],
                "date": r["date"], "time": r["time"],
                "system_verdict": r["verdict"], "actual_verdict": r["gt_verdict"], "verdict_correct": r["verdict_ok"],
                "scores": {k: r[f"{k}_score"] for k in CLASS_KEYS},
                "model": {k: ("O" if r[f"{k}_pred"] else "X") for k in CLASS_KEYS},
                "actual": {k: ("O" if r[f"{k}_gt"] else "X") for k in CLASS_KEYS},
                "errors": errors,
                "clips": {"train": {role: f"{row['bct']}/{row['ts']}/{role}.mp4" for role in row["train"]},
                          "tg": {role: f"{site.tg_prefix}{row['bct']}/{row['ts']}/{role}.mp4" for role in row["tg"]}},
                "retrain": {"cameras": cams,
                            "files": {c: {"minio_key": f"{row['bct']}/{row['ts']}/{c}.mp4",
                                          "nas_path": f"{site.code}/{r['date']}/{c}/{row['ts']}_{row['bct']}_{c}.mp4"} for c in cams}},
                "reviewed_by": r["by"], "reviewed_at": r["at"],
            })
        mine.sort(key=lambda i: (_bct_key(i["bct"]), i["ts"]))
        items += mine
        per_site[site.code] = {
            "name": site.name, "events": len(mine),
            "fp_events": sum(1 for i in mine if any(e["type"] == "fp" for e in i["errors"])),
            "fn_events": sum(1 for i in mine if any(e["type"] == "fn" for e in i["errors"])),
            "clips_to_fetch": sum(len(i["retrain"]["files"]) for i in mine),
            "minio": {"endpoint": f"{site.server.get('zerotier_ip')}:{site.server.get('minio_port', 9000)}",
                      "lan_endpoint": f"{site.server.get('lan_ip')}:{site.server.get('minio_port', 9000)}",
                      "bucket": site.minio_bucket, "tg_prefix": site.tg_prefix, "secure": site.minio_secure,
                      "credentials": f"config/secrets.local.json → sites.{site.code}"},
            "thresholds": th}
    by_class = {k: {t: sum(1 for i in items for e in i["errors"] if e["class"] == k and e["type"] == t) for t in ("fp", "fn")}
                for k in CLASS_KEYS}
    doc = {
        "title": f"{month} 성능 검수 오탐·미탐 이벤트 (추가 학습용 클립 목록)",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source": f"성능 검수 플랫폼 채점 결과 (_perf/{{site}}/{month}) — 유효 표본 중 항목이 하나라도 틀린 이벤트",
        "how_to_use": [
            "clips.train = MinIO 버킷의 학습용 원본 키(1280, 박스 없음). clips.tg = 박스 표시 검수용(640).",
            "retrain.files = 틀린 항목에 해당하는 카메라만 (안전고리 → hook, 안전모·하네스 → ppe). "
            "nas_path 는 NAS 저장 루트 아래 상대 경로 (오탐 검수 내보내기와 같은 규칙).",
            "errors[].type: fp = 오탐(모델 미착용 판정 · 실제 착용 → 부당 거부 원인), fn = 미탐(모델 착용 판정 · 실제 미착용 → 부당 승인 원인).",
            f"다운로드: scripts{BS}review fetch <SITE> --ids-file retrain_event_ids.txt --roles hook,ppe (현장 ZeroTier 접속 필요)",
        ],
        "summary": {"total_events": len(items), "total_clips_to_fetch": sum(len(i["retrain"]["files"]) for i in items),
                    "by_site": per_site, "by_class": by_class},
        "nas_root": settings.nas_root,
        "events": items,
    }
    out.mkdir(parents=True, exist_ok=True)
    pj, pi = out / "retrain_clips.json", out / "retrain_event_ids.txt"
    pj.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    pi.write_text("".join(i["event_id"] + chr(10) for i in items), encoding="utf-8")
    return pj, pi, doc["summary"]
