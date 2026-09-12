@echo off
chcp 65001 >nul
title 정보 아틀리에 + 퀴즈 라이브 서버
cd /d "%~dp0"
netstat -ano | findstr ":8000 " | findstr "LISTENING" >nul 2>nul
if not errorlevel 1 (
  echo [!] 포트 8000이 이미 사용 중입니다 - 서버가 이미 실행 중일 수 있습니다.
  echo     브라우저에서 http://localhost:8000/lab 을 열어 확인해 보세요.
  pause
  exit /b
)
where py >nul 2>nul
if errorlevel 1 (set "PYCMD=python") else (set "PYCMD=py -3")
echo ================================================
echo   정보 아틀리에 + 퀴즈 라이브 서버를 시작합니다 (포트 8000)
echo   교사: http://localhost:8000/lab/#/teacher
echo   학생: http://내부IP:8000/lab/#/student  (시작 화면에 IP 표시)
echo   종료: Ctrl+C 또는 창 닫기
echo ================================================
:loop
%PYCMD% server.py
if errorlevel 2 (
  echo 서버 시작에 실패했습니다. 포트 사용 여부를 확인해 주세요.
  pause
  exit /b
)
if not errorlevel 1 (
  echo 서버가 종료되었습니다.
  pause
  exit /b
)
echo.
echo [자동 재시작] 서버가 비정상 종료되어 3초 후 재시작합니다...
timeout /t 3 /nobreak >nul
goto loop