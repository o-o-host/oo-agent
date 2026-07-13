@echo off
:: oo-agent setup for Windows - double-click and enter your enrollment
:: code (shown by the "+ Server" wizard in the monitor.o-o.host panel).
:: Downloads the installer over HTTPS and runs it elevated.
setlocal EnableDelayedExpansion
title oo-agent setup

:: self-elevate
net session >nul 2>&1
if errorlevel 1 (
    echo Requesting administrator rights...
    powershell -NoProfile -Command "Start-Process -Verb RunAs -FilePath '%~f0'"
    exit /b
)

echo.
echo  oo-agent - monitoring agent for monitor.o-o.host
echo  ------------------------------------------------
echo.
set "CODE="
set /p CODE=Enrollment code (from the '+ Server' wizard, Enter = skip):

set "PSFILE=%TEMP%\oo-install.ps1"
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "iwr -useb https://monitor.o-o.host/dl/install.ps1 -OutFile '%PSFILE%'" || goto :dlfail

if defined CODE (
    powershell -NoProfile -ExecutionPolicy Bypass -File "%PSFILE%" -Server https://monitor.o-o.host/api -Enroll %CODE%
) else (
    powershell -NoProfile -ExecutionPolicy Bypass -File "%PSFILE%" -Server https://monitor.o-o.host/api
)
echo.
pause
exit /b

:dlfail
echo Could not download the installer - check the internet connection.
pause
exit /b 1
