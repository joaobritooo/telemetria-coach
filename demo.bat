@echo off
title Telemetria Coach (demo)
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
python main.py --demo %*
pause
