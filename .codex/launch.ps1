[CmdletBinding(PositionalBinding=$false)]
param(
    [string]$Model,
    [string]$Effort,
    [switch]$CheckOnly,
    [switch]$Desktop,
    [Parameter(ValueFromRemainingArguments=$true)]
    [string[]]$CodexArgs
)

$ErrorActionPreference = 'Stop'
$workspace = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
if ($workspace -ne 'D:\寰意' -or -not (Test-Path -LiteralPath (Join-Path $workspace 'AGENTS.md'))) {
    throw 'OEVIRA requires its current D:\寰意 development root.'
}
foreach ($argument in $CodexArgs) {
    if ($argument -cmatch '^(?:-C|--cd)(?:=|$)' -or $argument -cmatch '^-C.+') {
        throw 'This OEVIRA entry uses D:\寰意. Use the ordinary Codex command for other projects.'
    }
}
# Prefer the installed executable over a PATH shim that can select an older npm CLI.
$codexCommand = Get-Command codex.exe -CommandType Application -ErrorAction Stop | Select-Object -First 1
$arguments = @('--strict-config', '-C', $workspace)
if ($PSBoundParameters.ContainsKey('Model')) {
    $arguments += @('-m', $Model)
}
if ($PSBoundParameters.ContainsKey('Effort')) {
    if ($Effort -notmatch '^(none|minimal|low|medium|high|xhigh|max|ultra)$') {
        throw 'Unknown reasoning effort. Choose a value supported by your selected model.'
    }
    $arguments += @('-c', ('model_reasoning_effort="' + $Effort + '"'))
}
if ($Desktop) { $arguments += @('app', $workspace) }
$arguments += $CodexArgs
if ($CheckOnly) {
    [PSCustomObject]@{
        Workspace = $workspace
        Executable = $codexCommand.Source
        ModelOverride = $PSBoundParameters.ContainsKey('Model')
        EffortOverride = $PSBoundParameters.ContainsKey('Effort')
        ChangesGlobalConfiguration = $false
        StopsOtherProcesses = $false
        Launches = $false
    } | ConvertTo-Json
    return
}
Push-Location -LiteralPath $workspace
try {
    & $codexCommand.Source @arguments
    $result = $LASTEXITCODE
} finally {
    Pop-Location
}
exit $result
