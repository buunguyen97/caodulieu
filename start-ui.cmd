@echo off
cd /d "%~dp0"
echo Mo http://127.0.0.1:8766 trong trinh duyet.
".venv\Scripts\python.exe" -m garvee_crawler.web --port 8766
pause
