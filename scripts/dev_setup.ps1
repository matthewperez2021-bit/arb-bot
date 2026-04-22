Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

Write-Host "== Arb bot dev setup (Windows PowerShell) =="

if (-not (Test-Path ".\.venv")) {
  Write-Host "Creating virtualenv .venv..."
  python -m venv .venv
}

Write-Host "Activating .venv..."
. .\.venv\Scripts\Activate.ps1

Write-Host "Upgrading pip..."
python -m pip install -U pip

Write-Host "Installing requirements..."
python -m pip install -r requirements.txt

if (-not (Test-Path ".\config\secrets.env")) {
  Write-Host "Creating config\secrets.env from example..."
  Copy-Item ".\config\secrets.env.example" ".\config\secrets.env"
  Write-Host "NOTE: fill in your API keys in config\secrets.env"
}

Write-Host "Running tests..."
python -m pytest -q

Write-Host "Done."

