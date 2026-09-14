@echo off
rem BCT monthly performance review - local web launcher (browser opens at http://localhost:8502)
rem Override save root:  set BCT_REVIEW_OUT=D:\somewhere   before running.
rem Keep this file ASCII-only: cmd.exe reads .bat as the OEM codepage, not UTF-8.
setlocal
set PYTHONIOENCODING=utf-8
set STREAMLIT_BROWSER_GATHER_USAGE_STATS=false
if not exist "%USERPROFILE%\.streamlit" mkdir "%USERPROFILE%\.streamlit"
if not exist "%USERPROFILE%\.streamlit\credentials.toml" (
  >"%USERPROFILE%\.streamlit\credentials.toml" echo [general]
  >>"%USERPROFILE%\.streamlit\credentials.toml" echo email = ""
)
rem Run from the repo root so Streamlit picks up .streamlit\config.toml (theme).
pushd "%~dp0.."
".venv\Scripts\python.exe" -m streamlit run "review\perf_app.py" --server.port 8502 %*
popd
endlocal
