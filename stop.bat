@echo off
chcp 65001 >nul
taskkill /f /im ngrok.exe >nul 2>&1
taskkill /f /im pythonw.exe >nul 2>&1
taskkill /f /im python.exe >nul 2>&1
echo 已停止所有服务
timeout /t 2 >nul
