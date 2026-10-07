@echo off
chcp 65001 >nul 2>&1
cd /d "%~dp0"
title DPetroWorkbench Local Server

echo.
echo ============================================
echo    DPetroWorkbench / Xingzhe  Local Server
echo ============================================
echo.
echo Starting... please wait about 10 seconds.
echo Then open:  http://localhost:8501
echo.
echo [IMPORTANT] Keep this black window OPEN.
echo Closing it will stop the server.
echo ============================================
echo.

start "" http://localhost:8501

".venv\Scripts\python.exe" -B -m streamlit run app.py --server.port 8501 --server.headless true

echo.
echo Server stopped. Press any key to close.
pause >nul
