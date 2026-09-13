"""기간 리포트 렌더링 — summary.json → 차트 PNG + report.html (+ Chrome 이 있으면 report.pdf)."""
from __future__ import annotations

import base64
import json
import shutil
import subprocess
from pathlib import Path

ALLOW, DENY, ACCENT, INK, MUTED, GRID = "#16A34A", "#DC2626", "#2563EB", "#111827", "#6B7280", "#E5E7EB"
CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]


# ══════════════════════════════════════════════════════════════════════════
# 차트
# ══════════════════════════════════════════════════════════════════════════
def _mpl():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib import font_manager
    for name in ("Malgun Gothic", "Apple SD Gothic Neo", "NanumGothic", "Noto Sans CJK KR"):
        if any(f.name == name for f in font_manager.fontManager.ttflist):
            plt.rcParams["font.family"] = name
            break
    plt.rcParams["axes.unicode_minus"] = False
    plt.rcParams.update({"axes.spines.top": False, "axes.spines.right": False, "axes.edgecolor": GRID,
                         "axes.labelcolor": INK, "xtick.color": MUTED, "ytick.color": MUTED, "axes.titlecolor": INK,
                         "axes.titlesize": 12, "axes.titleweight": "bold", "font.size": 10, "figure.dpi": 150})
    return plt


def _stacked_with_rate(plt, labels, allowed, denied, rates, title, path, xlabel="", rotate=0, width=0.7):
    fig, ax = plt.subplots(figsize=(9, 3.6))
    x = range(len(labels))
    ax.bar(x, allowed, width, color=ALLOW, label="승인", zorder=3)
    ax.bar(x, denied, width, bottom=allowed, color=DENY, label="거부", zorder=3)
    ax.set_xticks(list(x)); ax.set_xticklabels(labels, rotation=rotate, ha="right" if rotate else "center")
    ax.set_ylabel("시도 세션 수"); ax.set_title(title, loc="left"); ax.grid(axis="y", color=GRID, zorder=0)
    if xlabel: ax.set_xlabel(xlabel)
    ax2 = ax.twinx(); ax2.spines["right"].set_visible(True); ax2.spines["right"].set_color(GRID)
    ax2.plot(list(x), rates, color=ACCENT, marker="o", lw=1.6, ms=4, zorder=4, label="성공률")
    for xi, r in zip(x, rates):
        if r is not None: ax2.annotate(f"{r:.0f}%", (xi, r), textcoords="offset points", xytext=(0, 6), ha="center", fontsize=8, color=ACCENT)
    ax2.set_ylim(0, 105); ax2.set_ylabel("성공률 (%)", color=ACCENT); ax2.tick_params(axis="y", colors=ACCENT)
    h1, l1 = ax.get_legend_handles_labels(); h2, l2 = ax2.get_legend_handles_labels()
    ax.legend(h1 + h2, l1 + l2, loc="upper left", frameon=False, ncol=3, fontsize=9)
    fig.tight_layout(); fig.savefig(path); plt.close(fig)


def make_charts(summary: dict, gap_hist: dict | None, out: Path) -> dict[str, Path]:
    plt = _mpl()
    charts = {}
    pb = summary["per_bct"]
    p = out / "chart_bct.png"
    _stacked_with_rate(plt, [b["bct"].upper() for b in pb], [b["allowed"] for b in pb], [b["denied"] for b in pb],
                       [b["success_rate"] for b in pb], "BCT 별 시도 세션과 출입 성공률", p); charts["bct"] = p

    dl = summary["daily"]; p = out / "chart_daily.png"
    _stacked_with_rate(plt, [f"{d['date'][5:]} ({d['weekday']})" for d in dl], [d["allowed"] for d in dl], [d["denied"] for d in dl],
                       [d["success_rate"] for d in dl], "일별 시도 세션과 출입 성공률", p); charts["daily"] = p

    hr = summary["hourly"]; p = out / "chart_hourly.png"
    fig, ax = plt.subplots(figsize=(9, 3.2)); x = [h["hour"] for h in hr]
    ax.bar(x, [h["allowed"] for h in hr], 0.8, color=ALLOW, label="승인", zorder=3)
    ax.bar(x, [h["denied"] for h in hr], 0.8, bottom=[h["allowed"] for h in hr], color=DENY, label="거부", zorder=3)
    ax.set_xticks(x); ax.set_xticklabels([f"{h:02d}" for h in x]); ax.set_xlabel("시각 (시)"); ax.set_ylabel("시도 세션 수")
    ax.set_title("시간대별 시도 세션 (기간 합산)", loc="left"); ax.grid(axis="y", color=GRID, zorder=0); ax.legend(frameon=False, fontsize=9)
    fig.tight_layout(); fig.savefig(p); plt.close(fig); charts["hourly"] = p

    ad = summary["attempts_dist"]; p = out / "chart_attempts.png"
    fig, ax = plt.subplots(figsize=(5.2, 3.2)); x = range(len(ad))
    ax.bar(x, [a["allowed"] for a in ad], 0.7, color=ALLOW, label="결국 승인", zorder=3)
    ax.bar(x, [a["denied"] for a in ad], 0.7, bottom=[a["allowed"] for a in ad], color=DENY, label="끝내 거부", zorder=3)
    for xi, a in zip(x, ad):
        ax.annotate(str(a["allowed"] + a["denied"]), (xi, a["allowed"] + a["denied"]), textcoords="offset points", xytext=(0, 3), ha="center", fontsize=8, color=MUTED)
    ax.set_xticks(list(x)); ax.set_xticklabels([a["attempts"] + "회" for a in ad]); ax.set_ylabel("세션 수")
    ax.set_title("세션당 시도 횟수", loc="left"); ax.grid(axis="y", color=GRID, zorder=0); ax.legend(frameon=False, fontsize=9)
    fig.tight_layout(); fig.savefig(p); plt.close(fig); charts["attempts"] = p

    rs = summary["reason_single"]; p = out / "chart_reasons.png"
    fig, ax = plt.subplots(figsize=(5.2, 2.6)); labels = list(rs.keys())[::-1]; vals = [rs[k] for k in labels]
    ax.barh(labels, vals, color=DENY, zorder=3)
    for i, v in enumerate(vals): ax.annotate(str(v), (v, i), textcoords="offset points", xytext=(4, -3), fontsize=9, color=INK)
    ax.set_title("거부 세션의 최종 사유 (항목별, 중복 포함)", loc="left"); ax.grid(axis="x", color=GRID, zorder=0); ax.set_xlabel("세션 수")
    fig.tight_layout(); fig.savefig(p); plt.close(fig); charts["reasons"] = p

    if gap_hist:
        p = out / "chart_gap.png"
        edges = gap_hist["edges"]; labels = [f"{int(a)}~{int(b) if b else '∞'}" for a, b in edges]
        fig, ax = plt.subplots(figsize=(9, 3.0)); x = range(len(labels)); w = 0.4
        ax.bar([i - w / 2 for i in x], gap_hist["after_denied"], w, color=DENY, label="직전 이벤트가 거부", zorder=3)
        ax.bar([i + w / 2 for i in x], gap_hist["after_allowed"], w, color=ALLOW, label="직전 이벤트가 승인", zorder=3)
        gap = summary["overview"]["gap_sec"]
        cut = next((i for i, (a, b) in enumerate(edges) if b == gap), None)
        if cut is not None:
            ax.axvline(cut + 0.5, color=ACCENT, ls="--", lw=1.2)
            ax.annotate(f"{gap}초 기준", (cut + 0.5, ax.get_ylim()[1] * 0.92), color=ACCENT, fontsize=9, ha="right", xytext=(-4, 0), textcoords="offset points")
        ax.set_xticks(list(x)); ax.set_xticklabels(labels); ax.set_xlabel("같은 BCT 의 다음 이벤트까지 간격 (초)"); ax.set_ylabel("이벤트 수")
        ax.set_title("재시도 기준 검증: 직전 판정별 다음 이벤트까지의 간격", loc="left"); ax.grid(axis="y", color=GRID, zorder=0); ax.legend(frameon=False, fontsize=9)
        fig.tight_layout(); fig.savefig(p); plt.close(fig); charts["gap"] = p
    return charts


# ══════════════════════════════════════════════════════════════════════════
# HTML
# ══════════════════════════════════════════════════════════════════════════
def _img(p: Path) -> str:
    return "data:image/png;base64," + base64.b64encode(p.read_bytes()).decode()


def _pct(v) -> str:
    return "–" if v is None else f"{v:.1f}%"


def _n(v) -> str:
    return f"{v:,}"


CSS = """
:root{--ink:#111827;--ink2:#4B5563;--muted:#6B7280;--line:#E5E7EB;--soft:#F5F7FA;--accent:#2563EB;--allow:#16A34A;--deny:#DC2626;--warn:#B45309;--warnbg:#FFFBEB}
*{box-sizing:border-box} body{margin:0;background:#fff;color:var(--ink);font-family:"IBM Plex Sans KR","Malgun Gothic","Apple SD Gothic Neo",sans-serif;font-size:13.5px;line-height:1.65}
.page{max-width:960px;margin:0 auto;padding:36px 40px 60px}
.head{border-bottom:1px solid var(--line);padding-bottom:14px;margin-bottom:22px;display:flex;align-items:baseline;gap:14px;flex-wrap:wrap}
.head .eyebrow{font-size:11px;letter-spacing:.14em;text-transform:uppercase;color:var(--accent);font-weight:700}
.head h1{font-size:22px;margin:0;font-weight:700} .head .meta{margin-left:auto;color:var(--muted);font-size:12.5px}
h2{font-size:16px;margin:30px 0 10px;padding-left:10px;border-left:3px solid var(--accent)} h3{font-size:13.5px;margin:18px 0 6px;color:var(--ink2)}
p{margin:0 0 10px;max-width:78ch} .muted{color:var(--muted)} .small{font-size:12px}
.tiles{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin:14px 0 6px}
.tile{border:1px solid var(--line);border-radius:8px;padding:12px 14px;background:#fff}
.tile .k{font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:var(--muted);font-weight:600}
.tile .v{font-size:24px;font-weight:700;line-height:1.2;margin-top:4px;font-variant-numeric:tabular-nums} .tile .s{font-size:12px;color:var(--muted);margin-top:2px}
.tile.allow .v{color:var(--allow)} .tile.deny .v{color:var(--deny)} .tile.accent .v{color:var(--accent)}
table{border-collapse:collapse;width:100%;font-size:12.5px;margin:8px 0 14px} th,td{padding:6px 9px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
th{background:var(--soft);font-size:11px;letter-spacing:.05em;text-transform:uppercase;color:var(--muted);font-weight:600;white-space:nowrap}
td.n{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap} th.n{text-align:right}
tr.hi td{background:#FEF2F2} td.good{color:var(--allow);font-weight:600} td.bad{color:var(--deny);font-weight:600}
.callout{border-left:3px solid var(--warn);background:var(--warnbg);padding:10px 14px;margin:12px 0;border-radius:0 6px 6px 0}
.callout.blue{border-left-color:var(--accent);background:#EFF6FF}
.chart{margin:8px 0 16px} .chart img{width:100%;height:auto;border:1px solid var(--line);border-radius:6px}
.two{display:grid;grid-template-columns:1fr 1fr;gap:16px;align-items:start}
ul{margin:0 0 10px;padding-left:20px} li{margin:3px 0}
.defs dt{font-weight:600;margin-top:8px} .defs dd{margin:0 0 4px 0;color:var(--ink2)}
@page{size:A4;margin:14mm 12mm} @media print{.page{padding:0;max-width:none} h2{break-after:avoid} .chart,.tile,table,.callout{break-inside:avoid}}
"""


def make_html(site_name: str, day_from: str, day_to: str, summary: dict, quality: dict, charts: dict[str, Path], out: Path) -> Path:
    o = summary["overview"]; pb = summary["per_bct"]; dl = summary["daily"]; an = summary["anomalies"]
    worst = min(pb, key=lambda b: b["success_rate"] if b["success_rate"] is not None else 999)
    best = max(pb, key=lambda b: b["success_rate"] if b["success_rate"] is not None else -1)
    top_reason = next(iter(summary["reason_single"]), "")
    top_reason_n = summary["reason_single"].get(top_reason, 0)
    long6 = sum(a["allowed"] + a["denied"] for a in summary["attempts_dist"] if a["attempts"] == "6+")
    long6_deny = sum(a["denied"] for a in summary["attempts_dist"] if a["attempts"] == "6+")
    weekday = [d for d in dl if d["weekday"] not in "토일"]; weekend = [d for d in dl if d["weekday"] in "토일"]
    wk_avg = round(sum(d["sessions"] for d in weekday) / len(weekday)) if weekday else 0
    we_avg = round(sum(d["sessions"] for d in weekend) / len(weekend)) if weekend else 0
    q_days = quality.get("days", {})
    join_pct = _pct(100.0 * quality["join_total"] / quality["events_total"]) if quality.get("events_total") else "–"
    extra_influx = (quality.get("influx_rows_total", 0) - quality.get("events_total", 0)) if quality else 0

    def bct_rows():
        rows = []
        for b in pb:
            hi = ' class="hi"' if b["success_rate"] is not None and b["success_rate"] < 70 else ""
            sr_cls = "bad" if (b["success_rate"] or 0) < 70 else ("good" if (b["success_rate"] or 0) >= 85 else "")
            rows.append(f"<tr{hi}><td><b>{b['bct'].upper()}</b></td><td class=n>{_n(b['events'])}</td><td class=n>{_n(b['sessions'])}</td>"
                        f"<td class=n>{_n(b['allowed'])}</td><td class=n>{_n(b['denied'])}</td><td class='n {sr_cls}'>{_pct(b['success_rate'])}</td>"
                        f"<td class=n>{_pct(b['first_try_rate'])}</td><td class=n>{_n(b['retried'])}</td><td class=n>{_pct(b['recovery_rate'])}</td>"
                        f"<td class=n>{b['avg_attempts']}</td><td class=n>{b['max_attempts']}</td><td>{b['top_reason'] or '–'}</td></tr>")
        return "".join(rows)

    def daily_rows():
        return "".join(f"<tr><td>{d['date']} ({d['weekday']})</td><td class=n>{_n(d['events'])}</td><td class=n>{_n(d['sessions'])}</td>"
                       f"<td class=n>{_n(d['allowed'])}</td><td class=n>{_n(d['denied'])}</td><td class=n>{_pct(d['success_rate'])}</td></tr>" for d in dl)

    def reason_rows():
        tot = o["denied"] or 1
        return "".join(f"<tr><td>{(k or '(사유 없음)').replace('|', ' + ')}</td><td class=n>{_n(v)}</td><td class=n>{_pct(100.0 * v / tot)}</td></tr>"
                       for k, v in summary["reason_sessions"].items())

    def long_rows():
        return "".join(f"<tr><td>{x['bct'].upper()}</td><td>{x['start']}</td><td class=n>{x['attempts']}</td><td class=n>{x['duration_min']}분</td>"
                       f"<td class='{'good' if x['outcome'] == 'allowed' else 'bad'}'>{'승인' if x['outcome'] == 'allowed' else '거부'}</td><td>{x['final_reasons'].replace('|', ' + ')}</td></tr>"
                       for x in an["long_sessions"])

    def sens_rows():
        return "".join(f"<tr{' class=hi' if s['gap_sec'] == o['gap_sec'] else ''}><td class=n>{s['gap_sec']}초</td><td class=n>{_n(s['sessions'])}</td>"
                       f"<td class=n>{_n(s['allowed'])}</td><td class=n>{_n(s['denied'])}</td><td class=n>{_pct(s['success_rate'])}</td></tr>" for s in summary["sensitivity"])

    zero = ", ".join(f"{z['bct'].upper()} {z['date'][5:]}" for z in an["zero_days"]) or "없음"
    def _delta(v):
        return "" if v.get("delta_med") is None else f"{v['delta_med']:.1f}s"
    quality_rows = "".join(f"<tr><td>{d}</td><td class=n>{_n(v['events'])}</td><td class=n>{_n(v['influx_rows'])}</td>"
                           f"<td class=n>{_n(v['joined'])}</td><td class=n>{_delta(v)}</td></tr>" for d, v in q_days.items())

    html = f"""<!doctype html><html lang="ko"><head><meta charset="utf-8"><title>{site_name} 출입 통계 {day_from} ~ {day_to}</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans+KR:wght@400;600;700&display=swap"><style>{CSS}</style></head><body><div class="page">
<div class="head"><span class="eyebrow">Paimedialab · 오탐 검수 플랫폼</span><h1>{site_name} 출입 통계 리포트</h1><span class="meta">{day_from} ~ {day_to} · 생성 {__import__('datetime').datetime.now():%Y-%m-%d %H:%M}</span></div>

<h2>요약</h2>
<div class="tiles">
  <div class="tile"><div class="k">이벤트 (원자료)</div><div class="v">{_n(o['events'])}</div><div class="s">카메라 트리거 건수</div></div>
  <div class="tile accent"><div class="k">시도 세션</div><div class="v">{_n(o['sessions'])}</div><div class="s">재시도를 1회로 묶음</div></div>
  <div class="tile allow"><div class="k">출입 승인</div><div class="v">{_n(o['allowed'])}</div><div class="s">성공률 {_pct(o['success_rate'])}</div></div>
  <div class="tile deny"><div class="k">출입 거부</div><div class="v">{_n(o['denied'])}</div><div class="s">끝내 통과 못 한 세션</div></div>
</div>
<div class="tiles">
  <div class="tile"><div class="k">첫 시도 성공률</div><div class="v">{_pct(o['first_try_rate'])}</div><div class="s">{_n(o['first_try_ok'])} / {_n(o['sessions'])} 세션</div></div>
  <div class="tile"><div class="k">재시도 후 회복률</div><div class="v">{_pct(o['recovery_rate'])}</div><div class="s">재시도 {_n(o['retried_sessions'])} 중 {_n(o['recovered'])} 승인</div></div>
  <div class="tile"><div class="k">평일 / 주말 하루 평균</div><div class="v">{wk_avg} / {we_avg}</div><div class="s">시도 세션 수</div></div>
  <div class="tile"><div class="k">6회 이상 반복 세션</div><div class="v">{_n(long6)}</div><div class="s">그중 끝내 거부 {_n(long6_deny)}</div></div>
</div>

<div class="callout"><b>한눈에</b>
<ul>
<li>기간 중 <b>{_n(o['sessions'])}번 출입을 시도</b>해 <b>{_pct(o['success_rate'])}</b> 가 통과했다. 첫 시도에 통과한 비율은 {_pct(o['first_try_rate'])} 이고, 한 번 거부된 뒤 다시 시도한 세션의 {_pct(o['recovery_rate'])} 는 결국 통과했다.</li>
<li>거부 세션의 최종 사유는 <b>{top_reason} {_n(top_reason_n)}건 ({_pct(100.0 * top_reason_n / (o['denied'] or 1))})</b> 로 압도적이다. 하네스·안전모는 소수.</li>
<li>BCT 별 편차가 크다. <b>{worst['bct'].upper()} 성공률 {_pct(worst['success_rate'])}</b> (첫 시도 {_pct(worst['first_try_rate'])}, 평균 {worst['avg_attempts']}회 시도) 로 가장 낮고, {best['bct'].upper()} 는 {_pct(best['success_rate'])}. {worst['bct'].upper()} 는 전체 거부 세션의 {_pct(100.0 * worst['denied'] / (o['denied'] or 1))} 를 차지한다.</li>
<li>6회 이상 반복한 세션이 {_n(long6)}건 있다. 사람이 계속 시도하는데 계속 거부된 경우라 <b>실제 미체결이거나 모델이 체결을 못 잡는 오탐</b> 둘 중 하나다. 검수 플랫폼에서 이 세션들부터 보는 것이 효율적이다.</li>
</ul></div>

<h2>BCT 별</h2>
<div class="chart"><img src="{_img(charts['bct'])}" alt="BCT 별 시도 세션과 성공률"></div>
<table><thead><tr><th>BCT</th><th class=n>이벤트</th><th class=n>시도</th><th class=n>승인</th><th class=n>거부</th><th class=n>성공률</th><th class=n>첫시도</th><th class=n>재시도</th><th class=n>회복률</th><th class=n>평균시도</th><th class=n>최대</th><th>주 거부 사유</th></tr></thead>
<tbody>{bct_rows()}</tbody></table>
<p class="small muted">성공률 70% 미만 행은 붉게 표시. 회복률 = 재시도한 세션 중 결국 승인된 비율. 최대 = 한 세션에서 가장 많이 시도한 횟수.</p>

<h2>일별 · 시간대별</h2>
<div class="chart"><img src="{_img(charts['daily'])}" alt="일별"></div>
<table><thead><tr><th>날짜</th><th class=n>이벤트</th><th class=n>시도</th><th class=n>승인</th><th class=n>거부</th><th class=n>성공률</th></tr></thead><tbody>{daily_rows()}</tbody></table>
<div class="chart"><img src="{_img(charts['hourly'])}" alt="시간대별"></div>

<h2>재시도와 거부 사유</h2>
<div class="two">
  <div><div class="chart"><img src="{_img(charts['attempts'])}" alt="시도 횟수 분포"></div></div>
  <div><div class="chart"><img src="{_img(charts['reasons'])}" alt="거부 사유"></div></div>
</div>
<h3>거부 세션의 최종 사유 (조합)</h3>
<table><thead><tr><th>사유</th><th class=n>세션</th><th class=n>비율</th></tr></thead><tbody>{reason_rows()}</tbody></table>
<h3>가장 오래 반복한 세션 상위 10</h3>
<table><thead><tr><th>BCT</th><th>시작</th><th class=n>시도</th><th class=n>지속</th><th>결과</th><th>최종 사유</th></tr></thead><tbody>{long_rows()}</tbody></table>
<div class="callout blue"><b>검수 우선순위 제안</b> — 반복 횟수가 많고 끝내 거부된 세션은 (1) 작업자가 실제로 체결하지 않았거나 (2) 체결했는데 모델이 못 잡는 경우다. 어느 쪽이든 조치 대상(교육 또는 재학습)이므로, 검수 플랫폼에서 <b>{worst['bct'].upper()} 의 거부 세션</b>과 <b>6회 이상 반복 세션</b>을 먼저 보면 오탐을 가장 빨리 찾는다.</div>

<h2>이상 징후 · 데이터 품질</h2>
<ul>
<li><b>이벤트 0건인 BCT-일:</b> {zero}. 주말(토·일)은 작업이 적어 정상일 수 있으나, 평일 0건은 카메라·트리거 점검 대상.</li>
<li><b>판정 연동:</b> MinIO 이벤트 {_n(quality.get('events_total', 0))}건 중 {_n(quality.get('join_total', 0))}건 Influx 조인 ({join_pct}). 클립 없이 Influx 에만 있는 행 {extra_influx}건 (업로드 누락 추정, 통계에서 제외).</li>
<li><b>클립 결손:</b> 판정 표시 영상 없음 {an['no_tg']}건 · 학습용 영상 없음 {an['no_train']}건 · 판정 없음 {an['no_verdict']}건.</li>
</ul>
<table><thead><tr><th>날짜</th><th class=n>MinIO 이벤트</th><th class=n>Influx 행</th><th class=n>조인</th><th class=n>시각차 중앙값</th></tr></thead><tbody>{quality_rows}</tbody></table>

<h2>부록 · 정의와 검증</h2>
<dl class="defs">
<dt>이벤트</dt><dd>카메라 트리거 1회. 엣지가 15초 분석 후 승인/거부를 내고 클립을 저장한다.</dd>
<dt>시도 세션</dt><dd>같은 BCT 에서 <b>직전 이벤트가 거부</b>이고 그로부터 <b>{o['gap_sec']}초 안</b>에 다음 이벤트가 오면 같은 세션(재시도)으로 묶는다. 승인이 나오면 세션은 거기서 끝난다. 세션 결과는 승인이 한 번이라도 있으면 승인, 없으면 거부 (거부는 몇 번을 반복해도 1회).</dd>
<dt>거부 사유</dt><dd>Influx 에는 사유 필드가 없어 점수와 현장 임계값(안전모 0.45 · 하네스 0.50 · 안전고리 0.50)으로 엣지 판정 규칙과 동일하게 유도했다. 거부 세션의 사유는 마지막 이벤트 기준.</dd>
</dl>
<h3>재시도 간격 {o['gap_sec']}초 ({o['gap_sec'] // 60}분) 기준의 근거</h3>
<div class="chart"><img src="{_img(charts['gap'])}" alt="간격 분포"></div>
<p class="small">직전이 승인이면 다음 이벤트는 대부분 10분 넘게 비어 있으므로(다른 사람) 이 분포를 기준선으로 삼았다. 직전이 거부인 경우 다음 이벤트의 초당 밀도는 기준선 대비 120초까지 20배 이상, 120~300초는 1.4~6.7배로 여전히 재시도가 우세하고, 300초부터는 기준선과 구분되지 않는다. 그래서 {o['gap_sec']}초를 기준으로 했다. 기준을 바꿔도 승인 수는 그대로이고 거부 세션 수만 달라진다:</p>
<table style="max-width:520px"><thead><tr><th class=n>기준</th><th class=n>세션</th><th class=n>승인</th><th class=n>거부</th><th class=n>성공률</th></tr></thead><tbody>{sens_rows()}</tbody></table>
<p class="small muted">함께 저장된 파일: events_raw.csv(이벤트 원자료), sessions.csv(세션 목록, 이벤트 ID 포함), per_bct.csv, daily.csv, hourly.csv, summary.json</p>
</div></body></html>"""
    p = out / "report.html"
    p.write_text(html, encoding="utf-8")
    return p


def html_to_pdf(html_path: Path, pdf_path: Path) -> bool:
    exe = next((c for c in CHROME_CANDIDATES if Path(c).exists()), None) or shutil.which("chrome") or shutil.which("msedge")
    if not exe:
        return False
    try:
        subprocess.run([exe, "--headless=new", "--disable-gpu", "--no-pdf-header-footer", "--virtual-time-budget=8000",
                        f"--print-to-pdf={pdf_path}", html_path.resolve().as_uri()], capture_output=True, timeout=120)
    except Exception:
        return False
    return pdf_path.exists() and pdf_path.stat().st_size > 0
