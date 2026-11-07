@echo off
chcp 65001 > nul
title Sistema de Gestión Unificado (Next-Gen)
color 0b

echo ======================================================================
echo    SISTEMA DE GESTION UNIFICADO (NEXT-GEN)
echo    PC de Mostrador + Celulares y Tablets Sincronizados por Wi-Fi
echo ======================================================================
echo.

cd /d "%~dp0"

REM Detectar Python del sistema o entorno virtual
set "PYTHON_EXE=python"
if exist ".venv\Scripts\python.exe" set "PYTHON_EXE=.venv\Scripts\python.exe"
if exist "..\sistema_gestion\.venv\Scripts\python.exe" set "PYTHON_EXE=..\sistema_gestion\.venv\Scripts\python.exe"

%PYTHON_EXE% --version >nul 2>nul
if %ERRORLEVEL% neq 0 (
    color 0c
    echo [ERROR] No se encontro Python en esta computadora.
    echo Por favor instala Python 3.10 o superior marcando 'Add Python to PATH'.
    echo Descarga: https://www.python.org/downloads/
    echo.
    pause
    exit /b 1
)

%PYTHON_EXE% -c "import fastapi, uvicorn, qrcode" >nul 2>nul
if %ERRORLEVEL% neq 0 (
    echo [INFO] Verificando dependencias necesarias...
    echo Esto se hace la primera vez y demora unos segundos.
    echo.
    %PYTHON_EXE% -m pip install -r requirements.txt
    echo.
)

%PYTHON_EXE% servidor_red.py

if %ERRORLEVEL% neq 0 (
    echo.
    echo El servidor se detuvo o encontro un error al iniciar.
    pause
)
