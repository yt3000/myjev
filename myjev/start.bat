@echo off
rem MyJev launcher (two-process topology, docs/04). ASCII-only text for cmd codepage safety.
rem Usage: start.bat [setup|demo|test|check|serve-all|serve|serve-admin|serve-runtime|stop-all|status|all]
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set PY=.venv\Scripts\python.exe
set PYTHONIOENCODING=utf-8
set PYTHONPATH=.

if "%~1"=="" goto :help
if /i "%~1"=="setup" goto :setup
if /i "%~1"=="demo"  goto :demo
if /i "%~1"=="serve-all" goto :serve_all
if /i "%~1"=="serve" goto :serve_admin
if /i "%~1"=="serve-admin" goto :serve_admin
if /i "%~1"=="serve-runtime" goto :serve_runtime
if /i "%~1"=="stop-all" goto :stop_all
if /i "%~1"=="status" goto :status
if /i "%~1"=="test"  goto :test
if /i "%~1"=="check" goto :check
if /i "%~1"=="all"   goto :all
goto :help

:setup
if not exist "%PY%" (
  echo [setup] creating venv .venv ...
  python -m venv .venv || goto :err
)
echo [setup] installing requirements.txt ...
"%PY%" -m pip install --upgrade pip >nul
"%PY%" -m pip install -r requirements.txt || goto :err
echo [setup] done.
goto :end

:serve_all
if not exist "%PY%" call :setup || goto :err
"%PY%" -m myjev.serve serve-all --daemon || goto :err
goto :end

:serve_admin
if not exist "%PY%" call :setup || goto :err
echo [serve] admin foreground :8090 (Ctrl+C stops only this console window!)
"%PY%" -m myjev.serve --role admin || goto :err
goto :end

:serve_runtime
if not exist "%PY%" call :setup || goto :err
echo [serve] runtime foreground :8091
"%PY%" -m myjev.serve --role runtime || goto :err
goto :end

:stop_all
if not exist "%PY%" goto :stop_ps
"%PY%" -m myjev.serve stop-all %2
goto :end

:stop_ps
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop.ps1" %2
goto :end

:status
"%PY%" -m myjev.serve status
goto :end

:demo
if not exist "%PY%" call :setup || goto :err
"%PY%" -m myjev.run_demo || goto :err
goto :end

:test
if not exist "%PY%" call :setup || goto :err
echo [test] kernel unit tests ...
"%PY%" -m unittest discover -s tests || goto :err
echo [test] integration (needs ~1min) ...
"%PY%" -m unittest discover -s scripts/tests || goto :err
goto :end

:check
if not exist "%PY%" (echo [check] missing .venv, run: start.bat setup & goto :end)
"%PY%" --version
"%PY%" -c "import importlib;[print(' ',m,getattr(importlib.import_module(m),'__version__','?')) for m in ['numpy','scipy','sklearn','lightgbm','fastapi','uvicorn']]"
goto :end

:all
call :setup && call :test && call :demo
goto :end

:help
echo MyJev launcher (A runtime :8091 / B admin :8090 / C app: open myjev\app\index.html)
echo   start.bat setup          create venv + deps
echo   start.bat serve-all      daemon-launch BOTH processes (recommended)
echo   start.bat stop-all       stop both (pid+port, idempotent)
echo   start.bat status         show processes + epochs
echo   start.bat serve-admin    admin foreground (debug)
echo   start.bat serve-runtime  runtime foreground (debug)
echo   start.bat demo           legacy single-process E2E demo (M2 pipeline)
echo   start.bat test           unit + integration
echo   start.bat check          deps self-check
goto :end

:err
echo [ERROR] last step failed.
exit /b 1

:end
endlocal
