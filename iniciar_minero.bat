@echo off
rem Minero de Bitcoin en solitario. Doble clic para empezar.
rem Puedes pasar opciones, p. ej.: iniciar_minero.bat --pool solo.ckpool.org:3333
cd /d "%~dp0"
title Minero BTC en solitario
where py >nul 2>nul
if not errorlevel 1 (
    py -3 minero.py %*
    goto fin
)
where python >nul 2>nul
if not errorlevel 1 (
    python minero.py %*
    goto fin
)
echo No se ha encontrado Python.
echo Instalalo desde https://www.python.org/downloads/ marcando "Add python.exe to PATH".
:fin
pause
