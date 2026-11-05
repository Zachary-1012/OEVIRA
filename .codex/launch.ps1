[CmdletBinding(PositionalBinding=$false)]
param(
    [string]$Model,
    [string]$Effort,
    [string]$CodexPath,
    [switch]$CheckOnly,
    [switch]$Desktop,
    [Parameter(ValueFromRemainingArguments=$true)]
    [string[]]$CodexArgs
)

$ErrorActionPreference = 'Stop'
$workspace = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$requiredRoot = 'D:\' + [char]0x5BF0 + [char]0x610F
if ($workspace -ne $requiredRoot -or -not (Test-Path -LiteralPath (Join-Path $workspace 'AGENTS.md'))) {
    throw 'OEVIRA requires its registered development root.'
}
foreach ($argument in $CodexArgs) {
    if ($argument -cmatch '^(?:-C|--cd)(?:=|$)' -or $argument -cmatch '^-C.+' -or $argument -cmatch '^--worktree(?:=|$)') {
        throw 'This entry uses the existing OEVIRA workspace, not another root or managed worktree.'
    }
}
# Resolve a real executable. Do not run an npm shim, install, or select a model.
if ($PSBoundParameters.ContainsKey('CodexPath')) {
    if (-not [IO.Path]::IsPathRooted($CodexPath) -or
        [IO.Path]::GetExtension($CodexPath) -ine '.exe' -or
        -not (Test-Path -LiteralPath $CodexPath -PathType Leaf)) {
        throw 'CodexPath must be an existing absolute .exe path; an invalid explicit choice is not replaced.'
    }
    $executable = (Get-Item -LiteralPath $CodexPath).FullName
    $resolution = 'explicit'
} else {
    $pathCommand = Get-Command codex.exe -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($pathCommand) {
        $executable = $pathCommand.Source
        $resolution = 'path-executable'
    } else {
        $packages = @()
        if (Get-Command Get-AppxPackage -ErrorAction SilentlyContinue) {
            $packages = @(Get-AppxPackage -Name OpenAI.Codex -ErrorAction SilentlyContinue | Sort-Object { [version]$_.Version } -Descending)
        }
        $executable = $null
        foreach ($package in $packages) {
            $candidate = Join-Path $package.InstallLocation 'app\resources\codex.exe'
            if (Test-Path -LiteralPath $candidate -PathType Leaf) {
                $executable = $candidate
                $resolution = 'registered-desktop-package'
                break
            }
        }
        if (-not $executable) {
            throw 'No Codex executable was resolved. Supply -CodexPath for an existing installation; this entry does not install software.'
        }
    }
}
$arguments = @('--strict-config', '-C', $workspace)
if ($PSBoundParameters.ContainsKey('Model')) { $arguments += @('-m', $Model) }
if ($PSBoundParameters.ContainsKey('Effort')) {
    if ($Effort -notmatch '^[a-z][a-z0-9_-]*$') {
        throw 'Use a simple effort identifier; the selected CLI and model validate supported values.'
    }
    $arguments += @('-c', ('model_reasoning_effort="' + $Effort + '"'))
}
if ($Desktop) { $arguments += @('app', $workspace) }
$arguments += $CodexArgs
if ($CheckOnly) {
    [PSCustomObject]@{
        Workspace = $workspace
        Executable = $executable
        Resolution = $resolution
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
    & $executable @arguments
    $result = $LASTEXITCODE
} finally {
    Pop-Location
}
exit $result
