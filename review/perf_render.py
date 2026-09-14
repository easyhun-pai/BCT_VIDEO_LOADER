"""월간 성능 리포트 렌더링 — perf.summarize() 결과 → 차트 PNG + report.html (+ Chrome 이 있으면 report.pdf) + CSV.

양식은 기간 출입 통계 리포트(report_render.py)와 같다: 같은 CSS·색·차트 스타일·PDF 변환.
"""
from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path

from .perf import CLASS_KEYS, CLASS_KO
from .report_render import ACCENT, ALLOW, CSS, DENY, GRID, INK, MUTED, _img, _mpl, _n, _pct, html_to_pdf

WARN = "#D97706"


def _ci(ci) -> str:
    return "–" if not ci else f"{ci[0]:.1f}~{ci[1]:.1f}"


def _delta(v, higher_better: bool) -> str:
    if v is None:
        return "–"
    good = (v > 0) == higher_better
    cls = "" if v == 0 else ("good" if good else "bad")
    return f"<span class='{cls}'>{v:+.1f}%p</span>"


# ══════════════════════════════════════════════════════════════════════════
# 차트
# ══════════════════════════════════════════════════════════════════════════
def make_charts(s: dict, out: Path) -> dict[str, Path]:
    plt = _mpl()
    charts: dict[str, Path] = {}
    pb = [b for b in s["per_bct"] if b["n"]]
    if pb:
        p = out / "chart_bct_accuracy.png"
        fig, ax = plt.subplots(figsize=(9, 3.4))
        x = list(range(len(pb)))
        acc = [b["accuracy"] for b in pb]
        lo = [b["accuracy"] - b["ci"][0] for b in pb]
        hi = [b["ci"][1] - b["accuracy"] for b in pb]
        colors = [ALLOW if a >= 95 else (ACCENT if a >= 85 else DENY) for a in acc]
        ax.bar(x, acc, 0.62, color=colors, zorder=3)
        ax.errorbar(x, acc, yerr=[lo, hi], fmt="none", ecolor=INK, elinewidth=1, capsize=3, zorder=4)
        for xi, a, b in zip(x, acc, pb):
            ax.annotate(f"{a:.0f}%", (xi, b["ci"][1]), textcoords="offset points", xytext=(0, 4), ha="center", fontsize=8, color=INK)
        ov = s["overview"]["accuracy"]
        if ov is not None:
            ax.axhline(ov, color=MUTED, ls="--", lw=1)
            ax.annotate(f"전체 {ov:.1f}%", (-0.5, ov), textcoords="offset points", xytext=(-4, 0), ha="right", va="center", fontsize=8, color=MUTED,
                        annotation_clip=False)
        ax.set_xticks(x); ax.set_xticklabels([b["bct"].upper() for b in pb])
        ymin = max(0, min(b["ci"][0] for b in pb) - 10)
        ax.set_ylim(ymin, 108); ax.set_ylabel("출입 판정 정확도 (%)")
        ax.set_title("BCT 별 출입 판정 정확도 (막대 끝 선 = 95% 신뢰구간)", loc="left"); ax.grid(axis="y", color=GRID, zorder=0)
        fig.tight_layout(); fig.savefig(p); plt.close(fig); charts["bct"] = p

    pc = s["per_class"]
    if any(c["n"] for c in pc):
        p = out / "chart_class_rates.png"
        fig, ax = plt.subplots(figsize=(6.2, 3.0))
        x = list(range(len(pc))); w = 0.36
        fpr = [c["fp_rate"] or 0 for c in pc]; fnr = [c["fn_rate"] or 0 for c in pc]
        ax.bar([i - w / 2 for i in x], fpr, w, color=DENY, label="오탐율 (미착용 판정 중 실제 착용)", zorder=3)
        ax.bar([i + w / 2 for i in x], fnr, w, color=WARN, label="미탐율 (착용 판정 중 실제 미착용)", zorder=3)
        for i, (a, b) in enumerate(zip(fpr, fnr)):
            ax.annotate(f"{a:.1f}%", (i - w / 2, a), textcoords="offset points", xytext=(0, 3), ha="center", fontsize=8, color=INK)
            ax.annotate(f"{b:.1f}%", (i + w / 2, b), textcoords="offset points", xytext=(0, 3), ha="center", fontsize=8, color=INK)
        ax.set_xticks(x); ax.set_xticklabels([c["name"] for c in pc]); ax.set_ylabel("%")
        ax.set_ylim(0, max(fpr + fnr + [1]) * 1.35)
        ax.set_title("클래스 별 오탐율 · 미탐율", loc="left"); ax.grid(axis="y", color=GRID, zorder=0)
        ax.legend(frameon=False, fontsize=8, loc="upper right")
        fig.tight_layout(); fig.savefig(p); plt.close(fig); charts["class"] = p
    return charts


# ══════════════════════════════════════════════════════════════════════════
# HTML
# ══════════════════════════════════════════════════════════════════════════
def make_html(s: dict, cmp: dict | None, charts: dict[str, Path], out: Path) -> Path:
    o = s["overview"]; pb = s["per_bct"]; pc = s["per_class"]; prog = s["progress"]
    graded = [b for b in pb if b["n"]]
    worst = min(graded, key=lambda b: b["accuracy"]) if graded else None
    best = max(graded, key=lambda b: b["accuracy"]) if graded else None
    top_fp = max(pc, key=lambda c: c["fp"]) if pc else None
    th = s["thresholds"]
    model = s.get("model") or "(미기재)"

    def img(key, alt):
        return f'<div class="chart"><img src="{_img(charts[key])}" alt="{alt}"></div>' if key in charts else ""

    def bct_rows():
        rows = []
        for b in pb:
            if not b["n"]:
                rows.append(f"<tr><td><b>{b['bct'].upper()}</b></td><td class=n>0</td><td class=n>{b['excluded']}</td>"
                            f"<td class=n colspan=6 style='text-align:left' >검수 전 (대기 {b['pending']})</td></tr>")
                continue
            hi = ' class="hi"' if b["accuracy"] < 85 else ""
            cls = "bad" if b["accuracy"] < 85 else ("good" if b["accuracy"] >= 95 else "")
            hk = b["classes"]["hook"]
            rows.append(f"<tr{hi}><td><b>{b['bct'].upper()}</b></td><td class=n>{_n(b['n'])}</td><td class=n>{b['excluded']}</td>"
                        f"<td class='n {cls}'>{_pct(b['accuracy'])}</td><td class=n>{_ci(b['ci'])}</td>"
                        f"<td class=n>{b['wrong_deny']} <span class=muted>({_pct(b['wrong_deny_rate'])})</span></td>"
                        f"<td class=n>{b['wrong_allow']} <span class=muted>({_pct(b['wrong_allow_rate'])})</span></td>"
                        f"<td class=n>{_pct(b['all_ok_rate'])}</td><td class=n>{_pct(hk['fp_rate'])}</td></tr>")
        return "".join(rows)

    def class_rows():
        return "".join(f"<tr><td><b>{c['name']}</b></td><td class=n>{_n(c['n'])}</td><td class=n>{_pct(c['accuracy'])}</td>"
                       f"<td class=n>{_n(c['pred_x'])}</td><td class=n>{_n(c['fp'])}</td><td class='n {'bad' if (c['fp_rate'] or 0) >= 10 else ''}'>{_pct(c['fp_rate'])}</td>"
                       f"<td class=n>{_n(c['pred_o'])}</td><td class=n>{_n(c['fn'])}</td><td class='n {'bad' if (c['fn_rate'] or 0) >= 5 else ''}'>{_pct(c['fn_rate'])}</td></tr>"
                       for c in pc)

    def matrix_rows():
        rows = []
        for b in pb:
            cells = "".join(f"<td class=n>{b['classes'][k]['fp']}</td><td class=n>{b['classes'][k]['fn']}</td>" for k in CLASS_KEYS)
            rows.append(f"<tr><td><b>{b['bct'].upper()}</b></td><td class=n>{_n(b['n'])}</td>{cells}</tr>")
        return "".join(rows)

    def error_rows(limit=300):
        vk = {"allowed": "승인", "denied": "거부"}
        out_rows = []
        for r in s["errors"][:limit]:
            wrong = " · ".join(f"{CLASS_KO[k]} {'오탐' if not r[f'{k}_pred'] else '미탐'}" for k in r["wrong"])
            out_rows.append(f"<tr{'' if r['verdict_ok'] else ' class=hi'}><td>{r['bct'].upper()}</td><td>{r['date'][5:]} {r['time']}</td>"
                            f"<td>{vk[r['verdict']]}</td><td>{vk[r['gt_verdict']]}</td><td>{wrong}</td><td class=small>{r['event_id']}</td></tr>")
        return "".join(out_rows)

    def compare_section():
        if not cmp:
            return ("<p class='muted'>비교할 기준 결과가 없습니다. 이전 달(또는 이전 모델) 성능 검수가 끝나면 "
                    "여기에 지표별 변화가 표시됩니다.</p>")
        mrows = "".join(f"<tr><td>{m['name']}</td><td class=n>{_pct(m['base'])}</td><td class=n>{_pct(m['cur'])}</td>"
                        f"<td class=n>{_delta(m['delta'], m['higher_better'])}</td></tr>" for m in cmp["metrics"])
        brows = "".join(f"<tr><td>{b['bct'].upper()}</td><td class=n>{_pct(b['base'])}</td><td class=n>{_pct(b['cur'])}</td>"
                        f"<td class=n>{_delta(b['delta'], True)}</td></tr>" for b in cmp["per_bct"])
        base_lab = f"{cmp['base_month']}" + (f" · {cmp['base_model']}" if cmp.get("base_model") else "")
        cur_lab = f"{s['month']}" + (f" · {cmp['cur_model']}" if cmp.get("cur_model") else "")
        return (f"<p>기준 <b>{base_lab}</b> → 이번 <b>{cur_lab}</b>. 오탐율·미탐율·부당 거부/승인율은 낮을수록 좋다.</p>"
                f"<div class='two'><table><thead><tr><th>지표</th><th class=n>기준</th><th class=n>이번</th><th class=n>변화</th></tr></thead><tbody>{mrows}</tbody></table>"
                f"<table><thead><tr><th>BCT</th><th class=n>기준 정확도</th><th class=n>이번</th><th class=n>변화</th></tr></thead><tbody>{brows}</tbody></table></div>")

    partial = "" if s["complete"] else (
        f"<div class='callout'><b>검수 진행 중 — 중간 집계</b> · 채점 {_n(prog['done'])}건 / 목표 {_n(prog['target'])}건 "
        f"(대기 {_n(prog['pending'])}건). 모든 표본을 채점하면 수치가 바뀔 수 있다.</div>")

    glance = []
    if o["n"]:
        glance.append(f"<li>BCT 당 {s['per_bct_target']}건씩 뽑은 표본 <b>{_n(o['n'])}건</b>에서 출입 판정 정확도는 <b>{_pct(o['accuracy'])}</b> "
                      f"(95% 신뢰구간 {_ci(o['ci'])}%). 세 항목을 모두 맞힌 비율은 {_pct(o['all_ok_rate'])}.</li>")
        glance.append(f"<li>틀린 출입 판정 중 <b>부당 거부 {_n(o['wrong_deny'])}건</b>(거부 판정의 {_pct(o['wrong_deny_rate'])}), "
                      f"<b>부당 승인 {_n(o['wrong_allow'])}건</b>(승인 판정의 {_pct(o['wrong_allow_rate'])}).</li>")
        if worst and best and worst["bct"] != best["bct"]:
            glance.append(f"<li>BCT 별로는 <b>{worst['bct'].upper()} {_pct(worst['accuracy'])}</b>가 가장 낮고 {best['bct'].upper()} {_pct(best['accuracy'])}가 가장 높다.</li>")
        if top_fp and top_fp["fp"]:
            glance.append(f"<li>오탐이 가장 많은 항목은 <b>{top_fp['name']}</b> — ❌ 판정 {_n(top_fp['pred_x'])}건 중 {_n(top_fp['fp'])}건이 실제로는 착용·체결 ({_pct(top_fp['fp_rate'])}).</li>")
    else:
        glance.append("<li>아직 채점된 표본이 없다.</li>")

    ps = s.get("pool_stats", {})
    fp_tiles = "".join(f"<div class='tile deny'><div class='k'>{c['name']} 오탐율</div><div class='v'>{_pct(c['fp_rate'])}</div>"
                       f"<div class='s'>❌ {_n(c['pred_x'])}건 중 {_n(c['fp'])}건 · 미탐율 {_pct(c['fn_rate'])}</div></div>" for c in pc)
    generated = datetime.now().strftime("%Y-%m-%d %H:%M")

    html = f"""<!doctype html><html lang="ko"><head><meta charset="utf-8"><title>{s['site_name']} 모델 성능 리포트 {s['month']}</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans+KR:wght@400;600;700&display=swap"><style>{CSS}
.good{{color:var(--allow);font-weight:600}} .bad{{color:var(--deny);font-weight:600}}</style></head><body><div class="page">
<div class="head"><span class="eyebrow">Paimedialab · 성능 검수 플랫폼</span><h1>{s['site_name']} 모델 성능 리포트</h1><span class="meta">{s['month']} · 모델 {model} · 생성 {generated}</span></div>
{partial}
<h2>요약</h2>
<div class="tiles">
  <div class="tile accent"><div class="k">출입 판정 정확도</div><div class="v">{_pct(o['accuracy'])}</div><div class="s">95% 신뢰구간 {_ci(o['ci'])}%</div></div>
  <div class="tile"><div class="k">검수 표본</div><div class="v">{_n(o['n'])}</div><div class="s">목표 {_n(prog['target'])} · 제외 {_n(prog['excluded'])}</div></div>
  <div class="tile deny"><div class="k">부당 거부</div><div class="v">{_n(o['wrong_deny'])}</div><div class="s">거부 판정의 {_pct(o['wrong_deny_rate'])}</div></div>
  <div class="tile deny"><div class="k">부당 승인</div><div class="v">{_n(o['wrong_allow'])}</div><div class="s">승인 판정의 {_pct(o['wrong_allow_rate'])}</div></div>
</div>
<div class="tiles">
  <div class="tile allow"><div class="k">전 항목 정답률</div><div class="v">{_pct(o['all_ok_rate'])}</div><div class="s">세 항목 모두 맞힘</div></div>
  {fp_tiles}
</div>
<div class="callout blue"><b>한눈에</b><ul>{''.join(glance)}</ul></div>

<h2>BCT 별</h2>
{img('bct', 'BCT 별 정확도')}
<table><thead><tr><th>BCT</th><th class=n>표본</th><th class=n>제외</th><th class=n>정확도</th><th class=n>95% CI</th><th class=n>부당 거부</th><th class=n>부당 승인</th><th class=n>전 항목</th><th class=n>안전고리 오탐율</th></tr></thead>
<tbody>{bct_rows()}</tbody></table>
<p class="small muted">정확도 85% 미만 행은 붉게 표시. BCT 당 표본이 100건이면 신뢰구간 폭이 넓으므로(±5%p 안팎) 작은 차이는 우연일 수 있다.</p>

<h2>클래스 별</h2>
<div style="max-width:640px">{img('class', '클래스 별 오탐율·미탐율')}</div>
<table><thead><tr><th>항목</th><th class=n>표본</th><th class=n>정확도</th><th class=n>미착용 판정</th><th class=n>오탐</th><th class=n>오탐율</th><th class=n>착용 판정</th><th class=n>미탐</th><th class=n>미탐율</th></tr></thead>
<tbody>{class_rows()}</tbody></table>
<h3>BCT × 항목 오류 건수</h3>
<table><thead><tr><th>BCT</th><th class=n>표본</th>{''.join(f"<th class=n>{CLASS_KO[k]} 오탐</th><th class=n>{CLASS_KO[k]} 미탐</th>" for k in CLASS_KEYS)}</tr></thead>
<tbody>{matrix_rows()}</tbody></table>

<h2>모델 개선 비교</h2>
{compare_section()}

<h2>오류 사례</h2>
<p class="small muted">한 항목이라도 틀린 표본 {_n(len(s['errors']))}건{' (앞 300건)' if len(s['errors']) > 300 else ''}. 출입 판정까지 틀린 행은 붉게 표시.</p>
<table><thead><tr><th>BCT</th><th>시각</th><th>시스템</th><th>실제</th><th>틀린 항목</th><th>이벤트 ID</th></tr></thead><tbody>{error_rows()}</tbody></table>

<h2>부록 · 표본과 정의</h2>
<dl class="defs">
<dt>표본</dt><dd>{s['month']} 이벤트 {_n(ps.get('events', 0))}건 중 판정 연동·점수·영상이 모두 있는 후보 {_n(ps.get('eligible', 0))}건에서 BCT 당 {s['per_bct_target']}건을 무작위로 뽑았다(시드 {s['seed']}, 후보 생성 {s.get('pool_created_at', '')[:16].replace('T', ' ')}). 후보가 부족한 BCT 는 전부. 판단이 불가능해 제외한 표본은 같은 BCT 의 다음 무작위 후보로 채웠다(제외 {_n(prog['excluded'])}건).</dd>
<dt>채점</dt><dd>검수자가 영상을 보고 모델 판정이 틀린 항목만 표시했다. 표시하지 않은 항목은 모델이 맞은 것으로 본다. 실제 출입 = 세 항목 모두 실제로 착용·체결이면 승인.</dd>
<dt>출입 판정 정확도</dt><dd>시스템의 승인/거부가 실제와 같은 비율. 괄호의 신뢰구간은 Wilson 95%.</dd>
<dt>부당 거부 · 부당 승인</dt><dd>부당 거부 = 시스템 거부인데 실제는 승인 대상(작업자 불편). 부당 승인 = 시스템 승인인데 실제는 거부 대상(안전 위험).</dd>
<dt>오탐율 · 미탐율 (항목별)</dt><dd>오탐율 = 모델이 ❌(미착용·미체결)로 본 것 중 실제로는 착용·체결인 비율. 미탐율 = 모델이 ⭕로 본 것 중 실제로는 미착용·미체결인 비율.</dd>
<dt>모델 판정 기준</dt><dd>점수 ≥ 현장 임계값이면 ⭕ — 안전모 {th.get('helmet_score')} · 하네스 {th.get('harness_score')} · 안전고리 {th.get('hook_score')} (엣지 판정 규칙과 동일).</dd>
<dt>이용량 가중 정확도</dt><dd>BCT 마다 같은 수를 뽑았으므로 위 정확도는 BCT 를 동일 가중한 값이다. BCT 별 실제 이벤트 수로 가중하면 <b>{_pct(o.get('weighted_accuracy'))}</b>.</dd>
<dt>검수자</dt><dd>{', '.join(s['reviewers']) or '–'}</dd>
</dl>
<p class="small muted">함께 저장된 파일: graded.csv(채점 원자료), per_bct.csv, per_class.csv, errors.csv, summary.json</p>
</div></body></html>"""
    p = out / "report.html"
    p.write_text(html, encoding="utf-8")
    return p


def write_outputs(s: dict, cmp: dict | None, graded: list[dict], out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)

    def wcsv(name, recs):
        if not recs:
            return
        with open(out / name, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=list(recs[0].keys()), extrasaction="ignore"); w.writeheader(); w.writerows(recs)

    flat = [{**r, "wrong": "|".join(r["wrong"])} for r in graded]
    wcsv("graded.csv", flat)
    wcsv("errors.csv", [r for r in flat if not r["all_ok"]])
    wcsv("per_bct.csv", [{k: (f"{v[0]}~{v[1]}" if k == "ci" and v else v) for k, v in b.items() if k != "classes"} for b in s["per_bct"]])
    wcsv("per_class.csv", s["per_class"])
    (out / "summary.json").write_text(json.dumps({"summary": s, "compare": cmp}, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def build_report(s: dict, cmp: dict | None, graded: list[dict], out: Path) -> tuple[Path, Path | None]:
    """CSV·차트·HTML·PDF 를 out 에 쓴다. (html, pdf 또는 None)"""
    write_outputs(s, cmp, graded, out)
    charts = make_charts(s, out)
    html = make_html(s, cmp, charts, out)
    pdf = out / "report.pdf"
    return html, (pdf if html_to_pdf(html, pdf) else None)
