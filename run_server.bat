@echo off
rem toolwatch 서버 기동 — config의 상대경로(인증서 등) 때문이 아니라도 저장소 루트에서 실행
cd /d "%~dp0"
.venv\Scripts\python.exe src\server\app.py
pause
