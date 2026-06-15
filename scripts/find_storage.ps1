<#
.SYNOPSIS
    엣지노드에서 "트리거 영상이 어디에 저장되는지" 자동 추적한다.
    remoteBaseDir 값을 모를 때 1회 실행해서 후보 경로를 찾는 용도.

.EXAMPLE
    .\find_storage.ps1 -User paimedia -Ip 172.81.161.146
    .\find_storage.ps1 -User paimedia            # config의 노드1로 접속
#>
[CmdletBinding()]
param(
    [string] $User,
    [string] $Ip,
    [int]    $Port = 22,
    [string] $KeyPath = "$HOME\.ssh\bct_edge"
)
$ErrorActionPreference = 'Stop'
$cfg = Get-Content (Join-Path $PSScriptRoot '..\config\nodes.json') -Raw | ConvertFrom-Json
if (-not $Ip)   { $Ip = $cfg.nodes[0].ip }
if (-not $User) {
    $secPath = Join-Path $PSScriptRoot '..\config\secrets.local.json'
    if (Test-Path $secPath) { $User = (Get-Content $secPath -Raw | ConvertFrom-Json).ssh.user }
}
if (-not $User) { throw "-User 를 지정하거나 config\secrets.local.json 의 ssh.user 를 채우세요." }
$opts = @('-p',"$Port",'-o','ConnectTimeout=8','-o','StrictHostKeyChecking=accept-new')
if (Test-Path $KeyPath) { $opts += @('-i',$KeyPath) }
$target = "$User@$Ip"

# 원격에서 한 번에 실행할 진단 스크립트(영상/서비스/텔레그램 흔적 추적)
$probe = @'
echo "===== [1] 최근 24시간 내 생성된 영상/이미지 (상위 20개) ====="
find /home /opt /data /var /tmp /mnt /srv -type f \
     \( -name "*.mp4" -o -name "*.avi" -o -name "*.mkv" -o -name "*.jpg" \) \
     -mmin -1440 -printf "%TY-%Tm-%Td %TH:%TM  %10s  %p\n" 2>/dev/null | sort | tail -20

echo ""
echo "===== [2] 녹화/탐지 관련 실행 중 프로세스 ====="
ps -eo pid,user,args 2>/dev/null | grep -iE "python|ffmpeg|gstreamer|record|detect|telegram|rtsp" | grep -v grep

echo ""
echo "===== [3] 프로세스 작업 디렉터리(cwd) — 저장 위치 단서 ====="
for pid in $(pgrep -f -iE "python|ffmpeg|record|detect" 2>/dev/null); do
  echo "  pid $pid -> $(readlink -f /proc/$pid/cwd 2>/dev/null)"
done | sort -u

echo ""
echo "===== [4] 자동 실행 등록(systemd / cron) ====="
systemctl list-units --type=service --state=running 2>/dev/null | grep -iE "record|detect|cam|ppe|hook|telegram|bct"
crontab -l 2>/dev/null | grep -vE "^\s*#"

echo ""
echo "===== [5] 텔레그램 전송 코드 위치 ====="
grep -rilE "telegram|sendVideo|sendPhoto|bot[0-9]" /home /opt /srv 2>/dev/null | head -10
echo "----- 봇 토큰 후보(숫자:문자열) -----"
grep -rhoE "[0-9]{8,10}:[A-Za-z0-9_-]{30,45}" /home /opt /srv 2>/dev/null | sort -u | head
echo "----- chat_id 후보 -----"
grep -rhoE "chat_id['\"]?\s*[:=]\s*['\"]?-?[0-9]{6,}" /home /opt /srv 2>/dev/null | sort -u | head

echo ""
echo "===== [6] 디스크 사용량 상위 디렉터리 ====="
du -h -d 2 /home /opt /data 2>/dev/null | sort -rh | head -15
'@

Write-Host "▶ $target 에 접속해 저장 위치를 추적합니다...`n" -ForegroundColor Cyan
& ssh @opts $target $probe
Write-Host "`n▶ 위 [1]의 경로에서 BCT/날짜 상위 폴더를 골라 config\nodes.json 의 remoteBaseDir 에 넣으세요." -ForegroundColor Cyan
