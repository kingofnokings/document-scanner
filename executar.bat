@echo off
rem ============================================================
rem  Scanner de Documentos - executa o programa no Windows,
rem  instalando o que faltar.
rem
rem  Uso: de dois cliques neste arquivo.
rem
rem  O que ele faz, nesta ordem (e so o que for necessario):
rem    1. confere o Python 3;
rem    2. cria o ambiente virtual .venv;
rem    3. instala as bibliotecas Python (requirements.txt);
rem    4. confere o Tesseract OCR e, se faltar, instala com o winget;
rem    5. abre o programa.
rem  Nao e preciso configurar variaveis de ambiente nem instalar
rem  o idioma portugues.
rem ============================================================
setlocal
cd /d "%~dp0"
chcp 65001 >nul
set PYTHONUTF8=1
title Scanner de Documentos

echo ==================================================
echo   Scanner de Documentos
echo ==================================================

rem ---- 1) Python 3 -----------------------------------------
set "PY="
py -3 -c "import sys" >nul 2>nul
if not errorlevel 1 set "PY=py -3"
if not defined PY (
  python -c "import sys" >nul 2>nul
  if not errorlevel 1 set "PY=python"
)
if not defined PY goto :sem_python

rem ---- 2) Ambiente virtual ---------------------------------
if not exist ".venv\Scripts\python.exe" (
  echo Criando o ambiente virtual...
  %PY% -m venv .venv
  if errorlevel 1 goto :erro
)
set "VPY=.venv\Scripts\python.exe"

rem ---- 3) Bibliotecas Python (so na 1a vez ou se requirements.txt mudar) ----
fc /b requirements.txt ".venv\requirements.instalado.txt" >nul 2>nul
if errorlevel 1 (
  echo Instalando as bibliotecas Python ^(so na primeira vez; pode demorar alguns minutos^)...
  "%VPY%" -m pip install --upgrade pip >nul 2>nul
  "%VPY%" -m pip install -r requirements.txt
  if errorlevel 1 goto :erro
  copy /y requirements.txt ".venv\requirements.instalado.txt" >nul
)

rem ---- 4) Tesseract OCR ------------------------------------
"%VPY%" -m nucleo.verificar_ambiente --silencioso
if not errorlevel 1 goto :iniciar

echo.
echo O Tesseract OCR nao foi encontrado. Tentando instalar com o winget...
echo ^(o Windows pode pedir permissao de administrador: clique em Sim^)
where winget >nul 2>nul
if errorlevel 1 goto :sem_tesseract
winget install --id UB-Mannheim.TesseractOCR -e --silent --accept-package-agreements --accept-source-agreements
"%VPY%" -m nucleo.verificar_ambiente --silencioso
if errorlevel 1 goto :sem_tesseract

rem ---- 5) Abrir o programa ---------------------------------
:iniciar
echo Abrindo o programa...
"%VPY%" main.py
if errorlevel 1 goto :erro
goto :fim

:sem_python
echo.
echo [ERRO] Python 3 nao encontrado.
echo Instale em https://www.python.org/downloads/ e MARQUE a opcao "Add python.exe to PATH".
echo Depois execute este arquivo novamente.
goto :erro_pausa

:sem_tesseract
echo.
echo [ERRO] Nao foi possivel instalar o Tesseract automaticamente.
echo Instale manualmente: https://github.com/UB-Mannheim/tesseract/wiki
echo ^(nao precisa configurar o PATH: o programa encontra o Tesseract sozinho^)
goto :erro_pausa

:erro
echo.
echo [ERRO] Algo deu errado. Leia a mensagem acima.
:erro_pausa
pause
exit /b 1

:fim
endlocal
