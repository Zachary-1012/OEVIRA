[CmdletBinding()]
param(
    [ValidateNotNullOrEmpty()][string]$DataRoot = 'data\development',
    [ValidateRange(1024,65535)][int]$Port = 8317,
    [ValidateSet('Web','Windows','None')][string]$Client = 'Web',
    [string]$Python,
    [switch]$CheckOnly
)
$ErrorActionPreference = 'Stop'
$repoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$requiredInputs = @(
    'scripts\check_python_runtime.py',
    'server\oevira\__main__.py', 'server\oevira\__init__.py',
    'server\oevira\api.py', 'server\oevira\auth.py', 'server\oevira\monitoring.py',
    'server\oevira\store.py', 'server\oevira\worker.py'
)
$missingInputs = @($requiredInputs | Where-Object { -not (Test-Path -LiteralPath (Join-Path $repoRoot $_) -PathType Leaf) })
if ($missingInputs.Count) {
    throw "Required startup files are missing from this checkout: $($missingInputs -join ', '). Use a checkout containing the corresponding product sources. No data directory was created and no service was started."
}
if ($DataRoot -match '^[A-Za-z]:($|[^\\/])' -or $DataRoot -match '^[\\/](?![\\/])') {
    throw 'Use a fully qualified data root or a path relative to the repository; drive-relative paths are ambiguous.'
}
$dataPath = if ([System.IO.Path]::IsPathRooted($DataRoot)) {
    [System.IO.Path]::GetFullPath($DataRoot)
} else { [System.IO.Path]::GetFullPath((Join-Path $repoRoot $DataRoot)) }
$webPath = Join-Path $repoRoot 'app\build\web'
if (-not $Python) { $Python = Join-Path $repoRoot '.venv\Scripts\python.exe' }
elseif (-not [System.IO.Path]::IsPathRooted($Python)) { $Python = Join-Path $repoRoot $Python }
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw 'Prepare the runtime with scripts/setup.ps1 before opening OEVIRA.'
}
$pythonPath = (Resolve-Path -LiteralPath $Python).Path
$pythonVersion = & $pythonPath -I -X utf8 -c "import sys; print('%d.%d.%d' % sys.version_info[:3])"
if ($LASTEXITCODE -ne 0 -or $pythonVersion -notmatch '^3\.13\.\d+$') {
    throw "Python 3.13 is required for this runtime (found: $pythonVersion)."
}
$runtimeJson = & $pythonPath -I -X utf8 (Join-Path $PSScriptRoot 'check_python_runtime.py')
$runtimeExit = $LASTEXITCODE
$runtimeInfo = $runtimeJson | ConvertFrom-Json
if ($runtimeExit -ne 0 -or -not $runtimeInfo.supported) {
    throw "The selected SQLite runtime lacks the supported WAL-reset fix or was withdrawn: $($runtimeInfo.sqlite_version). Select an existing supported Python 3.13 runtime with -Python; setup.ps1 does not patch or install Python/SQLite. No service was started."
}
$windowsPath = Join-Path $repoRoot 'app\build\windows\x64\runner\Release\oevira.exe'
$selectedWebRoot = if ($Client -eq 'Web') { $webPath } else { $null }
$clientArtifact = switch ($Client) {
    'Web' { Join-Path $webPath 'index.html' }
    'Windows' { $windowsPath }
    'None' { $null }
}
$clientReady = -not $clientArtifact -or (Test-Path -LiteralPath $clientArtifact -PathType Leaf)
if ($CheckOnly) {
    [pscustomobject]@{Mode='CheckOnly'; RepositoryRoot=$repoRoot; RequiredInputs=$requiredInputs; Python=$pythonPath; PythonVersion=$pythonVersion; SQLiteVersion=$runtimeInfo.sqlite_version; SQLiteSourceId=$runtimeInfo.sqlite_source_id; DataRoot=$dataPath; Client=$Client; RequiredClientArtifact=$clientArtifact; ClientArtifactExists=$clientReady; WebRoot=$selectedWebRoot; Port=$Port}
    if (-not $clientReady) { throw "The $Client client artifact is missing: $clientArtifact. No service was started." }
    return
}
if (-not $clientReady) { throw "The $Client client artifact is missing: $clientArtifact. Build that client before selecting it." }
New-Item -ItemType Directory -Path $dataPath -Force | Out-Null
$receiptPath = Join-Path $dataPath 'service.private.json'
$capabilityPath = Join-Path $dataPath 'launch.capability'
$service = $null
if (Test-Path -LiteralPath $receiptPath) {
    $receipt = Get-Content -LiteralPath $receiptPath -Raw | ConvertFrom-Json
    $candidate = Get-Process -Id $receipt.pid -ErrorAction SilentlyContinue
    if ($candidate -and $candidate.StartTime.ToUniversalTime().Ticks -eq ([datetime]$receipt.started_at).ToUniversalTime().Ticks) {
        if ($receipt.repo_root -ne $repoRoot -or $receipt.data_root -ne $dataPath -or $receipt.port -ne $Port -or $receipt.PSObject.Properties.Name -notcontains 'web_root' -or $receipt.web_root -ne $selectedWebRoot -or $candidate.Path -ne $pythonPath) {
            throw 'A different managed runtime is using this data root. Stop it explicitly before changing its configuration.'
        }
        $service = $candidate
    }
}
$baseUrl = "http://127.0.0.1:$Port"
if (-not $service) {
    $listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    if ($listener) { throw "Port $Port is already in use. No unrelated process will be stopped." }
    # The equivalent trailing dot prevents a directory separator escaping the native closing quote.
    $arguments = @('-m','oevira','--data-root',('"' + $dataPath.TrimEnd('\') + '\."'),'--port',"$Port",'--bootstrap-file',('"' + $capabilityPath + '"'))
    if ($selectedWebRoot) { $arguments += @('--web-root',('"' + $selectedWebRoot + '"')) }
    $service = Start-Process -FilePath $pythonPath -ArgumentList $arguments -WorkingDirectory (Join-Path $repoRoot 'server') -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $dataPath 'service.stdout.log') -RedirectStandardError (Join-Path $dataPath 'service.stderr.log')
    $service.Refresh()
    $receipt = @{pid=$service.Id; started_at=$service.StartTime.ToUniversalTime().ToString('o'); repo_root=$repoRoot; data_root=$dataPath; port=$Port; web_root=$selectedWebRoot}
    [System.IO.File]::WriteAllText($receiptPath, ($receipt | ConvertTo-Json), [System.Text.UTF8Encoding]::new($false))
} else {
    Push-Location (Join-Path $repoRoot 'server')
    try {
        & $pythonPath -m oevira --data-root $dataPath --bootstrap-file $capabilityPath --issue-session
        if ($LASTEXITCODE -ne 0) { throw 'Could not create the local sign-in capability.' }
    } finally { Pop-Location }
}
$ready = $false
for ($attempt = 0; $attempt -lt 40; $attempt++) {
    $service.Refresh()
    if ($service.HasExited) { throw "The product runtime stopped. See the local service.stderr.log in the selected data root." }
    try {
        $status = Invoke-RestMethod -Uri "$baseUrl/api/status" -TimeoutSec 2
        $ready = $status.data.product -eq 'OEVIRA' -and $status.data.runtime.api -eq 'available'
    } catch [System.Net.WebException] { $ready = $false }
      catch [System.Net.Http.HttpRequestException] { $ready = $false }
    if ($ready) { break }
    Start-Sleep -Milliseconds 250
}
if (-not $ready) { throw 'The runtime did not confirm readiness. The startup receipt and diagnostics are preserved.' }
$listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop | Where-Object LocalAddress -eq '127.0.0.1'
$runtime = Get-Process -Id $listener.OwningProcess -ErrorAction Stop
if ($runtime.Id -ne $service.Id) {
    $runtimeInfo = Get-CimInstance Win32_Process -Filter "ProcessId=$($runtime.Id)"
    if ($runtimeInfo.ParentProcessId -ne $service.Id) {
        throw 'The listening process does not belong to the runtime launched for this workspace.'
    }
}
$receipt = @{pid=$service.Id; started_at=$service.StartTime.ToUniversalTime().ToString('o'); repo_root=$repoRoot; data_root=$dataPath; port=$Port; web_root=$selectedWebRoot; runtime_pid=$runtime.Id; runtime_started_at=$runtime.StartTime.ToUniversalTime().ToString('o'); runtime_path=$runtime.Path}
[System.IO.File]::WriteAllText($receiptPath, ($receipt | ConvertTo-Json), [System.Text.UTF8Encoding]::new($false))
if ($Client -ne 'None') {
    $capability = [System.IO.File]::ReadAllText($capabilityPath).Trim()
    if ($Client -eq 'Web') {
        Start-Process -FilePath "$baseUrl/#bootstrap=$([Uri]::EscapeDataString($capability))" | Out-Null
    } else {
        Start-Process -FilePath $windowsPath -ArgumentList @('--server',$baseUrl,'--capability-file',('"' + $capabilityPath + '"')) | Out-Null
    }
    $capability = $null
}
Write-Output "The OEVIRA API service is available at $baseUrl. Data root: $dataPath. Worker behavior, client operation and task recovery require separate verification; monitoring stops when the service stops."
