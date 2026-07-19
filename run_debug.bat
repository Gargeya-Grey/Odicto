@echo off
cd /d "%~dp0"
echo Running Odicto in DEBUG mode...
echo This console window will print any startup warnings or crash logs.
echo Keep this window open to test. Press Ctrl+C in this window to stop.
echo.
echo Python: %~dp0.venv\Scripts\python.exe
"%~dp0.venv\Scripts\python.exe" "%~dp0main.py"
pause
