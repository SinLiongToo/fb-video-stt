@echo off
cd /d "%~dp0"
start "" http://localhost:8792/
python server.py 8792
