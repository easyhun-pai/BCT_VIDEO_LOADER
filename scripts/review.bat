@echo off
rem BCT false-positive review CLI launcher.  e.g.  scripts\review list HANIL 2026-09-07 --verdict denied
rem Keep this file ASCII-only: cmd.exe reads .bat as the OEM codepage, not UTF-8.
set PYTHONIOENCODING=utf-8
"%~dp0..\.venv\Scripts\python.exe" -m review %*
