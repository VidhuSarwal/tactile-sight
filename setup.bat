@echo off
setlocal

where py >nul 2>&1
if %errorlevel% equ 0 ( set PYEXE=py & goto install )
where python >nul 2>&1
if %errorlevel% equ 0 ( set PYEXE=python & goto install )

echo Python not found in PATH.
echo Download Python 3.10+ from https://www.python.org/downloads/
echo (tick "Add Python to PATH" during install)
pause
exit /b 1

:install
echo Using: %PYEXE%
echo.
echo Installing dependencies...
pip install numpy pyyaml pyorbbecsdk2
if %errorlevel% neq 0 (
    echo.
    echo pip install failed. Check your internet connection and try again.
    pause
    exit /b 1
)
echo.
echo Done.
echo   Run a scene :  run.bat --scene wall_approach
echo   Check camera:  check_camera.bat
pause
endlocal
