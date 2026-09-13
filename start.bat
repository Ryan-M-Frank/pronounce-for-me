@echo off
rem Runs pronounce-for-me with a visible console so you can see what it hears.
rem Any arguments are passed through, e.g.:  start.bat --list-voices
cd /d "%~dp0"
if exist "python\python.exe" (
    "python\python.exe" pronounce_for_me.py %*
) else (
    python pronounce_for_me.py %*
)
