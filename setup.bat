@echo off
echo Installing Python dependencies...
pip install numpy pyyaml openni
if %errorlevel% neq 0 (
    echo.
    echo pip failed. Make sure Python 3.10+ is installed and in PATH.
    echo Download Python: https://www.python.org/downloads/
    pause
    exit /b 1
)
echo.
echo Done. Run the pipeline with:
echo     run.bat --scene wall_approach
echo.
echo To verify the camera:
echo     python check_camera.py
pause
