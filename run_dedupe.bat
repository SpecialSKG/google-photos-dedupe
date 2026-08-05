@echo off
setlocal
cd /d "%~dp0"

set "PYEXE=.venv\Scripts\python.exe"
set "PYW=.venv\Scripts\pythonw.exe"

rem Asegura el entorno virtual y las dependencias la primera vez.
if not exist "%PYEXE%" (
  echo Creando el entorno virtual...
  python -m venv .venv
  if errorlevel 1 ( echo No se pudo crear .venv. Asegurate de tener Python instalado. & exit /b 1 )
  "%PYEXE%" -m pip install -r requirements.txt
  if errorlevel 1 ( echo Error al instalar dependencias. & exit /b 1 )
)

if /i "%~1"=="gui" (
  start "" "%PYW%" "%~dp0gui.py"
  exit /b 0
)

"%PYEXE%" -m photos_dedupe %*
exit /b %errorlevel%