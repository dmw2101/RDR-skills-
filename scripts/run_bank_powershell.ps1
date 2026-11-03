param(
    [ValidateSet('pilot', 'dev34', 'affected6', 'initial_only', 'initial_with_requests', 'initial_only34', 'initial_with_requests34', 'initial_requests_retry12')][string]$Experiment = 'pilot',
    [Parameter(Mandatory = $true)][ValidatePattern('^[a-zA-Z0-9][a-zA-Z0-9_-]*$')][string]$Name,
    [switch]$Resume,
    [switch]$CheckOnly,
    [string]$DependencyBundle,
    [switch]$SingleSelection
)

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$work = Join-Path $repo 'experiments\bank-windows'
$python = Join-Path $work '.venv\Scripts\python.exe'
$runRoot = Join-Path $work "runs\$Name"
$configPath = Join-Path $runRoot 'experiment-config.json'
$resultPath = Join-Path $runRoot 'results'
$service = $null
$priorKey = $env:MODEL_API_KEY

function Invoke-Checked([string]$Program, [string[]]$Arguments) {
    & $Program @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Program failed with exit code $LASTEXITCODE." }
}

if (-not (Test-Path -LiteralPath $python)) { throw 'Run scripts/setup_bank_powershell.ps1 first.' }
if ($Resume -and $CheckOnly) { throw 'Use -Resume or -CheckOnly, not both.' }
if ($DependencyBundle -and ($Experiment -ne 'initial_only' -or $Resume)) {
    throw 'Use -DependencyBundle with a new initial_only experiment.'
}
if ($SingleSelection -and ($Experiment -notin @('initial_only', 'initial_with_requests') -or $Resume)) {
    throw 'Use -SingleSelection with a new initial-guidance pilot.'
}
if ($Resume -and -not (Test-Path -LiteralPath $configPath)) { throw 'No saved configuration exists for this run name.' }
if (-not $Resume -and (Test-Path -LiteralPath $resultPath)) { throw "Results already exist at $resultPath. Choose a new -Name or use -Resume." }
New-Item -ItemType Directory -Force -Path $runRoot | Out-Null

if (-not $Resume) {
    $template = Join-Path $repo "examples\agent\sopbench.bank.$Experiment.json"
    $config = Get-Content -LiteralPath $template -Raw | ConvertFrom-Json
    $config.run.output_dir = $resultPath
    if ($SingleSelection -or $DependencyBundle) {
        $config.skill.resolver.options | Add-Member -NotePropertyName selection_attempts -NotePropertyValue 1
    }
    $json = $config | ConvertTo-Json -Depth 100
    [System.IO.File]::WriteAllText($configPath, $json + "`n", (New-Object System.Text.UTF8Encoding($false)))
} else {
    $config = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
    if ($config.run.output_dir -ne $resultPath) { throw 'Saved configuration points to a different results directory.' }
}

Push-Location $repo
try {
    # Refuse to connect to or stop a service started by someone else.
    $listener = New-Object System.Net.Sockets.TcpListener([System.Net.IPAddress]::Parse('127.0.0.1'), 8766)
    try { $listener.Start() } catch { throw 'Port 8766 is in use. Stop the existing service before running this script.' } finally { $listener.Stop() }
    $outLog = Join-Path $runRoot 'service.log'
    $errLog = Join-Path $runRoot 'service-error.log'
    $service = Start-Process -FilePath $python -ArgumentList '-m rdr_service serve --store experiments/bank-snapshots --port 8766' -WorkingDirectory $repo -WindowStyle Hidden -RedirectStandardOutput $outLog -RedirectStandardError $errLog -PassThru
    $healthy = $false
    for ($i = 0; $i -lt 100; $i++) {
        if ($service.HasExited) { throw "Skill service exited. Check $errLog and $outLog." }
        try {
            $health = Invoke-RestMethod -Uri 'http://127.0.0.1:8766/health' -TimeoutSec 1
            if ($health.status -eq 'ok' -and $health.contract -eq 'v1') { $healthy = $true; break }
        } catch { }
        Start-Sleep -Milliseconds 200
    }
    if (-not $healthy) { throw "Skill service did not start. Check $errLog and $outLog." }

    if (-not $Resume -and $Experiment -in @('initial_only', 'initial_with_requests', 'initial_only34', 'initial_with_requests34', 'initial_requests_retry12')) {
        $receiptPath = Join-Path $runRoot 'skill-snapshot.json'
        $bundle = if ($DependencyBundle) { $DependencyBundle } else { 'skills/sopbench_bank_fixed_policy' }
        Invoke-Checked $python @('-m', 'rdr_service', 'prepare', $bundle, '--store', 'experiments/bank-snapshots', '--output', $receiptPath)
        $config.skill.receipt = $receiptPath
        [System.IO.File]::WriteAllText($configPath, ($config | ConvertTo-Json -Depth 100) + "`n", (New-Object System.Text.UTF8Encoding($false)))
    }

    if ($CheckOnly -and -not $env:MODEL_API_KEY) { $env:MODEL_API_KEY = 'not-used-for-doctor' }
    if (-not $env:MODEL_API_KEY) {
        $secureKey = Read-Host 'OpenRouter API key' -AsSecureString
        $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureKey)
        try { $env:MODEL_API_KEY = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer) }
        finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer) }
        if (-not $env:MODEL_API_KEY) { throw 'An API key is required.' }
    }
    Write-Host 'Checking the Bank setup...'
    Invoke-Checked $python @('-m', 'skill_agent', 'doctor', '--config', $configPath)
    if (-not $CheckOnly) {
        Write-Host "Running $Experiment. Results: $resultPath"
        $argsList = @('-m', 'skill_agent', 'run', '--config', $configPath)
        if ($Resume) { $argsList += '--resume' }
        Invoke-Checked $python $argsList
        if ($config.run.skill_loading -in @('initial_only', 'initial_with_requests')) {
            $summaryText = (Invoke-Checked $python @('-m', 'skill_agent', 'summarize', $resultPath)) -join "`n"
            $summary = $summaryText | ConvertFrom-Json
            $summary.benchmark_summaries | Format-Table condition, cases, successes, success_rate, median_input_tokens, total_cost_usd
            $usage = $summary.usage.reduced
            Write-Host ("Inference: {0} input / {1} output tokens. Selector: {2} input / {3} output tokens." -f $usage.inference_usage.input_tokens, $usage.inference_usage.output_tokens, $usage.selector_usage.input_tokens, $usage.selector_usage.output_tokens)
            $inspectionText = (Invoke-Checked $python @('-m', 'skill_agent', 'inspect-skills', $resultPath)) -join "`n"
            $inspectionPath = Join-Path $runRoot 'skill-inspection.json'
            [System.IO.File]::WriteAllText($inspectionPath, $inspectionText + "`n", (New-Object System.Text.UTF8Encoding($false)))
            ($inspectionText | ConvertFrom-Json).episodes | Format-Table task_id, status, model_followup_skill_calls, distinct_guidance_views
            Write-Host "Detailed selections: $inspectionPath"
        } else {
            Invoke-Checked $python @('-m', 'skill_agent', 'summarize', $resultPath)
            Invoke-Checked $python @('-m', 'skill_agent', 'inspect-skills', $resultPath)
        }
        Write-Host "Results and configuration saved at $runRoot"
    } else {
        Write-Host 'Setup check passed. No model calls were made.'
    }
} finally {
    if ($service -and -not $service.HasExited) { Stop-Process -Id $service.Id -ErrorAction SilentlyContinue }
    $env:MODEL_API_KEY = $priorKey
    Pop-Location
}
