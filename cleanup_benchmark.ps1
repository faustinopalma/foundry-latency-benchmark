param(
    [ValidatePattern('^[a-z0-9-]{1,40}$')][string]$PrimaryRun = 'main-20260930a',
    [switch]$Delete
)

. (Join-Path $PSScriptRoot 'azure_benchmark.ps1') -Action Status
$ExecutionBase = "/subscriptions/$Subscription/resourceGroups/$ExecutionGroup"
$exportRoot = Join-Path $Evidence $PrimaryRun
$runRoot = Join-Path $exportRoot "results/$PrimaryRun"
$manifest = Get-Content (Join-Path $runRoot 'manifest.json') -Raw | ConvertFrom-Json -Depth 100
$completion = Get-Content (Join-Path $runRoot 'completion.json') -Raw | ConvertFrom-Json -Depth 100
$receipt = Get-Content (Join-Path $exportRoot 'storage-verification.json') -Raw | ConvertFrom-Json -Depth 100
$isNetwork = $manifest.PSObject.Properties.Name -contains 'probe_pairs'
if ((-not $isNetwork -and $manifest.run_id -ne $PrimaryRun) -or $receipt.run_id -ne $PrimaryRun -or $receipt.all_equal -isnot [bool] -or -not $receipt.all_equal) { throw 'Evidence identity or verification mismatch' }
if ($isNetwork) {
    if ($manifest.vantage -ne 'Azure Sweden Central ACI' -or $manifest.probe_pairs -ne 60 -or $manifest.inference_repeats -ne 20 -or $completion.measured_inference_calls -ne 320) { throw 'Network campaign is incomplete or unexpected' }
    $null = & (Join-Path $PSScriptRoot '.venv/Scripts/python.exe') -c "from pathlib import Path; from analyze_network import analyze, verify_export; verify_export(Path('evidence')/'$PrimaryRun', '$PrimaryRun'); analyze(Path('evidence')/'$PrimaryRun'/'results'/'$PrimaryRun')"
    if ($LASTEXITCODE -ne 0) { throw 'Network evidence validation failed' }
} elseif ($completion.completed -isnot [bool] -or -not $completion.completed) { throw 'Primary benchmark is incomplete' }
$files = @(Get-ChildItem $runRoot -Recurse -File -Filter '*.json' | Where-Object { $_.Name -notlike 'analysis*.json' })
if ($files.Count -eq 0 -or $files.Count -ne $receipt.files.Count) { throw 'Evidence file count mismatch' }
$measured = 0
foreach ($file in $files) {
    $relative = [IO.Path]::GetRelativePath($runRoot,$file.FullName).Replace('\','/')
    $remoteName = "runs/$PrimaryRun/$relative"
    $expected = @($receipt.files | Where-Object name -eq $remoteName)
    if ($expected.Count -ne 1) { throw "Receipt entry missing: $relative" }
    $content = [IO.File]::ReadAllText($file.FullName).TrimEnd("`n")
    $hash = [Convert]::ToHexString([Security.Cryptography.SHA256]::HashData([Text.Encoding]::UTF8.GetBytes($content))).ToLowerInvariant()
    if ($hash -ne $expected[0].sha256) { throw "Evidence changed after export: $relative" }
    if ($relative.StartsWith('workflows/')) {
        $record = $content | ConvertFrom-Json -Depth 100
        if ($record.phase -ne 'warmup') { $measured++ }
    }
    if ($isNetwork -and $relative.StartsWith('inference/')) { $measured++ }
}
$planned = if ($isNetwork) { $completion.measured_inference_calls } else { $manifest.planned_measured_workflows }
if ($measured -ne $planned) { throw 'Measured denominator mismatch' }
$baselineName = if ($isNetwork) { $PrimaryRun.Replace('network-','network-baseline-') + '.json' } else { 'baseline.json' }
$baseline = Get-Content (Join-Path $Evidence $baselineName) -Raw | ConvertFrom-Json -Depth 100
$accountState = Invoke-AzJson @('cognitiveservices','account','show','-g',$Group,'-n',$Account)
if ($accountState.properties.disableLocalAuth -ne $baseline.account.properties.disableLocalAuth) { throw 'Original Foundry authentication state has not been restored' }
$deployments = @(Invoke-AzJson @('cognitiveservices','account','deployment','list','-g',$Group,'-n',$Account))
if ($isNetwork) {
    if ($deployments.Count -ne $baseline.deployments.Count) { throw 'Deployment inventory changed during network test' }
    $storageState = Invoke-AzJson @('storage','account','show','-g',$Group,'-n',$Storage)
    foreach ($field in @('publicNetworkAccess','allowBlobPublicAccess','allowSharedKeyAccess')) {
        if ($storageState.$field -ne $baseline.storage.$field) { throw "Storage security changed: $field" }
    }
}
foreach ($original in $baseline.deployments) {
    $current = @($deployments | Where-Object name -eq $original.name)
    if ($current.Count -ne 1 -or $current[0].sku.name -ne $original.sku.name -or $current[0].properties.model.name -ne $original.properties.model.name -or $current[0].properties.model.version -ne $original.properties.model.version -or $current[0].properties.raiPolicyName -ne $original.properties.raiPolicyName) { throw "Preexisting deployment changed: $($original.name)" }
    if ($current[0].sku.capacity -ne $original.sku.capacity) {
        throw 'Deployment capacity changed; preserve current state and review before cleanup.'
    }
}
$execution = Invoke-AzJson @('group','show','-n',$ExecutionGroup)
if ($execution.tags.benchmarkOwner -ne $OwnerTag -or $execution.id -ne $ExecutionBase) { throw 'Execution group ownership mismatch' }
$inventory = @(Invoke-AzJson @('resource','list','-g',$ExecutionGroup))
if ($inventory.Count -eq 0) { throw 'Unexpected empty execution group' }
$endpoint = Invoke-AzJson @('network','private-endpoint','show','-g',$ExecutionGroup,'-n',"pe-$ResourcePrefix-storage")
$allowedTypes = @('Microsoft.Network/publicIPAddresses','Microsoft.Network/natGateways','Microsoft.Network/virtualNetworks','Microsoft.Network/privateDnsZones','Microsoft.Network/privateEndpoints','Microsoft.ContainerInstance/containerGroups')
foreach ($resource in $inventory) {
    if ($resource.type -eq 'Microsoft.Network/networkInterfaces' -and $endpoint.networkInterfaces.id -contains $resource.id) { continue }
    if ($resource.type -eq 'Microsoft.Network/privateDnsZones/virtualNetworkLinks' -and $resource.id.StartsWith("$ExecutionBase/providers/Microsoft.Network/privateDnsZones/privatelink.blob.core.windows.net/virtualNetworkLinks/")) { continue }
    if ($resource.type -notin $allowedTypes -or $resource.tags.benchmarkOwner -ne $OwnerTag) { throw "Unrecognized resource; cleanup blocked: $($resource.id)" }
    if ($resource.type -eq 'Microsoft.ContainerInstance/containerGroups') {
        $runId = $resource.tags.benchmarkRun
        if (-not $runId -or -not (Test-Path (Join-Path $Evidence "$runId.zip")) -or -not (Test-Path (Join-Path $Evidence "$runId/process-status.json"))) { throw "Container evidence missing: $($resource.name)" }
        $containerExport = Get-Content (Join-Path $Evidence "$runId/process-status.json") -Raw | ConvertFrom-Json
        if ($containerExport.exit_code -ne 0) { throw "Container run did not complete successfully: $runId" }
    }
}
Write-Evidence "cleanup-check-$([DateTime]::UtcNow.ToString('yyyyMMddTHHmmss')).json" @{primary_run=$PrimaryRun;network_campaign=$isNetwork;measured_units=$measured;unit=$(if ($isNetwork) {'inference-call'} else {'two-call-workflow'});evidence_files=$files.Count;original_deployments_checked=$baseline.deployments.Count;disableLocalAuth=$accountState.properties.disableLocalAuth;resources=$inventory;delete_requested=[bool]$Delete}
Write-Host "Verified $measured measured units, $($files.Count) cloud-matched evidence files and $($baseline.deployments.Count) baseline deployments."
if (-not $Delete) { Write-Host 'Validation only. No resources deleted.'; return }
foreach ($resource in $inventory) { Add-Ledger $resource.id $resource.type 'deletion-requested-after-evidence-verification' 'dedicated execution infrastructure' }
Add-Ledger $ExecutionBase 'dedicated-resource-group' 'deletion-requested' 'remove fixed-cost benchmark execution infrastructure'
$null = Invoke-AzJson @('group','delete','-n',$ExecutionGroup,'--yes')
$exists = Invoke-AzJson @('group','exists','-n',$ExecutionGroup)
if ($exists -ne $false) { throw 'Group deletion has not been verified' }
foreach ($resource in $inventory) { Add-Ledger $resource.id $resource.type 'deleted-verified-group-absent' 'fixed infrastructure removed' }
Add-Ledger $ExecutionBase 'dedicated-resource-group' 'deleted-verified' 'fixed infrastructure removed'
Write-Evidence "cleanup-complete-$([DateTime]::UtcNow.ToString('yyyyMMddTHHmmss')).json" @{group=$ExecutionGroup;group_exists=$exists;deleted_resources=$inventory;retained_group=$Group}
Write-Host 'Dedicated execution resource group deleted and absence verified. Original group retained.'