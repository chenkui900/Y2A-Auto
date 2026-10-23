@echo off
chcp 65001 >nul
title Y2A-Auto 停止

echo.
echo ========================================
echo   停止 Y2A-Auto 服务（仅本项目，按端口 5500）
echo ========================================
echo.

powershell -NoProfile -Command "$c = Get-NetTCPConnection -LocalPort 5500 -State Listen -ErrorAction SilentlyContinue; if ($c) { $c.OwningProcess | Select-Object -Unique | ForEach-Object { Write-Output ('已停止进程 ' + $_); Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue } } else { Write-Output '端口 5500 无监听，Y2A-Auto 未在运行' }"

echo.
echo 说明：只停止占用 5500 端口的 Y2A-Auto 进程，
echo       不影响 DB_Sync (5000) 和 PLC 采集器 (5100)。
echo.
pause
