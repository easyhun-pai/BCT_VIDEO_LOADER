"""BCT 월간 성능 검수 — 검수자 PC 로컬 웹 (Streamlit, :8502).

실행:  scripts\\perf_app.ps1   (또는  python -m streamlit run review/perf_app.py --server.port 8502)
흐름:  현장 선택 → 월 선택(없으면 BCT 당 N건 표본 만들기) → 2×2 채점(틀린 항목·제외) → 리포트(웹 집계 · PDF)
저장:  {저장루트}/_perf/{site}/{yyyy-mm}/pool.json · review.json · report/   (규칙은 review/perf.py 머리말)
"""
from __future__ import annotations

import random
import sys
from datetime import date as _date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import streamlit as st  # noqa: E402

from review import webui  # noqa: E402
from review.catalog import Event  # noqa: E402
from review.perf import (CLASS_KEYS, CLASS_KO, DEFAULT_PER_BCT, PerfStore, build_pool, collect_candidates,  # noqa: E402
                         compare, graded_rows, list_months, summarize)
from review.webui import VERDICT_KO, access, cached_days, class_marks, header, out_root, pills, settings  # noqa: E402

APP_NAME = "성능 검수 플랫폼"
webui.setup(APP_NAME, "📊")

GRID_N = 4
KEYMAP = {"p": "저장", "P": "저장", "z": "되돌리기", "Z": "되돌리기"}
MARKS = list(CLASS_KEYS) + ["excluded"]
MARK_KO = {**CLASS_KO, "excluded": "제외"}
STATE_KEYS = ("site_code", "month", "store", "pf_idx", "pf_idx_init")


def _pct(v) -> str:
    return "–" if v is None else f"{v:.1f}%"


# ══════════════════════════════════════════════════════════════════════════
# 현장 선택
# ══════════════════════════════════════════════════════════════════════════
def page_sites():
    st_ = settings()
    header("현장 선택")
    root, src = out_root()
    if src != "NAS":
        c1, c2 = st.columns([5, 1])
        with c1:
            st.warning("NAS에 연결되지 않아 검수 결과를 이 PC에 저장합니다.")
        with c2:
            if st.button("NAS 다시 연결", width="stretch"):
                st_.force_nas_retry(); st.rerun()
    cols = st.columns(3)
    for i, site in enumerate(st_.sites.values()):
        with cols[i % 3]:
            with st.container(border=True):
                st.markdown(f"### {site.name}")
                st.caption(f"카메라 {len(site.cameras)}대 · BCT {len(site.bcts)}개")
                if not site.minio_access:
                    st.warning("접속 정보가 설정되지 않은 현장입니다.")
                if st.button("이 현장 열기", key=f"site_{site.code}", type="primary", disabled=not site.minio_access, width="stretch"):
                    st.session_state.site_code = site.code
                    st.session_state.pop("month", None)
                    st.rerun()


# ══════════════════════════════════════════════════════════════════════════
# 월 선택 · 표본 만들기
# ══════════════════════════════════════════════════════════════════════════
def _prev_month(today: _date) -> str:
    return f"{today.year - 1}-12" if today.month == 1 else f"{today.year}-{today.month - 1:02d}"


def month_status(root: Path, code: str, month: str) -> dict:
    s = PerfStore.read_status(root, code, month)
    if s is None:
        return {"label": "⚪ 표본 없음", "cls": "", "exists": False, "s": {}}
    if not s.get("done"):
        return {"label": "⚪ 미검수", "cls": "", "exists": True, "s": s}
    if s.get("pending"):
        return {"label": "🟡 진행 중", "cls": "warn", "exists": True, "s": s}
    return {"label": "✅ 완료", "cls": "ok", "exists": True, "s": s}


def page_months(site):
    header(f"{site.name} · 월 선택")
    if st.button("← 현장 선택"):
        st.session_state.pop("site_code", None); st.rerun()
    with st.spinner("월 목록을 불러오는 중…"):
        try:
            days = cached_days(site.code)
        except Exception as e:
            st.error("현장에 연결하지 못했습니다. 네트워크 연결을 확인해 주세요.")
            with st.expander("자세히"):
                st.code(str(e))
            return
    root, _ = out_root()
    ev_by_month: dict[str, int] = {}
    for d, per in days.items():
        ev_by_month[d[:7]] = ev_by_month.get(d[:7], 0) + sum(per.values())
    months = sorted(set(ev_by_month) | set(list_months(root, site.code)), reverse=True)
    if not months:
        st.warning("이벤트가 없습니다."); return
    default = _prev_month(_date.today())
    default = default if default in months else months[0]

    pick_key, tbl_key = f"month_pick_{site.code}", f"months_table_{site.code}"
    webui.table_pick(months, pick_key, tbl_key, default, clamp=lambda m: m if m in months else default)
    stat = {m: month_status(root, site.code, m) for m in months}

    c1, c2, c3 = st.columns([2, 1, 1])
    with c1:
        pick = st.selectbox("월", months, key=pick_key)
    ms = stat[pick]
    with c2:
        st.metric("해당 월 이벤트", f"{ev_by_month.get(pick, 0):,}")
        if ms["exists"]:
            s = ms["s"]
            st.markdown(f'<span class="pm-pill {ms["cls"]}">{ms["label"]} <b>{s.get("done", 0)}/{s.get("done", 0) + s.get("pending", 0)}</b></span>'
                        + (f' <span style="color:#6B7280;font-size:13px">정확도 {_pct(s.get("accuracy"))}</span>' if s.get("done") else ""),
                        unsafe_allow_html=True)
        else:
            st.markdown(f'<span class="pm-pill">{ms["label"]}</span>', unsafe_allow_html=True)
    with c3:
        st.write(""); st.write("")
        if ms["exists"]:
            if st.button("이 달 열기 →", type="primary", width="stretch"):
                open_month(site, pick)
    if not ms["exists"]:
        with st.container(border=True):
            st.markdown(f"**{pick} 표본 만들기**")
            a, b, c = st.columns([1, 2, 1])
            with a:
                n = st.number_input("BCT 당 표본", min_value=10, max_value=1000, value=DEFAULT_PER_BCT, step=10)
            with b:
                model = st.text_input("모델 버전", placeholder="예: hook-v3 (2026-09 재학습)")
            with c:
                st.write(""); st.write("")
                make = st.button("표본 만들기", type="primary", width="stretch", disabled=not ev_by_month.get(pick))
            if make:
                make_sample(site, pick, int(n), model.strip())

    st.subheader("월별")
    table = []
    for m in months:
        s = stat[m]["s"]
        table.append({"월": m, "상태": stat[m]["label"],
                      "채점": f"{s.get('done', 0)}/{s.get('done', 0) + s.get('pending', 0)}" if stat[m]["exists"] else "",
                      "제외": str(s.get("excluded", "")) if stat[m]["exists"] else "",
                      "정확도": _pct(s.get("accuracy")) if s.get("done") else "",
                      "모델": s.get("model", ""), "이벤트": ev_by_month.get(m, 0)})
    st.dataframe(table, width="stretch", hide_index=True, key=tbl_key, on_select="rerun", selection_mode=["single-row", "single-cell"])
    webui.inject_table_dblclick("이 달 열기")


def make_sample(site, month: str, per_bct: int, model: str):
    root, _ = out_root()
    st_ = settings()
    bar = st.progress(0.0, text="이벤트를 모으는 중…")
    try:
        site.require_secrets()
        acc = access(site)
        rows, stats = collect_candidates(site, acc, month, st_.timezone,
                                         progress=lambda i, n, d, k: bar.progress(i / n, text=f"{d} · 이벤트 {k}건"))
    except BaseException as e:                        # require_secrets 는 SystemExit
        bar.empty()
        st.error("이벤트를 모으지 못했습니다. 네트워크 연결을 확인해 주세요.")
        with st.expander("자세히"):
            st.code(f"{type(e).__name__}: {e}")
        return
    if not rows:
        bar.empty(); st.warning("판정 정보가 있는 이벤트가 없어 표본을 만들 수 없습니다."); return
    pool = build_pool(site, month, rows, stats, per_bct, seed=random.SystemRandom().randrange(1, 2**31), by=st.session_state.user["id"])
    try:
        PerfStore.create(root, pool, model)
    except OSError as e:
        bar.empty()
        st.error("표본을 저장하지 못했습니다. 저장 위치(NAS) 연결을 확인해 주세요.")
        with st.expander("자세히"):
            st.code(str(e))
        return
    bar.progress(1.0, text="완료")
    open_month(site, month)


def open_month(site, month: str):
    st.session_state.month = month
    for k in ("store", "pf_idx", "pf_idx_init"):
        st.session_state.pop(k, None)
    st.rerun()


# ══════════════════════════════════════════════════════════════════════════
# 검수 · 리포트
# ══════════════════════════════════════════════════════════════════════════
def event_of(store: PerfStore, eid: str) -> Event:
    r = store.rows[eid]
    site, bct, ts = eid.split("-")[0], r["bct"], r["ts"]
    tg_prefix = store.pool.get("tg_prefix", "_tg/")
    return Event(site=site, bct=bct, ts=ts,
                 train={role: f"{bct}/{ts}/{role}.mp4" for role in r.get("train", [])},
                 tg={role: f"{tg_prefix}{bct}/{ts}/{role}.mp4" for role in r.get("tg", [])})


def page_month(site, month: str):
    root, src = out_root()
    if not PerfStore.exists(root, site.code, month):
        st.session_state.pop("month", None); st.rerun()
    if st.session_state.get("store") is None or st.session_state.get("store_key") != (site.code, month):
        with st.spinner("표본을 여는 중…"):
            store = PerfStore.open(root, site.code, month)
        u = st.session_state.user
        store.note_reviewer(u["id"], u.get("ip", ""))
        st.session_state.store, st.session_state.store_key = store, (site.code, month)
    store: PerfStore = st.session_state.store

    with st.sidebar:
        st.markdown(f"**{site.name}** · {month}")
        if st.button("← 월 선택", width="stretch"):
            for k in ("month", "store", "store_key", "pf_idx", "pf_idx_init"):
                st.session_state.pop(k, None)
            st.rerun()
        if st.button("🔄 새로고침", width="stretch"):
            store.refresh(); st.rerun()
        view = st.radio("화면", ["검수", "리포트"], horizontal=True, key="pf_view")
        st.divider()
        model = st.text_input("모델 버전", value=store.data.get("model", ""), key=f"pf_model_{month}")
        if model.strip() != store.data.get("model", ""):
            store.set_model(model.strip())
        if view == "검수":
            rate = st.select_slider("재생 속도", options=[1.0, 1.5, 2.0, 3.0], value=st.session_state.get("rate", 2.0),
                                    format_func=lambda x: f"{x:g}x", key="rate")
            st.divider()
            st.markdown("**필터**")
            bcts = sorted(store.pool["bcts"], key=lambda b: int(b[3:]))
            f_bct = st.multiselect("BCT", bcts)
            f_state = st.radio("상태", ["전체", "미검수", "검수됨", "틀림만", "제외만"], horizontal=True)

    prog = store.progress()["total"]
    header(f"{site.name} · {month}")
    pills([("표본", f"{prog['target']:,}", ""), ("채점", f"{prog['done']:,}/{prog['done'] + prog['pending']:,}", ""),
           ("유효", f"{prog['valid']:,}", "ok"), ("제외", f"{prog['excluded']:,}", "fp" if prog["excluded"] else ""),
           ("모델", store.data.get("model") or "미기재", "" if store.data.get("model") else "warn"),
           ("저장", "NAS" if src == "NAS" else "이 PC", "" if src == "NAS" else "warn")])

    if view == "리포트":
        page_report(site, store, root)
        return

    ids = []
    for eid in store.queue:
        r, j = store.rows[eid], store.judgment(eid)
        if f_bct and r["bct"] not in f_bct:
            continue
        if f_state == "미검수" and j:
            continue
        if f_state == "검수됨" and not j:
            continue
        if f_state == "틀림만" and not (j and j.get("wrong")):
            continue
        if f_state == "제외만" and not (j and j.get("excluded")):
            continue
        ids.append(eid)
    if not ids:
        st.info("조건에 맞는 이벤트가 없습니다."); return

    if st.session_state.get("pf_idx_init") != (site.code, month):
        st.session_state.pf_idx_init = (site.code, month)
        st.session_state.pf_idx = store.first_pending_index(ids)
    idx = max(0, min(st.session_state.get("pf_idx", 0), len(ids) - 1))
    review_grid(site, store, ids, idx, f_state, rate)


def _tile_key(i: int, judged: bool, marks: list[str], denied: bool) -> str:
    kind = "ex" if "excluded" in marks else ("fp" if marks else "tp")
    if judged:
        return f"tile_done_{kind}_{i}"
    if kind != "tp":
        return f"tile_{kind}_{i}"
    return f"tile_deny_{i}" if denied else f"tile_{i}"


def _saved_marks(j: dict | None) -> list[str]:
    if not j:
        return []
    return ["excluded"] if j.get("excluded") else [k for k in CLASS_KEYS if k in (j.get("wrong") or [])]


def review_grid(site, store: PerfStore, ids: list[str], start: int, f_state: str, rate: float):
    batch = ids[start:start + GRID_N]
    n_pages = (len(ids) + GRID_N - 1) // GRID_N
    nav = st.columns([1, 6, 1])
    with nav[0]:
        if st.button("◀ 이전 4개", disabled=start == 0, width="stretch"):
            st.session_state.pf_idx = max(0, start - GRID_N); st.rerun()
    with nav[1]:
        st.markdown(f"<div style='text-align:center;color:#6B7280;padding-top:6px'>"
                    f"{start + 1}–{start + len(batch)} / {len(ids)} &nbsp;·&nbsp; 묶음 {start // GRID_N + 1} / {n_pages}"
                    f" &nbsp;·&nbsp; <b>P</b> 저장 · <b>Z</b> 되돌리기</div>", unsafe_allow_html=True)
    with nav[2]:
        if st.button("다음 4개 ▶", disabled=start + GRID_N >= len(ids), width="stretch"):
            st.session_state.pf_idx = min(start + GRID_N, len(ids) - 1); st.rerun()

    grid = [st.columns(2), st.columns(2)]
    cur: dict[str, list[str]] = {}
    for i, eid in enumerate(batch):
        r, j = store.rows[eid], store.judgment(eid)
        ev = event_of(store, eid)
        key = f"pf_marks_{eid}"
        if key not in st.session_state:
            st.session_state[key] = _saved_marks(j)
        marks = list(st.session_state[key] or [])
        with grid[i // 2][i % 2]:
            with st.container(key=_tile_key(i, bool(j), marks, r["verdict"] == "denied")):
                if not j:
                    state_txt, vcls = "미검수", "muted"
                elif j.get("excluded"):
                    state_txt, vcls = "제외", "muted"
                elif j.get("wrong"):
                    state_txt, vcls = "틀림 · " + " · ".join(CLASS_KO[k] for k in j["wrong"]), "v-fp"
                else:
                    state_txt, vcls = "맞음", "v-tp"
                marks_txt = class_marks(r, store.thresholds())
                st.markdown(
                    f'<div class="pm-tile-head"><span class="num">{i + 1}</span><code>{eid}</code><span class="{vcls}">{state_txt}</span></div>'
                    f'<div class="pm-tile-sub">{r["ts"][4:6]}-{r["ts"][6:8]} {ev.time_str} · <b>{r["bct"].upper()}</b> · '
                    f'<b>{VERDICT_KO.get(r["verdict"], "")}</b>' + (f" · {marks_txt}" if marks_txt else "") + "</div>",
                    unsafe_allow_html=True)
                vc = st.columns(max(1, len(site.cameras)))
                for c, role in zip(vc, site.cameras):
                    with c:
                        webui.video(site, ev, role, label_role=False)
                cur[eid] = st.pills("틀린 항목", MARKS, selection_mode="multi", format_func=lambda m: MARK_KO[m],
                                    key=key, label_visibility="collapsed") or []
    for i in range(len(batch), GRID_N):
        with grid[i // 2][i % 2]:
            st.empty()

    b = st.columns([1.3, 1, 4])
    with b[0]:
        if st.button("저장 · P", key="btn_grid_tp", width="stretch"):
            u = st.session_state.user
            store.commit({eid: {"wrong": [m for m in cur.get(eid, []) if m != "excluded"], "excluded": "excluded" in cur.get(eid, [])}
                          for eid in batch}, u["id"], u.get("ip", ""))
            for eid in batch:
                st.session_state.pop(f"pf_marks_{eid}", None)
            if f_state != "미검수":                      # 미검수 필터면 저장한 묶음이 목록에서 빠지므로 제자리가 곧 다음 묶음
                st.session_state.pf_idx = min(start + GRID_N, max(0, len(ids) - 1))
            st.rerun()
    with b[1]:
        if st.button("되돌리기 Z", key="btn_undo", width="stretch", disabled=not store.data["history"]):
            undone = store.undo()
            for eid in undone:
                st.session_state.pop(f"pf_marks_{eid}", None)
            if undone and undone[0] in ids:
                st.session_state.pf_idx = ids.index(undone[0])
            st.rerun()
    webui.inject_helpers(KEYMAP, rate)


def page_report(site, store: PerfStore, root: Path):
    from review.perf_render import build_report

    s = summarize(store)
    o, prog = s["overview"], s["progress"]
    if not s["complete"]:
        st.warning(f"검수가 끝나지 않아 중간 집계입니다. 대기 {prog['pending']:,}건.")
    webui.kpis([
        ("출입 판정 정확도", _pct(o["accuracy"]), f"95% 신뢰구간 {o['ci'][0]:.1f}~{o['ci'][1]:.1f}%" if o["ci"] else "채점 전", "accent"),
        ("전 항목 정답률", _pct(o["all_ok_rate"]), f"{o['all_ok']:,} / {o['n']:,}", "ok"),
        ("부당 거부", f"{o['wrong_deny']:,}", f"거부 판정의 {_pct(o['wrong_deny_rate'])}", "bad"),
        ("부당 승인", f"{o['wrong_allow']:,}", f"승인 판정의 {_pct(o['wrong_allow_rate'])}", "bad"),
    ])
    webui.kpis([(f"{c['name']} 오탐율", _pct(c["fp_rate"]), f"❌ {c['pred_x']:,}건 중 {c['fp']:,} · 미탐율 {_pct(c['fn_rate'])}", "bad")
                for c in s["per_class"]] + [("검수 표본", f"{o['n']:,}", f"목표 {prog['target']:,} · 제외 {prog['excluded']:,}", "")])

    st.subheader("BCT 별")
    st.dataframe([{"BCT": b["bct"].upper(), "표본": b["n"], "제외": b["excluded"], "대기": b["pending"],
                   "정확도": _pct(b["accuracy"]), "95% CI": f"{b['ci'][0]:.1f}~{b['ci'][1]:.1f}" if b["ci"] else "",
                   "부당 거부": b["wrong_deny"], "부당 승인": b["wrong_allow"], "전 항목": _pct(b["all_ok_rate"]),
                   **{f"{CLASS_KO[k]} 오탐율": _pct(b["classes"][k]["fp_rate"]) for k in CLASS_KEYS}} for b in s["per_bct"]],
                 width="stretch", hide_index=True)

    st.subheader("클래스 별")
    st.dataframe([{"항목": c["name"], "표본": c["n"], "정확도": _pct(c["accuracy"]), "❌ 판정": c["pred_x"], "오탐": c["fp"],
                   "오탐율": _pct(c["fp_rate"]), "⭕ 판정": c["pred_o"], "미탐": c["fn"], "미탐율": _pct(c["fn_rate"])}
                  for c in s["per_class"]], width="stretch", hide_index=True)

    st.subheader("모델 개선 비교")
    others = [m for m in list_months(root, site.code) if m != store.pool["month"]]
    earlier = [m for m in others if m < store.pool["month"]]
    base_month = st.selectbox("비교 기준", ["없음"] + sorted(others, reverse=True),
                              index=(1 + sorted(others, reverse=True).index(max(earlier))) if earlier else 0, key="pf_base")
    cmp = None
    if base_month != "없음":
        try:
            cmp = compare(s, summarize(PerfStore.open(root, site.code, base_month)))
        except (OSError, ValueError, KeyError):
            st.warning("비교 기준 결과를 읽지 못했습니다.")
    if cmp:
        st.dataframe([{"지표": m["name"], "기준": _pct(m["base"]), "이번": _pct(m["cur"]),
                       "변화": "–" if m["delta"] is None else f"{m['delta']:+.1f}%p"} for m in cmp["metrics"]],
                     width="stretch", hide_index=True)
    elif base_month == "없음":
        st.info("비교할 기준 결과가 없습니다.")

    if s["errors"]:
        with st.expander(f"오류 사례 ({len(s['errors']):,}건)"):
            vk = {"allowed": "승인", "denied": "거부"}
            st.dataframe([{"BCT": r["bct"].upper(), "시각": f"{r['date'][5:]} {r['time']}", "시스템": vk[r["verdict"]],
                           "실제": vk[r["gt_verdict"]],
                           "틀린 항목": " · ".join(f"{CLASS_KO[k]} {'오탐' if not r[f'{k}_pred'] else '미탐'}" for k in r["wrong"]),
                           "이벤트": r["event_id"]} for r in s["errors"]], width="stretch", hide_index=True, height=320)

    st.divider()
    c1, c2, c3 = st.columns([1.3, 1, 1])
    out = store.dir / "report"
    with c1:
        if st.button("📄 리포트 만들기", type="primary", width="stretch", disabled=not o["n"]):
            with st.spinner("리포트를 만드는 중…"):
                try:
                    build_report(s, cmp, graded_rows(store), out)
                    st.session_state.pop("pf_report_err", None)
                except Exception as e:
                    st.session_state.pf_report_err = f"{type(e).__name__}: {e}"
    if st.session_state.get("pf_report_err"):
        st.error("리포트를 만들지 못했습니다.")
        with st.expander("자세히"):
            st.code(st.session_state.pf_report_err)
    pdf, html = out / "report.pdf", out / "report.html"
    name = f"{site.code}_{store.pool['month']}_성능리포트"
    with c2:
        if pdf.exists():
            st.download_button("PDF 받기", pdf.read_bytes(), file_name=f"{name}.pdf", mime="application/pdf", width="stretch")
    with c3:
        if html.exists():
            st.download_button("HTML 받기", html.read_bytes(), file_name=f"{name}.html", mime="text/html", width="stretch")


# ══════════════════════════════════════════════════════════════════════════
def main():
    webui.inject_css()
    st_ = settings()
    if not st.session_state.get("user"):
        webui.page_login(); return
    webui.sidebar_user(STATE_KEYS + ("store_key",))
    code = st.session_state.get("site_code")
    if not code:
        page_sites(); return
    site = st_.site(code)
    month = st.session_state.get("month")
    if not month:
        page_months(site); return
    page_month(site, month)


main()
