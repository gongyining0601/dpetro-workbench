@echo off
chcp 65001 > nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"
C:\Users\Administrator\AppData\Local\Programs\Python\Python312\python.exe -B crawler.py >> data\crawler.log 2>&1
