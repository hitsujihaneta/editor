@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

set "CONDA_ACTIVATE="
for %%P in (
    "%USERPROFILE%\anaconda3"
    "%USERPROFILE%\miniconda3"
    "%LOCALAPPDATA%\anaconda3"
    "%LOCALAPPDATA%\miniconda3"
    "%ProgramData%\anaconda3"
    "%ProgramData%\miniconda3"
) do (
    if not defined CONDA_ACTIVATE (
        if exist "%%~P\Scripts\activate.bat" (
            set "CONDA_ACTIVATE=%%~P\Scripts\activate.bat"
        )
    )
)

if not defined CONDA_ACTIVATE (
    echo [ERROR] Could not find Anaconda / Miniconda.
    echo Right-click this file, choose Edit, and set CONDA_ACTIVATE
    echo to the path of your anaconda3 folder.
    echo   example: set "CONDA_ACTIVATE=C:\Users\yourname\anaconda3\Scripts\activate.bat"
    pause
    exit /b 1
)

call "%CONDA_ACTIVATE%" labelme
if errorlevel 1 (
    echo [ERROR] Failed to activate the "labelme" conda environment.
    echo Make sure it has been created with: conda create -n labelme
    pause
    exit /b 1
)

python main.py
if errorlevel 1 (
    echo.
    echo The app exited with an error.
    pause
)
