@echo off
cd /d "%~dp0"
python "BubbleNav_XPhase.py" %*
if errorlevel 1 pause
