@echo off
title tvgroup.media
echo ========================================
echo   tvgroup.media - Content Writer
echo ========================================
echo.
echo Dang khoi dong...
echo.

cd /d "%~dp0"

set PATH=C:\Users\ADMIN\AppData\Local\Programs\Python\Python312;C:\Users\ADMIN\AppData\Local\Programs\Python\Python312\Scripts;%PATH%

:: Open browser after 2 seconds
start "" cmd /c "timeout /t 2 >nul && start http://127.0.0.1:8000"

:: Start server
python run.py
