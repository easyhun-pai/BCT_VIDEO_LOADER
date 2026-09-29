"""sites.json + secrets.local.json 로드.

BCT_REVIEW_CONFIG 환경변수로 설정 디렉터리를 바꿀 수 있다 (P1 에서 NAS bct-review/_config/ 를 가리킬 때 사용).
"""
from __future__ import annotations

import json
import os
import re
import socket
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CFG_DIR = ROOT / "config"


def _load_json(p: Path) -> dict:
    return json.loads(p.read_text(encoding="utf-8"))


@dataclass
class Site:
    code: str
    name: str
    cameras: list[str]
    access: str                 # "direct" | "tunnel"
    server: dict
    tunnel: dict
    minio: dict
    influx: dict
    thresholds: dict
    bcts: dict
    secrets: dict = field(default_factory=dict)

    # ── 편의 접근자 ──
    @property
    def minio_bucket(self) -> str:
        return self.minio.get("bucket", "bct-clips")

    @property
    def tg_prefix(self) -> str:
        return self.minio.get("tg_prefix", "_tg/")

    @property
    def minio_secure(self) -> bool:
        return bool(self.minio.get("secure", False))

    @property
    def minio_access(self) -> str:
        return self.secrets.get("minio_access", "")

    @property
    def minio_secret(self) -> str:
        return self.secrets.get("minio_secret", "")

    @property
    def influx_token(self) -> str:
        return self.secrets.get("influx_token", "")

    def require_secrets(self) -> None:
        missing = [k for k in ("minio_access", "minio_secret") if not self.secrets.get(k)]
        if missing:
            raise SystemExit(
                f"[{self.code}] secrets.local.json 의 sites.{self.code} 에 {', '.join(missing)} 가 없습니다."
            )


@dataclass
class Settings:
    cfg_dir: Path
    nas_root: str
    local_fallback: str
    timezone: str
    sites: dict[str, Site]
    nas_secrets: dict = field(default_factory=dict)   # secrets.local.json 의 nas: {share, user, password}
    nas_last_error: str = ""                          # 마지막 NAS 연결 실패 사유 (UI 표시용)

    def site(self, code: str) -> Site:
        key = code.upper()
        if key not in self.sites:
            raise SystemExit(f"알 수 없는 현장: {code}  (등록된 현장: {', '.join(self.sites)})")
        return self.sites[key]

    def _try_connect_nas(self) -> bool:
        """NAS 경로가 안 보이면 secrets.nas 자격으로 SMB 연결을 한 번 시도한다 (Windows net use)."""
        nas = self.nas_secrets
        if not (nas.get("share") and nas.get("user")) or os.name != "nt":
            self.nas_last_error = "secrets.local.json 에 nas.share / nas.user 가 없음"
            return False
        import subprocess, time
        # 연속 재시도 방지: 60초 쿨다운 (앱은 렌더마다 이 함수를 부른다). 강제 재시도는 force_nas_retry().
        if time.time() - getattr(self, "_nas_tried_at", 0.0) < 60:
            return False
        self._nas_tried_at = time.time()
        try:
            r = subprocess.run(["net", "use", nas["share"], nas.get("password", ""), f"/user:{nas['user']}", "/persistent:yes"],
                               capture_output=True, text=True, errors="replace", timeout=20)
            msg = (r.stdout + r.stderr).replace(nas.get("password", ""), "***").strip()
            self.nas_last_error = "" if r.returncode == 0 else f"net use rc={r.returncode}: {msg[:200]}"
        except Exception as e:
            self.nas_last_error = f"net use 실행 실패: {e}"
            return False
        ok = os.path.isdir(self.nas_root)
        if not ok and not self.nas_last_error:
            self.nas_last_error = f"연결은 됐으나 경로가 없음: {self.nas_root}"
        return ok

    def force_nas_retry(self) -> None:
        """'NAS 다시 연결' 버튼: 쿨다운과 기억해 둔 저장 루트를 지워 다음 호출에서 다시 확인한다."""
        self._nas_tried_at = 0.0
        self._root_cache = None

    def _nas_reachable(self, timeout: float = 2.0) -> bool:
        """NAS 호스트의 SMB 포트(445)에 TCP 로 닿는지 먼저 본다. 안 닿으면 os.path.isdir / net use 는 20초씩 걸리므로 건너뛴다."""
        m = re.match("^" + chr(92) * 4 + "([^" + chr(92) * 2 + "]+)", self.nas_root or "")   # UNC \host\share 의 host
        if not m:
            return True                                   # UNC 가 아니면 (로컬 경로 등) 그냥 확인
        try:
            with socket.create_connection((m.group(1), 445), timeout=timeout):
                return True
        except OSError as e:
            self.nas_last_error = f"NAS {m.group(1)}:445 에 닿지 않음 ({e.__class__.__name__})"
            return False

    def _probe_root(self) -> tuple[Path, str]:
        """실제 확인 (느릴 수 있음): NAS 포트 → 경로 → net use → 로컬 폴백."""
        if self.nas_root and self._nas_reachable() and (os.path.isdir(self.nas_root) or self._try_connect_nas()):
            return Path(self.nas_root), "NAS"
        fb = Path(self.local_fallback)
        if not fb.is_absolute():
            fb = (self.cfg_dir / fb).resolve()
        return fb, "local_fallback"

    def _refresh_root_bg(self) -> None:
        try:
            res = self._probe_root()
            self._root_cache = (time.time(), res[0], res[1])
        finally:
            self._root_refreshing = False

    ROOT_RECHECK_SEC = 300

    def resolve_out_root(self, override: str | None = None) -> tuple[Path, str]:
        """저장 루트 결정: --out > NAS(접근 가능할 때, 필요하면 자동 연결) > 로컬 폴백. (경로, 출처) 반환.

        결과는 기억해 둔다 — 웹앱은 렌더마다 부르는데 NAS 가 안 닿는 PC 에서는 확인 한 번이 20초씩 걸렸다 (2026-09-27).
        로컬 폴백으로 정해진 뒤에는 5분마다 뒤(스레드)에서만 다시 확인하고, 렌더는 절대 막지 않는다. 강제 재확인은 force_nas_retry().
        """
        if override:
            return Path(override), "--out"
        c = getattr(self, "_root_cache", None)
        if c is not None:
            at, path, src = c
            if src != "NAS" and time.time() - at > self.ROOT_RECHECK_SEC and not getattr(self, "_root_refreshing", False):
                self._root_refreshing = True
                threading.Thread(target=self._refresh_root_bg, daemon=True).start()
            return path, src
        path, src = self._probe_root()
        self._root_cache = (time.time(), path, src)
        return path, src


def load(cfg_dir: str | os.PathLike | None = None) -> Settings:
    d = Path(cfg_dir or os.environ.get("BCT_REVIEW_CONFIG") or DEFAULT_CFG_DIR)
    sites_path = d / "sites.json"
    if not sites_path.exists():
        raise SystemExit(f"설정 파일이 없습니다: {sites_path}")
    cfg = _load_json(sites_path)

    sec_path = d / "secrets.local.json"
    sec = _load_json(sec_path) if sec_path.exists() else {}
    site_secrets = sec.get("sites", {})

    sites: dict[str, Site] = {}
    for s in cfg.get("sites", []):
        code = s["code"].upper()
        sites[code] = Site(
            code=code,
            name=s.get("name", code),
            cameras=list(s.get("cameras", ["hook", "ppe"])),
            access=s.get("access", "tunnel"),
            server=s.get("server", {}),
            tunnel=s.get("tunnel", {}),
            minio=s.get("minio", {}),
            influx=s.get("influx", {}),
            thresholds={k: v for k, v in s.get("thresholds", {}).items() if not k.startswith("_")},
            bcts=s.get("bcts", {}),
            secrets=site_secrets.get(code, {}),
        )
    return Settings(
        cfg_dir=d,
        nas_root=cfg.get("nas_root", ""),
        local_fallback=cfg.get("local_fallback", "../data/review"),
        timezone=cfg.get("timezone", "+09:00"),
        sites=sites,
        nas_secrets=sec.get("nas", {}),
    )
