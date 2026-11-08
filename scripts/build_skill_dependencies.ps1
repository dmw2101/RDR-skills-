param(
    [Parameter(Mandatory = $true)][string]$Skill,
    [Parameter(Mandatory = $true)][string]$Output,
    [string]$Features,
    [string]$Prompt,
    [string]$Model = 'gemini-3.1-flash-lite',
    [string]$Python = 'python',
    [string]$BaseBundle,
    [string]$ReviewedDependencies,
    [switch]$AcceptGeminiProposal
)

$ErrorActionPreference = 'Stop'
function Invoke-Python([string[]]$CommandArgs) {
    & $Python @CommandArgs
    if ($LASTEXITCODE -ne 0) { throw "Pipeline step failed: $($CommandArgs -join ' ')" }
}

$source = (Resolve-Path -LiteralPath $Skill).Path
$skillName = Split-Path -Leaf $source
$markdown = Join-Path $source 'initial.md'
if (-not (Test-Path -LiteralPath $markdown -PathType Leaf)) { throw "Missing $markdown" }
$root = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Output)
$viewer = Join-Path (Split-Path -Parent $PSScriptRoot) 'skills/visualise_tree.py'
if ($ReviewedDependencies) {
    $review = (Resolve-Path -LiteralPath $ReviewedDependencies).Path
}
if (Test-Path -LiteralPath $root) { throw 'Choose a new output directory.' }
if ($BaseBundle) {
    $base = (Resolve-Path -LiteralPath $BaseBundle).Path
    if ((Split-Path -Leaf $base) -ne $skillName) { throw 'Base bundle must have the same skill name.' }
    if ((Get-FileHash -LiteralPath (Join-Path $base 'initial.md')).Hash -ne
        (Get-FileHash -LiteralPath $markdown).Hash) { throw 'Base bundle Markdown differs from the skill.' }
    if (Test-Path -LiteralPath (Join-Path $base 'compiled/dependencies.json')) {
        throw 'Pass a dependency-free base bundle.'
    }
    New-Item -ItemType Directory -Path $root | Out-Null
} else {
    if (-not $Features) {
        $localFeatures = Join-Path $source 'feature_schema.json'
        $Features = if (Test-Path -LiteralPath $localFeatures -PathType Leaf) { $localFeatures } else {
            Join-Path (Split-Path -Parent $source) 'feature_schemas.json'
        }
    }
    $Features = (Resolve-Path -LiteralPath $Features).Path
    $base = Join-Path $root "base/$skillName"
    $ir = Join-Path $root 'gemini-ir.json'
    $extract = @('-m', 'rdr_engine.skill_compiler', $markdown, '--features', $Features, '--model', $Model, '--output', $ir)
    $localPrompt = Join-Path $source 'extraction_prompt.md'
    if (-not $Prompt -and (Test-Path -LiteralPath $localPrompt -PathType Leaf)) { $Prompt = $localPrompt }
    if ($Prompt) { $extract += @('--prompt', (Resolve-Path -LiteralPath $Prompt).Path) }
    Invoke-Python $extract
    New-Item -ItemType Directory -Path $base | Out-Null
    Copy-Item -LiteralPath $markdown -Destination $base
    Invoke-Python @('-m', 'rdr_engine.skill_tool', 'compile', $base, '--ir', $ir, '--features', $Features, '--model-id', $Model)
}
Invoke-Python @($viewer, (Join-Path $base 'compiled/tree.json'), '-o', (Join-Path $root 'base-tree.html'))
if (-not $ReviewedDependencies) {
    $review = Join-Path $root 'gemini-dependencies.json'
    Invoke-Python @('-m', 'rdr_engine.dependencies', 'propose', $base, $review, '--model', $Model)
    if (-not $AcceptGeminiProposal) {
        Write-Host "Review $review. Rerun with -BaseBundle '$base' -ReviewedDependencies '$review' and a new -Output to build the dependency view."
        return
    }
}
$bundle = Join-Path $root "with-dependencies/$skillName"
Invoke-Python @('-m', 'rdr_engine.dependencies', 'build', $base, $review, $bundle)
Invoke-Python @($viewer, (Join-Path $bundle 'compiled/tree.json'), '-o', (Join-Path $root 'dependency-tree.html'))
