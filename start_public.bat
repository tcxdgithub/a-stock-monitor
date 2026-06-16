@echo off
chcp 65001 >nul
title A股监控 - 公网访问
echo ========================================
echo   A股资金流向监控 - 公网启动
echo ========================================
echo.
echo 启动中，请稍候...

:: 杀掉旧进程
taskkill /f /im ngrok.exe >nul 2>&1
taskkill /f /im pythonw.exe >nul 2>&1
timeout /t 2 >nul

:: 启动Flask
cd /d "%~dp0"
start /b pythonw app.py
timeout /t 3 >nul

:: 启动ngrok
start /b "" "%LOCALAPPDATA%\ngrok\ngrok.exe" http 5000 >nul
timeout /t 6 >nul

:: 显示地址
echo.
echo ========================================
echo  公网访问地址 (复制下面的链接到手机浏览器):
echo ========================================
echo.
curl -s http://127.0.0.1:4040/api/tunnels | python -c "import sys,json;d=json.load(sys.stdin);print('  ',d['tunnels'][0]['public_url'])"
echo.
echo ========================================
echo  关闭此窗口 = 停止所有服务
echo ========================================
echo.
pause
