<#
.SYNOPSIS
  One-command local start on synthetic data (milestone M1: "app starts locally with one command").
.DESCRIPTION
  Copies .env.example to .env (with a generated SECRET_KEY), creates the SQLite schema with Alembic,
  creates a clinic and a reviewer account (password prompt + TOTP QR code), generates a synthetic
  batch, imports claims/notes/documents/835s, runs the pipeline and prints the URL to open after
  `denial-agent serve`.
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\bootstrap.ps1 -Email reviewer@example.test
#>
param(
    [string]$Email = "",
    [int]$Claims = 200,
    [int]$Seed = 20261028,
    [string]$OutDir = "data\synthetic"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$Python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    if (Get-Command uv -ErrorAction SilentlyContinue) {
        Write-Host "Creating the virtual environment with uv sync ..."
        uv sync
        if ($LASTEXITCODE -ne 0) { throw "uv sync failed" }
    } else {
        throw "No .venv found and uv is not installed. Install uv (https://docs.astral.sh/uv/) and run 'uv sync'."
    }
}

function Invoke-Agent {
    & $Python -m app.cli @args
    if ($LASTEXITCODE -ne 0) { throw "denial-agent $($args -join ' ') failed (exit $LASTEXITCODE)" }
}

if (-not (Test-Path ".env")) {
    $secret = & $Python -c "import secrets; print(secrets.token_urlsafe(48))"
    $lines = Get-Content ".env.example" | ForEach-Object { $_ -replace '^SECRET_KEY=.*$', "SECRET_KEY=$secret" }
    [System.IO.File]::WriteAllLines((Join-Path $Root ".env"), $lines)   # UTF-8 without BOM
    Write-Host "Wrote .env with a generated SECRET_KEY (edit it later to enable the real model)."
}

New-Item -ItemType Directory -Force -Path "data" | Out-Null

Write-Host "== Secret scanning hook (gitleaks through pre-commit, on every commit)"
$PreCommit = Join-Path (Split-Path -Parent $Python) "pre-commit.exe"
if (-not (Test-Path ".git")) {
    Write-Host "(not a git checkout: no hook to install)"
} elseif (Test-Path $PreCommit) {
    & $PreCommit install
    if ($LASTEXITCODE -ne 0) { throw "pre-commit install failed (exit $LASTEXITCODE)" }
} else {
    Write-Warning ("pre-commit is not installed, so commits are NOT scanned for secrets. " +
        "Run 'uv sync' (the dev dependencies include pre-commit), then 'uv run pre-commit install'.")
}

Write-Host "== Database schema (alembic upgrade head)"
Invoke-Agent init-db --migrate

Write-Host "== Clinic"
$clinicJson = & $Python -m app.cli create-clinic --name "Synthetic Pilot Clinic" --pm-system synthetic
if ($LASTEXITCODE -ne 0) { throw "create-clinic failed" }
$clinicId = (($clinicJson -join "") | ConvertFrom-Json).clinic_id

Write-Host "== Reviewer account (you will be asked for a password; scan the QR code with an authenticator app)"
if (-not $Email) { $Email = Read-Host "Reviewer email" }
Invoke-Agent create-user --email $Email --role admin

Write-Host "== Synthetic data ($Claims claims, seed $Seed) -> $OutDir"
Invoke-Agent generate-synthetic --out $OutDir --seed $Seed --claims $Claims

Write-Host "== Imports"
Invoke-Agent import-claims (Join-Path $OutDir "claims_export.csv") --clinic-id $clinicId
if (Test-Path (Join-Path $OutDir "notes.csv")) {
    Invoke-Agent import-notes (Join-Path $OutDir "notes.csv") --clinic-id $clinicId
}
if (Test-Path (Join-Path $OutDir "payer_documents.csv")) {
    Invoke-Agent import-documents (Join-Path $OutDir "payer_documents.csv") --clinic-id $clinicId
}
Get-ChildItem -Path $OutDir -Filter "remit_*.835" | Sort-Object Name | ForEach-Object {
    Invoke-Agent import-835 $_.FullName
}

Write-Host "== Pipeline"
Invoke-Agent process

Write-Host ""
Write-Host "Done. Start the app with:  $Python -m app.cli serve"
Write-Host "Then open: http://127.0.0.1:8000"
