[CmdletBinding()]
param([ValidateNotNullOrEmpty()][string]$DataRoot = 'data\development', [switch]$CheckOnly)
$ErrorActionPreference = 'Stop'
$repoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
if ($DataRoot -match '^[A-Za-z]:($|[^\\/])' -or $DataRoot -match '^[\\/](?![\\/])') {
    throw 'Use a fully qualified data root or a path relative to the repository; drive-relative paths are ambiguous.'
}
$dataPath = if ([System.IO.Path]::IsPathRooted($DataRoot)) {
    [System.IO.Path]::GetFullPath($DataRoot)
} else { [System.IO.Path]::GetFullPath((Join-Path $repoRoot $DataRoot)) }
$receiptPath = Join-Path $dataPath 'service.private.json'
if ($CheckOnly) {
    [pscustomobject]@{Mode='CheckOnly'; RepositoryRoot=$repoRoot; DataRoot=$dataPath; ReceiptPath=$receiptPath; RuntimeInspected=$false}
    return
}
if (-not (Test-Path -LiteralPath $receiptPath)) { throw 'No managed runtime receipt exists for this data root.' }
$receipt = Get-Content -LiteralPath $receiptPath -Raw | ConvertFrom-Json
if ($receipt.repo_root -ne $repoRoot -or $receipt.data_root -ne $dataPath) { throw 'The receipt belongs to a different runtime.' }
$service = Get-Process -Id $receipt.pid -ErrorAction SilentlyContinue
if ($service) {
    if ($service.StartTime.ToUniversalTime().Ticks -ne ([datetime]$receipt.started_at).ToUniversalTime().Ticks) { throw 'The recorded process ID now belongs to another process.' }
}
if ($receipt.runtime_pid) {
    $runtime = Get-Process -Id $receipt.runtime_pid -ErrorAction SilentlyContinue
    if ($runtime) {
        if ($runtime.StartTime.ToUniversalTime().Ticks -ne ([datetime]$receipt.runtime_started_at).ToUniversalTime().Ticks -or $runtime.Path -ne $receipt.runtime_path) {
            throw 'The recorded runtime identity has changed; no process was stopped.'
        }
        Stop-Process -Id $runtime.Id -ErrorAction Stop
        if (-not $runtime.WaitForExit(5000)) { throw 'The runtime has not confirmed exit. Its receipt and data were preserved; recovery is not verified.' }
    }
}
if ($service -and -not $service.HasExited) {
    Stop-Process -Id $service.Id -ErrorAction Stop
    if (-not $service.WaitForExit(5000)) { throw 'The managed process has not confirmed exit. Its receipt and data were preserved; recovery is not verified.' }
}
Write-Output 'No recorded managed process remains running. This command uses forced termination, not graceful shutdown; interrupted work and recovery have not been verified. The data directory and receipt were not deleted.'
