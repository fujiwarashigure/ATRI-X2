@echo off
setlocal
cd /d "%~dp0"
rem Use the existing backend; do not start or replace the installed service.
python -m atri_x.gui --no-service
if errorlevel 1 pause
endlocal
