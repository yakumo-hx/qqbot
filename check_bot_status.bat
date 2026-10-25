@echo off
setlocal EnableExtensions
chcp 65001 >nul
title QQbot - process and port status

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\bot_launcher.ps1" -Action Status
set "RC=%ERRORLEVEL%"
echo.
if /i "%~1"=="--no-pause" exit /b %RC%
pause
exit /b %RC%
