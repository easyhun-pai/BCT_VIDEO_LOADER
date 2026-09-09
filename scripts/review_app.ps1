# Paimedialab 오탐 검수 플랫폼 — 로컬 웹 실행기 (PowerShell)
#   .\review_app.ps1            브라우저가 http://localhost:8501 로 열린다
#   Ctrl+C 로 종료. (.bat 과 달리 "일괄 작업을 끝내시겠습니까?" 안 묻는다)
#   저장 루트를 바꾸려면 실행 전:  $env:BCT_REVIEW_OUT = 'D:\somewhere'
[CmdletBinding()]
param([Parameter(ValueFromRemainingArguments = $true)] [string[]] $Rest)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$py   = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $py)) { throw "가상환경이 없습니다: $py  (python -m venv .venv; .venv\Scripts\pip install -r requirements.txt)" }

$env:PYTHONIOENCODING = 'utf-8'
$env:STREAMLIT_BROWSER_GATHER_USAGE_STATS = 'false'

# Streamlit 첫 실행 이메일 프롬프트 방지 (credentials.toml 존재만 확인함)
$stDir = Join-Path $env:USERPROFILE '.streamlit'
$cred  = Join-Path $stDir 'credentials.toml'
if (-not (Test-Path $stDir)) { New-Item -ItemType Directory -Path $stDir | Out-Null }
if (-not (Test-Path $cred))  { "[general]`nemail = `"`"" | Set-Content -Path $cred -Encoding ascii }

# 저장소 루트에서 실행해야 .streamlit\config.toml(테마)을 읽는다.
# PowerShell 의 Set-Location 은 자식 프로세스의 작업 폴더까지 보장하지 않으므로
# .NET 프로세스 cwd 도 같이 맞추고, 앱 경로는 절대경로로 넘긴다.
$app = Join-Path $root 'review\app.py'
Push-Location $root
[Environment]::CurrentDirectory = $root
try {
    & $py -m streamlit run $app --server.port 8501 @Rest
}
finally {
    Pop-Location
}
