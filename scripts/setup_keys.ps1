<#
.SYNOPSIS
    6개 엣지노드에 SSH 공개키를 1회 설치한다.
    설치 후에는 비밀번호 없이(BatchMode) pull_videos / trigger_listener 가 무인 동작.

.DESCRIPTION
    - 키가 없으면 ~/.ssh/bct_edge (ed25519) 를 새로 생성.
    - 각 노드의 ~/.ssh/authorized_keys 에 공개키를 추가.
    - PuTTY plink(pscp) 가 설치돼 있으면 비밀번호를 자동 주입(완전 무인).
      없으면 Windows 기본 ssh 로 진행되며 노드마다 비밀번호를 직접 입력(secrets 참고).

.EXAMPLE
    .\setup_keys.ps1
    .\setup_keys.ps1 -Node edge-node1     # 한 노드만
#>
[CmdletBinding()]
param(
    [string] $Node,
    [string] $KeyPath = "$HOME\.ssh\bct_edge"
)
$ErrorActionPreference = 'Stop'
$cfg = Get-Content (Join-Path $PSScriptRoot '..\config\nodes.json') -Raw -Encoding UTF8 | ConvertFrom-Json

# secrets 에서 계정/비밀번호
$secPath = Join-Path $PSScriptRoot '..\config\secrets.local.json'
if (-not (Test-Path $secPath)) { throw "config\secrets.local.json 이 필요합니다." }
$sec  = Get-Content $secPath -Raw -Encoding UTF8 | ConvertFrom-Json
$user = $sec.ssh.user
$pw   = $sec.ssh.password
$port = $cfg.ssh.port

# 1) 키 생성
if (-not (Test-Path $KeyPath)) {
    New-Item -ItemType Directory -Force -Path (Split-Path $KeyPath) | Out-Null
    Write-Host "▶ SSH 키 생성: $KeyPath" -ForegroundColor Cyan
    & ssh-keygen -t ed25519 -f $KeyPath -N '""' -C 'bct-loader' | Out-Null
}
$pub = (Get-Content "$KeyPath.pub" -Raw).Trim()

# 원격에서 authorized_keys 에 키 추가하는 명령
$remoteCmd = "mkdir -p ~/.ssh && chmod 700 ~/.ssh && " +
             "grep -qxF '$pub' ~/.ssh/authorized_keys 2>/dev/null || echo '$pub' >> ~/.ssh/authorized_keys && " +
             "chmod 600 ~/.ssh/authorized_keys && echo INSTALLED"

$plink = (Get-Command plink.exe -ErrorAction SilentlyContinue)
if ($plink) { Write-Host "▶ plink 발견 — 비밀번호 자동 주입(무인) 모드`n" -ForegroundColor Cyan }
else        { Write-Host "▶ plink 없음 — 노드마다 비밀번호($pw)를 직접 입력하세요`n" -ForegroundColor Yellow }

$targets = if ($Node) { $cfg.nodes | Where-Object { $_.name -eq $Node } } else { $cfg.nodes }
foreach ($n in $targets) {
    $t = "$user@$($n.ip)"
    Write-Host "── [$($n.name)] $t ──" -ForegroundColor Green
    if ($plink) {
        # 호스트키 자동 수락(-hostkey 미지정 시 첫 접속 프롬프트 회피용으로 'y' 전달)
        cmd /c "echo y | plink -ssh -P $port -pw $pw $t `"$remoteCmd`""
    } else {
        & ssh -p $port -o StrictHostKeyChecking=accept-new $t $remoteCmd
    }
}
Write-Host "`n▶ 완료. 테스트: ssh -i $KeyPath $user@$($cfg.nodes[0].ip) 'hostname'" -ForegroundColor Cyan
