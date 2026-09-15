"""BCT 오탐 검수 세션 — 검수자 PC 로컬 웹 (Streamlit).

실행:  scripts\\review_app.bat   (또는  python -m streamlit run review/app.py)
흐름:  현장 선택 → 일자 선택 → 하루치 목록·필터 → 검수(정탐/오탐, 1개씩 또는 2×2 그리드) → 오탐 내보내기
저장:  {저장루트}/{site}/{date}/session.json  (판정), {hook|ppe}/{ts}_{bct}_{role}.mp4 (오탐 학습용 클립, review/export.py)
공통 화면 부품(로그인·CSS·영상·단축키)은 review/webui.py — 성능 검수 앱(perf_app.py)과 같이 쓴다.
"""
from __future__ import annotations

import sys
from datetime import date as _date, time as _time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import streamlit as st  # noqa: E402

from review import export, webui  # noqa: E402
from review.catalog import Event, list_events  # noqa: E402
from review.influx import join_events, query_day, reasons_from  # noqa: E402
from review.session import FP_CLASSES, VERDICTS, Session  # noqa: E402
from review.webui import (CLASSES, VERDICT_KO, VERDICT_SHORT, access, cached_days, class_marks, client,  # noqa: E402
                          header, inject_css, labeled_pills, out_root, page_login, pills, settings)

APP_NAME = "오탐 검수 플랫폼"
webui.setup(APP_NAME, "🔎")

BTN = {"tp": "정탐 ←", "fp": "오탐 →", "skip": "건너뛰기 ␣", "undo": "되돌리기 Z"}
GRID_N = 4                                   # 그리드 모드: 한 화면에 이벤트 4개 (2×2), 카메라 2대면 영상 8개


# ══════════════════════════════════════════════════════════════════════════
# 설정 · 접근
# ══════════════════════════════════════════════════════════════════════════
def load_day(site, date: str):
    tz = settings().timezone
    c = client(site)
    events = list_events(c, site.minio_bucket, site.code, date, tg_prefix=site.tg_prefix)
    matched, stats, err = {}, {}, ""
    if site.influx_token:
        try:
            inf = site.influx
            rows = query_day(access(site).influx_url, site.influx_token, inf.get("org", "mithril"),
                             inf.get("bucket", "gate_events"), inf.get("measurement", "gate_event"), date, tz)
            matched, stats = join_events(events, rows, tz, int(inf.get("join_tolerance_sec", 90)))
        except Exception as e:  # Influx 가 죽어도 목록은 나오게
            err = str(e)
    return events, matched, stats, err


# ══════════════════════════════════════════════════════════════════════════
# 행 구성 · 필터
# ══════════════════════════════════════════════════════════════════════════
def build_rows(site, events, matched, sess: Session) -> list[dict]:
    rows = []
    for ev in events:
        r = matched.get(ev.id)
        v = sess.verdict(ev.id)
        rows.append({
            "ev": ev, "id": ev.id, "time": ev.time_str, "bct": ev.bct,
            "verdict": (r or {}).get("verdict", ""),
            "hook": (r or {}).get("hook_score"), "helmet": (r or {}).get("helmet_score"), "harness": (r or {}).get("harness_score"),
            "reasons": reasons_from(r, site.thresholds),
            "tg": bool(ev.tg), "train": bool(ev.train),
            "my": (v or {}).get("verdict", ""), "memo": (v or {}).get("memo", ""), "fp_classes": (v or {}).get("classes", []),
        })
    return rows


def apply_filters(rows: list[dict], f: dict) -> list[dict]:
    out = []
    t0, t1 = f["time"]
    for r in rows:
        hh, mm = int(r["time"][:2]), int(r["time"][3:5])
        t = _time(hh, mm)
        if not (t0 <= t <= t1):
            continue
        if f["verdict"] != "전체" and r["verdict"] != f["verdict"]:
            continue
        if f["bcts"] and r["bct"] not in f["bcts"]:
            continue
        if f["reasons"] and not any(x in r["reasons"] for x in f["reasons"]):
            continue
        if r["hook"] is not None and not (f["hook"][0] <= r["hook"] <= f["hook"][1]):
            continue
        miss = f["missing"]
        if miss:
            sc = {"hook": r["hook"], "helmet": r["helmet"], "harness": r["harness"]}
            if not all((sc[k] is not None and sc[k] <= 0.0) for k in miss):
                continue
        s = f["status"]
        if s == "미검수" and r["my"]:
            continue
        if s == "검수됨" and not r["my"]:
            continue
        if s == "오탐만" and r["my"] != "fp":
            continue
        if s == "항목 미분류" and not (r["my"] == "fp" and not r["fp_classes"]):
            continue
        out.append(r)
    return out


# ══════════════════════════════════════════════════════════════════════════
# 키보드
#   1개씩: ← 정탐, → 오탐, Space 건너뛰기, Z 되돌리기, 1~3 오탐 클래스 토글
#   그리드: 1~4 타일 체크 → 이어서 1~3 그 타일의 오탐 클래스 (Esc 는 고르지 않고 빠짐)
#           P 전부 정탐, N 체크된 것 오탐(나머지 정탐), Z 되돌리기
# ══════════════════════════════════════════════════════════════════════════
KEYMAP_SINGLE = {"ArrowLeft": "정탐", "ArrowRight": "오탐", " ": "건너뛰기", "z": "되돌리기", "Z": "되돌리기"}
KEYMAP_GRID = {"p": "전부 정탐", "P": "전부 정탐", "n": "체크 오탐", "N": "체크 오탐", "z": "되돌리기", "Z": "되돌리기"}
FP_CLASS_KO = {key: label for label, key in CLASSES}
FP_CLASS_OPTS = list(FP_CLASSES)                 # 칩 순서 = 숫자키 순서 (1 안전모 · 2 하네스 · 3 안전고리)


def fp_class_label(k: str) -> str:
    return f"{FP_CLASS_OPTS.index(k) + 1} {FP_CLASS_KO[k]}"


def fp_class_text(classes: list[str]) -> str:
    return " · ".join(FP_CLASS_KO[k] for k in FP_CLASS_OPTS if k in (classes or []))


def inject_helpers(grid: bool, rate: float, batch: str = "") -> None:
    pick = {"mode": "grid", "tiles": GRID_N, "n": len(FP_CLASS_OPTS), "batch": batch} if grid else {"mode": "single", "n": len(FP_CLASS_OPTS)}
    webui.inject_helpers(KEYMAP_GRID if grid else KEYMAP_SINGLE, rate, pick)


def sidebar_user():
    webui.sidebar_user(("site_code", "date", "loaded", "events", "matched", "stats", "sess", "idx"), sync_all=True)


# ══════════════════════════════════════════════════════════════════════════
# 페이지 1 · 현장 선택
# ══════════════════════════════════════════════════════════════════════════
def page_sites():
    st_ = settings()
    header("현장 선택")
    root, src = out_root()
    if src == "NAS":
        st.caption(f"저장 위치: NAS · `{root}`")
    else:
        c1, c2 = st.columns([5, 1])
        with c1:
            st.warning("NAS에 연결되지 않아 검수 결과를 이 PC에 저장합니다.")
        with c2:
            st.write("")
            if st.button("NAS 다시 연결", width="stretch"):
                st_.force_nas_retry(); st.rerun()
    st.subheader("현장 선택")
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
                    st.session_state.pop("date", None)
                    st.rerun()


# ══════════════════════════════════════════════════════════════════════════
# 페이지 2 · 일자 선택
# ══════════════════════════════════════════════════════════════════════════
def page_days(site):
    header(f"{site.name} · 일자 선택")
    if st.button("← 현장 선택"):
        st.session_state.pop("site_code", None); st.rerun()
    with st.spinner("날짜 목록을 불러오는 중…"):
        try:
            days = cached_days(site.code)
        except Exception as e:
            st.error("현장에 연결하지 못했습니다. 네트워크 연결을 확인해 주세요.")
            with st.expander("자세히"):
                st.code(str(e))
            return
    if not days:
        st.warning("이벤트가 없습니다."); return
    root, _ = out_root()
    latest = sorted(days)[-1]
    recent = sorted(days)[-14:][::-1]                 # 표 행 순서 (최신이 위)
    d_min = _date.fromisoformat(sorted(days)[0])
    d_max = max(_date.today(), _date.fromisoformat(latest))

    # 표에서 행을 누르면 일자 입력이 그 날짜로, 일자를 직접 바꾸면 표의 그 행이 선택된다
    pick_key, tbl_key = f"day_pick_{site.code}", f"days_table_{site.code}"
    webui.table_pick(recent, pick_key, tbl_key, latest, to_widget=_date.fromisoformat, from_widget=lambda d: d.isoformat(),
                     clamp=lambda d: min(max(d, d_min), d_max))

    c1, c2, c3 = st.columns([2, 1, 1])
    with c1:
        pick = st.date_input("일자", min_value=d_min, max_value=d_max, key=pick_key)
    with c2:
        n_pick = sum(days.get(pick.isoformat(), {}).values())
        st.metric("해당 일자 이벤트", n_pick)
        p = day_progress(root, site.code, pick.isoformat(), n_pick)
        st.markdown(f'<span class="pm-pill {p["cls"]}">{p["label"]} <b>{p["done"]}/{n_pick}</b></span>'
                    + (f' <span class="muted" style="color:#6B7280;font-size:13px">정탐 {p["tp"]} · 오탐 {p["fp"]}</span>' if p["done"] else ""),
                    unsafe_allow_html=True)
    with c3:
        st.write(""); st.write("")
        if st.button("이 날짜 열기 →", type="primary", width="stretch"):
            st.session_state.date = pick.isoformat()
            st.session_state.pop("loaded", None)
            st.rerun()
    st.subheader("최근 14일")
    table = []
    for d in recent:
        total = sum(days[d].values())
        p = day_progress(root, site.code, d, total)
        table.append({"일자": d, "상태": p["label"], "검수": f"{p['done']}/{total}", "정탐": p["tp"], "오탐": p["fp"],
                      "항목 미분류": p["unclassified"],
                      "이벤트": total, **{b: days[d].get(b, 0) for b in site.bcts}})
    st.dataframe(table, width="stretch", hide_index=True, key=tbl_key,
                 on_select="rerun", selection_mode=["single-row", "single-cell"])
    webui.inject_table_dblclick("이 날짜 열기")


DAY_STATUS = {"none": ("⚪ 미검수", ""), "doing": ("🟡 진행 중", "warn"), "done": ("✅ 완료", "ok")}


def day_progress(root: Path, site_code: str, date: str, total: int) -> dict:
    """저장 루트의 session.json 을 읽기만 해서 그 날의 검수 진행 상태를 낸다 (없으면 미검수, 파일을 만들지 않음).

    일자 화면을 열 때마다 새로 읽으므로 다른 검수자가 진행한 것도 바로 반영된다.
    """
    tp = fp = done = uncls = 0
    try:
        data = __import__("json").loads((root / site_code / date / "session.json").read_text(encoding="utf-8"))
        vals = list((data.get("verdicts") or {}).values())
        vs = [v.get("verdict") for v in vals]
        tp, fp, done = vs.count("tp"), vs.count("fp"), len(vs)
        uncls = sum(1 for v in vals if v.get("verdict") == "fp" and not v.get("classes"))
    except Exception:                         # 파일 없음·NAS 끊김·깨진 파일 → 미검수로 표시
        pass
    state = "none" if done == 0 else "done" if total and done >= total else "doing"
    label, cls = DAY_STATUS[state]
    return {"state": state, "label": label, "cls": cls, "done": done, "tp": tp, "fp": fp, "unclassified": uncls}


# ══════════════════════════════════════════════════════════════════════════
# 페이지 3 · 검수
# ══════════════════════════════════════════════════════════════════════════
def page_review(site, date: str):
    st_ = settings()
    root, src = out_root()

    # ── 데이터 로드 (세션 상태에 보관, 새로고침 버튼으로 재조회) ──
    # 코드 핫리로드로 Session 클래스가 바뀌면 메모리의 객체는 옛 클래스라 새 메서드가 없다 → 다시 연다.
    if st.session_state.get("loaded") == (site.code, date) and type(st.session_state.get("sess")) is not Session:
        st.session_state.pop("loaded", None)
    if st.session_state.get("loaded") != (site.code, date):
        with st.spinner(f"{date} 이벤트를 불러오는 중…"):
            try:
                events, matched, stats, err = load_day(site, date)
            except Exception as e:
                st.error("이벤트를 불러오지 못했습니다. 네트워크 연결을 확인해 주세요.")
                with st.expander("자세히"):
                    st.code(str(e))
                if st.button("← 일자 선택"):
                    st.session_state.pop("date", None); st.rerun()
                return
        acc = access(site)
        sess0 = Session.open(root, site.code, date, {"access": acc.mode, "minio": acc.minio_endpoint})
        u = st.session_state.user
        sess0.note_reviewer(u["id"], u.get("ip", ""))
        st.session_state.pop("export_err", None)
        st.session_state.update(events=events, matched=matched, stats=stats, influx_err=err,
                                sess=sess0, loaded=(site.code, date), idx=0)
    events: list[Event] = st.session_state.events
    matched: dict = st.session_state.matched
    stats: dict = st.session_state.stats
    sess: Session = st.session_state.sess

    # ── 사이드바: 검수자 · 필터 · 내보내기 ──
    with st.sidebar:
        st.markdown(f"**{site.name}** · {date}")
        if st.button("← 일자 선택", width="stretch"):
            st.session_state.pop("date", None); st.session_state.pop("loaded", None); st.rerun()
        if st.button("🔄 새로고침", width="stretch"):
            st.session_state.pop("loaded", None); st.rerun()
        rate = st.select_slider("재생 속도", options=[1.0, 1.5, 2.0, 3.0], value=st.session_state.get("rate", 2.0),
                                format_func=lambda x: f"{x:g}x", key="rate")
        mode = st.radio("보기", ["4개씩 (2×2)", "1개씩"], horizontal=True, key="view_mode")
        grid = mode.startswith("4")
        reviewer = st.session_state.user["id"]           # 로그인 ID 가 곧 검수자 (session.json 의 by)
        st.divider()

        rows_all = build_rows(site, events, matched, sess)

        # ── 세션 정보 · 내보내기 (필터보다 위) ──
        st.markdown("**세션 정보**")
        cnt = sess.counts()
        n_uncls = len(sess.unclassified_fp_ids())
        st.caption(f"정탐 {cnt['tp']} · 오탐 {cnt['fp']}" + (f" · 항목 미분류 {n_uncls}" if n_uncls else "") + f" · 내보냄 {cnt['exported']}")
        plan = export.plan_day(root, site.code, date, site.cameras)
        n_pend = plan.n_events if plan else 0
        if st.button(f"📦 영상 내보내기 ({n_pend})", disabled=not (plan and not plan.empty), width="stretch", type="primary"):
            export_day(site, plan)
            if not st.session_state.get("export_err"):
                st.rerun()
        if st.session_state.get("export_err"):
            st.error("영상을 저장하지 못했습니다. 저장 위치(NAS) 연결을 확인하고 다시 시도해 주세요.")
            with st.expander("자세히"):
                st.code(st.session_state.export_err)
        st.divider()

        st.markdown("**필터**")
        all_reasons = sorted({x for r in rows_all for x in r["reasons"]})
        all_bcts = sorted({r["bct"] for r in rows_all}, key=lambda b: int(b[3:]))
        f = {
            "time": st.slider("시간대", value=(_time(0, 0), _time(23, 59)), step=__import__("datetime").timedelta(minutes=5), format="HH:mm"),
            "verdict": {"전체": "전체", "승인": "allowed", "거부": "denied"}[st.radio("판정", ["전체", "승인", "거부"], horizontal=True)],
            "reasons": st.multiselect("사유", all_reasons),
            "bcts": st.multiselect("BCT", all_bcts),
            "hook": st.slider("Hook 점수", 0.0, 1.0, (0.0, 1.0), 0.05),
            "missing": st.multiselect("클래스 미검출 (점수 0)", ["hook", "helmet", "harness"]),
            "status": st.radio("상태", ["전체", "미검수", "검수됨", "오탐만", "항목 미분류"], horizontal=True),
        }

    flist = apply_filters(rows_all, f)
    n_done = sum(1 for r in flist if r["my"])
    sess.data["filters_last"] = {k: (str(v) if k == "time" else v) for k, v in f.items()}

    # ── 상단 요약 ──
    header(f"{site.name} · {date}")
    c = sess.counts()
    inf_val = f"{stats.get('matched', 0)}/{stats.get('events', len(events))}" if stats else "미조인"
    items = [("전체", f"{len(events)}", ""), ("필터", f"{len(flist)}", ""), ("검수", f"{n_done}/{len(flist)}", ""),
             ("정탐", f"{c['tp']}", "ok"), ("오탐", f"{c['fp']}", "fp"),
             ("판정 연동", inf_val, "warn" if st.session_state.get("influx_err") else ""), ("저장", "NAS" if src == "NAS" else "이 PC", "" if src == "NAS" else "warn")]
    model = webui.model_label(site)
    if model:
        items.append(("모델", model, ""))
    pills(items)
    if st.session_state.get("influx_err"):
        st.warning("판정 정보를 불러오지 못해 영상만 표시합니다. 새로고침으로 다시 시도할 수 있습니다.")
        with st.expander("자세히"):
            st.code(st.session_state.influx_err)

    if not flist:
        st.info("필터 조건에 맞는 이벤트가 없습니다."); return

    # ── 필터를 바꾸면 목록이 달라지므로 맨 앞(미검수 필터면 첫 미검수)부터 ──
    f_sig = repr(sess.data["filters_last"])
    if st.session_state.get("filters_sig") not in (None, f_sig) and st.session_state.get("idx_init") == (site.code, date):
        st.session_state.idx = 0
    st.session_state.filters_sig = f_sig

    # ── 이어하기: 세션을 처음 열면 마지막으로 판정한 이벤트 바로 다음부터 ──
    if st.session_state.get("idx_init") != (site.code, date):
        st.session_state.idx_init = (site.code, date)
        start = 0
        last = sess.last_judged_id()
        if last:
            pos = next((i for i, x in enumerate(flist) if x["id"] == last), None)
            if pos is not None:
                start = min(pos + 1, len(flist) - 1)
            else:                                   # 마지막 판정이 필터 밖이면 첫 미검수로
                start = next((i for i, x in enumerate(flist) if not x["my"]), 0)
        st.session_state.idx = start

    idx = max(0, min(st.session_state.get("idx", 0), len(flist) - 1))
    st.session_state.idx = idx

    if grid:
        # 판정하면 목록에서 빠지는 필터에선 저장 후 제자리가 곧 다음 묶음
        review_grid(site, flist, idx, sess, reviewer, rate, shrinking=f["status"] in ("미검수", "항목 미분류"))
    else:
        review_single(site, flist, idx, sess, reviewer, rate)

    # ── 목록 ──
    with st.expander(f"하루치 목록 (필터 {len(flist)}건)", expanded=False):
        st.dataframe(
            [{"#": i + 1, "시각": x["time"], "BCT": x["bct"].upper(), "판정": VERDICT_SHORT.get(x["verdict"], "-"),
              **{lab: ("⭕" if x[key] is not None and x[key] >= site.thresholds.get(f"{key}_score", 0.5) else "❌" if x[key] is not None else "–")
                 for lab, key in CLASSES},
              "내 판정": VERDICTS.get(x["my"], ""), "오탐 항목": fp_class_text(x["fp_classes"]) if x["my"] == "fp" else "",
              "메모": x["memo"], "id": x["id"]} for i, x in enumerate(flist)],
            width="stretch", hide_index=True, height=360)


def _event_line(site, r: dict) -> tuple[str, str]:
    """(판정 문구, 클래스 ⭕/❌ 문구)"""
    has_row = r["hook"] is not None or r["helmet"] is not None or r["harness"] is not None
    return VERDICT_KO.get(r["verdict"], VERDICT_KO[""]), (class_marks(r, site.thresholds) if has_row else "")


# ── 1개씩 모드 ────────────────────────────────────────────────────────────
def review_single(site, flist: list[dict], idx: int, sess: Session, reviewer: str, rate: float):
    r = flist[idx]
    ev: Event = r["ev"]

    nav = st.columns([1, 6, 1])
    with nav[0]:
        if st.button("◀ 이전", disabled=idx == 0, width="stretch"):
            st.session_state.idx = idx - 1; st.rerun()
    with nav[1]:
        opts = [f"{x['time']}  {x['bct']:6s}  {VERDICT_SHORT.get(x['verdict'], '-'):4s}  {VERDICTS.get(x['my'], '·')}  {x['id']}" for x in flist]
        pick = st.selectbox("이벤트로 이동", range(len(flist)), index=idx, format_func=lambda i: opts[i], label_visibility="collapsed")
        if pick != idx:
            st.session_state.idx = pick; st.rerun()
    with nav[2]:
        if st.button("다음 ▶", disabled=idx >= len(flist) - 1, width="stretch"):
            st.session_state.idx = idx + 1; st.rerun()

    mine = VERDICTS.get(r["my"], "")
    if r["my"] == "fp" and r["fp_classes"]:
        mine += " · " + fp_class_text(r["fp_classes"])
    vcls = {"tp": "v-tp", "fp": "v-fp"}.get(r["my"], "muted")
    # 판정한 이벤트는 패널 색으로 바로 보이게 (미검수 하늘색 · 미검수 거부 파스텔 노랑 · 정탐 연두 · 오탐 연빨강)
    if r["my"] in ("tp", "fp"):
        panel_key = f"action_panel_{r['my']}"
    elif r["verdict"] == "denied":
        panel_key = "action_panel_deny"
    else:
        panel_key = "action_panel"
    with st.container(key=panel_key):
        verdict_txt, marks = _event_line(site, r)
        st.markdown(
            f'<div class="pm-ev"><code>{ev.id}</code> &nbsp; <span class="muted">{idx + 1} / {len(flist)}</span> &nbsp; '
            f'<span class="{vcls}">{mine or "미검수"}</span><br>'
            f'{ev.time_str} · <b>{ev.bct.upper()}</b> &nbsp;·&nbsp; <b>{verdict_txt}</b>'
            + (f' &nbsp;·&nbsp; {marks}' if marks else "") + '</div>',
            unsafe_allow_html=True,
        )

        vcols = st.columns(len(site.cameras))
        for col, role in zip(vcols, site.cameras):
            with col:
                with st.spinner(f"{role} 영상 준비…"):
                    webui.video(site, ev, role)

        # ── 오탐 클래스 칩 (오탐 → 을 누를 때 함께 기록) ──
        cls_key = f"cls1_{ev.id}"
        if cls_key not in st.session_state:
            st.session_state[cls_key] = list(r["fp_classes"])
        cls_now = labeled_pills("오탐 내역", FP_CLASS_OPTS, fp_class_label, cls_key)

        # ── 판정 버튼 ──
        b = st.columns([1, 1, 1, 1, 3])
        memo_key = f"memo_{ev.id}"
        with b[4]:
            memo = st.text_input("메모", value=r["memo"], key=memo_key, placeholder="한 줄 메모 (선택)")
        def _set(v):
            u = st.session_state.user
            sess.set(ev.id, v, reviewer, st.session_state.get(memo_key, ""), ip=u.get("ip", ""), classes=cls_now)
            st.session_state.idx = min(idx + 1, len(flist) - 1) if idx < len(flist) - 1 else idx
            st.rerun()
        with b[0]:
            if st.button(BTN["tp"], key="btn_tp", width="stretch"): _set("tp")
        with b[1]:
            if st.button(BTN["fp"], key="btn_fp", width="stretch"): _set("fp")
        with b[2]:
            if st.button(BTN["skip"], key="btn_skip", width="stretch", disabled=idx >= len(flist) - 1):
                st.session_state.idx = idx + 1; st.rerun()
        with b[3]:
            if st.button(BTN["undo"], key="btn_undo", width="stretch", disabled=not sess.data["history"]):
                ids = sess.undo()
                pos = next((i for i, x in enumerate(flist) if x["id"] in ids), None)
                if pos is not None:
                    st.session_state.idx = pos
                st.rerun()
        if r["my"] and memo != r["memo"]:
            sess.set_memo(ev.id, memo)
        if r["my"] == "fp" and sorted(cls_now) != sorted(r["fp_classes"]):
            sess.set_classes(ev.id, cls_now)
    inject_helpers(grid=False, rate=rate)


# ── 4개씩 모드 (2×2) ──────────────────────────────────────────────────────
def suggested_classes(site, r: dict) -> list[str]:
    """항목 분류 전 오탐의 제안값: 그 이벤트에서 임계값에 못 미친(❌) 클래스. 거부된 오탐은 대개 이것이 오탐 항목이다."""
    return [k for k in FP_CLASS_OPTS if r.get(k) is not None and r[k] < site.thresholds.get(f"{k}_score", 0.5)]


def review_grid(site, flist: list[dict], idx: int, sess: Session, reviewer: str, rate: float, shrinking: bool = False):
    """한 화면에 이벤트 4개. 1~4 체크 = 오탐 후보. P = 전부 정탐, N = 체크 오탐·나머지 정탐. 되돌리기는 묶음 단위.

    이미 오탐으로 판정한 타일은 체크된 채로 열리고 칩에 저장된 항목(없으면 ❌ 클래스 제안)이 들어가 있다 → N 으로 항목 저장.
    """
    # 묶음의 시작은 4의 배수가 아니어도 되지만, 페이지 이동은 4칸씩
    start = idx
    batch = flist[start:start + GRID_N]

    nav = st.columns([1, 6, 1])
    with nav[0]:
        if st.button("◀ 이전 4개", disabled=start == 0, width="stretch"):
            st.session_state.idx = max(0, start - GRID_N); st.rerun()
    with nav[2]:
        if st.button("다음 4개 ▶", disabled=start + GRID_N >= len(flist), width="stretch"):
            st.session_state.idx = min(start + GRID_N, len(flist) - 1); st.rerun()

    # 체크 상태는 묶음이 바뀌면 판정 기록으로 다시 채운다 (오탐이면 체크 + 항목)
    batch_key = tuple(x["id"] for x in batch)
    if st.session_state.get("grid_batch") != batch_key:
        st.session_state.grid_batch = batch_key
        for i in range(GRID_N):
            r = batch[i] if i < len(batch) else None
            is_fp = bool(r and r["my"] == "fp")
            st.session_state[f"chk_{i}"] = is_fp
            st.session_state[f"cls_{i}"] = (list(r["fp_classes"]) or suggested_classes(site, r)) if is_fp else []

    rows2 = [st.columns(2), st.columns(2)]
    chk_now: dict[int, bool] = {}                 # 이번 렌더의 체크 값 (위젯 반환값 — session_state 보다 한 박자 빠름)
    cls_now: dict[int, list[str]] = {}            # 체크한 타일의 오탐 클래스
    for i, r in enumerate(batch):
        ev: Event = r["ev"]
        col = rows2[i // 2][i % 2]
        checked = bool(st.session_state.get(f"chk_{i}", False))
        if r["my"] in ("tp", "fp"):
            tile_key = f"tile_done_{r['my']}_{i}"
        elif checked:
            tile_key = f"tile_fp_{i}"
        elif r["verdict"] == "denied":
            tile_key = f"tile_deny_{i}"
        else:
            tile_key = f"tile_{i}"
        with col:
            with st.container(key=tile_key):
                verdict_txt, marks = _event_line(site, r)
                mine = VERDICTS.get(r["my"], "")
                if r["my"] == "fp" and r["fp_classes"]:
                    mine += " · " + fp_class_text(r["fp_classes"])
                vcls = {"tp": "v-tp", "fp": "v-fp"}.get(r["my"], "muted")
                st.markdown(
                    f'<div class="pm-tile-head"><span class="num">{i + 1}</span><code>{ev.id}</code>'
                    f'<span class="{vcls}">{mine or "미검수"}</span></div>'
                    f'<div class="pm-tile-sub">{ev.time_str} · <b>{ev.bct.upper()}</b> · <b>{verdict_txt}</b>'
                    + (f' · {marks}' if marks else "") + '</div>',
                    unsafe_allow_html=True,
                )
                vc = st.columns(len(site.cameras))
                for c, role in zip(vc, site.cameras):
                    with c:
                        webui.video(site, ev, role, label_role=False)
                chk_now[i] = st.checkbox(f"{'☑' if checked else '☐'} {i + 1} · 오탐으로 표시", key=f"chk_{i}")
                if chk_now[i]:
                    cls_now[i] = labeled_pills("오탐 내역", FP_CLASS_OPTS, fp_class_label, f"cls_{i}")

    # 빈 칸 채우기 (마지막 묶음이 4개 미만일 때)
    for i in range(len(batch), GRID_N):
        with rows2[i // 2][i % 2]:
            st.empty()

    # ── 판정 버튼 ──
    def _commit(mark_fp: bool):
        u = st.session_state.user
        verdicts, classes = {}, {}
        for i, r in enumerate(batch):
            verdicts[r["id"]] = "fp" if (mark_fp and chk_now.get(i, False)) else "tp"
            classes[r["id"]] = cls_now.get(i, [])
        sess.set_many(verdicts, reviewer, ip=u.get("ip", ""), classes=classes)
        if not shrinking:
            st.session_state.idx = min(start + GRID_N, len(flist) - 1) if start + GRID_N < len(flist) else start
        st.session_state.grid_batch = None
        st.rerun()

    n_chk = sum(1 for i in range(len(batch)) if chk_now.get(i, False))
    b = st.columns([1.3, 1.3, 1, 3])
    with b[0]:
        if st.button("전부 정탐 · P", key="btn_grid_tp", width="stretch"): _commit(False)
    with b[1]:
        if st.button(f"체크 오탐 · N ({n_chk})", key="btn_grid_fp", width="stretch", disabled=n_chk == 0): _commit(True)
    with b[2]:
        if st.button(BTN["undo"], key="btn_undo", width="stretch", disabled=not sess.data["history"]):
            ids = sess.undo()
            pos = next((i for i, x in enumerate(flist) if x["id"] in ids), None)
            if pos is not None:
                st.session_state.idx = pos
            st.session_state.grid_batch = None
            st.rerun()
    inject_helpers(grid=True, rate=rate, batch="|".join(batch_key))


def export_day(site, plan: "export.DayPlan") -> None:
    """이 날짜의 오탐 영상을 판정·오탐 항목에 맞춰 NAS 에 반영 (필요한 카메라만 받고, 필요 없는 영상은 지움)."""
    bar = st.progress(0.0, text="영상 반영 중…")
    try:
        c = client(site) if plan.download else None
        res = export.apply_plan(plan, c, site.minio_bucket, site.cameras,
                                progress=lambda i, n, eid: bar.progress(i / n, text=f"{i}/{n} · {eid}"))
    except Exception as e:
        bar.empty()
        # st.stop() 을 쓰면 본문까지 안 그려져 화면이 비므로, 실패해도 계속 그린다.
        st.session_state.export_err = f"{type(e).__name__}: {e}"
        return
    st.session_state.pop("export_err", None)
    webui.reload_session()
    bar.progress(1.0, text=f"완료 · 받음 {res.downloaded} · 옮김 {res.moved} · 정리 {res.removed}")


# ══════════════════════════════════════════════════════════════════════════
def main():
    inject_css()
    st_ = settings()
    if not st.session_state.get("user"):
        page_login(); return
    sidebar_user()
    code = st.session_state.get("site_code")
    if not code:
        page_sites(); return
    site = st_.site(code)
    date = st.session_state.get("date")
    if not date:
        page_days(site); return
    page_review(site, date)


main()
