"""BCT 오탐 검수 세션 — 검수자 PC 로컬 웹 (Streamlit).

실행:  scripts\\review_app.bat   (또는  python -m streamlit run review/app.py)
흐름:  현장 선택 → 일자 선택 → 하루치 목록·필터 → 검수(정탐/오탐/애매) → 오탐 내보내기
저장:  {저장루트}/{site}/{date}/session.json  (판정), {event_id}/{hook,ppe}.mp4 (오탐 학습용 클립)
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from datetime import date as _date, datetime, time as _time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import streamlit as st  # noqa: E402
import streamlit.components.v1 as components  # noqa: E402

from review import config as cfgmod  # noqa: E402
from review.access import SiteAccess  # noqa: E402
from review.catalog import Event, list_days, list_events, minio_client  # noqa: E402
from review.download import fetch_events  # noqa: E402
from review.influx import join_events, query_day, reasons_from  # noqa: E402
from review.session import VERDICTS, Session  # noqa: E402
from review.auth import list_users, verify_user  # noqa: E402

APP_NAME = "오탐 검수 플랫폼"
ORG = "Paimedialab"
st.set_page_config(page_title=f"{ORG} {APP_NAME}", page_icon="🔎", layout="wide")

PLAYABLE = {"h264", "avc1", "vp9", "vp8", "av1", "hevc"}   # 브라우저가 재생하는 코덱 (hevc 는 환경에 따라)
BTN = {"tp": "정탐 ←", "fp": "오탐 →", "unsure": "애매 ↓", "skip": "건너뛰기 ␣", "undo": "되돌리기 Z"}
VERDICT_KO = {"allowed": "출입 승인 ✅", "denied": "출입 거부 ❌", "": "판정 정보 없음"}
VERDICT_SHORT = {"allowed": "승인", "denied": "거부", "": "-"}
CLASSES = (("안전모", "helmet"), ("하네스", "harness"), ("안전고리", "hook"))


def class_marks(r: dict, thresholds: dict) -> str:
    """점수·임계값 → '안전모 ⭕ · 하네스 ⭕ · 안전고리 ❌' (엣지 decision_engine 과 같은 기준)."""
    out = []
    for label, key in CLASSES:
        v = r.get(key)
        if v is None:
            out.append(f"{label} –")
        else:
            out.append(f"{label} {'⭕' if v >= thresholds.get(f'{key}_score', 0.5) else '❌'}")
    return " · ".join(out)

CSS = """
<style>
/* Streamlit 기본 장식 제거 */
#MainMenu, footer, [data-testid="stToolbar"], [data-testid="stDecoration"], [data-testid="stStatusWidget"],
.stDeployButton, [data-testid="stAppDeployButton"] { display:none !important; }
header[data-testid="stHeader"] { background:transparent; height:0; }
.block-container { padding-top:1.1rem; padding-bottom:3rem; max-width:1280px; }
/* 팔레트: 화이트 바탕 + 블루(#2563EB) 포인트. 판정색만 의미색(초록/빨강/회색) 유지 */
/* 상단 헤더 */
.pm-head { display:flex; align-items:baseline; gap:14px; border-bottom:1px solid #E3E8EF; padding:2px 0 10px; margin:0 0 18px; }
.pm-head .eyebrow { font-size:11.5px; letter-spacing:.14em; text-transform:uppercase; color:#2563EB; font-weight:700; }
.pm-head h1 { font-size:21px; margin:0; font-weight:700; letter-spacing:-.01em; color:#111827; }
.pm-head .crumb { margin-left:auto; color:#6B7280; font-size:13px; }
/* 상태 알약 */
.pm-pills { display:flex; gap:8px; flex-wrap:wrap; margin:0 0 12px; }
.pm-pill { border:1px solid #E3E8EF; border-radius:999px; padding:2px 12px; font-size:13px; background:#F5F7FA; color:#4B5563; white-space:nowrap; }
.pm-pill b { color:#111827; font-weight:600; }
.pm-pill.fp b { color:#DC2626; } .pm-pill.ok b { color:#16A34A; } .pm-pill.warn { border-color:#F59E0B; background:#FFFBEB; }
/* 현재 이벤트 카드 */
.pm-ev { border:1px solid #E3E8EF; border-left:4px solid #2563EB; background:#FFFFFF; padding:10px 14px; margin:4px 0 10px; font-size:14px; line-height:1.7; border-radius:0 6px 6px 0; }
.pm-ev code { font-family:ui-monospace,Consolas,monospace; font-size:13px; background:#EFF4FF; color:#1E3A8A; padding:1px 6px; border-radius:3px; }
.pm-ev .v-tp { color:#16A34A; font-weight:700; } .pm-ev .v-fp { color:#DC2626; font-weight:700; } .pm-ev .v-un { color:#6B7280; font-weight:700; }
.pm-ev .muted { color:#6B7280; }
/* 액션 패널: 이벤트 카드 + 영상 + 판정 버튼 + 메모를 하늘색 박스로 묶음 */
.st-key-action_panel { background:#EFF6FF; border:1px solid #BFDBFE; border-radius:12px; padding:16px 18px 12px; margin:4px 0 14px; }
.st-key-action_panel .pm-ev { border-color:#BFDBFE; }
.st-key-action_panel [data-testid="stCaptionContainer"] { color:#1E3A8A; }
/* 판정 버튼 */
.st-key-btn_tp button { border-color:#16A34A; color:#16A34A; font-weight:600; }
.st-key-btn_tp button:hover { background:#DCFCE7; }
.st-key-btn_fp button { background:#DC2626; border-color:#DC2626; color:#FFFFFF; font-weight:600; }
.st-key-btn_fp button:hover { background:#B91C1C; border-color:#B91C1C; }
.st-key-btn_unsure button { border-color:#9CA3AF; color:#4B5563; font-weight:600; }
.st-key-btn_skip button, .st-key-btn_undo button { color:#4B5563; }
/* 카드·테두리 */
div[data-testid="stVerticalBlockBorderWrapper"] { border-color:#E3E8EF !important; background:#FFFFFF; }
/* 사이드바 */
section[data-testid="stSidebar"] { background:#F8FAFC; border-right:1px solid #E3E8EF; }
section[data-testid="stSidebar"] .block-container { padding-top:1rem; }
h3 { font-size:17px !important; color:#111827; }
</style>
"""


def inject_css() -> None:
    st.markdown(CSS, unsafe_allow_html=True)


def header(crumb: str = "") -> None:
    st.markdown(
        f'<div class="pm-head"><span class="eyebrow">{ORG}</span><h1>{APP_NAME}</h1><span class="crumb">{crumb}</span></div>',
        unsafe_allow_html=True,
    )


def pills(items: list[tuple[str, str, str]]) -> None:
    """[(라벨, 값, css클래스)] → 알약 한 줄."""
    html = "".join(f'<span class="pm-pill {cls}">{lab} <b>{val}</b></span>' for lab, val, cls in items)
    st.markdown(f'<div class="pm-pills">{html}</div>', unsafe_allow_html=True)


# ══════════════════════════════════════════════════════════════════════════
# 설정 · 접근
# ══════════════════════════════════════════════════════════════════════════
@st.cache_resource(show_spinner=False)
def settings():
    return cfgmod.load()


def out_root() -> tuple[Path, str]:
    return settings().resolve_out_root(os.environ.get("BCT_REVIEW_OUT") or None)


def cache_root() -> Path:
    """영상 캐시는 NAS 가 아니라 항상 로컬 (재생용 임시 파일)."""
    st_ = settings()
    fb = Path(st_.local_fallback)
    if not fb.is_absolute():
        fb = (st_.cfg_dir / fb).resolve()
    p = fb / "_cache"
    p.mkdir(parents=True, exist_ok=True)
    return p


def access(site) -> SiteAccess:
    key = f"_acc_{site.code}"
    if key not in st.session_state:
        acc = SiteAccess(site, quiet=True)
        acc.__enter__()                       # tunnel 이면 ssh 프로세스가 앱 수명 동안 유지
        st.session_state[key] = acc
    return st.session_state[key]


def client(site):
    site.require_secrets()
    acc = access(site)
    return minio_client(acc.minio_endpoint, site.minio_access, site.minio_secret, site.minio_secure)


@st.cache_data(ttl=300, show_spinner=False)
def cached_days(code: str) -> dict:
    site = settings().site(code)
    return list_days(client(site), site.minio_bucket)


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
# 영상 (로컬 캐시 → 필요 시 H.264 변환)
# ══════════════════════════════════════════════════════════════════════════
def _codec(path: Path) -> str:
    if not shutil.which("ffprobe"):
        return "?"
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                              "stream=codec_name", "-of", "csv=p=0", str(path)],
                             capture_output=True, text=True, timeout=20)
        return out.stdout.strip().lower() or "?"
    except Exception:
        return "?"


def playable_path(site, ev: Event, role: str) -> tuple[Path | None, str]:
    """(재생 가능한 로컬 파일, 라벨). _tg(박스) 우선, 없으면 학습용."""
    key, label = ev.key_for(role, tg=True), "판정 표시 영상"
    if key is None:
        key, label = ev.key_for(role), "원본 영상"
    if key is None:
        return None, "영상 없음"
    d = cache_root() / site.code / ev.date / ev.id
    d.mkdir(parents=True, exist_ok=True)
    kind = "tg" if key.startswith(site.tg_prefix) else "train"
    raw = d / f"{role}_{kind}.mp4"
    h264 = d / f"{role}_{kind}_h264.mp4"
    if h264.exists() and h264.stat().st_size > 0:
        return h264, label
    if not (raw.exists() and raw.stat().st_size > 0):
        client(site).fget_object(site.minio_bucket, key, str(raw))
    codec = _codec(raw)
    if codec in PLAYABLE or codec == "?" or not shutil.which("ffmpeg"):
        return raw, label
    # mp4v 등 브라우저 미지원 → H.264 로 한 번만 변환
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(raw), "-c:v", "libx264", "-preset", "veryfast",
                    "-crf", "23", "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-an", str(h264)],
                   capture_output=True, timeout=120)
    if h264.exists() and h264.stat().st_size > 0:
        return h264, label
    return raw, label + " (재생이 안 될 수 있음)"


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
            "my": (v or {}).get("verdict", ""), "memo": (v or {}).get("memo", ""),
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
        out.append(r)
    return out


# ══════════════════════════════════════════════════════════════════════════
# 키보드 (← 정탐, → 오탐, ↓ 애매, Space 건너뛰기, Z 되돌리기)
# ══════════════════════════════════════════════════════════════════════════
def keyboard():
    try:
        _keyboard_html()
    except Exception:       # 헤드리스 테스트(AppTest) 등 컴포넌트 미지원 환경
        pass


def _keyboard_html():
    components.html("""
<script>
(function(){
  const doc = window.parent.document;
  if (doc.__bctKeys) return; doc.__bctKeys = true;
  const map = {ArrowLeft:'정탐', ArrowRight:'오탐', ArrowDown:'애매', ' ':'건너뛰기', z:'되돌리기', Z:'되돌리기'};
  doc.addEventListener('keydown', (e) => {
    const tag = (e.target && e.target.tagName) || '';
    if (tag === 'INPUT' || tag === 'TEXTAREA' || e.target.isContentEditable) return;
    const label = map[e.key]; if (!label) return;
    const btn = [...doc.querySelectorAll('button')].find(b => (b.innerText || '').trim().startsWith(label));
    if (btn) { e.preventDefault(); btn.click(); }
  }, true);
})();
</script>""", height=0)


# ══════════════════════════════════════════════════════════════════════════
# 로그인
# ══════════════════════════════════════════════════════════════════════════
def client_ip() -> str:
    """접속 IP. 공통 계정을 사람별로 구분하는 근거. localhost 접속이면 127.0.0.1."""
    try:
        ip = st.context.ip_address           # Streamlit ≥1.44. localhost 면 None
    except Exception:
        ip = None
    if not isinstance(ip, str) or not ip.strip():   # 헤드리스 테스트에선 Mock 이 온다
        return "127.0.0.1"
    return ip.strip()


def log_login(user: dict) -> None:
    """저장 루트에 _logins.jsonl 한 줄 추가 (누가·어디서·언제)."""
    try:
        root, _ = out_root()
        root.mkdir(parents=True, exist_ok=True)
        rec = {"at": datetime.now().isoformat(timespec="seconds"), "id": user["id"], "ip": user["ip"]}
        with open(root / "_logins.jsonl", "a", encoding="utf-8") as f:
            f.write(__import__("json").dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def page_login():
    st_ = settings()
    header("로그인")
    _, mid, _ = st.columns([1, 1.2, 1])
    with mid:
        with st.container(border=True):
            st.markdown("**로그인**")
            if not list_users(st_):
                st.error("등록된 계정이 없습니다. 관리자에게 문의하세요.")
                return
            uid = st.text_input("ID", key="login_id", autocomplete="username")
            pw = st.text_input("비밀번호", type="password", key="login_pw", autocomplete="current-password")
            if st.button("로그인", key="btn_login", type="primary", width="stretch"):
                u = verify_user(st_, uid, pw)
                if u:
                    u["ip"] = client_ip()
                    u["at"] = datetime.now().isoformat(timespec="seconds")
                    st.session_state.user = u
                    st.session_state.pop("login_pw", None)
                    log_login(u)
                    st.rerun()
                st.error("ID 또는 비밀번호가 맞지 않습니다.")


def sidebar_user():
    u = st.session_state.get("user")
    if not u:
        return
    with st.sidebar:
        c1, c2 = st.columns([2, 1])
        with c1:
            st.markdown(f"👤 **{u['name']}**")
            st.caption(u.get("ip", ""))
        with c2:
            if st.button("로그아웃", key="btn_logout", width="stretch"):
                for k in ("user", "site_code", "date", "loaded", "events", "matched", "stats", "sess", "idx"):
                    st.session_state.pop(k, None)
                st.rerun()
        st.divider()


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
    latest = sorted(days)[-1]
    c1, c2, c3 = st.columns([2, 1, 1])
    with c1:
        pick = st.date_input("일자", value=_date.fromisoformat(latest), min_value=_date.fromisoformat(sorted(days)[0]),
                             max_value=max(_date.today(), _date.fromisoformat(latest)))
    with c2:
        st.metric("해당 일자 이벤트", sum(days.get(pick.isoformat(), {}).values()))
    with c3:
        st.write(""); st.write("")
        if st.button("이 날짜 열기 →", type="primary", width="stretch"):
            st.session_state.date = pick.isoformat()
            st.session_state.pop("loaded", None)
            st.rerun()
    st.subheader("최근 14일")
    recent = sorted(days)[-14:]
    st.dataframe([{"date": d, "total": sum(days[d].values()), **{b: days[d].get(b, 0) for b in site.bcts}} for d in reversed(recent)],
                 width="stretch", hide_index=True)


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
        reviewer = st.session_state.user["id"]           # 로그인 ID 가 곧 검수자 (session.json 의 by)
        st.divider()
        st.markdown("**필터**")
        rows_all = build_rows(site, events, matched, sess)
        all_reasons = sorted({x for r in rows_all for x in r["reasons"]})
        all_bcts = sorted({r["bct"] for r in rows_all}, key=lambda b: int(b[3:]))
        f = {
            "time": st.slider("시간대", value=(_time(0, 0), _time(23, 59)), step=__import__("datetime").timedelta(minutes=5), format="HH:mm"),
            "verdict": {"전체": "전체", "승인": "allowed", "거부": "denied"}[st.radio("판정", ["전체", "승인", "거부"], horizontal=True)],
            "reasons": st.multiselect("사유", all_reasons),
            "bcts": st.multiselect("BCT", all_bcts),
            "hook": st.slider("Hook 점수", 0.0, 1.0, (0.0, 1.0), 0.05),
            "missing": st.multiselect("클래스 미검출 (점수 0)", ["hook", "helmet", "harness"]),
            "status": st.radio("상태", ["전체", "미검수", "검수됨", "오탐만"], horizontal=True),
        }
        st.divider()
        c = sess.counts()
        st.markdown("**세션**")
        st.caption(f"정탐 {c['tp']} · 오탐 {c['fp']} · 애매 {c['unsure']} · 내보냄 {c['exported']}")
        pend = sess.unexported_ids()
        n_pend = sum(len(v) for v in pend.values())
        if st.button(f"📦 영상 내보내기 (오탐 {len(pend['fp'])} · 애매 {len(pend['unsure'])})", disabled=not n_pend, width="stretch", type="primary"):
            export_clips(site, events, sess, root, pend)
            st.rerun()

    flist = apply_filters(rows_all, f)
    n_done = sum(1 for r in flist if r["my"])
    sess.data["filters_last"] = {k: (str(v) if k == "time" else v) for k, v in f.items()}

    # ── 상단 요약 ──
    header(f"{site.name} · {date}")
    c = sess.counts()
    inf_val = f"{stats.get('matched', 0)}/{stats.get('events', len(events))}" if stats else "미조인"
    items = [("전체", f"{len(events)}", ""), ("필터", f"{len(flist)}", ""), ("검수", f"{n_done}/{len(flist)}", ""),
             ("정탐", f"{c['tp']}", "ok"), ("오탐", f"{c['fp']}", "fp"), ("애매", f"{c['unsure']}", ""),
             ("판정 연동", inf_val, "warn" if st.session_state.get("influx_err") else ""), ("저장", "NAS" if src == "NAS" else "이 PC", "" if src == "NAS" else "warn")]
    pills(items)
    if st.session_state.get("influx_err"):
        st.warning("판정 정보를 불러오지 못해 영상만 표시합니다. 새로고침으로 다시 시도할 수 있습니다.")
        with st.expander("자세히"):
            st.code(st.session_state.influx_err)

    if not flist:
        st.info("필터 조건에 맞는 이벤트가 없습니다."); return

    # ── 현재 이벤트 ──
    idx = max(0, min(st.session_state.get("idx", 0), len(flist) - 1))
    st.session_state.idx = idx
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
    vcls = {"tp": "v-tp", "fp": "v-fp", "unsure": "v-un"}.get(r["my"], "muted")
    with st.container(key="action_panel"):
        has_row = r["hook"] is not None or r["helmet"] is not None or r["harness"] is not None
        verdict_txt = VERDICT_KO.get(r["verdict"], VERDICT_KO[""])
        marks = class_marks(r, site.thresholds) if has_row else ""
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
                    try:
                        path, label = playable_path(site, ev, role)
                    except Exception as e:
                        path, label = None, "영상을 불러오지 못했습니다"
                st.caption(f"**{role}** · {label}")
                if path:
                    st.video(str(path), autoplay=True, loop=True, muted=True)   # 자동·반복 재생, 컨트롤은 그대로
                else:
                    st.warning("영상 없음")

        # ── 판정 버튼 ──
        b = st.columns([1, 1, 1, 1, 1, 3])
        memo_key = f"memo_{ev.id}"
        with b[5]:
            memo = st.text_input("메모", value=r["memo"], key=memo_key, placeholder="한 줄 메모 (선택)")
        def _set(v):
            u = st.session_state.user
            sess.set(ev.id, v, reviewer, st.session_state.get(memo_key, ""), ip=u.get("ip", ""))
            st.session_state.idx = min(idx + 1, len(flist) - 1) if idx < len(flist) - 1 else idx
            st.rerun()
        with b[0]:
            if st.button(BTN["tp"], key="btn_tp", width="stretch"): _set("tp")
        with b[1]:
            if st.button(BTN["fp"], key="btn_fp", width="stretch"): _set("fp")
        with b[2]:
            if st.button(BTN["unsure"], key="btn_unsure", width="stretch"): _set("unsure")
        with b[3]:
            if st.button(BTN["skip"], key="btn_skip", width="stretch", disabled=idx >= len(flist) - 1):
                st.session_state.idx = idx + 1; st.rerun()
        with b[4]:
            if st.button(BTN["undo"], key="btn_undo", width="stretch", disabled=not sess.data["history"]):
                eid = sess.undo()
                pos = next((i for i, x in enumerate(flist) if x["id"] == eid), None)
                if pos is not None:
                    st.session_state.idx = pos
                st.rerun()
        if r["my"] and memo != r["memo"]:
            sess.set_memo(ev.id, memo)
    keyboard()

    # ── 목록 ──
    with st.expander(f"하루치 목록 (필터 {len(flist)}건)", expanded=False):
        st.dataframe(
            [{"#": i + 1, "시각": x["time"], "BCT": x["bct"].upper(), "판정": VERDICT_SHORT.get(x["verdict"], "-"),
              **{lab: ("⭕" if x[key] is not None and x[key] >= site.thresholds.get(f"{key}_score", 0.5) else "❌" if x[key] is not None else "–")
                 for lab, key in CLASSES},
              "내 판정": VERDICTS.get(x["my"], ""), "메모": x["memo"], "id": x["id"]} for i, x in enumerate(flist)],
            width="stretch", hide_index=True, height=360)


def export_clips(site, events: list[Event], sess: Session, root: Path, pend: dict[str, list[str]]) -> None:
    """오탐 → fp/, 애매 → unsure/ 하위 폴더로 원본 영상을 받는다. 정탐은 기록만."""
    idx = {e.id: e for e in events}
    jobs = [(v, [idx[i] for i in ids if i in idx]) for v, ids in pend.items() if ids]
    total = sum(len(t) for _, t in jobs)
    if not total:
        st.warning("내보낼 이벤트가 없습니다. 새로고침 후 다시 시도해 주세요."); return
    c = client(site)
    bar = st.progress(0.0, text="영상 다운로드 중…")
    done = 0
    for verdict, targets in jobs:
        def prog(i, n, res, _v=verdict):
            nonlocal done
            done += 1
            bar.progress(done / total, text=f"{done}/{total} · {VERDICTS[_v]} · {res.event_id}")
            sess.mark_exported(res.event_id, [f"{_v}/{f}" for f in res.downloaded + res.skipped])
        fetch_events(c, site.minio_bucket, targets, root, site.cameras, include_tg=False, progress=prog, subdir=verdict)
    bar.progress(1.0, text=f"완료 · {total}건 저장됨")


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
