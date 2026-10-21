[CmdletBinding()]
param([Parameter(ValueFromRemainingArguments = $true)][string[]]$BridgeArguments)
$ErrorActionPreference = 'Stop'
$bridgeScript = (Join-Path $PSScriptRoot 'trace2skill_bridge.sh').Replace('\', '/')
$resolvedBridgeScript = & wsl.exe -d Ubuntu -- wslpath -a -u $bridgeScript
if ($LASTEXITCODE -ne 0 -or $null -eq $resolvedBridgeScript) { throw 'Could not resolve the WSL launcher path.' }
$linuxBridgeScript = $resolvedBridgeScript.Trim()
& wsl.exe -d Ubuntu -- bash $linuxBridgeScript @BridgeArguments
exit $LASTEXITCODE
