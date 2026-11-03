$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$work = Join-Path $repo 'experiments\bank-windows'
$python = Join-Path $work '.venv\Scripts\python.exe'

function Invoke-Checked([string]$Program, [string[]]$Arguments) {
    & $Program @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Program failed with exit code $LASTEXITCODE." }
}

Push-Location $repo
try {
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) { throw 'Git is required.' }
    if (-not (Get-Command py -ErrorAction SilentlyContinue)) { throw 'The Python launcher and Python 3.12 are required.' }
    New-Item -ItemType Directory -Force -Path $work | Out-Null
    if (-not (Test-Path -LiteralPath $python)) {
        Write-Host 'Creating the Bank Python 3.12 environment...'
        Invoke-Checked 'py' @('-3.12', '-m', 'venv', (Join-Path $work '.venv'))
    }
    Write-Host 'Installing the Bank dependencies...'
    Invoke-Checked $python @('-m', 'pip', 'install', '-e', '.[skill-service]')
    Write-Host 'Preparing the pinned SOPBench source...'
    Invoke-Checked $python @('scripts/prepare_sopbench_bank.py')
    Write-Host 'Preparing the compiled Bank skill snapshot...'
    Invoke-Checked $python @('-m', 'rdr_service', 'prepare', 'skills/sopbench_bank_fixed_policy', '--store', 'experiments/bank-snapshots', '--output', 'experiments/bank-snapshot-mcrdr.json')
    Write-Host 'Bank setup complete. Next: powershell -ExecutionPolicy Bypass -File scripts/run_bank_powershell.ps1 -Experiment pilot -Name pilot2'
} finally {
    Pop-Location
}
