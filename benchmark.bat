@echo off
rem Mide la velocidad de tu CPU sin conectarse a ningun pool.
cd /d "%~dp0"
title Benchmark del minero BTC
call :buscar_python
if not defined PY goto sin_python
%PY% minero.py --benchmark %*
goto fin

:buscar_python
set "PY="
py -3 -c "import sys; sys.exit(sys.version_info < (3, 9))" >nul 2>nul && set "PY=py -3"
if defined PY exit /b
python -c "import sys; sys.exit(sys.version_info < (3, 9))" >nul 2>nul && set "PY=python"
exit /b

:sin_python
echo No se ha encontrado Python 3.9 o superior.
echo Instalalo desde https://www.python.org/downloads/ marcando "Add python.exe to PATH".

:fin
pause
