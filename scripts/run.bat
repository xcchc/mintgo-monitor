@echo off
REM mintgo-monitor: single run entry point (used by the scheduled task).
REM Keep this file ASCII-only: cmd.exe misparses UTF-8 comments on GBK consoles.
REM
REM Order matters:
REM   1. run_once        refresh data + score + push the screening report
REM   2. auto_mint       inspect candidates, mint if all safety gates pass
REM   3. watch_holdings  check floor prices of held NFTs, alert on targets
setlocal
cd /d "%~dp0.."
set PYTHONIOENCODING=utf-8
set NODE_USE_ENV_PROXY=1
if not defined MINTGO_PROXY set MINTGO_PROXY=http://127.0.0.1:7990
if not defined HTTPS_PROXY set HTTPS_PROXY=%MINTGO_PROXY%
if not defined HTTP_PROXY set HTTP_PROXY=%MINTGO_PROXY%
if not exist logs mkdir logs

echo. >> logs\task.log
echo ==== run at %DATE% %TIME% ==== >> logs\task.log

python -X utf8 src\run_once.py >> logs\task.log 2>&1
python -X utf8 scripts\paper.py record >> logs\task.log 2>&1
python -X utf8 scripts\alert_mint.py >> logs\task.log 2>&1
python -X utf8 scripts\auto_mint.py --live >> logs\task.log 2>&1
python -X utf8 scripts\watch_holdings.py >> logs\task.log 2>&1

exit /b %ERRORLEVEL%
