@echo off
rem Start Y2A-Auto with clean environment (for Task Scheduler)
rem Clear injected env vars; add Deno for yt-dlp n-challenge
set "NODE_OPTIONS="
set "PYTHONSTARTUP="
set "PYTHONPATH="
set "PATH=C:\Users\chenkui\tools\deno;%PATH%"
set "PORT=5300"
cd /d "D:\Program Files\Y2A-Auto"
"C:\Program Files\Python\Python312\python.exe" -u app.py
