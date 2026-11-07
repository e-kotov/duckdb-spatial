param(
    [string]$RuntimeSources = 'build/windows-shutdown/runtime',
    [string]$OutputDirectory = 'build/windows-shutdown',
    [ValidateRange(1, 1000)][int]$Attempts = 30,
    [ValidateRange(1, 1000)][int]$ControlAttempts = 5
)

# This is experiment orchestration, not a software-test success gate. Each
# native/R/SQL test retains its actual status. assess_experiment.py makes the
# final comparison after all three arms have been collected.
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false

function Invoke-LoggedNative {
    param([string]$Executable, [string[]]$Arguments, [string]$LogPath, [switch]$Check, [switch]$Quiet)
    Write-Host ('Running: ' + $Executable + ' ' + ($Arguments -join ' '))
    if ($Quiet) {
        & $Executable @Arguments 2>&1 | Out-File -Encoding utf8 -FilePath $LogPath
    } else {
        & $Executable @Arguments 2>&1 | Tee-Object -FilePath $LogPath | Out-Host
    }
    $code = $LASTEXITCODE
    if ($Check -and $code -ne 0) {
        throw "Command failed with status ${code}: $Executable (see $LogPath)"
    }
    return [int]$code
}

$sourceRoot = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
Set-Location $sourceRoot
$runtimeRoot = (Resolve-Path $RuntimeSources).Path
New-Item -ItemType Directory -Force $OutputDirectory | Out-Null
$outputRoot = (Resolve-Path $OutputDirectory).Path
$expectedDuckDB = 'd54cbbe3df17e8e5ce68312f483b4155836112aa'
$expectedCITools = '969bc76276acec1c0a3922ff500d2ec3a1a3b037'
$duckdbCommit = (& git -C duckdb rev-parse HEAD).Trim()
$ciToolsCommit = (& git -C extension-ci-tools rev-parse HEAD).Trim()
if ($duckdbCommit -ne $expectedDuckDB -or $ciToolsCommit -ne $expectedCITools) {
    throw 'Experiment requires the recorded upstream DuckDB and extension-ci-tools gitlinks'
}
if ($env:DUCKDB_VERSION -ne 'v2.0.0' -or $env:DUCKDB_PLATFORM -ne 'windows_amd64_mingw') {
    throw 'Explicit v2.0.0 / windows_amd64_mingw build metadata is required for the R 2.0.0 host'
}
$compiler = (Get-Command g++ -ErrorAction Stop).Source
$nm = Join-Path (Split-Path $compiler) 'nm.exe'
$objdump = Join-Path (Split-Path $compiler) 'objdump.exe'
$compilerVersion = (& $compiler -dumpfullversion).Trim()
if ($compilerVersion -ne '10.4.0') { throw "Expected GCC 10.4.0, found $compilerVersion" }
$vcpkgCommit = (& git -C $env:VCPKG_ROOT rev-parse HEAD).Trim()
if ($vcpkgCommit -ne 'cd61e1e26a038e82d6550a3ebbe0fbbfe7da78e3') {
    throw 'Expected the same vcpkg revision as the upstream Spatial build'
}

$archiveRecords = @()
foreach ($archiveName in @('libstdc++.a', 'libgcc.a', 'libgcc_eh.a', 'libpthread.a', 'libwinpthread.a')) {
    $archivePath = (& $compiler "-print-file-name=$archiveName").Trim()
    if (Test-Path -LiteralPath $archivePath) {
        $archivePath = (Resolve-Path -LiteralPath $archivePath).Path
        $archiveRecords += @{
            name = $archiveName; path = $archivePath
            sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $archivePath).Hash.ToLowerInvariant()
        }
    }
}
if (-not ($archiveRecords | Where-Object { $_.name -eq 'libstdc++.a' })) {
    throw 'Could not resolve the compiler static C++ runtime archive'
}
$provenance = @{
    source_commit = (& git rev-parse HEAD).Trim()
    duckdb_commit = $duckdbCommit; ci_tools_commit = $ciToolsCommit
    duckdb_version = $env:DUCKDB_VERSION; platform = $env:DUCKDB_PLATFORM
    compiler_path = $compiler; compiler_version = $compilerVersion
    compiler_sha256 = (Get-FileHash -Algorithm SHA256 $compiler).Hash.ToLowerInvariant()
    archives = $archiveRecords; runner_image = $env:ImageOS; runner_image_version = $env:ImageVersion
    vcpkg_commit = $vcpkgCommit
}
$provenance | ConvertTo-Json -Depth 10 | Set-Content -Encoding utf8 (Join-Path $outputRoot 'build-provenance.json')
Invoke-LoggedNative -Executable $compiler -Arguments @('-v') -LogPath (Join-Path $outputRoot 'compiler-version.log') -Check | Out-Null

$env:EXT_FLAGS = '-DVCPKG_APPLOCAL_DEPS=OFF -DCMAKE_C_COMPILER_LAUNCHER=ccache -DCMAKE_CXX_COMPILER_LAUNCHER=ccache'
$binary = Join-Path $sourceRoot 'build/release/extension/spatial/spatial.duckdb_extension'
$repositoryBinary = Join-Path $sourceRoot 'build/release/repository/v2.0.0/windows_amd64_mingw/spatial.duckdb_extension'
$runner = Join-Path $sourceRoot 'build/release/test/unittest.exe'
$arms = @(
    @{ name = 'upstream'; source = '' },
    @{ name = 'rebuilt-baseline'; source = (Join-Path $runtimeRoot 'baseline.cc') },
    @{ name = 'guarded'; source = (Join-Path $runtimeRoot 'guarded.cc') }
)
foreach ($arm in $arms) {
    $armDirectory = Join-Path $outputRoot $arm.name
    New-Item -ItemType Directory -Force $armDirectory | Out-Null
    $runtimeSource = $arm.source.Replace('\', '/')
    $linkMap = (Join-Path $armDirectory 'spatial-link.map').Replace('\', '/')
    # Reconfigure one tree: dependencies, compiler and ordinary build options
    # are identical. A full release build also refreshes the local repository.
    $flags = "-DSPATIAL_MINGW_TLS_ATEXIT_SOURCE=$runtimeSource -DSPATIAL_MINGW_LINK_MAP=$linkMap"
    Invoke-LoggedNative -Executable 'make' -Arguments @('release', "EXT_RELEASE_FLAGS=$flags") `
        -LogPath (Join-Path $armDirectory 'build.log') -Check | Out-Null

    if ((Get-Content 'build/release/duckdb_platform_out' -Raw).Trim() -ne 'windows_amd64_mingw') {
        throw 'Build output reports the wrong extension ABI/platform'
    }
    foreach ($required in @($binary, $repositoryBinary, $runner, $linkMap)) {
        if (-not (Test-Path -LiteralPath $required)) { throw "Missing build output: $required" }
    }
    $archiveBinary = Join-Path $armDirectory 'spatial.duckdb_extension'
    Copy-Item -LiteralPath $binary -Destination $archiveBinary
    $binaryHash = (Get-FileHash -Algorithm SHA256 $archiveBinary).Hash.ToLowerInvariant()
    if ((Get-FileHash -Algorithm SHA256 $repositoryBinary).Hash.ToLowerInvariant() -ne $binaryHash) {
        throw 'Build repository contains a different DLL from this experiment arm'
    }
    Copy-Item 'build/release/CMakeCache.txt' (Join-Path $armDirectory 'CMakeCache.txt')
    Copy-Item 'build/release/compile_commands.json' (Join-Path $armDirectory 'compile_commands.json')
    Copy-Item 'build/release/build.ninja' (Join-Path $armDirectory 'build.ninja')
    $symbolsPath = Join-Path $armDirectory 'defined-symbols.txt'
    Invoke-LoggedNative -Executable $nm -Arguments @('--defined-only', $archiveBinary) `
        -LogPath $symbolsPath -Check -Quiet | Out-Null
    $definitions = @(Select-String -LiteralPath $symbolsPath -Pattern '^\s*[0-9a-fA-F]+\s+[Tt]\s+__cxa_thread_atexit\s*$')
    if ($definitions.Count -ne 1) {
        throw "Expected one __cxa_thread_atexit definition, found $($definitions.Count)"
    }
    Invoke-LoggedNative -Executable $objdump -Arguments @('-p', $archiveBinary) `
        -LogPath (Join-Path $armDirectory 'pe-headers.txt') -Check -Quiet | Out-Null
    $mapText = Get-Content -LiteralPath $linkMap -Raw
    if ($arm.name -eq 'upstream') {
        if ($mapText -notmatch 'libstdc\+\+\.a\([^)]*atexit_thread\.(?:o|obj)\)') {
            throw 'The unmodified arm did not select the compiler archive atexit_thread.o'
        }
    } else {
        $sourceName = [System.IO.Path]::GetFileName($arm.source)
        if ($mapText -notmatch ([regex]::Escape($sourceName) + '\.(obj|o)')) {
            throw 'The supplied runtime source object is absent from the extension link map'
        }
        if ($mapText -match 'libstdc\+\+\.a\([^)]*atexit_thread\.(?:o|obj)\)') {
            throw 'Compiler archive atexit_thread.o was linked alongside the supplied runtime source'
        }
    }

    $rCode = Invoke-LoggedNative -Executable 'python' -Arguments @(
        'scripts/windows-shutdown/run_spatial.py', '--extension', $archiveBinary,
        '--inputs', $runtimeRoot,
        '--output', (Join-Path $armDirectory 'r'), '--attempts', "$Attempts",
        '--control-attempts', "$ControlAttempts"
    ) -LogPath (Join-Path $armDirectory 'r-supervisor.log')
    $sqlCode = Invoke-LoggedNative -Executable 'python' -Arguments @(
        'scripts/windows-shutdown/run_sql_tests.py', '--runner', $runner,
        '--extension', $archiveBinary, '--repository-extension', $repositoryBinary,
        '--output', (Join-Path $armDirectory 'sql')
    ) -LogPath (Join-Path $armDirectory 'sql-supervisor.log')
    $runtimeHash = if ($arm.source) {
        (Get-FileHash -Algorithm SHA256 $arm.source).Hash.ToLowerInvariant()
    } else { $null }
    @{
        arm = $arm.name; build_completed = $true; runtime_source = $arm.source
        runtime_source_sha256 = $runtimeHash; extension_sha256 = $binaryHash
        r_exit_code = $rCode; sql_exit_code = $sqlCode
        note = 'Test failures are retained. Completion of this arm is not a software pass.'
    } | ConvertTo-Json -Depth 10 | Set-Content -Encoding utf8 (Join-Path $armDirectory 'arm-result.json')
}

# Assert that the experiment did not patch the compiler installation in place.
foreach ($archive in $archiveRecords) {
    if ((Get-FileHash -Algorithm SHA256 -LiteralPath $archive.path).Hash.ToLowerInvariant() -ne $archive.sha256) {
        throw "The installed runtime archive changed: $($archive.name)"
    }
}
exit 0
