@echo off
setlocal

where py >nul 2>&1
if %errorlevel% equ 0 ( set PYEXE=py & goto run )
where python >nul 2>&1
if %errorlevel% equ 0 ( set PYEXE=python & goto run )

echo Python not found.
echo Download Python 3.10+ from https://www.python.org/downloads/
echo (tick "Add Python to PATH" during install, then re-run this file)
pause
exit /b 1

:run
%PYEXE% "%~dp0check_camera.py"
pause
endlocal
