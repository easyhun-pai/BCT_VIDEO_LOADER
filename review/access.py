"""현장 접근 — direct(ZeroTier IP) 또는 tunnel(엣지노드 SSH 포트포워딩).

현장 서버(MinIO :9000, Influx :8086)는 LAN IP 라 ZeroTier 에서 바로 안 닿는 경우가 있다.
그때는 ZeroTier 로 닿는 엣지노드를 점프호스트로 써서 로컬 포트로 끌어온다.
어느 쪽이든 현장은 읽기만 한다.
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

from .config import Site


def _port_free(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.2)
        return s.connect_ex(("127.0.0.1", port)) != 0


def _wait_port(port: int, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket() as s:
            s.settimeout(0.5)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.25)
    return False


class SiteAccess:
    """with SiteAccess(site) as acc:  acc.minio_endpoint / acc.influx_url"""

    def __init__(self, site: Site, quiet: bool = False):
        self.site = site
        self.quiet = quiet
        self.proc: subprocess.Popen | None = None
        self.minio_endpoint = ""
        self.influx_url = ""
        self.mode = site.access

    # ── 진입/종료 ──
    def __enter__(self) -> "SiteAccess":
        srv = self.site.server
        mp, ip = int(srv.get("minio_port", 9000)), int(srv.get("influx_port", 8086))
        if self.mode == "direct":
            host = srv.get("zerotier_ip") or srv.get("lan_ip")
            if not host:
                raise SystemExit(f"[{self.site.code}] server.zerotier_ip 가 비어 있습니다 (direct 모드).")
            self.minio_endpoint = f"{host}:{mp}"
            self.influx_url = f"http://{host}:{ip}"
            self._log(f"direct  → {host}  (MinIO :{mp}, Influx :{ip})")
            return self
        self._start_tunnel(mp, ip)
        return self

    def __exit__(self, *exc) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None

    # ── 터널 ──
    def _start_tunnel(self, remote_minio: int, remote_influx: int) -> None:
        t, srv = self.site.tunnel, self.site.server
        lan = srv.get("lan_ip")
        if not lan:
            raise SystemExit(f"[{self.site.code}] server.lan_ip 가 비어 있습니다 (tunnel 모드).")
        jump, user = t.get("jump_host"), t.get("ssh_user", "paimedialab")
        key = os.path.expanduser(t.get("ssh_key", "~/.ssh/bct_edge"))
        port = int(t.get("ssh_port", 22))
        lm, li = int(t.get("local_minio_port", 19000)), int(t.get("local_influx_port", 18086))
        # 로컬 포트가 이미 쓰이면 다음 포트로 (이전 터널 잔류 등)
        while not _port_free(lm):
            lm += 1
        while not _port_free(li) or li == lm:
            li += 1

        cmd = [
            "ssh", "-N",
            "-o", "BatchMode=yes",
            "-o", "ExitOnForwardFailure=yes",
            "-o", "ConnectTimeout=10",
            "-o", "ServerAliveInterval=15",
            "-o", "ServerAliveCountMax=4",
            "-o", "StrictHostKeyChecking=accept-new",
            "-i", key, "-p", str(port),
            "-L", f"127.0.0.1:{lm}:{lan}:{remote_minio}",
            "-L", f"127.0.0.1:{li}:{lan}:{remote_influx}",
            f"{user}@{jump}",
        ]
        creation = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        self.proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, creationflags=creation)
        if not _wait_port(lm, timeout=20):
            err = ""
            if self.proc.poll() is not None and self.proc.stderr:
                err = self.proc.stderr.read().decode(errors="replace").strip()
            self.__exit__()
            raise SystemExit(f"[{self.site.code}] SSH 터널 실패 ({user}@{jump}): {err or '포트가 열리지 않음'}")
        self.minio_endpoint = f"127.0.0.1:{lm}"
        self.influx_url = f"http://127.0.0.1:{li}"
        self._log(f"tunnel  → {jump} ⇒ {lan}  (MinIO 127.0.0.1:{lm}, Influx 127.0.0.1:{li})")

    def _log(self, msg: str) -> None:
        if not self.quiet:
            print(f"[access] {self.site.code} {msg}", file=sys.stderr)
