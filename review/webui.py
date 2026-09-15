"""검수 웹 앱 공통 — 오탐 검수(app.py, :8501)와 성능 검수(perf_app.py, :8502)가 같이 쓰는 화면 부품.

페이지 설정 · CSS · 헤더/알약 · 설정·현장 접속 · 영상 캐시 · 로그인 · 단축키/자동재생 스크립트 · 표 더블클릭.
이 파일을 고치면 Streamlit 서버를 다시 켜야 반영된다 (앱 스크립트만 핫리로드됨).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

import streamlit as st
import streamlit.components.v1 as components

from review import config as cfgmod
from review.access import SiteAccess
from review.auth import list_users, verify_user
from review.catalog import Event, list_days, minio_client

ORG = "Paimedialab"
_APP = {"name": ""}

PLAYABLE = {"h264", "avc1", "vp9", "vp8", "av1", "hevc"}   # 브라우저가 재생하는 코덱 (hevc 는 환경에 따라)
VERDICT_KO = {"allowed": "출입 승인 ✅", "denied": "출입 거부 ❌", "": "판정 정보 없음"}
VERDICT_SHORT = {"allowed": "승인", "denied": "거부", "": "-"}
CLASSES = (("안전모", "helmet"), ("하네스", "harness"), ("안전고리", "hook"))


def setup(app_name: str, icon: str) -> None:
    """페이지 설정. 스크립트에서 가장 먼저 부른다.

    initial_sidebar_state="locked": 브라우저가 "접힘" 상태를 localStorage 에 기억해 expanded 설정을 무시하므로,
    넓은 화면에선 아예 접을 수 없게 고정. 좁은 화면(모바일 폭)에선 auto 처럼 동작 → 펼침 버튼은 CSS 에서 살려 둔다.
    """
    _APP["name"] = app_name
    st.set_page_config(page_title=f"{ORG} {app_name}", page_icon=icon, layout="wide", initial_sidebar_state="locked")


def class_marks(r: dict, thresholds: dict) -> str:
    """점수·임계값 → 임계 미달(❌) 클래스만 '안전고리 ❌' (엣지 decision_engine 과 같은 기준). 전부 통과면 ''.

    3개 클래스를 ⭕/❌ 로 다 늘어놓으면 한눈에 안 들어와서, 걸린 것만 보여준다. 점수 없는 클래스도 생략.
    """
    out = [f"{label} ❌" for label, key in CLASSES
           if r.get(key) is not None and r[key] < thresholds.get(f"{key}_score", 0.5)]
    return " · ".join(out)


CSS = """
<style>
/* Streamlit 기본 장식 제거. stToolbar 자체는 숨기면 안 됨 — 사이드바 펼침 버튼(stExpandSidebarButton)이 그 안에 있다 */
#MainMenu, footer, [data-testid="stMainMenu"], [data-testid="stToolbarActions"], [data-testid="stDecoration"], [data-testid="stStatusWidget"],
.stDeployButton, [data-testid="stAppDeployButton"] { display:none !important; }
header[data-testid="stHeader"] { background:transparent; pointer-events:none; }
[data-testid="stExpandSidebarButton"] { pointer-events:auto; }
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
/* 액션 패널: 이벤트 카드 + 영상 + 판정 버튼 + 메모를 한 박스로 묶음. 미검수=하늘색, 판정하면 그 색으로 */
.st-key-action_panel { background:#EFF6FF; border:1px solid #BFDBFE; border-radius:12px; padding:16px 18px 12px; margin:4px 0 14px; }
.st-key-action_panel .pm-ev { border-color:#BFDBFE; }
.st-key-action_panel [data-testid="stCaptionContainer"] { color:#1E3A8A; }
.st-key-action_panel_tp { background:#F0FDF4; border-color:#BBF7D0; }
.st-key-action_panel_tp .pm-ev { border-color:#BBF7D0; }
.st-key-action_panel_tp [data-testid="stCaptionContainer"] { color:#166534; }
.st-key-action_panel_fp { background:#FEF2F2; border-color:#FECACA; }
.st-key-action_panel_fp .pm-ev { border-color:#FECACA; }
.st-key-action_panel_fp [data-testid="stCaptionContainer"] { color:#991B1B; }
/* 미검수 + 출입 거부 이벤트 = 파스텔 노랑 */
.st-key-action_panel_deny { background:#FEFCE8; border-color:#FDE68A; }
.st-key-action_panel_deny .pm-ev { border-color:#FDE68A; }
.st-key-action_panel_deny [data-testid="stCaptionContainer"] { color:#854D0E; }
/* 그리드 타일 (2×2). 키: tile_{상태}_{i} 또는 tile_{i}
   미검수=하늘색 · 미검수 거부=파스텔 노랑 · 체크/틀림 표시(오탐 후보)=연빨강 · 제외 표시=회색 · 판정 끝난 것은 판정색 */
[class*="st-key-tile_"] { border:1px solid #BFDBFE; background:#EFF6FF; border-radius:10px; padding:10px 12px 8px; margin:0 0 10px; }
[class*="st-key-tile_deny_"] { background:#FEFCE8; border-color:#FDE68A; }
[class*="st-key-tile_fp_"] { background:#FEF2F2; border-color:#FCA5A5; box-shadow:0 0 0 2px #FECACA inset; }
[class*="st-key-tile_ex_"] { background:#F3F4F6; border-color:#D1D5DB; box-shadow:0 0 0 2px #E5E7EB inset; }
[class*="st-key-tile_done_tp_"] { background:#F0FDF4; border-color:#BBF7D0; box-shadow:none; }
[class*="st-key-tile_done_fp_"] { background:#FEF2F2; border-color:#FECACA; box-shadow:none; }
[class*="st-key-tile_done_ex_"] { background:#F3F4F6; border-color:#E5E7EB; box-shadow:none; }
/* 숫자키로 고른 타일: 다음 숫자(1~3)가 이 타일의 오탐 클래스로 들어간다 */
[data-bct-active] { outline:3px solid #2563EB; outline-offset:2px; }
.pm-tile-head { display:flex; align-items:center; gap:10px; font-size:13px; margin:0 0 4px; }
.pm-tile-head .num { display:inline-flex; align-items:center; justify-content:center; width:24px; height:24px; border-radius:6px; background:#2563EB; color:#fff; font-weight:700; font-size:13px; }
.pm-tile-head code { font-family:ui-monospace,Consolas,monospace; font-size:12px; background:#fff; padding:1px 6px; border-radius:3px; color:#1E3A8A; border:1px solid #E3E8EF; }
.pm-tile-head .v-tp { color:#16A34A; font-weight:700; } .pm-tile-head .v-fp { color:#DC2626; font-weight:700; } .pm-tile-head .muted { color:#6B7280; }
.pm-tile-sub { font-size:12.5px; color:#374151; margin:0 0 6px; }
/* 그리드 판정 버튼 */
.st-key-btn_grid_tp button { border-color:#16A34A; color:#16A34A; font-weight:700; }
.st-key-btn_grid_tp button:hover { background:#DCFCE7; }
.st-key-btn_grid_fp button { background:#DC2626; border-color:#DC2626; color:#fff; font-weight:700; }
.st-key-btn_grid_fp button:hover { background:#B91C1C; border-color:#B91C1C; }
/* 판정 버튼 */
.st-key-btn_tp button { border-color:#16A34A; color:#16A34A; font-weight:600; }
.st-key-btn_tp button:hover { background:#DCFCE7; }
.st-key-btn_fp button { background:#DC2626; border-color:#DC2626; color:#FFFFFF; font-weight:600; }
.st-key-btn_fp button:hover { background:#B91C1C; border-color:#B91C1C; }
.st-key-btn_skip button, .st-key-btn_undo button { color:#4B5563; }
/* 지표 타일 */
.pm-kpis { display:grid; grid-template-columns:repeat(4, minmax(0, 1fr)); gap:10px; margin:6px 0 14px; }
.pm-kpi { border:1px solid #E3E8EF; border-radius:10px; padding:12px 14px; background:#fff; }
.pm-kpi .k { font-size:12px; color:#6B7280; font-weight:600; }
.pm-kpi .v { font-size:26px; font-weight:700; line-height:1.25; margin-top:2px; font-variant-numeric:tabular-nums; color:#111827; }
.pm-kpi .s { font-size:12px; color:#6B7280; }
.pm-kpi.ok .v { color:#16A34A; } .pm-kpi.bad .v { color:#DC2626; } .pm-kpi.accent .v { color:#2563EB; }
@media (max-width: 800px) { .pm-kpis { grid-template-columns:repeat(2, minmax(0, 1fr)); } }
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
        f'<div class="pm-head"><span class="eyebrow">{ORG}</span><h1>{_APP["name"]}</h1><span class="crumb">{crumb}</span></div>',
        unsafe_allow_html=True,
    )


def pills(items: list[tuple[str, str, str]]) -> None:
    """[(라벨, 값, css클래스)] → 알약 한 줄."""
    html = "".join(f'<span class="pm-pill {cls}">{lab} <b>{val}</b></span>' for lab, val, cls in items)
    st.markdown(f'<div class="pm-pills">{html}</div>', unsafe_allow_html=True)


def labeled_pills(label: str, options: list[str], format_func, key: str) -> list[str]:
    """'오탐 내역  [1 안전모] [2 하네스] [3 안전고리]' — 칩 앞에 무엇을 고르는 칩인지 이름을 붙인 다중 선택."""
    c1, c2 = st.columns([1, 6], vertical_alignment="center", gap="small")
    with c1:
        # 마크다운 블록 아래 여백만큼 글자가 칩보다 내려가 보여서 살짝 올린다
        st.markdown(f'<div style="font-size:13px;font-weight:700;color:#374151;white-space:nowrap;position:relative;top:-7px">{label}</div>',
                    unsafe_allow_html=True)
    with c2:
        return st.pills(label, options, selection_mode="multi", format_func=format_func, key=key, label_visibility="collapsed") or []


def kpis(items: list[tuple[str, str, str, str]]) -> None:
    """[(제목, 값, 보조문구, css클래스)] → 지표 타일 한 줄."""
    html = "".join(f'<div class="pm-kpi {cls}"><div class="k">{k}</div><div class="v">{v}</div><div class="s">{s}</div></div>'
                   for k, v, s, cls in items)
    st.markdown(f'<div class="pm-kpis">{html}</div>', unsafe_allow_html=True)


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


def video(site, ev: Event, role: str, label_role: bool = True):
    try:
        path, label = playable_path(site, ev, role)
    except Exception:
        path, label = None, "영상을 불러오지 못했습니다"
    if label_role:
        st.caption(f"**{role}** · {label}")
    if path:
        st.video(str(path), autoplay=True, loop=True, muted=True)
    else:
        st.warning("영상 없음")


# ══════════════════════════════════════════════════════════════════════════
# 페이지 스크립트 (단축키 · 재생 속도 · 자동재생 복구 · 표 더블클릭)
# ══════════════════════════════════════════════════════════════════════════
# 페이지에 직접 심는 헬퍼 스크립트 (iframe 없음). 매 렌더마다 실행돼도 안전하도록 멱등으로 짠다:
#   · 키맵/속도는 항상 최신 값으로 덮어쓴다
#   · 리스너·감시자는 한 번만 설치하되, 같은 문서에서 이미 설치됐어도 페이지 교체 후엔 다시 설치된다
#   · 브라우저가 자동재생을 막아 멈춘 <video> 는 다시 play() 시도 (동시 8개일 때 일부만 재생되는 경우)
# 키맵 값: 버튼 라벨 앞부분(그 버튼 클릭) 또는 '☐ n'(라벨이 '☐ n'/'☑ n' 으로 시작하는 체크박스 토글)
# 클래스 고르기(pick) — 숫자키가 키맵보다 먼저 여기로 온다:
#   grid   : 대기 상태에서 n(1~tiles) = 타일 n 을 '활성'(파란 테두리)으로. 체크 안 된 타일이면 체크도 한다
#            활성 상태에서 k(1~n) = 그 타일의 k번째 칩(오탐 클래스) 토글 후 대기로 복귀
#            Backspace/Delete = 활성 타일 체크 해제 · Esc/Enter = 고르지 않고 복귀
#   single : k(1~n) = 액션 패널의 k번째 칩 토글
#   칩은 체크 직후 재실행이 끝나야 생기므로 잠깐 기다렸다가 누른다. 묶음(batch)이 바뀌면 활성은 풀린다.
_HELPER_JS = """
<script>
(function(){
  const w = window;
  w.__bctRate   = __RATE__;
  w.__bctKeyMap = __KEYMAP__;
  w.__bctPick   = __PICK__;
  const batch = w.__bctPick ? w.__bctPick.batch : null;
  if (batch !== w.__bctPickBatch) { w.__bctActive = null; w.__bctPickBatch = batch; }

  const tileOf = (n) => [...document.querySelectorAll('[class*="st-key-tile_"]')]
    .find(el => [...el.classList].some(c => c.startsWith('st-key-tile_') && c.endsWith('_' + (n - 1))));
  const chkOf = (n) => {
    const l = [...document.querySelectorAll('label')].find(l => { const m = (l.innerText || '').trim().match(/^[☐☑]\\s*(\\d)/); return m && m[1] === String(n); });
    return l ? (l.querySelector('input[type=checkbox]') || l) : null;
  };
  const chips = (scope) => scope ? [...scope.querySelectorAll('[data-testid="stButtonGroup"] button')] : [];
  const markActive = () => {
    document.querySelectorAll('[data-bct-active]').forEach(el => el.removeAttribute('data-bct-active'));
    if (w.__bctActive) { const t = tileOf(w.__bctActive); if (t) t.setAttribute('data-bct-active', '1'); }
  };
  const clickWhenReady = (get, i) => {
    const t0 = Date.now();
    const tick = () => { const b = get()[i]; if (b) b.click(); else if (Date.now() - t0 < 3000) setTimeout(tick, 80); };
    tick();
  };
  w.__bctOnPick = (e) => {
    const P = w.__bctPick; if (!P) return false;
    if (e.key === 'Escape' || e.key === 'Enter' || e.key === 'Backspace' || e.key === 'Delete') {
      if (!w.__bctActive) return false;
      if (e.key === 'Backspace' || e.key === 'Delete') { const c = chkOf(w.__bctActive); if (c && c.checked) c.click(); }
      w.__bctActive = null; markActive(); e.preventDefault(); return true;
    }
    if (!/^[1-9]$/.test(e.key)) { if ('pPnNzZ'.includes(e.key)) { w.__bctActive = null; markActive(); } return false; }
    const d = Number(e.key);
    if (P.mode === 'single') {
      if (d > P.n) return false;
      e.preventDefault(); clickWhenReady(() => chips(document.querySelector('[class*="st-key-action_panel"]')), d - 1); return true;
    }
    if (w.__bctActive) {
      if (d > P.n) return true;
      e.preventDefault();
      const a = w.__bctActive; w.__bctActive = null; markActive();
      clickWhenReady(() => chips(tileOf(a)), d - 1); return true;
    }
    if (d > P.tiles) return false;
    const c = chkOf(d); if (!c) return true;
    e.preventDefault();
    if (!c.checked) c.click();                                    // 이미 체크된 타일(저장된 오탐 등)은 그대로 두고 항목만 고르게
    w.__bctActive = d; markActive(); return true;
  };
  w.__bctMarkActive = () => {
    if (w.__bctActive) { const c = chkOf(w.__bctActive); if (c && c.type === 'checkbox' && !c.checked) w.__bctActive = null; }
    markActive();
  };

  const apply = () => {
    const vids = document.querySelectorAll('video');
    vids.forEach(v => {
      if (w.__bctRate && v.playbackRate !== w.__bctRate) v.playbackRate = w.__bctRate;
      // 모드 전환 등으로 다시 만들어진 플레이어는 autoplay 속성 없이 오기도 한다 → 속성과 무관하게 우리가 관리
      if (!v.__bctManaged) { v.__bctManaged = true; v.loop = true; v.muted = true; }
      if (v.paused && !v.ended && !v.__bctUserPaused && v.readyState >= 2) {
        const pr = v.play(); if (pr && pr.catch) pr.catch(() => {});
      }
    });
    if (w.__bctMarkActive) w.__bctMarkActive();          // 재렌더로 타일이 다시 그려져도 활성 테두리 유지
  };
  w.__bctApply = apply;

  if (!w.__bctInstalled) {
    w.__bctInstalled = true;
    // 사용자가 컨트롤로 직접 멈춘 영상만 기억 (우리 play() 실패로 생기는 pause 는 __bctManaged 직후라 제외)
    document.addEventListener('pause', e => {
      const v = e.target; if (!v || v.tagName !== 'VIDEO' || v.ended) return;
      if (v.readyState >= 3 && v.currentTime > 0.2) v.__bctUserPaused = true;
    }, true);
    document.addEventListener('play',  e => { const v = e.target; if (v && v.tagName === 'VIDEO') { v.__bctUserPaused = false; apply(); } }, true);
    ['loadedmetadata', 'canplay'].forEach(ev => document.addEventListener(ev, e => { if (e.target && e.target.tagName === 'VIDEO') apply(); }, true));
    new MutationObserver(() => apply()).observe(document.body, {childList: true, subtree: true});
    setInterval(apply, 800);

    document.addEventListener('keydown', (e) => {
      const tag = (e.target && e.target.tagName) || '';
      // 글자 입력칸에서만 무시 (마우스로 누른 체크박스에 포커스가 남아도 단축키는 동작)
      const typing = (tag === 'INPUT' && !['checkbox', 'radio', 'button'].includes(e.target.type)) || tag === 'TEXTAREA' || tag === 'SELECT';
      if (typing || (e.target && e.target.isContentEditable)) return;
      if (e.ctrlKey || e.metaKey || e.altKey) return;
      if (w.__bctOnPick && w.__bctOnPick(e)) return;
      const label = (w.__bctKeyMap || {})[e.key]; if (!label) return;
      let el = null;
      if (label.startsWith('☐ ')) {
        const n = label.slice(2);
        el = [...document.querySelectorAll('label')].find(l => { const m = (l.innerText || '').trim().match(/^[☐☑]\\s*(\\d)/); return m && m[1] === n; });
        if (el) { const inp = el.querySelector('input[type=checkbox]'); if (inp) el = inp; }
      } else {
        el = [...document.querySelectorAll('button')].find(b => (b.innerText || '').trim().startsWith(label));
      }
      if (el) { e.preventDefault(); el.click(); }
    }, true);
  }
  apply();
})();
</script>
"""


def _inject_script(html: str) -> None:
    try:
        st.html(html, unsafe_allow_javascript=True)
    except TypeError:                      # 구버전 Streamlit: 파라미터 없음 → 컴포넌트 iframe 으로 폴백
        components.html(html.replace("const w = window;", "const w = window.parent; const document = w.document;"), height=0)
    except Exception:                      # 헤드리스 테스트 등
        pass


def inject_helpers(keymap: dict[str, str], rate: float, pick: dict | None = None) -> None:
    """키보드 단축키 + 재생 속도 + 자동재생 복구 스크립트를 페이지에 심는다 (컴포넌트 iframe 없음).

    pick: 숫자키로 오탐 클래스 칩 고르기. {"mode": "grid", "tiles": 4, "n": 3, "batch": "..."} 또는 {"mode": "single", "n": 3}
    """
    _inject_script(_HELPER_JS.replace("__RATE__", str(float(rate)))
                   .replace("__KEYMAP__", json.dumps(keymap, ensure_ascii=False))
                   .replace("__PICK__", json.dumps(pick, ensure_ascii=False)))


# 표를 더블클릭하면 지정한 버튼(라벨 앞부분)을 대신 누른다.
#   · 표는 캔버스라 두 번째 클릭에서 셀 편집 오버레이가 떠 dblclick 의 대상이 캔버스가 아니게 된다
#     → dblclick 대신 캔버스 위 pointerdown 두 번(가까운 시간·위치)을 직접 잡는다 (캡처 단계라 표보다 먼저 받음).
#   · 첫 클릭이 일으킨 재실행(선택 반영)이 끝난 뒤에 눌러야 방금 고른 행으로 열리므로, 앱이 쉴 때까지 기다린다.
_TABLE_DBLCLICK_JS = """
<script>
(function(){
  const w = window;
  w.__bctDblBtn = __LABEL__;
  if (w.__bctDblInstalled) return;
  w.__bctDblInstalled = true;
  let last = null;
  const findBtn = () => w.__bctDblBtn && [...document.querySelectorAll('button')].find(b => (b.innerText || '').trim().startsWith(w.__bctDblBtn));
  document.addEventListener('pointerdown', (e) => {
    const t = e.target;
    if (e.button !== 0 || !t || t.tagName !== 'CANVAS' || !t.closest('[data-testid="stDataFrame"]')) { last = null; return; }
    if (e.clientY - t.getBoundingClientRect().top < 36) { last = null; return; }   // 헤더(열 이름)는 무시
    const now = Date.now();
    const dbl = last && now - last.t < 450 && Math.abs(e.clientX - last.x) < 8 && Math.abs(e.clientY - last.y) < 8;
    last = dbl ? null : {t: now, x: e.clientX, y: e.clientY};
    if (!dbl || !findBtn()) return;                               // 그 버튼이 없는 화면에서는 아무것도 안 함
    e.preventDefault(); e.stopPropagation();                      // 두 번째 클릭으로 셀 편집 오버레이가 뜨지 않게
    const started = Date.now();
    const tick = () => {
      const app = document.querySelector('[data-testid="stApp"]');
      const busy = app && app.getAttribute('data-test-script-state') === 'running';
      if (busy && Date.now() - started < 8000) { setTimeout(tick, 80); return; }
      const b = findBtn(); if (b) b.click();
    };
    setTimeout(tick, 150);
  }, true);
})();
</script>
"""


def inject_table_dblclick(button_label: str) -> None:
    _inject_script(_TABLE_DBLCLICK_JS.replace("__LABEL__", json.dumps(button_label, ensure_ascii=False)))


def table_pick(options: list[str], pick_key: str, tbl_key: str, default: str,
               to_widget=lambda s: s, from_widget=lambda v: v, clamp=lambda v: v) -> str:
    """표(행 = options) ↔ 선택 위젯(pick_key) 동기화. 위젯·표를 그리기 전에 부른다. 현재 선택값(문자열)을 돌려준다.

    표에서 행(또는 칸)을 누르면 그 값으로 선택 위젯이 바뀌고, 위젯을 직접 바꾸면 표의 해당 행이 선택된다.
    둘 다 위젯이라 "이번 실행에서 무엇이 바뀌었나"는 표에서 마지막으로 반영한 값(last)과 비교해 가린다.
    위젯 값이 문자열이 아니면(date_input 등) to_widget/from_widget 으로 바꾸고, clamp 로 허용 범위에 넣는다.
    표는 st.dataframe(key=tbl_key, on_select="rerun", selection_mode=["single-row", "single-cell"]) 로 그린다.
    """
    last_key = f"{tbl_key}__last"
    if pick_key not in st.session_state:
        st.session_state[pick_key] = to_widget(default)
    sel = (st.session_state.get(tbl_key) or {}).get("selection") or {}
    clicked = [c[0] for c in (sel.get("cells") or [])] or list(sel.get("rows") or [])
    clicked_val = options[clicked[0]] if clicked and 0 <= clicked[0] < len(options) else None
    if clicked_val and clicked_val != st.session_state.get(last_key):
        st.session_state[pick_key] = to_widget(clicked_val)
    st.session_state[pick_key] = clamp(st.session_state[pick_key])
    cur = from_widget(st.session_state[pick_key])
    st.session_state[tbl_key] = {"selection": {"rows": [options.index(cur)] if cur in options else [], "columns": [], "cells": []}}
    st.session_state[last_key] = cur
    return cur


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
    """저장 루트에 _logins.jsonl 한 줄 추가 (누가·어디서·언제·어느 앱)."""
    try:
        root, _ = out_root()
        root.mkdir(parents=True, exist_ok=True)
        rec = {"at": datetime.now().isoformat(timespec="seconds"), "id": user["id"], "ip": user["ip"], "app": _APP["name"]}
        with open(root / "_logins.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
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
            # 폼으로 묶으면 어느 칸에서든 Enter 로 제출되고, 입력값이 제출 시점에 함께 확정된다.
            # (버튼만 쓰면 값 확정과 클릭이 따로 처리돼 방금 친 값이 반영되지 않는 경우가 있다)
            with st.form("login_form", border=False, enter_to_submit=True):
                uid = st.text_input("ID", key="login_id", autocomplete="username")
                pw = st.text_input("비밀번호", type="password", key="login_pw", autocomplete="current-password")
                submitted = st.form_submit_button("로그인", key="btn_login", type="primary", width="stretch")
            if submitted:
                u = verify_user(st_, uid, pw)
                if u:
                    u["ip"] = client_ip()
                    u["at"] = datetime.now().isoformat(timespec="seconds")
                    st.session_state.user = u
                    st.session_state.pop("login_err", None)
                    log_login(u)
                    st.rerun()
                st.session_state.login_err = True
            if st.session_state.get("login_err"):
                st.error("ID 또는 비밀번호가 맞지 않습니다.")


def sidebar_user(clear_keys: tuple[str, ...], sync_all: bool = False):
    """사이드바 맨 위 사용자·로그아웃. 로그아웃하면 user 와 clear_keys 를 지운다. sync_all 이면 전량 업데이트 버튼도."""
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
                for k in ("user",) + tuple(clear_keys):
                    st.session_state.pop(k, None)
                st.rerun()
        if sync_all:
            if st.button("☁️ 검수 자료 전량 업데이트", key="btn_sync_all", width="stretch"):
                run_sync_all()
            msg = st.session_state.get("sync_all_msg")
            if msg:
                (st.success if msg["ok"] else st.error)(msg["text"])
                if msg.get("detail"):
                    with st.expander("자세히"):
                        st.code(msg["detail"])
        st.divider()


# ══════════════════════════════════════════════════════════════════════════
# NAS 반영 (오탐 영상) · 모델 정보
# ══════════════════════════════════════════════════════════════════════════
def reload_session() -> None:
    """앱이 들고 있는 오탐 검수 세션을 디스크에서 다시 읽는다 (NAS 반영이 session.json 의 exported 를 고친 뒤)."""
    from review.session import Session
    s = st.session_state.get("sess")
    if isinstance(s, Session) and s.path.exists():
        st.session_state.sess = Session(s.path, json.loads(s.path.read_text(encoding="utf-8")))


def run_sync_all() -> None:
    """모든 현장·일자의 검수 기록 중 NAS 에 반영 안 된 오탐 영상을 한 번에 받고, 필요 없어진 영상은 정리한다."""
    from review import export

    st_ = settings()
    root, src = out_root()
    with st.spinner("검수 자료를 확인하는 중…"):
        plans = export.plan_all(root, st_.sites.values())
    if not plans:
        st.session_state.sync_all_msg = {"ok": True, "text": "모든 검수 자료가 반영돼 있습니다."}
        return
    bar = st.progress(0.0, text="반영 중…")
    total = sum(max(1, p.n_events) for p in plans)
    done, got, moved, removed, missing, errors = 0, 0, 0, 0, [], []
    for p in plans:
        site = st_.site(p.site)
        base = done
        try:
            c = client(site) if p.download else None
            res = export.apply_plan(p, c, site.minio_bucket, site.cameras,
                                    progress=lambda i, n, eid: bar.progress(min(1.0, (base + i) / total), text=f"{p.date} · {eid}"))
            got += res.downloaded; moved += res.moved; removed += res.removed; missing += res.missing
        except (Exception, SystemExit) as e:           # 현장 하나가 안 닿아도 나머지는 진행
            errors.append(f"{p.site} {p.date}: {type(e).__name__}: {e}")
        done = base + max(1, p.n_events)
        bar.progress(min(1.0, done / total))
    bar.empty()
    reload_session()
    days = len({(p.site, p.date) for p in plans})
    text = f"{days}일치 반영 · 받음 {got} · 옮김 {moved} · 정리 {removed}" + (f" · 원본 없음 {len(missing)}" if missing else "")
    detail = "\n".join(errors + [f"원본 없음: {m}" for m in missing])
    if errors:
        text = f"일부를 반영하지 못했습니다 ({len(errors)}일). " + text
    st.session_state.sync_all_msg = {"ok": not errors, "text": text, "detail": detail}


_MODEL_REFRESH: dict[str, float] = {}               # 현장별 마지막 갱신 시도 시각 (서버 프로세스 전체 공유)


def model_label(site) -> str:
    """현장 탐지 모델 이름 ('PPE 260827ppe2 · Hook 260828hook'). 저장된 정보가 6시간보다 오래되면 뒤에서 갱신."""
    import threading
    import time
    from review import models

    root, _ = out_root()
    info = models.load(root, site.code)
    try:
        age = (datetime.now() - datetime.fromisoformat(info["checked_at"])).total_seconds() if info else None
    except (KeyError, ValueError):
        age = None
    if (age is None or age > 6 * 3600) and time.time() - _MODEL_REFRESH.get(site.code, 0) > 1800:
        _MODEL_REFRESH[site.code] = time.time()

        def _bg(st_=settings(), site_=site, root_=root):
            try:
                models.refresh(st_, site_, root_)
            except Exception:                           # SSH 키가 없는 PC 등 — 저장된 정보만 쓴다
                pass
        threading.Thread(target=_bg, daemon=True).start()
    return models.label(info)
