# ==== VideoLibraryOptimizer - lancement simple (PowerShell) ====
# Usage : clic droit > "Exécuter avec PowerShell", ou  ./start.ps1
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

$PyExe = ".\.venv\Scripts\python.exe"
$VLOHost = "127.0.0.1"
$Port = "8077"

# --- Première utilisation : créer le venv et installer ---
if (-not (Test-Path $PyExe)) {
    Write-Host "[VLO] Première installation : création de l'environnement Python..." -ForegroundColor Cyan
    $py = (Get-Command py -ErrorAction SilentlyContinue) ? "py" : "python"
    & $py -m venv .venv
    if (-not (Test-Path $PyExe)) {
        Write-Host "[VLO] ERREUR : Python introuvable. Installez Python 3.11+ depuis python.org." -ForegroundColor Red
        Read-Host "Appuyez sur Entrée pour fermer"; exit 1
    }
    Write-Host "[VLO] Installation des dépendances (peut prendre une minute)..." -ForegroundColor Cyan
    & $PyExe -m pip install --upgrade pip
    & $PyExe -m pip install -e .
}

# --- Vérifier ffmpeg ---
if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
    Write-Host "[VLO] ATTENTION : ffmpeg introuvable dans le PATH (l'encodage échouera)." -ForegroundColor Yellow
}

Write-Host ""
Write-Host "[VLO] Démarrage sur http://$VLOHost`:$Port" -ForegroundColor Green
Write-Host "[VLO] Ctrl+C pour arrêter." -ForegroundColor Green
Write-Host ""

# --- Ouvrir le navigateur après un court délai, puis lancer le serveur ---
Start-Job { Start-Sleep 2; Start-Process "http://$using:VLOHost`:$using:Port" } | Out-Null
& $PyExe -m uvicorn vlo.main:app --host $VLOHost --port $Port --reload --reload-dir backend
