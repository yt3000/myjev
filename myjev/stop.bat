@echo off
rem MyJev service stopper (Windows). Usage: stop.bat [port, default 8090]
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop.ps1" %1
