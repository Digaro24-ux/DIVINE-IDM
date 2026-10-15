@echo off
REM DIVINE IDM launcher for Windows: creates a virtual environment, installs everything, starts the app.
cd /d "%~dp0"
if not exist venv ( python -m venv venv || ( echo Python is not installed or not in PATH & pause & exit /b 1 ) )
call venv\Scripts\activate
python -m pip install -q -r requirements.txt
python app.py
pause
