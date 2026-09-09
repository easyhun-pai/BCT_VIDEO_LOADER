@echo off
rem BCT 오탐 검수 세션 — 로컬 웹 실행기. 브라우저가 자동으로 열린다 (http://localhost:8501)
rem 저장 루트를 바꾸려면:  set BCT_REVIEW_OUT=D:\somewhere  후 실행
set PYTHONIOENCODING=utf-8
"%~dp0..\.venv\Scripts\python.exe" -m streamlit run "%~dp0..\review\app.py" --browser.gatherUsageStats false --server.port 8501 %*
