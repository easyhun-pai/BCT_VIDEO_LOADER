"""로그인 계정 — users 파일에 PBKDF2-HMAC-SHA256 해시로 보관 (추가 패키지 없음).

찾는 순서: BCT_REVIEW_USERS 환경변수 > NAS {nas_root}/_config/users.json > config/users.local.json
계정 추가:  python -m review user add admin
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from datetime import datetime
from pathlib import Path

from .config import Settings

ITERATIONS = 200_000


def users_path(st: Settings, for_write: bool = False) -> Path:
    env = os.environ.get("BCT_REVIEW_USERS")
    if env:
        return Path(env)
    if st.nas_root and os.path.isdir(st.nas_root):
        nas = Path(st.nas_root) / "_config" / "users.json"
        if nas.exists() or for_write and False:   # 쓰기 기본은 로컬. NAS 공유는 명시적으로 옮길 때만.
            return nas
    return st.cfg_dir / "users.local.json"


def _load(p: Path) -> dict:
    if not p.exists():
        return {"users": {}}
    d = json.loads(p.read_text(encoding="utf-8"))
    d.setdefault("users", {})
    return d


def _save(p: Path, d: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)


def _hash(password: str, salt: bytes, iterations: int) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)


def list_users(st: Settings) -> dict[str, dict]:
    return _load(users_path(st))["users"]


def set_password(st: Settings, user_id: str, password: str, name: str | None = None) -> Path:
    user_id = user_id.strip().lower()
    if not user_id or len(password) < 4:
        raise ValueError("ID 가 비었거나 비밀번호가 4자 미만입니다.")
    p = users_path(st, for_write=True)
    d = _load(p)
    salt = secrets.token_bytes(16)
    prev = d["users"].get(user_id, {})
    d["users"][user_id] = {
        "name": name or prev.get("name") or user_id,
        "salt": salt.hex(),
        "hash": _hash(password, salt, ITERATIONS).hex(),
        "iterations": ITERATIONS,
        "created_at": prev.get("created_at") or datetime.now().isoformat(timespec="seconds"),
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    _save(p, d)
    return p


def delete_user(st: Settings, user_id: str) -> bool:
    p = users_path(st, for_write=True)
    d = _load(p)
    if d["users"].pop(user_id.strip().lower(), None) is None:
        return False
    _save(p, d)
    return True


def verify_user(st: Settings, user_id: str, password: str) -> dict | None:
    """성공 시 {"id", "name"} 반환, 실패 시 None. 타이밍 공격 완화를 위해 항상 해시 계산."""
    users = list_users(st)
    u = users.get((user_id or "").strip().lower())
    salt = bytes.fromhex(u["salt"]) if u else secrets.token_bytes(16)
    iters = int(u.get("iterations", ITERATIONS)) if u else ITERATIONS
    calc = _hash(password or "", salt, iters)
    if u and hmac.compare_digest(calc, bytes.fromhex(u["hash"])):
        return {"id": user_id.strip().lower(), "name": u.get("name") or user_id}
    return None
