@echo off
title PIOU AI - public link
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-public.ps1"
echo.
echo PIOU AI stopped. Press any key to close.
pause >nul
