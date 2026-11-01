@echo off
title Y2A-Auto - YouTube to AcFun
echo.
echo ================================================
echo    Y2A-Auto - YouTube to AcFun
echo ================================================
echo.
echo Starting... port 5300
echo Web UI: http://localhost:5300
echo.
echo Stop: press Ctrl+C
echo ================================================
echo.
rem Port 5300: 5000 used by DB_Sync; 5500 falls in Hyper-V reserved range
rem Clear injected env vars to protect yt-dlp child processes
set "NODE_OPTIONS="
set "PYTHONSTARTUP="
set "PYTHONPATH="
rem Deno: required JS runtime for yt-dlp YouTube n-challenge solver
set "PATH=C:\Users\chenkui\tools\deno;%PATH%"
set PORT=5300
cd /d "D:\Program Files\Y2A-Auto"
"C:\Program Files\Python\Python312\python.exe" -u app.py
if not "%errorlevel%"=="0" echo Program exited with error code: %errorlevel%
echo.
echo Program exited. Press any key to close...
pause >nul
