@echo off
chcp 65001 >nul
title 서버 종료
set FOUND=0
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8000 " ^| findstr "LISTENING"') do (
  set FOUND=1
  taskkill /PID %%a /F >nul 2>nul
)
if "%FOUND%"=="1" (echo 8000번 포트의 서버를 종료했습니다.) else (echo 실행 중인 서버를 찾지 못했습니다.)
pause