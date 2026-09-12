@echo off
REM Launcher for the Crafty panel (with the Content module)
cd /d "%~dp0"
echo Arrancando Crafty Controller...
echo Abre https://192.168.0.19:8443  (usuario/clave en app\config\default-creds.txt)
echo.
".venv\Scripts\python.exe" main.py
pause
