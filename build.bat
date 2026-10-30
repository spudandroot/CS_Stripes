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
rem hx_key.py holds the printer-password key. make_version.py writes it only when this shell
rem has STRIPES_HX_KEY set, and it is deleted again once the exe is built: tests never see it.
if exist hx_key.py del /f hx_key.py
python -m unittest || goto :fail
python make_version.py %VERSION% || goto :fail
set "KEYED="
if exist hx_key.py set "KEYED=1"
pyinstaller --noconfirm pyinstaller.spec || goto :fail
if exist hx_key.py del /f hx_key.py
copy /y dist\Stripes.exe "dist\Stripes-%VERSION%.exe" >nul || goto :fail

echo.
echo Done: %cd%\dist\Stripes-%VERSION%.exe (and dist\Stripes.exe)
if not defined KEYED echo Local builds have the development key: printer passwords saved with them don't work in a
if not defined KEYED echo release. Only put releases from GitHub on the share.
pause
exit /b 0

:fail
if exist hx_key.py del /f hx_key.py
echo.
echo Build failed - see the messages above.
pause
exit /b 1
