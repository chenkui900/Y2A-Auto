@echo off
chcp 65001 >nul
title Y2A-Auto - YouTube to AcFun 自动化工具

echo.
echo ================================================
echo    Y2A-Auto - YouTube to AcFun 自动化工具
echo ================================================
echo.
echo 正在启动程序（源码模式，端口 5500）...
echo Web界面将在 http://localhost:5500 启动
echo.
echo 首次启动可能需要几分钟时间进行初始化
echo 请耐心等待，不要关闭此窗口
echo.
echo 要停止程序，请按 Ctrl+C
echo ================================================
echo.

rem 端口 5500：5000 已被 DB_Sync 占用
set PORT=5500
"C:\Program Files\Python\Python312\python.exe" app.py

if errorlevel 1 (
    echo.
    echo 程序异常退出，错误代码: %errorlevel%
    echo 请检查日志文件或联系技术支持
    echo.
)

echo.
echo 程序已退出，按任意键关闭窗口...
pause >nul
