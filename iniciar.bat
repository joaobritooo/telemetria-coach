@echo off
title Telemetria Coach
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
python main.py %*
pause
