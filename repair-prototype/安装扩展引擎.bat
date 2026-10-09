@echo off
cd /d "%~dp0"
python setup_extensions.py
if errorlevel 1 echo Installation failed. Please check your network and try again.
pause
