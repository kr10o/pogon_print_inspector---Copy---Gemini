@echo off
setlocal
title DTF/DTG Prepress Indexer Pipeline

:: Force context to the directory where the batch file resides
cd /d "%~dp0"

echo ======================================================
echo    INITIALIZING PREPRESS ENVIRONMENT (.VENV)
echo ======================================================
echo.

:: 1. Verify or initialize virtual environment (.venv)
if exist ".venv\Scripts\python.exe" goto VENV_READY

echo [*] Initializing isolated virtual environment .venv ...
python -m venv .venv
if %errorlevel% neq 0 (
    echo [!] Error: Failed to create .venv. Ensure Python is in your PATH.
    echo.
    pause
    exit /b %errorlevel%
)
echo [+] Virtual environment created successfully.

:VENV_READY
:: 2. Verify and install required dependencies strictly inside .venv
echo [*] Verifying dependencies inside .venv ...
.\.venv\Scripts\python.exe -m pip install --quiet pymupdf pillow pytesseract thefuzz rapidfuzz 2>NUL
if %errorlevel% neq 0 (
    echo [!] Warning: Silent dependency check encountered an issue. Retrying verbosely...
    .\.venv\Scripts\python.exe -m pip install pymupdf pillow pytesseract thefuzz rapidfuzz
)

echo [+] Environment verified (.venv active). Launching pipeline...
echo.

:: 3. Launch the Resumable Pipeline using the virtual environment
.\.venv\Scripts\python.exe prepress_indexer.py

if %errorlevel% neq 0 (
    echo.
    echo [!] Pipeline exited with status code %errorlevel%.
)

echo.
echo ======================================================
echo Execution paused. Press any key to close this window.
echo ======================================================
pause
