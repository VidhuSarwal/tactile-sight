@echo off
setlocal

:: pip is present but "python" may not be in PATH.
:: Try the Windows Python Launcher (py), then fall back to python.
where py >nul 2>&1
if %errorlevel% equ 0 (
    set PYEXE=py
    goto found
)
where python >nul 2>&1
if %errorlevel% equ 0 (
    set PYEXE=python
    goto found
)

echo Python launcher not found.
echo pip is installed but the Python executable is not in PATH.
echo Fix: re-run the Python installer and tick "Add Python to PATH".
echo Download: https://www.python.org/downloads/
pause
exit /b 1

:found
echo Using: %PYEXE%
echo.
echo Installing dependencies...
pip install numpy pyyaml openni
if %errorlevel% neq 0 (
    echo pip install failed. Check your internet connection.
    pause
    exit /b 1
)
echo.
echo Done.
echo   Run a scene :  run.bat --scene wall_approach
echo   Check camera:  check_camera.bat
pause
endlocal
