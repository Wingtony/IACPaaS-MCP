@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
  echo Сначала установите зависимости по README.md
  pause
  exit /b 1
)
if not exist ".env" copy ".env.example" ".env" >nul
start "" "%~dp0.venv\Scripts\pythonw.exe" -X utf8 -m iacpaas_mcp.launcher
