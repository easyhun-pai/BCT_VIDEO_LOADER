@echo off
rem BCT 오탐 검수 세션 CLI 실행기.  예)  scripts\review list HANIL 2026-09-07 --verdict denied
set PYTHONIOENCODING=utf-8
"%~dp0..\.venv\Scripts\python.exe" -m review %*
