@echo off
REM ==== VideoLibraryOptimizer - lancement portable (zero installation) ====
setlocal
cd /d "%~dp0"

set "PY=runtime\python.exe"
set "HOST=127.0.0.1"
set "PORT=8077"
set "VLO_FFMPEG_PATH=%~dp0ffmpeg\bin\ffmpeg.exe"
set "VLO_FFPROBE_PATH=%~dp0ffmpeg\bin\ffprobe.exe"
set "VLO_DB_PATH=%~dp0data\vlo.db"
set "VLO_WORK_DIR=%~dp0work"
set "PYTHONPATH=%~dp0backend"

if not exist "%PY%" (
    echo [VLO] ERREUR : runtime Python introuvable. Paquet incomplet ?
    pause
    exit /b 1
)

REM --- Premier lancement : telecharger ffmpeg (~80 Mo) si absent ---
if not exist "ffmpeg\bin\ffmpeg.exe" (
    echo [VLO] Telechargement de ffmpeg ^(une seule fois^)...
    "%PY%" tools\fetch_ffmpeg.py || ( echo [VLO] Echec du telechargement de ffmpeg. & pause & exit /b 1 )
)

echo.
echo [VLO] Demarrage sur http://%HOST%:%PORT%
echo [VLO] Fermez cette fenetre pour arreter l'application.
echo.

start "" /b cmd /c "timeout /t 2 >nul & start http://%HOST%:%PORT%"
"%PY%" -m uvicorn vlo.main:app --host %HOST% --port %PORT%

pause
