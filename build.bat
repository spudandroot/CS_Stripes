@echo off
rem Builds dist\Stripes.exe locally. Needs Python 3.10+ from python.org.
rem (Normally GitHub Actions builds it - see README.)
rem   build.bat           version "dev"
rem   build.bat v1.2.0    stamp a version
setlocal
cd /d "%~dp0"
set "VERSION=%~1"
if "%VERSION%"=="" set "VERSION=dev"

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
python make_version.py %VERSION% || goto :fail
pyinstaller --noconfirm --onefile --windowed --name Stripes --icon assets\icon.ico --add-data "assets;assets" --hidden-import pystray._win32 --version-file version_info.txt stripes.py || goto :fail
copy /y dist\Stripes.exe "dist\Stripes-%VERSION%.exe" >nul || goto :fail

echo.
echo Done: %cd%\dist\Stripes-%VERSION%.exe (and dist\Stripes.exe)
pause
exit /b 0

:fail
echo.
echo Build failed - see the messages above.
pause
exit /b 1
