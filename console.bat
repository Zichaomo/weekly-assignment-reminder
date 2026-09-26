@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
cd /d "%~dp0data"
powershell -NoProfile -ExecutionPolicy Bypass -File "console.ps1"