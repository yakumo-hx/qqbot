@echo off
setlocal EnableExtensions
chcp 65001 >nul
title QQbot - NapCat rollback launcher

set "CHECK_ARG="
if /i "%~1"=="--check" set "CHECK_ARG=-CheckOnly"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\bot_launcher.ps1" -Action StartNapCat %CHECK_ARG%
set "RC=%ERRORLEVEL%"
echo.
if /i "%~1"=="--check" exit /b %RC%
pause
exit /b %RC%
