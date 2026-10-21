@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Сначала выполните установку из README.md
  pause
  exit /b 1
)
if not exist ".env" copy ".env.example" ".env" >nul
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Start-Ollama.ps1"
if errorlevel 1 goto end
set /p "QUESTION=Ваш вопрос к базе: "
if not defined QUESTION goto end
".venv\Scripts\python.exe" -m iacpaas_mcp.agent "%QUESTION%"
:end
pause
