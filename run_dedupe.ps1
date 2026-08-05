<#
.SYNOPSIS
  Ejecuta google-photos-dedupe usando el entorno virtual del proyecto.

.EXAMPLE
  .\run_dedupe.ps1                     # dry-run por defecto
  .\run_dedupe.ps1 -Action copy
  .\run_dedupe.ps1 -Action move -ConfirmMove
  .\run_dedupe.ps1 -Config otro.yaml -Action dry-run
.PARAMETER Config
  Ruta del archivo YAML de configuracion (por defecto: config.yaml).
.PARAMETER Action
  Accion a ejecutar: dry-run | copy | move.
.PARAMETER ConfirmMove
  Requerido para la accion move (destructiva).
#>
param(
  [string]$Config = "config.yaml",
  [ValidateSet("dry-run", "copy", "move")]
  [string]$Action = "dry-run",
  [switch]$ConfirmMove
)

$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $MyInvocation.MyCommand.Path)

$py = Join-Path (Get-Location) ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
  Write-Host "Creando el entorno virtual e instalando dependencias..."
  python -m venv .venv
  & $py -m pip install -r requirements.txt
}

$cliArgs = @("--config", $Config, "--action", $Action)
if ($ConfirmMove) { $cliArgs += "--confirm-move" }

& $py -m photos_dedupe @cliArgs
exit $LASTEXITCODE