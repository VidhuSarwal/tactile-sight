@echo off
setlocal

where py >nul 2>&1
if %errorlevel% equ 0 ( set PYEXE=py & goto run )
where python >nul 2>&1
if %errorlevel% equ 0 ( set PYEXE=python & goto run )
echo Python not found. Run setup.bat first.
pause & exit /b 1

:run
%PYEXE% check_camera.py %*
endlocal
