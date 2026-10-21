@echo off
rem Runs Stripes straight from source (no build needed).
cd /d "%~dp0"
py -m pip install --quiet -r requirements.txt
start "" pyw stripes.py
