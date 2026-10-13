@echo off
rem Mide la velocidad de tu CPU sin conectarse a ningun pool.
cd /d "%~dp0"
where py >nul 2>nul
if not errorlevel 1 (
    py -3 minero.py --benchmark %*
    goto fin
)
python minero.py --benchmark %*
:fin
pause
