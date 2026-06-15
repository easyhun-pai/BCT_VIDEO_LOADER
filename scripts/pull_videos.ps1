<#
.SYNOPSIS
    BCT 엣지노드(6대)에 저장된 트리거 영상/결과 파일을 내 PC로 증분(incremental) 수집한다.

.DESCRIPTION
    - config/nodes.json 의 인벤토리를 읽어 각 엣지노드에 SSH로 접속한다.
    - 원격 저장 경로(remoteBaseDir)에서 대상 파일 목록을 받아온 뒤,
      로컬에 아직 없는 파일만 scp로 내려받는다(= 매번 전체 재다운로드 안 함).
    - 받은 파일은  data/<노드명>/<원격 상대경로>  구조로 정리된다.
    - Windows 10 기본 OpenSSH(ssh.exe, scp.exe)만 사용하므로 추가 설치 불필요.

.PARAMETER Node
    특정 노드만 수집. 예: -Node edge-node3  (생략 시 전체 6대)

.PARAMETER SinceDays
    최근 N일 이내 수정된 파일만 수집(0 = 전체). config 값을 덮어씀.

.PARAMETER DryRun
    실제 다운로드 없이 받을 파일만 출력.

.PARAMETER PurgeRemote
    다운로드 성공한 파일을 원격에서 삭제(원격 디스크 정리용). 기본 비활성.

.EXAMPLE
    .\pull_videos.ps1
    .\pull_videos.ps1 -Node edge-node5 -SinceDays 1
    .\pull_videos.ps1 -DryRun
#>

[CmdletBinding()]
param(
    [string] $Node,
    [int]    $SinceDays = -1,
    [switch] $DryRun,
    [switch] $PurgeRemote
)

$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}   # 한글 깨짐 방지
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$root      = Split-Path -Parent $scriptDir

# ── 설정 로드 ─────────────────────────────────────────────────────────────
$cfgPath = Join-Path $scriptDir '..\config\nodes.json'
if (-not (Test-Path $cfgPath)) { throw "설정 파일이 없습니다: $cfgPath" }
$cfg = Get-Content $cfgPath -Raw -Encoding UTF8 | ConvertFrom-Json

# secrets.local.json(git 제외) 의 민감값을 덮어쓴다.
$secPath = Join-Path $scriptDir '..\config\secrets.local.json'
if (Test-Path $secPath) {
    $sec = Get-Content $secPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($sec.ssh.user)     { $cfg.ssh.user     = $sec.ssh.user }
    if ($sec.ssh.password) { $cfg.ssh.password = $sec.ssh.password }
    if ($sec.trigger.telegramBotToken) { $cfg.trigger.telegramBotToken = $sec.trigger.telegramBotToken }
}

$sshUser   = $cfg.ssh.user
$sshPort   = $cfg.ssh.port
$keyPath   = $ExecutionContext.InvokeCommand.ExpandString($cfg.ssh.keyPath) -replace '^~', $HOME
$remoteBase= $cfg.transfer.remoteBaseDir
# localBaseDir 가 절대경로(예: 구글 Drive for Desktop의 G:\내 드라이브\...)면 그대로,
# 상대경로면 scripts 기준으로 해석한다.
$localBase = if ([System.IO.Path]::IsPathRooted($cfg.transfer.localBaseDir)) {
    $cfg.transfer.localBaseDir
} else {
    Join-Path $scriptDir $cfg.transfer.localBaseDir
}
$patterns  = $cfg.transfer.filePatterns
$since     = if ($SinceDays -ge 0) { $SinceDays } else { [int]$cfg.transfer.sinceDays }
$purge     = $PurgeRemote.IsPresent -or $cfg.transfer.purgeRemoteAfterPull

if ($sshUser -like '*===*') { throw "config/nodes.json 의 ssh.user 를 실제 값으로 채워주세요." }
if ($remoteBase -like '*===*') { throw "config/nodes.json 의 transfer.remoteBaseDir 를 실제 값으로 채워주세요." }

# 로그 디렉터리
$logDir = Join-Path $scriptDir '..\logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$stamp  = Get-Date -Format 'yyyyMMdd_HHmmss'
$logFile= Join-Path $logDir "pull_$stamp.log"

function Log {
    param([string]$msg, [string]$level = 'INFO')
    $line = "{0} [{1}] {2}" -f (Get-Date -Format 'HH:mm:ss'), $level, $msg
    Write-Host $line
    Add-Content -Path $logFile -Value $line -Encoding UTF8
}

# 공통 옵션(-o, -i). 포트 플래그는 ssh=-p / scp=-P 로 다르므로 분리한다.
$commonOpts = @(
    '-o', "ConnectTimeout=$($cfg.ssh.connectTimeoutSec)",
    '-o', "StrictHostKeyChecking=$($cfg.ssh.strictHostKeyChecking)",
    '-o', 'BatchMode=yes'
)
if (Test-Path $keyPath) { $commonOpts += @('-i', $keyPath) }
else { Log "SSH 키를 못 찾음($keyPath). 비밀번호/에이전트 인증으로 시도합니다." 'WARN' }
$sshOpts = @('-p', "$sshPort") + $commonOpts   # ssh 용
$scpOpts = @('-P', "$sshPort") + $commonOpts   # scp 용(포트는 대문자 -P)

# 원격 find 의 -name 패턴식 구성:  \( -name '*.mp4' -o -name '*.jpg' ... \)
$nameExpr = ($patterns | ForEach-Object { "-name '$_'" }) -join ' -o '
$timeExpr = if ($since -gt 0) { "-mtime -$since" } else { '' }
# 제외 디렉터리(예: raw 프레임 — 대용량):  -not -path '*/raw/*'
$excludeExpr = ''
if ($cfg.transfer.excludeDirs) {
    $excludeExpr = ($cfg.transfer.excludeDirs | ForEach-Object { "-not -path '*/$_/*'" }) -join ' '
}

# ── 노드 선택 ─────────────────────────────────────────────────────────────
$targets = if ($Node) { $cfg.nodes | Where-Object { $_.name -eq $Node } } else { $cfg.nodes }
if (-not $targets) { throw "노드를 찾을 수 없음: $Node" }

$grandTotal = [ordered]@{ downloaded = 0; skipped = 0; failed = 0 }
Log ("==== BCT 영상 수집 시작 (대상 {0}개 노드, since={1}일, dryrun={2}) ====" -f @($targets).Count, $since, $DryRun)

foreach ($n in $targets) {
    $target = "$sshUser@$($n.ip)"
    Log ""
    Log "── [$($n.name)] $target  (BCT: $($n.bcts -join ', ')) ──"

    # 1) 연결 확인
    & ssh @sshOpts $target 'echo ok' *> $null
    if ($LASTEXITCODE -ne 0) {
        Log "SSH 접속 실패 → 건너뜀 (ZeroTier/방화벽/키 확인)" 'ERROR'
        $grandTotal.failed++
        continue
    }

    # 2) 원격 파일 목록 수집 (널 구분자로 안전하게)
    $findCmd = "find '$remoteBase' -type f \( $nameExpr \) $excludeExpr $timeExpr -printf '%P\n' 2>/dev/null"
    $remoteFiles = & ssh @sshOpts $target $findCmd
    if ($LASTEXITCODE -ne 0) { Log "원격 목록 조회 실패 → 건너뜀" 'ERROR'; $grandTotal.failed++; continue }
    $remoteFiles = $remoteFiles | Where-Object { $_ -and $_.Trim() -ne '' }

    if (-not $remoteFiles) { Log "신규/대상 파일 없음."; continue }
    Log "원격 대상 파일 $($remoteFiles.Count)개 발견."

    $nodeLocal = Join-Path $localBase $n.name
    New-Item -ItemType Directory -Force -Path $nodeLocal | Out-Null

    # 로컬에 아직 없는 파일만 추림(증분)
    $missing = @()
    foreach ($rel in $remoteFiles) {
        $relF = $rel -replace '\\', '/'
        if (-not (Test-Path (Join-Path $nodeLocal ($relF -replace '/', '\')))) { $missing += $relF }
    }
    $already = $remoteFiles.Count - $missing.Count
    $grandTotal.skipped += $already
    if (-not $missing) { Log "신규 없음 (보유 $already개)."; continue }
    Log "신규 $($missing.Count)개 다운로드 (보유 $already개 건너뜀)..."

    if ($DryRun) {
        $missing | ForEach-Object { Log "  [DRY] $($n.name)/$_" }
        $grandTotal.downloaded += $missing.Count
        continue
    }

    # 단일 ssh+tar 스트림으로 일괄 전송: 연결 1회, 바이너리 안전(Start-Process 파일 리다이렉트)
    $listFile = [IO.Path]::GetTempFileName()
    $tarFile  = [IO.Path]::GetTempFileName()
    [IO.File]::WriteAllText($listFile, (($missing -join "`n") + "`n"), (New-Object Text.UTF8Encoding $false))
    # 원격: 파일목록을 stdin(-T -)으로 받아 tar 스트림을 stdout(-cf -)으로
    $remoteTar = "tar -C '$remoteBase' --ignore-failed-read -cf - -T - 2>/dev/null"
    $sshArgs   = $sshOpts + @($target, $remoteTar)
    Start-Process -FilePath 'ssh' -ArgumentList $sshArgs `
        -RedirectStandardInput $listFile -RedirectStandardOutput $tarFile `
        -NoNewWindow -Wait | Out-Null

    if ((Test-Path $tarFile) -and (Get-Item $tarFile).Length -gt 0) {
        & tar -xf $tarFile -C $nodeLocal 2>$null
        $got = ($missing | Where-Object { Test-Path (Join-Path $nodeLocal ($_ -replace '/', '\')) }).Count
        Log "  ↓ $got/$($missing.Count) 파일 수신"
        $grandTotal.downloaded += $got
        $grandTotal.failed     += ($missing.Count - $got)
        if ($purge -and $got -gt 0) {
            $rmCmd = "cd '$remoteBase' && tr '\n' '\0' | xargs -0 rm -f"
            $sshRm = $sshOpts + @($target, $rmCmd)
            Start-Process -FilePath 'ssh' -ArgumentList $sshRm -RedirectStandardInput $listFile -NoNewWindow -Wait | Out-Null
            Log "    (원격 $got개 삭제 시도)"
        }
    } else {
        Log "  ✗ tar 스트림 실패(빈 출력) — 키/경로 확인" 'ERROR'
        $grandTotal.failed += $missing.Count
    }
    Remove-Item $listFile, $tarFile -Force -ErrorAction SilentlyContinue
}

Log ""
Log ("==== 완료: 다운로드 {0} / 건너뜀 {1} / 실패 {2} ====" -f $grandTotal.downloaded, $grandTotal.skipped, $grandTotal.failed)
Log "로그: $logFile"
Log "저장 위치: $((Resolve-Path $localBase -ErrorAction SilentlyContinue).Path)"
