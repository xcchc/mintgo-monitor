@echo off
REM mintgo-monitor: single run entry point (used by the scheduled task).
REM Keep this file ASCII-only: cmd.exe misparses UTF-8 comments on GBK consoles.
setlocal
cd /d "%~dp0.."
set PYTHONIOENCODING=utf-8
if not exist logs mkdir logs
python -X utf8 src\run_once.py >> logs\task.log 2>&1
exit /b %ERRORLEVEL%
