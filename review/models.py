"""현장 탐지 모델 정보 — 엣지노드 SSH 로 모델 파일 해시와 체크포인트 메타데이터(학습 이름·기반 모델·학습일)를 읽는다.

    {저장루트}/_config/models/{SITE}.json
        checked_at, nodes: {node: {"ppe": md5, "hook": md5}}, models: {md5: {role, name, base, trained, dataset, ultralytics, file}}

  · 엣지 compose.yml 의 MODEL_PATH(/weights/*.pt) 를 노드별로 md5 한다 (빠름).
  · 처음 보는 해시만 litserver 컨테이너의 python 으로 체크포인트를 열어 train_args.name 등을 읽는다 (느림, 모델 교체 때만).
  · 검수자 PC 에 SSH 키가 없으면 갱신은 실패하고, 저장된 파일만 보여준다.

python -m review models HANIL
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

META_SCRIPT = r'''
import json, sys, torch
out = {}
for p in sys.argv[1:]:
    ck = torch.load(p, map_location="cpu", weights_only=False)
    ta = ck.get("train_args") or {}
    m = ck.get("model") or ck.get("ema")
    out[p] = {"name": ta.get("name"), "base": str(ta.get("model") or ""), "trained": str(ck.get("date") or ""),
              "dataset": str(ta.get("data") or ""), "ultralytics": str(ck.get("version") or ""),
              "classes": list((getattr(m, "names", None) or {}).values())}
print(json.dumps(out))
'''


def models_path(root: Path, site_code: str) -> Path:
    return Path(root) / "_config" / "models" / f"{site_code}.json"


def load(root: Path, site_code: str) -> dict | None:
    try:
        return json.loads(models_path(root, site_code).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def summary(info: dict | None) -> dict[str, list[str]]:
    """{role: [모델 이름...]} — 노드마다 다르면 여러 개."""
    out: dict[str, list[str]] = {}
    if not info:
        return out
    models = info.get("models") or {}
    for node in (info.get("nodes") or {}).values():
        for role, md5 in node.items():
            name = (models.get(md5) or {}).get("name") or md5[:8]
            if name not in out.setdefault(role, []):
                out[role].append(name)
    return out


def label(info: dict | None) -> str:
    s = summary(info)
    if not s:
        return ""
    order = sorted(s, key=lambda r: {"ppe": 0, "hook": 1}.get(r, 9))
    return " · ".join(f"{r.upper() if r == 'ppe' else r.capitalize()} {s[r][0]}" + (f" 외 {len(s[r]) - 1}" if len(s[r]) > 1 else "")
                      for r in order)


def _role_of(path: str) -> str:
    base = os.path.basename(path).lower()
    return "hook" if "hook" in base else ("ppe" if "ppe" in base else base.split(".")[0])


def _ssh(user: str, key: str, ip: str, cmd: str, stdin: str | None = None, timeout: int = 60) -> str:
    creation = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", "-o", "StrictHostKeyChecking=accept-new",
                        "-i", key, f"{user}@{ip}", cmd], input=stdin, capture_output=True, text=True, timeout=timeout,
                       encoding="utf-8", errors="replace", creationflags=creation)
    if r.returncode != 0:
        raise RuntimeError(f"ssh {ip}: {(r.stderr or r.stdout).strip()[:200]}")
    return r.stdout


def refresh(settings, site, root: Path) -> dict:
    """노드들을 조회해 모델 정보 파일을 갱신하고 그 내용을 돌려준다."""
    nodes_cfg = json.loads((settings.cfg_dir / "nodes.json").read_text(encoding="utf-8"))
    ip_of = {n["name"]: n["ip"] for n in nodes_cfg.get("nodes", [])}
    app_dir = os.path.dirname((nodes_cfg.get("transfer") or {}).get("remoteBaseDir", "/home/paimedialab/mithril_ai_server/results").rstrip("/"))
    user = site.tunnel.get("ssh_user", "paimedialab")
    key = os.path.expanduser(site.tunnel.get("ssh_key", "~/.ssh/bct_edge"))
    names = sorted({v.split("/")[0] for v in site.bcts.values() if v.split("/")[0] in ip_of})

    def probe(node: str):
        out = _ssh(user, key, ip_of[node],
                   f"cd {app_dir} && for p in $(grep -oE 'MODEL_PATH=/weights/[A-Za-z0-9_.-]+' compose.yml | cut -d= -f2); "
                   f"do md5sum \"weights/${{p#/weights/}}\" | sed \"s#weights/#/weights/#\"; done")
        return node, {_role_of(path): md5 for md5, path in (line.split() for line in out.splitlines() if line.strip())}, \
            {md5: path for md5, path in (line.split() for line in out.splitlines() if line.strip())}

    prev = load(root, site.code) or {}
    models = dict(prev.get("models") or {})
    nodes, errors, paths = {}, {}, {}
    with ThreadPoolExecutor(max_workers=max(1, len(names))) as ex:
        for fut, node in [(ex.submit(probe, n), n) for n in names]:
            try:
                _, roles, p = fut.result()
                nodes[node] = roles
                for md5, path in p.items():
                    paths.setdefault(md5, (node, path))
            except Exception as e:                       # 노드 하나가 꺼져 있어도 나머지로 갱신
                errors[node] = str(e)
    unknown = {md5: np for md5, np in paths.items() if md5 not in models}
    if unknown:
        by_node: dict[str, list[tuple[str, str]]] = {}
        for md5, (node, path) in unknown.items():
            by_node.setdefault(node, []).append((md5, path))
        for node, items in by_node.items():
            try:
                raw = _ssh(user, key, ip_of[node], "docker exec -i litserver python3 - " + " ".join(p for _, p in items),
                           stdin=META_SCRIPT, timeout=180)
                meta = json.loads(raw.strip().splitlines()[-1])
                for md5, path in items:
                    models[md5] = {"role": _role_of(path), "file": os.path.basename(path), **(meta.get(path) or {})}
            except Exception as e:
                errors[f"{node} (메타데이터)"] = str(e)
                for md5, path in items:
                    models.setdefault(md5, {"role": _role_of(path), "file": os.path.basename(path), "name": None})
    if not nodes:
        raise RuntimeError("; ".join(f"{k}: {v}" for k, v in errors.items()) or "조회할 노드가 없습니다")
    info = {"site": site.code, "checked_at": datetime.now().isoformat(timespec="seconds"), "nodes": nodes,
            "models": models, "errors": errors}
    p = models_path(root, site.code)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)
    return info
