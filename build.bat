@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    where uv >nul 2>&1
    if errorlevel 1 (
        py -3.12 -m venv .venv || exit /b 1
    ) else (
        uv venv --python 3.12 .venv || exit /b 1
    )
)

set "PYTHON=.venv\Scripts\python.exe"
where uv >nul 2>&1
if errorlevel 1 (
    "%PYTHON%" -m pip install -r requirements.txt || exit /b 1
) else (
    uv pip install --python "%PYTHON%" -r requirements.txt || exit /b 1
)

set "QT_QPA_PLATFORM=offscreen"
"%PYTHON%" -m unittest discover -s tests || exit /b 1
"%PYTHON%" -m PyInstaller --noconfirm --clean --onedir --windowed --name DriveReview main.py || exit /b 1
echo Built dist\DriveReview\DriveReview.exe
