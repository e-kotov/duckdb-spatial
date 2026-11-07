# Collect every bounded native outcome before the actual Spatial comparison.
# Expected hangs and old storage-order failures retain their software/contract status.
param([string]$OutputDirectory = 'build/windows-pthread-spatial')

$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false
$output = $OutputDirectory
$results = @()
foreach ($arm in @('original', 'rebuilt-baseline', 'reverse-order')) {
  $expected = if ($arm -eq 'reverse-order') { 'callback-first' } else { 'storage-first' }
  $library = "$output/pthread/$arm/lib"
  python scripts/windows-shutdown/run_tls_storage_order.py `
    --cxx "$env:SPATIAL_TEST_CXX" `
    --baseline-runtime-source "$output/runtime/baseline.cc" `
    --guarded-runtime-source "$output/runtime/guarded.cc" `
    --pthread-library-dir "$library" --expected-order "$expected" `
    --output-dir "$output/ordering/$arm" --timeout 30
  $orderExit = $LASTEXITCODE
  python scripts/windows-shutdown/run_runtime.py `
    --cxx "$env:SPATIAL_TEST_CXX" `
    --baseline-runtime-source "$output/runtime/baseline.cc" `
    --guarded-runtime-source "$output/runtime/guarded.cc" `
    --pthread-library-dir "$library" `
    --output-dir "$output/native/$arm" --timeout 10 --cycles 16 --workers 8
  $cleanupExit = $LASTEXITCODE
  $record = [ordered]@{
    pthread_arm = $arm
    expected_storage_order = $expected
    storage_driver_exit = $orderExit
    cleanup_driver_exit = $cleanupExit
    diagnostic_expectation_met = $false
  }
  try {
    $order = Get-Content "$output/ordering/$arm/summary.json" -Raw | ConvertFrom-Json
    $cleanup = Get-Content "$output/native/$arm/summary.json" -Raw | ConvertFrom-Json
    $record.storage_cases = @($order.results).Count
    $record.storage_contract_passes = @($order.results | Where-Object { $_.storage_lifetime_contract_passed -eq $true }).Count
    $record.storage_contract_failures = @($order.results | Where-Object { $_.storage_lifetime_contract_passed -eq $false }).Count
    $record.cleanup_cases = @($cleanup.results).Count
    $record.cleanup_software_passes = @($cleanup.results | Where-Object { $_.software_exit_passed -eq $true }).Count
    $record.cleanup_reproduced_hangs = @($cleanup.results | Where-Object { $_.classification -eq 'expected-diagnostic-hang' -and $_.software_exit_passed -eq $false }).Count
    $expectedPasses = if ($arm -eq 'reverse-order') { 6 } else { 0 }
    $record.diagnostic_expectation_met = (
      $orderExit -eq 0 -and $cleanupExit -eq 0 `
      -and $order.diagnostic_expectation_met -eq $true `
      -and $cleanup.candidate_validated_for_runtime_fixture -eq $true `
      -and $record.storage_cases -eq 6 -and $record.cleanup_cases -eq 12 `
      -and $record.storage_contract_passes -eq $expectedPasses `
      -and $record.storage_contract_failures -eq (6 - $expectedPasses) `
      -and $record.cleanup_software_passes -eq 10 -and $record.cleanup_reproduced_hangs -eq 2)
  } catch {
    $record.assessment_error = $_.Exception.Message
  }
  $results += $record
  $results | ConvertTo-Json -Depth 8 | Set-Content "$output/comparison.json"
}
$passed = @($results | Where-Object { $_.diagnostic_expectation_met -eq $true }).Count -eq 3
@{
  diagnostic_expectation_met = $passed
  spatial_workload_assessed = $false
  all_software_exits_passed = $false
  all_storage_contracts_passed = $false
  interpretation = 'Expected old ordering failures and deliberate baseline hangs remain failed software/lifetime outcomes.'
  results = $results
} | ConvertTo-Json -Depth 8 | Set-Content "$output/verdict.json"
if (-not $passed) { exit 1 }
