@echo off
rem toolwatch server launcher (run from repo root; ASCII only - cmd reads CP949)
cd /d "%~dp0"
rem daily DB snapshot before start (server is down, plain copy is safe)
if not exist backups mkdir backups
powershell -NoProfile -Command "Copy-Item src\server\toolwatch.db -Destination ('backups\toolwatch-' + (Get-Date -Format yyyyMMdd) + '.db') -Force" >nul 2>&1
.venv\Scripts\python.exe src\server\app.py
pause
