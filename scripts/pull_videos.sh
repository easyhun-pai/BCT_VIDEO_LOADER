#!/usr/bin/env bash
#
# BCT 엣지노드 영상 수집 (rsync 버전) — WSL / Git Bash / Linux / macOS 용
#
# rsync 는 증분 전송 + 중단 재개 + 무결성 검증이 기본이라, 대용량 영상에는
# scp 보다 안정적이다. ZeroTier 로 엣지노드(172.81.161.x)에 도달 가능해야 한다.
#
# 사용법:
#   ./pull_videos.sh                # 전체 노드
#   ./pull_videos.sh edge-node5     # 특정 노드만
#   DRY=1 ./pull_videos.sh          # 미리보기(실제 전송 안 함)
#   PURGE=1 ./pull_videos.sh        # 전송 성공분 원격 삭제
#
set -euo pipefail

# ── 채워야 할 설정 ───────────────────────────────────────────────────────
SSH_USER="=== SSH 계정명 ==="           # 예: paimedia
SSH_PORT=22
SSH_KEY="$HOME/.ssh/bct_edge"           # 키 파일 경로
REMOTE_BASE="=== 엣지노드 영상 저장 경로 ==="   # 예: /home/paimedia/captures
LOCAL_BASE="$(cd "$(dirname "$0")/.." && pwd)/data"
SINCE_DAYS=0                            # 0 = 전체, N = 최근 N일만
PATTERNS=( "*.mp4" "*.avi" "*.mkv" "*.jpg" "*.json" )

# 노드명 → IP  (IP 테이블 기준)
declare -A NODES=(
  [edge-node1]=172.81.161.146   # BCT13
  [edge-node2]=172.81.161.143   # BCT11, BCT12
  [edge-node3]=172.81.161.144   # BCT9, BCT10
  [edge-node4]=172.81.161.145   # BCT7, BCT8
  [edge-node5]=172.81.161.148   # BCT5, BCT6
  [edge-node6]=172.81.161.147   # BCT4
)
# ─────────────────────────────────────────────────────────────────────────

[[ "$SSH_USER"   == *"==="* ]] && { echo "SSH_USER 를 채워주세요"; exit 1; }
[[ "$REMOTE_BASE" == *"==="* ]] && { echo "REMOTE_BASE 를 채워주세요"; exit 1; }

SSH_CMD="ssh -p $SSH_PORT -o ConnectTimeout=8 -o StrictHostKeyChecking=accept-new"
[[ -f "$SSH_KEY" ]] && SSH_CMD="$SSH_CMD -i $SSH_KEY"

# rsync include/exclude 필터 구성 (지정 확장자만)
FILTERS=( --include='*/' )
for p in "${PATTERNS[@]}"; do FILTERS+=( --include="$p" ); done
FILTERS+=( --exclude='*' )

RSYNC_OPTS=( -avz --partial --prune-empty-dirs --info=progress2 -e "$SSH_CMD" )
# 최근 N일만: rsync 단독으론 까다로워, mtime 필터는 PowerShell 버전(-SinceDays) 권장.
[[ "${DRY:-0}"   == "1" ]] && RSYNC_OPTS+=( --dry-run )
[[ "${PURGE:-0}" == "1" ]] && RSYNC_OPTS+=( --remove-source-files )

mkdir -p "$LOCAL_BASE"
LOG="$(cd "$(dirname "$0")/.." && pwd)/logs/pull_$(date +%Y%m%d_%H%M%S).log"
mkdir -p "$(dirname "$LOG")"

pull_one() {
  local name="$1" ip="$2"
  echo "── [$name] $SSH_USER@$ip ──" | tee -a "$LOG"
  if ! $SSH_CMD "$SSH_USER@$ip" 'echo ok' >/dev/null 2>&1; then
    echo "  SSH 접속 실패 → 건너뜀" | tee -a "$LOG"; return
  fi
  mkdir -p "$LOCAL_BASE/$name"
  rsync "${RSYNC_OPTS[@]}" "${FILTERS[@]}" \
        "$SSH_USER@$ip:$REMOTE_BASE/" "$LOCAL_BASE/$name/" 2>&1 | tee -a "$LOG"
}

if [[ $# -ge 1 ]]; then
  pull_one "$1" "${NODES[$1]:?알 수 없는 노드: $1}"
else
  for name in "${!NODES[@]}"; do pull_one "$name" "${NODES[$name]}"; done
fi

echo "완료. 저장 위치: $LOCAL_BASE  / 로그: $LOG"
