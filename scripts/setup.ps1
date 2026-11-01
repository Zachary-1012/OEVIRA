[CmdletBinding()]
param([string]$Python, [string]$Flutter = 'flutter', [switch]$SkipClientBuild, [switch]$CheckOnly)
$ErrorActionPreference = 'Stop'
$repoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$requiredInputs = @('scripts\check_python_runtime.py', 'server\requirements.txt', 'server\constraints.txt')
if (-not $SkipClientBuild) {
    $requiredInputs += @('app\pubspec.yaml', 'app\pubspec.lock', 'app\lib\main.dart', 'app\web\index.html', 'app\windows\CMakeLists.txt', 'app\windows\runner\CMakeLists.txt', 'app\windows\runner\main.cpp')
}
$missingInputs = @($requiredInputs | Where-Object { -not (Test-Path -LiteralPath (Join-Path $repoRoot $_) -PathType Leaf) })
if ($missingInputs.Count) {
    throw "Required preparation files are missing from this checkout: $($missingInputs -join ', '). Use a checkout containing the corresponding product sources and dependency manifests. No environment was changed and no installation or build was started."
}
$environmentPath = Join-Path $repoRoot '.venv'
$pythonPath = Join-Path $environmentPath 'Scripts\python.exe'

function Get-PythonVersion([string]$Executable) {
    try {
        $version = & $Executable -I -X utf8 -c "import sys; print('%d.%d.%d' % sys.version_info[:3])" 2>$null
        if ($LASTEXITCODE -eq 0 -and $version -match '^\d+\.\d+\.\d+$') { return [string]$version }
    } catch { }
    return $null
}

function Resolve-Python313([string]$Requested) {
    if ($Requested) {
        $command = Get-Command $Requested -CommandType Application -ErrorAction Stop | Select-Object -First 1
        $version = Get-PythonVersion $command.Source
        if ($version -notmatch '^3\.13\.\d+$') { throw "Python 3.13 is required; the selected interpreter is $($command.Source) (version: $version)." }
        return $command.Source
    }
    $projectPython = Join-Path $repoRoot '.runtime\tools\python-3.13.15-sqlite-3.51.3\python.exe'
    $candidates = @()
    if (Test-Path -LiteralPath $projectPython -PathType Leaf) { $candidates += $projectPython }
    $launcher = Get-Command py.exe -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($launcher) {
        try {
            $candidate = & $launcher.Source -3.13 -I -c 'import sys; print(sys.executable)' 2>$null
            if ($LASTEXITCODE -eq 0 -and $candidate) { $candidates += [string]$candidate }
        } catch { }
    }
    $candidates += @(Get-Command python.exe -All -CommandType Application -ErrorAction SilentlyContinue |
        Where-Object { $_.Source -notlike '*\Microsoft\WindowsApps\*' } | Select-Object -ExpandProperty Source)
    foreach ($candidate in ($candidates | Select-Object -Unique)) {
        if ((Get-PythonVersion $candidate) -match '^3\.13\.\d+$') { return $candidate }
    }
    throw 'No Python 3.13 interpreter was found. Supply -Python with an existing Python 3.13 executable; no interpreter was installed or changed.'
}

# An explicit interpreter is validated even when the existing environment is reused.
$bootstrapPython = if ($Python) { Resolve-Python313 $Python } else { $null }
$environmentExists = Test-Path -LiteralPath $pythonPath -PathType Leaf
if ($environmentExists) {
    $pythonVersion = Get-PythonVersion $pythonPath
    if ($pythonVersion -notmatch '^3\.13\.\d+$') {
        throw "The existing .venv must use Python 3.13 (found: $pythonVersion). It was preserved without changes."
    }
    if (-not $bootstrapPython) { $bootstrapPython = $pythonPath }
} else {
    if (Test-Path -LiteralPath $environmentPath) { throw 'The existing .venv is incomplete. It was preserved; inspect it before preparing a replacement.' }
    if (-not $bootstrapPython) { $bootstrapPython = Resolve-Python313 }
    $pythonVersion = Get-PythonVersion $bootstrapPython
}
$flutterPath = if (-not $SkipClientBuild) {
    (Get-Command $Flutter -CommandType Application -ErrorAction Stop | Select-Object -First 1).Source
} else { $null }
$runtimeCheck = Join-Path $PSScriptRoot 'check_python_runtime.py'
foreach ($selectedPython in (@($bootstrapPython, $(if ($environmentExists) { $pythonPath })) | Where-Object { $_ } | Select-Object -Unique)) {
    $runtimeJson = & $selectedPython -I -X utf8 $runtimeCheck
    $runtimeExit = $LASTEXITCODE
    $runtimeInfo = $runtimeJson | ConvertFrom-Json
    if ($runtimeExit -ne 0 -or -not $runtimeInfo.supported) {
        throw "The selected Python/SQLite runtime is unsupported: $selectedPython, Python $($runtimeInfo.python_version), SQLite $($runtimeInfo.sqlite_version). Use Python 3.13 with SQLite 3.51.3 or later (excluding withdrawn 3.52.0), or the fixed 3.50.7/3.44.6 backports. No environment was changed."
    }
}
if ($CheckOnly) {
    [pscustomobject]@{Mode='CheckOnly'; RepositoryRoot=$repoRoot; RequiredInputs=$requiredInputs; Python=$pythonPath; BootstrapPython=$bootstrapPython; PythonVersion=$pythonVersion; SQLiteVersion=$runtimeInfo.sqlite_version; SQLiteSourceId=$runtimeInfo.sqlite_source_id; Environment=$environmentPath; EnvironmentExists=$environmentExists; Flutter=$flutterPath; BuildClients=(-not $SkipClientBuild)}
    return
}
if (-not $environmentExists) {
    & $bootstrapPython -m venv $environmentPath
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the product Python environment.' }
    if ((Get-PythonVersion $pythonPath) -notmatch '^3\.13\.\d+$') { throw 'The new environment did not confirm Python 3.13.' }
    & $pythonPath -I -X utf8 $runtimeCheck
    if ($LASTEXITCODE -ne 0) { throw 'The new environment did not retain a supported SQLite runtime. It was preserved for diagnosis.' }
}
& $pythonPath -m pip install --disable-pip-version-check -r (Join-Path $repoRoot 'server\requirements.txt') -c (Join-Path $repoRoot 'server\constraints.txt')
if ($LASTEXITCODE -ne 0) { throw 'Product dependencies did not install.' }
if (-not $SkipClientBuild) {
    $originalProgramFiles = [Environment]::GetEnvironmentVariable('ProgramFiles(x86)', 'Process')
    $configuredVswhere = Join-Path $originalProgramFiles 'Microsoft Visual Studio\Installer\vswhere.exe'
    $systemProgramFiles = Join-Path $env:SystemDrive 'Program Files (x86)'
    $systemVswhere = Join-Path $systemProgramFiles 'Microsoft Visual Studio\Installer\vswhere.exe'
    if (-not (Test-Path -LiteralPath $configuredVswhere) -and (Test-Path -LiteralPath $systemVswhere)) {
        # Only correct this build process's lookup; preserve the user's OS settings.
        [Environment]::SetEnvironmentVariable('ProgramFiles(x86)', $systemProgramFiles, 'Process')
    }
    Push-Location (Join-Path $repoRoot 'app')
    try {
        & $flutterPath pub get --enforce-lockfile
        if ($LASTEXITCODE -ne 0) { throw 'Flutter dependencies did not resolve.' }
        & $flutterPath build web --release --no-web-resources-cdn
        if ($LASTEXITCODE -ne 0) { throw 'Web build did not complete.' }
        & $flutterPath build windows --release
        if ($LASTEXITCODE -ne 0) { throw 'Windows build did not complete.' }
    } finally {
        Pop-Location
        [Environment]::SetEnvironmentVariable('ProgramFiles(x86)', $originalProgramFiles, 'Process')
    }
}
Write-Output 'Development dependencies are prepared. Use scripts/start.ps1 -CheckOnly to check the selected client. Preparation does not establish product acceptance.'
