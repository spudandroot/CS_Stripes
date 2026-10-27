@echo off
rem Builds dist\Stripes.exe locally. Needs Python 3.10+ from python.org.
rem (Normally GitHub Actions builds it - see README.)
setlocal
cd /d "%~dp0"

where py >nul 2>nul
if errorlevel 1 (
    echo Python is not installed. Get it from https://www.python.org/downloads/
    pause
    exit /b 1
)

if not exist .venv py -m venv .venv || goto :fail
call .venv\Scripts\activate.bat || goto :fail
python -m pip install --quiet --upgrade pip
python -m pip install --quiet -r requirements.txt pyinstaller || goto :fail
python -m unittest || goto :fail
pyinstaller --noconfirm --onefile --windowed --name Stripes --icon assets\icon.ico --add-data "assets;assets" --hidden-import pystray._win32 stripes.py || goto :fail

echo.
echo Done: %cd%\dist\Stripes.exe
pause
exit /b 0

:fail
echo.
echo Build failed - see the messages above.
pause
exit /b 1
