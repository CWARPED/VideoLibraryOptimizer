@echo off
REM ==== VideoLibraryOptimizer - lancement simple (double-cliquable) ====
setlocal
cd /d "%~dp0"

set "PYEXE=.venv\Scripts\python.exe"
set "HOST=127.0.0.1"
set "PORT=8077"

REM --- Premiere utilisation : creer le venv et installer les dependances ---
if not exist "%PYEXE%" (
    echo [VLO] Premiere installation : creation de l'environnement Python...
    where py >nul 2>nul && ( py -m venv .venv ) || ( python -m venv .venv )
    if not exist "%PYEXE%" (
        echo [VLO] ERREUR : Python introuvable. Installez Python 3.11+ depuis python.org.
        pause
        exit /b 1
    )
    echo [VLO] Installation des dependances ^(peut prendre une minute^)...
    "%PYEXE%" -m pip install --upgrade pip
    "%PYEXE%" -m pip install -e .
)

REM --- Verifier ffmpeg ---
where ffmpeg >nul 2>nul || echo [VLO] ATTENTION : ffmpeg introuvable dans le PATH (l'encodage echouera).

echo.
echo [VLO] Demarrage sur http://%HOST%:%PORT%
echo [VLO] Fermez cette fenetre pour arreter l'application.
echo.

REM --- Ouvrir le navigateur apres un court delai, puis lancer le serveur ---
start "" /b cmd /c "timeout /t 2 >nul & start http://%HOST%:%PORT%"
"%PYEXE%" -m uvicorn vlo.main:app --host %HOST% --port %PORT% --reload --reload-dir backend

pause
