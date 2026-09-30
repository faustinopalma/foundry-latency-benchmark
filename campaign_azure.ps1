param(
    [ValidateSet('Provision','Run','Status','Collect','Metrics','StopFinished')][string]$Operation = 'Status',
    [ValidatePattern('^[a-z0-9-]{1,35}$')][string]$BatchId = 'campaign-20260930',
    [ValidatePattern('^[a-z0-9]{4,16}$')][string]$Suffix = '20260930fp',
    [string]$StartUtc,
    [string]$EndUtc,
    [string[]]$PilotProfiles,
    [ValidatePattern('^[a-z0-9-]{1,35}$')][string]$PilotBatchId,
    [string]$PythonPath = 'python',
    [switch]$Pilot,
    [ValidatePattern('^$|^[a-z0-9-]{1,40}$')][string]$SnapshotLabel
)

if ($SnapshotLabel -and $Operation -ne 'Collect') { throw 'Snapshots are supported only for Collect' }
. (Join-Path $PSScriptRoot 'azure_benchmark.ps1') -Action Status
$OwnerTag = "latency-campaign-$Suffix"
$planPath = Join-Path $PSScriptRoot ".private/campaign-$Suffix.json"
$modelGroup = "rg-latency-campaign-models-$Suffix"
$clientGroups = @{swedencentral="rg-latency-campaign-sc-$Suffix";italynorth="rg-latency-campaign-it-$Suffix"}
$identityResource = Invoke-AzJson @('identity','show','-g',$Group,'-n',$Identity)
$storageResource = Invoke-AzJson @('storage','account','show','-g',$Group,'-n',$Storage)
if ($identityResource.tags.benchmarkOwner -ne $settings.owner_tag -or $storageResource.tags.benchmarkOwner -ne $settings.owner_tag) { throw 'Retained resource ownership mismatch' }
if ($storageResource.publicNetworkAccess -ne 'Disabled' -or $storageResource.allowSharedKeyAccess -ne $false -or $storageResource.allowBlobPublicAccess -ne $false) { throw 'Private storage security requirements are not met' }

if ($Operation -eq 'Provision') {
    $catalog = @(Invoke-AzJson @('cognitiveservices','model','list','--location','italynorth'))
    foreach ($expected in @(@{name='gpt-5.4';version='2026-03-05'},@{name='gpt-5.4-mini';version='2026-03-17'})) {
        $matches = @($catalog | Where-Object { $_.model.name -eq $expected.name -and $_.model.version -eq $expected.version })
        $skus = @($matches | ForEach-Object { $_.model.skus.name })
        if ($skus -notcontains 'GlobalStandard' -or $skus -notcontains 'DataZoneStandard') { throw 'Italy North does not expose the required model SKUs' }
    }
    $snapshotName = "campaign-$Suffix-baseline.json"
    if (-not (Test-Path (Join-Path $Evidence $snapshotName))) {
        Write-Evidence $snapshotName @{utc=[DateTimeOffset]::UtcNow.ToString('o'); account=(Invoke-AzJson @('cognitiveservices','account','show','-g',$Group,'-n',$Account)); deployments=@(Invoke-AzJson @('cognitiveservices','account','deployment','list','-g',$Group,'-n',$Account)); storage=$storageResource; identity=$identityResource}
    }
    $existingGroups = @(Invoke-AzJson @('group','list'))
    $existingGroup = @($existingGroups | Where-Object name -eq $modelGroup)
    if ($existingGroup.Count -gt 0 -and $existingGroup[0].tags.benchmarkOwner -ne $OwnerTag) { throw 'Model group ownership mismatch' }
    if ($existingGroup.Count -eq 0) {
        $null = Invoke-AzJson @('group','create','-n',$modelGroup,'-l','swedencentral','--tags',"benchmarkOwner=$OwnerTag")
        Add-Ledger "/subscriptions/$Subscription/resourceGroups/$modelGroup" 'campaign-model-group' 'created' 'pay-per-token; no PTU'
    }
    $deployments = [Collections.Generic.List[object]]::new()
    foreach ($regional in @(@{region='swedencentral';code='sc'},@{region='italynorth';code='it'})) {
        $foundryName = "aif-latency-$($regional.code)-$Suffix"
        $foundryResourceId = "/subscriptions/$Subscription/resourceGroups/$modelGroup/providers/Microsoft.CognitiveServices/accounts/$foundryName"
        $inventory = @(Invoke-AzJson @('resource','list','-g',$modelGroup))
        $existing = @($inventory | Where-Object id -eq $foundryResourceId)
        if ($existing.Count -gt 0 -and $existing[0].tags.benchmarkOwner -ne $OwnerTag) { throw 'Foundry resource ownership mismatch' }
        if ($existing.Count -eq 0) {
            Add-Ledger $foundryResourceId 'campaign-foundry' 'creation-requested' 'pay-per-token'
            $null = Invoke-Arm 'PUT' $foundryResourceId '2025-06-01' @{location=$regional.region;kind='AIServices';sku=@{name='S0'};tags=@{benchmarkOwner=$OwnerTag};identity=@{type='SystemAssigned'};properties=@{customSubDomainName=$foundryName;allowProjectManagement=$true;disableLocalAuth=$true;publicNetworkAccess='Enabled'}}
        }
        $verified = Invoke-AzJson @('cognitiveservices','account','show','-g',$modelGroup,'-n',$foundryName)
        if ($verified.properties.provisioningState -ne 'Succeeded') { throw "Foundry provisioning is $($verified.properties.provisioningState); rerun Provision after inspecting its state" }
        if ($verified.location -ne $regional.region -or $verified.properties.disableLocalAuth -ne $true -or $verified.properties.publicNetworkAccess -ne 'Enabled') { throw 'Foundry configuration mismatch' }
        Add-Ledger $foundryResourceId 'campaign-foundry' 'verified' 'pay-per-token'
        $assignments = @(Invoke-AzJson @('role','assignment','list','--scope',$foundryResourceId))
        $existingGrant = @($assignments | Where-Object { $_.principalId -eq $identityResource.principalId -and $_.roleDefinitionName -eq 'Cognitive Services OpenAI User' })
        if ($existingGrant.Count -eq 0) {
            $grant = Invoke-AzJson @('role','assignment','create','--assignee-object-id',$identityResource.principalId,'--assignee-principal-type','ServicePrincipal','--role','Cognitive Services OpenAI User','--scope',$foundryResourceId)
            Add-Ledger $grant.id 'campaign-inference-role' 'created' 'no standalone fixed charge'
        }
        foreach ($model in @(@{model='gpt-5.4';version='2026-03-05';code='gpt54'},@{model='gpt-5.4-mini';version='2026-03-17';code='mini'})) {
            foreach ($sku in @(@{name='GlobalStandard';code='global'},@{name='DataZoneStandard';code='eu'})) {
                $name = "$($model.code)-$($sku.code)"
                $deploymentId = "$foundryResourceId/deployments/$name"
                $present = @(Invoke-AzJson @('cognitiveservices','account','deployment','list','-g',$modelGroup,'-n',$foundryName) | Where-Object name -eq $name)
                if ($present.Count -gt 0 -and ($present[0].properties.model.name -ne $model.model -or $present[0].properties.model.version -ne $model.version -or $present[0].sku.name -ne $sku.name -or $present[0].sku.capacity -notin @(100,200))) { throw 'Existing campaign deployment cannot be resized safely' }
                if ($present.Count -eq 0 -or $present[0].sku.capacity -ne 100) {
                    Add-Ledger $deploymentId 'campaign-model-deployment' 'configuration-requested' 'pay-per-token; approved capacity 100 K TPM'
                    $null = Invoke-Arm 'PUT' $deploymentId '2025-06-01' @{sku=@{name=$sku.name;capacity=100};properties=@{model=@{format='OpenAI';name=$model.model;version=$model.version};raiPolicyName='Microsoft.DefaultV2';versionUpgradeOption='NoAutoUpgrade';dynamicThrottlingEnabled=$false}}
                }
                $actual = Invoke-AzJson @('cognitiveservices','account','deployment','show','-g',$modelGroup,'-n',$foundryName,'--deployment-name',$name)
                if ($actual.properties.provisioningState -ne 'Succeeded' -or $actual.properties.model.name -ne $model.model -or $actual.properties.model.version -ne $model.version -or $actual.sku.name -ne $sku.name -or $actual.sku.capacity -ne 100 -or $actual.properties.raiPolicyName -ne 'Microsoft.DefaultV2' -or $actual.properties.versionUpgradeOption -ne 'NoAutoUpgrade' -or $actual.properties.dynamicThrottlingEnabled -eq $true) { throw "Deployment not ready or mismatched: $name" }
                Add-Ledger $deploymentId 'campaign-model-deployment' 'verified' 'pay-per-token; no reservation'
                $deployments.Add(@{label="$($regional.code)-$name";name=$name;model=$model.model;version=$model.version;sku=$sku.name;capacity=100;region=$regional.region;endpoint="https://$foundryName.openai.azure.com/openai/v1/";resource_id=$deploymentId})
                Write-Host "Verified $($regional.region) $name $($model.version) $($sku.name) 100 K TPM"
            }
        }
    }
    $plan = @{subscription=$Subscription;tenant=$Tenant;model_group=$modelGroup;client_groups=$clientGroups;owner_tag=$OwnerTag;resource_prefix='latency';storage_endpoint="https://$Storage.blob.core.windows.net";identity_client_id=$identityResource.clientId;deployments=$deployments.ToArray();repeats=100}
    if (Test-Path $planPath) {
        $existingPlan = Get-Content $planPath -Raw | ConvertFrom-Json -AsHashtable
        if ($existingPlan.owner_tag -ne $OwnerTag -or $existingPlan.subscription -ne $Subscription) { throw 'Existing campaign plan does not match' }
    } else {
        [IO.File]::WriteAllText($planPath, ($plan | ConvertTo-Json -Depth 50), [Text.UTF8Encoding]::new($false))
    }
    foreach ($region in @('swedencentral','italynorth')) {
        & (Join-Path $PSScriptRoot 'private_benchmark.ps1') -Operation Network -CampaignConfigPath $planPath -ClientRegion $region
    }
    Write-Evidence "campaign-$Suffix-ready-$([DateTimeOffset]::UtcNow.ToString('yyyyMMddTHHmmss')).json" @{utc=[DateTimeOffset]::UtcNow.ToString('o');plan=$plan;resources=@(Invoke-AzJson @('resource','list','-g',$modelGroup))}
    return
}

if (-not (Test-Path $planPath)) { throw 'Provision the campaign first' }
$plan = Get-Content $planPath -Raw | ConvertFrom-Json -AsHashtable
$runPath = Join-Path $PSScriptRoot ".private/campaign-run-$BatchId.json"
if ($Operation -eq 'StopFinished') {
    $verifiedContainers = @()
    foreach ($regional in @(@{region='swedencentral';code='sc'},@{region='italynorth';code='it'})) {
        $run = "$BatchId-$($regional.code)"
        $archivePath = Join-Path $Evidence "$run.zip"
        $archive = [IO.Compression.ZipFile]::OpenRead($archivePath)
        try {
            $readJson = {
                param($name)
                $entry = $archive.GetEntry($name)
                if (-not $entry) { throw "Archive member is missing: $name" }
                $reader = [IO.StreamReader]::new($entry.Open())
                try { $reader.ReadToEnd() | ConvertFrom-Json -AsHashtable } finally { $reader.Dispose() }
            }
            $receipt = & $readJson 'storage-verification.json'
            $status = & $readJson 'process-status.json'
            $summary = & $readJson "results/$run/summary.json"
            if ($receipt.run_id -ne $run -or $receipt.all_equal -ne $true -or $receipt.files.Count -eq 0 -or $status.exit_code -ne 0 -or $summary.complete -ne $true) { throw 'Completed verified export is required before stopping a container' }
            $names = [Collections.Generic.HashSet[string]]::new()
            foreach ($file in $receipt.files) {
                $cloudPrefix = "runs/$run/"
                if (-not $file.name.StartsWith($cloudPrefix)) { throw 'Unexpected receipt prefix' }
                $name = "results/$run/" + $file.name.Substring($cloudPrefix.Length)
                if (-not $names.Add($name)) { throw 'Duplicate receipt entry' }
                $entry = $archive.GetEntry($name)
                if (-not $entry) { throw 'Receipt member missing from archive' }
                $stream = $entry.Open()
                $buffer = [IO.MemoryStream]::new()
                try { $stream.CopyTo($buffer); $bytes = $buffer.ToArray() } finally { $stream.Dispose(); $buffer.Dispose() }
                $length = $bytes.Length
                if ($length -gt 0 -and $bytes[$length-1] -eq 10) { $length-- }
                $hasher = [Security.Cryptography.SHA256]::Create()
                try { $hash = [Convert]::ToHexString($hasher.ComputeHash($bytes,0,$length)).ToLowerInvariant() } finally { $hasher.Dispose() }
                if ($length -ne $file.bytes -or $hash -ne $file.sha256) { throw 'Archive evidence hash mismatch' }
            }
            $members = @($archive.Entries | Where-Object { $_.FullName.StartsWith("results/$run/") -and $_.FullName.EndsWith('.json') })
            if ($members.Count -ne $names.Count -or @($members | Where-Object { -not $names.Contains($_.FullName) }).Count) { throw 'Archive inventory mismatch' }
        } finally { $archive.Dispose() }
        $groupName = $plan.client_groups[$regional.region]
        $name = "aci-latency-$run"
        $container = Invoke-AzJson @('container','show','-g',$groupName,'-n',$name)
        if ($container.tags.benchmarkOwner -ne $OwnerTag -or $container.tags.benchmarkRun -ne $run) { throw 'Container ownership mismatch' }
        $verifiedContainers += @{group=$groupName;name=$name;run=$run;archive_sha256=(Get-FileHash $archivePath -Algorithm SHA256).Hash.ToLowerInvariant();files=$names.Count}
    }
    foreach ($container in $verifiedContainers) {
        $null = Invoke-AzJson @('container','stop','-g',$container.group,'-n',$container.name)
        $actual = Invoke-AzJson @('container','show','-g',$container.group,'-n',$container.name)
        if ($actual.instanceView.state -ne 'Stopped') { throw 'Container stop postcondition not yet satisfied' }
        Add-Ledger $actual.id 'verified-campaign-container' 'stopped' 'compute stopped after immutable export verification'
        Write-Evidence "campaign-stop-$($container.run)-$([DateTimeOffset]::UtcNow.ToString('yyyyMMddTHHmmss')).json" @{utc=[DateTimeOffset]::UtcNow.ToString('o');verified=$container;state=$actual.instanceView.state}
        Write-Host "Stopped verified container: $($container.run)"
    }
    return
}
if ($Operation -eq 'Status') {
    foreach ($regional in @(@{region='swedencentral';code='sc'},@{region='italynorth';code='it'})) {
        $container = Invoke-AzJson @('container','show','-g',$plan.client_groups[$regional.region],'-n',"aci-latency-$BatchId-$($regional.code)")
        @{observed_utc=[DateTimeOffset]::UtcNow.ToString('o');client_region=$regional.region;provisioning=$container.provisioningState;container_state=$container.containers[0].instanceView.currentState.state;group_state=$container.instanceView.state} | ConvertTo-Json
        if ($container.provisioningState -eq 'Succeeded' -and $container.containers[0].instanceView.currentState.state -eq 'Running') {
            (Invoke-Arm 'GET' "$($container.id)/containers/benchmark/logs" '2023-05-01&tail=5&timestamps=true' $null).content
        }
    }
    return
}
if ($Operation -eq 'Metrics') {
    if ($StartUtc -notmatch '(Z|\+00:00)$' -or $EndUtc -notmatch '(Z|\+00:00)$') { throw 'Metric interval requires explicit UTC timestamps' }
    $from = [DateTimeOffset]::Parse($StartUtc)
    $until = [DateTimeOffset]::Parse($EndUtc)
    if ($until -le $from) { throw 'Metric interval is empty or reversed' }
    $wanted = @('AzureOpenAITTLTInMS','AzureOpenAITimeToResponse','AzureOpenAINormalizedTBTInMS','AzureOpenAINormalizedTTFTInMS','GeneratedTokens','ProcessedPromptTokens')
    $observations = [Collections.Generic.List[object]]::new()
    $resources = @($plan.deployments | ForEach-Object { $_.resource_id -replace '/deployments/[^/]+$','' } | Sort-Object -Unique)
    foreach ($resourceId in $resources) {
        $resource = Invoke-Arm 'GET' $resourceId '2025-06-01' $null
        if ($resource.tags.benchmarkOwner -ne $OwnerTag) { throw 'Metric target ownership mismatch' }
        $definitions = @(Invoke-AzJson @('monitor','metrics','list-definitions','--resource',$resourceId))
        foreach ($name in $wanted) {
            $definition = @($definitions | Where-Object { $_.name.value -eq $name })
            if ($definition.Count -ne 1) { throw "Metric definition unavailable: $name" }
            $aggregation = if ($name -in @('GeneratedTokens','ProcessedPromptTokens')) { 'Total' } else { 'Average' }
            if ($definition[0].supportedAggregationTypes -notcontains $aggregation) { throw 'Metric aggregation is unsupported' }
            $response = Invoke-AzJson @('monitor','metrics','list','--resource',$resourceId,'--metric',$name,'--aggregation',$aggregation,'--interval','PT1M','--start-time',$from.ToString('o'),'--end-time',$until.ToString('o'),'--filter',"ModelDeploymentName eq '*'",'--top','50')
            $observations.Add(@{resource_id=$resourceId;metric=$name;definition=$definition[0];response=$response})
            $points = @($response.value.timeseries.data | Where-Object { $null -ne $_.average -or $null -ne $_.total })
            Write-Host "$($resource.location) $name : $($points.Count) populated one-minute points"
        }
    }
    Write-Evidence "campaign-metrics-$BatchId-$([DateTimeOffset]::UtcNow.ToString('yyyyMMddTHHmmss')).json" @{queried_utc=[DateTimeOffset]::UtcNow.ToString('o');start_utc=$from.ToString('o');end_utc=$until.ToString('o');observations=$observations.ToArray()}
    return
}
if ($Operation -eq 'Run') {
    if (Test-Path $runPath) { throw 'Batch configuration already exists; inspect it before a targeted restart' }
    if ($PilotProfiles) {
        if (-not $Pilot -or $PilotProfiles -notcontains 'geography') { throw 'A pilot profile subset must include geography' }
        $plan['pilot_profiles'] = $PilotProfiles
    }
    if (-not $Pilot) {
        if (-not $PilotBatchId) { throw 'Specify the successfully validated pilot batch before launching the full campaign' }
        $pilotArchives = @((Join-Path $Evidence "$PilotBatchId-sc.zip"),(Join-Path $Evidence "$PilotBatchId-it.zip"))
        $gate = & $PythonPath (Join-Path $PSScriptRoot 'analyze_benchmark.py') $PSScriptRoot --validate-pilot-archives @pilotArchives
        if ($LASTEXITCODE -ne 0) { throw 'Pilot verification failed; full campaign was not launched' }
        $pilotReceipt = $gate | ConvertFrom-Json -AsHashtable
        if ($pilotReceipt.validated -ne $true) { throw 'Pilot did not validate' }
        Write-Evidence "campaign-pilot-gate-$BatchId.json" @{utc=[DateTimeOffset]::UtcNow.ToString('o');validation=$pilotReceipt}
        $parsed = [DateTimeOffset]::Parse($StartUtc)
        if ($parsed.Offset -ne [TimeSpan]::Zero -or $parsed -le [DateTimeOffset]::UtcNow) { throw 'Full campaign start must be in the future with UTC offset' }
    }
    $plan['start_utc'] = if ($Pilot) { [DateTimeOffset]::UtcNow.ToString('o') } else { $parsed.ToString('o') }
    $plan['peer_run_ids'] = @("$BatchId-sc","$BatchId-it")
    [IO.File]::WriteAllText($runPath, ($plan | ConvertTo-Json -Depth 50), [Text.UTF8Encoding]::new($false))
    Write-Evidence "campaign-run-$BatchId.json" @{utc=[DateTimeOffset]::UtcNow.ToString('o');pilot=[bool]$Pilot;configuration=$plan}
}
foreach ($regional in @(@{region='swedencentral';code='sc'},@{region='italynorth';code='it'})) {
    & (Join-Path $PSScriptRoot 'private_benchmark.ps1') -Operation $Operation -CampaignConfigPath $(if ($Operation -eq 'Status') { $planPath } else { $runPath }) -ClientRegion $regional.region -BenchmarkPhase campaign -BenchmarkRun "$BatchId-$($regional.code)" -CampaignPilot:$Pilot -SnapshotLabel $SnapshotLabel
}