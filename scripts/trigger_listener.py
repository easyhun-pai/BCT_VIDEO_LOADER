#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
트리거 연동 수집기 (Telegram long-poll 기반)

엣지노드는 작업 트리거 후 3초 영상을 저장하고 '결과'를 텔레그램으로 전송한다.
이 스크립트는 내 PC에서 그 텔레그램 메시지를 실시간 수신 → 메시지에 적힌
BCT 번호를 읽어 → 해당 BCT를 담당하는 엣지노드만 즉시 pull 한다.
(엣지노드 측 코드를 수정할 필요 없음. 추가 의존성 없음 — 표준 라이브러리만 사용.)

준비물: config/nodes.json 의 trigger.telegramBotToken.
        결과를 보내는 그 봇의 토큰이어야 하고, 봇이 해당 대화/그룹에 들어가 있어야 함.

실행:   python trigger_listener.py
"""
import json
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CFG = json.loads((ROOT / "config" / "nodes.json").read_text(encoding="utf-8"))

# secrets.local.json(git 제외) 의 민감값을 덮어쓴다.
_sec_path = ROOT / "config" / "secrets.local.json"
if _sec_path.exists():
    _sec = json.loads(_sec_path.read_text(encoding="utf-8"))
    if _sec.get("trigger", {}).get("telegramBotToken"):
        CFG["trigger"]["telegramBotToken"] = _sec["trigger"]["telegramBotToken"]
    if _sec.get("ssh", {}).get("user"):
        CFG["ssh"]["user"] = _sec["ssh"]["user"]

TOKEN = CFG["trigger"]["telegramBotToken"]
ALLOWED = set(str(c) for c in CFG["trigger"].get("allowedChatIds", []))
BCT_RE = re.compile(CFG["trigger"]["bctRegex"], re.IGNORECASE)
POLL = int(CFG["trigger"].get("pollTimeoutSec", 50))
PULL_PS1 = ROOT / "scripts" / "pull_videos.ps1"

if not TOKEN or "===" in TOKEN:
    sys.exit("config/secrets.local.json 의 trigger.telegramBotToken 을 채워주세요. "
             "(토큰 모르면 find_storage.ps1 [5] 또는 BotFather /mytoken)")

# BCT 번호 -> 노드명  (nodes.json 의 노드별 bcts 로 역매핑 자동 생성)
BCT_TO_NODE = {}
for n in CFG["nodes"]:
    for b in n["bcts"]:
        m = re.search(r"(\d+)", b)
        if m:
            BCT_TO_NODE[int(m.group(1))] = n["name"]

API = f"https://api.telegram.org/bot{TOKEN}"


def tg(method, **params):
    url = f"{API}/{method}?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=POLL + 10) as r:
        return json.loads(r.read().decode("utf-8"))


def pull_node(node_name):
    """해당 노드만 즉시 증분 수집 (pull_videos.ps1 재사용)."""
    print(f"  ▶ {node_name} 수집 시작...", flush=True)
    subprocess.Popen(
        ["powershell", "-ExecutionPolicy", "Bypass", "-File", str(PULL_PS1),
         "-Node", node_name, "-SinceDays", "1"],
        cwd=str(ROOT / "scripts"),
    )


def handle(msg):
    chat_id = str(msg.get("chat", {}).get("id", ""))
    if ALLOWED and chat_id not in ALLOWED:
        return
    text = msg.get("text") or msg.get("caption") or ""
    m = BCT_RE.search(text)
    if not m:
        return
    bct = int(m.group(1))
    node = BCT_TO_NODE.get(bct)
    print(f"[수신] chat={chat_id} BCT{bct} -> {node or '매핑없음'} | {text[:60]!r}", flush=True)
    if node:
        # 노드가 영상 저장을 끝낼 시간을 약간 둔다(3초 녹화 + 인코딩 여유)
        time.sleep(5)
        pull_node(node)


def main():
    print(f"트리거 리스너 시작. BCT->노드 매핑: {BCT_TO_NODE}", flush=True)
    offset = None
    while True:
        try:
            res = tg("getUpdates", timeout=POLL, offset=offset or "")
            for upd in res.get("result", []):
                offset = upd["update_id"] + 1
                msg = upd.get("message") or upd.get("channel_post")
                if msg:
                    handle(msg)
        except KeyboardInterrupt:
            print("\n종료.")
            break
        except Exception as e:
            print(f"[경고] {e} (5초 후 재시도)", flush=True)
            time.sleep(5)


if __name__ == "__main__":
    main()
