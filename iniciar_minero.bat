@echo off
rem Minero de Bitcoin en solitario. Doble clic para empezar.
rem Puedes pasar opciones, p. ej.: iniciar_minero.bat --pool solo.ckpool.org:3333
cd /d "%~dp0"
title Minero BTC en solitario
call :buscar_python
if not defined PY goto sin_python
%PY% minero.py %*
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
