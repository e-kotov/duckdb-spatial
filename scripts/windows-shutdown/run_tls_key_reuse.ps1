param(
    [Parameter(Mandatory = $true)][string]$Cxx,
    [Parameter(Mandatory = $true)][string]$OutputDirectory
)

$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false
$output = (Resolve-Path $OutputDirectory).Path
$results = @()
foreach ($arm in @('original', 'rebuilt-baseline', 'reverse-order')) {
    python scripts/windows-shutdown/run_tls_key_reuse.py `
        --cxx "$Cxx" `
        --baseline-runtime-source "$output/runtime/baseline.cc" `
        --guarded-runtime-source "$output/runtime/guarded.cc" `
        --preparation "$output/pthread" --pthread-arm "$arm" `
        --output-dir "$output/observations/$arm" --timeout 30
    $driverExit = $LASTEXITCODE
    $record = [ordered]@{
        pthread_arm = $arm
        driver_exit = $driverExit
        diagnostic_expectation_met = $false
        all_software_exits_passed = $false
    }
    try {
        $summary = Get-Content "$output/observations/$arm/summary.json" -Raw | ConvertFrom-Json
        $normal = @($summary.results | Where-Object { $_.scenario -eq 'normal' })
        $reuse = @($summary.results | Where-Object { $_.scenario -eq 'key-reuse' })
        $record.cases = @($summary.results).Count
        $record.valid_observations = @($summary.results | Where-Object { $_.observation_valid -eq $true }).Count
        $record.normal_cases = $normal.Count
        $record.key_reuse_cases = $reuse.Count
        $record.normal_lifetime_passes = @($normal | Where-Object { $_.storage_lifetime_contract_passed -eq $true }).Count
        $record.key_reuse_lifetime_passes = @($reuse | Where-Object { $_.storage_lifetime_contract_passed -eq $true }).Count
        $record.lifetime_failures = @($summary.results | Where-Object { $_.storage_lifetime_contract_passed -eq $false }).Count
        $record.input_integrity_passed = $summary.input_integrity_passed -eq $true
        $record.all_software_exits_passed = $summary.all_software_exits_passed -eq $true
        $expectedNormalPasses = if ($arm -eq 'reverse-order') { 6 } else { 0 }
        $record.diagnostic_expectation_met = (
            $driverExit -eq 0 -and $summary.diagnostic_expectation_met -eq $true `
            -and $record.input_integrity_passed -and $record.all_software_exits_passed `
            -and $record.cases -eq 12 -and $record.valid_observations -eq 12 `
            -and $record.normal_cases -eq 6 -and $record.key_reuse_cases -eq 6 `
            -and $record.normal_lifetime_passes -eq $expectedNormalPasses `
            -and $record.key_reuse_lifetime_passes -eq (6 - $expectedNormalPasses) `
            -and $record.lifetime_failures -eq 6)
    } catch {
        $record.assessment_error = $_.Exception.Message
    }
    $results += $record
    $results | ConvertTo-Json -Depth 8 | Set-Content "$output/comparison.json"
}
$passed = $results.Count -eq 3 -and @($results | Where-Object { $_.diagnostic_expectation_met -eq $true }).Count -eq 3
@{
    diagnostic_expectation_met = $passed
    spatial_workload_assessed = $false
    production_fix_validated = $false
    all_software_exits_passed = $results.Count -eq 3 -and @($results | Where-Object { $_.all_software_exits_passed -eq $true }).Count -eq 3
    all_storage_contracts_passed = $false
    interpretation = 'Diagnostic success can confirm a key-reuse lifetime regression. Storage-before-callback observations remain failed lifetime contracts despite normal process exits.'
    results = $results
} | ConvertTo-Json -Depth 8 | Set-Content "$output/verdict.json"
if (-not $passed) { exit 1 }
