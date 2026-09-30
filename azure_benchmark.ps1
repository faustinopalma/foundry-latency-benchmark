param(
    [ValidateSet('Provision', 'FinishSetup', 'Run', 'Collect', 'RestoreSecurity', 'Status')]
    [string]$Action = 'Status',
    [ValidateSet('pilot', 'main', 'sensitivity')]
    [string]$Phase = 'pilot',
    [ValidatePattern('^[a-z0-9-]{1,40}$')]
    [string]$RunId = 'pilot-20260930a',
    [ValidateRange(1, 60)]
    [int]$Repeats = 1
)

$ErrorActionPreference = 'Stop'
$env:AZURE_CONFIG_DIR = Join-Path $PSScriptRoot '.azure'
$env:PYTHONUTF8 = '1'
$EnvironmentPath = Join-Path $PSScriptRoot '.private/environment.json'
if (-not (Test-Path -LiteralPath $EnvironmentPath)) { throw 'Create .private/environment.json from environment.example.json before using Azure commands.' }
$settings = Get-Content -LiteralPath $EnvironmentPath -Raw | ConvertFrom-Json -AsHashtable
foreach ($field in @('subscription','tenant','resource_group','location','foundry','storage','identity','owner_tag','execution_group','resource_prefix','deployment_suffix')) {
    if ($settings[$field] -isnot [string] -or [string]::IsNullOrWhiteSpace($settings[$field])) { throw "Missing environment setting: $field" }
}
foreach ($field in @('subscription','tenant')) {
    $identifier = [guid]::Empty
    if (-not [guid]::TryParse($settings[$field], [ref]$identifier) -or $identifier -eq [guid]::Empty) { throw "Invalid environment identifier: $field" }
}
foreach ($field in @('resource_prefix','deployment_suffix')) {
    if ($settings[$field] -cnotmatch '^[a-z0-9-]{1,20}$') { throw "Invalid naming setting: $field" }
}
if ($settings.execution_group -eq $settings.resource_group) { throw 'Execution and retained resource groups must be different.' }
$Subscription = $settings.subscription
$Tenant = $settings.tenant
$Group = $settings.resource_group
$Location = $settings.location
$Account = $settings.foundry
$Storage = $settings.storage
$Identity = $settings.identity
$OwnerTag = $settings.owner_tag
$ExecutionGroup = $settings.execution_group
$ResourcePrefix = $settings.resource_prefix
$DeploymentSuffix = $settings.deployment_suffix
$BaseId = "/subscriptions/$Subscription/resourceGroups/$Group"
$FoundryId = "$BaseId/providers/Microsoft.CognitiveServices/accounts/$Account"
$StorageId = "$BaseId/providers/Microsoft.Storage/storageAccounts/$Storage"
$IdentityId = "$BaseId/providers/Microsoft.ManagedIdentity/userAssignedIdentities/$Identity"
$ContainerScope = "$StorageId/blobServices/default/containers/benchmark"
$Evidence = Join-Path $PSScriptRoot 'evidence'
[IO.Directory]::CreateDirectory($Evidence) | Out-Null
$Ledger = Join-Path $Evidence 'resources.jsonl'
$ConfigPath = Join-Path $PSScriptRoot 'config.json'

function Invoke-AzJson {
    param([string[]]$Arguments)
    if ($Arguments[0] -eq 'ad') {
        $captured = & az @Arguments --only-show-errors -o json
    } else {
        $captured = & az @Arguments --subscription $Subscription --only-show-errors -o json
    }
    if ($LASTEXITCODE -ne 0) { throw "Azure CLI operation failed: $($Arguments[0..1] -join ' ')" }
    if ($captured) { return ($captured | ConvertFrom-Json -Depth 100) }
}

function Write-Evidence {
    param([string]$Name, [object]$Value)
    $path = Join-Path $Evidence $Name
    if (Test-Path -LiteralPath $path) { throw "Refusing to overwrite evidence: $Name" }
    [IO.File]::WriteAllText($path, ($Value | ConvertTo-Json -Depth 100), [Text.UTF8Encoding]::new($false))
}

function Add-Ledger {
    param([string]$Id, [string]$Kind, [string]$State, [string]$Cost)
    $entry = @{utc = [DateTimeOffset]::UtcNow.ToString('o'); id = $Id; kind = $Kind; state = $State; cost = $Cost; owner = $OwnerTag}
    [IO.File]::AppendAllText($Ledger, (($entry | ConvertTo-Json -Compress) + "`n"), [Text.UTF8Encoding]::new($false))
}

function Invoke-Arm {
    param([string]$Method, [string]$Id, [string]$ApiVersion, [object]$Body)
    $access = Invoke-AzJson @('account', 'get-access-token', '--resource', 'https://management.azure.com/')
    $parameters = @{Method = $Method; Uri = "https://management.azure.com${Id}?api-version=$ApiVersion"; Headers = @{Authorization = "Bearer $($access.accessToken)"}; TimeoutSec = 60}
    if ($PSVersionTable.PSVersion -ge [version]'7.4') { $parameters.OperationTimeoutSeconds = 90 }
    if ($null -ne $Body) {
        $parameters.Body = $Body | ConvertTo-Json -Depth 100 -Compress
        $parameters.ContentType = 'application/json'
    }
    try { Invoke-RestMethod @parameters } finally { $access = $null; $parameters = $null }
}

function Set-LocalAuth {
    param([bool]$Disabled)
    $null = Invoke-Arm 'PATCH' $FoundryId '2025-06-01' @{properties = @{disableLocalAuth = $Disabled}}
    $verified = Invoke-AzJson @('cognitiveservices', 'account', 'show', '-g', $Group, '-n', $Account)
    if ($verified.properties.disableLocalAuth -ne $Disabled) { throw 'Local authentication postcondition failed' }
    Add-Ledger $FoundryId 'existing-foundry-security' "disableLocalAuth=$Disabled" 'unchanged'
    Write-Host "Verified disableLocalAuth=$Disabled"
}

$context = Invoke-AzJson @('account', 'show')
if ($context.id -ne $Subscription -or $context.tenantId -ne $Tenant) { throw 'Wrong subscription or tenant' }

if ($Action -in @('Provision', 'FinishSetup')) {
    if ($Action -eq 'Provision') {
    $existing = @(Invoke-AzJson @('resource', 'list', '-g', $Group))
    if ($existing.name -contains $Storage -or $existing.name -contains $Identity) { throw 'Dedicated resource names already exist; inspect inventory before resuming' }
    $accountBefore = Invoke-AzJson @('cognitiveservices', 'account', 'show', '-g', $Group, '-n', $Account)
    $deploymentsBefore = @(Invoke-AzJson @('cognitiveservices', 'account', 'deployment', 'list', '-g', $Group, '-n', $Account))
    if (Test-Path (Join-Path $Evidence 'baseline.json')) {
        $baseline = Get-Content (Join-Path $Evidence 'baseline.json') -Raw | ConvertFrom-Json -Depth 100
        if ($baseline.account.id -ne $FoundryId) { throw 'Baseline target mismatch' }
        foreach ($original in $baseline.deployments) {
            $present = @($deploymentsBefore | Where-Object name -eq $original.name)
            if ($present.Count -ne 1 -or $present[0].properties.model.version -ne $original.properties.model.version -or $present[0].sku.capacity -ne $original.sku.capacity) { throw 'Original deployment drift; inspect before resuming' }
        }
    } else {
        Write-Evidence 'baseline.json' @{utc = [DateTimeOffset]::UtcNow.ToString('o'); resources = $existing; account = $accountBefore; deployments = $deploymentsBefore}
    }
    $models = @(
        @{name="$ResourcePrefix-gpt54-global-$DeploymentSuffix"; model='gpt-5.4'; version='2026-03-05'; sku='GlobalStandard'; capacity=100},
        @{name="$ResourcePrefix-gpt54-eu-$DeploymentSuffix"; model='gpt-5.4'; version='2026-03-05'; sku='DataZoneStandard'; capacity=100},
        @{name="$ResourcePrefix-mini-global-$DeploymentSuffix"; model='gpt-5.4-mini'; version='2026-03-17'; sku='GlobalStandard'; capacity=100},
        @{name="$ResourcePrefix-mini-eu-$DeploymentSuffix"; model='gpt-5.4-mini'; version='2026-03-17'; sku='DataZoneStandard'; capacity=100}
    )
    foreach ($model in $models) {
        $deploymentId = "$FoundryId/deployments/$($model.name)"
        $present = @($deploymentsBefore | Where-Object name -eq $model.name)
        if ($present.Count -gt 0) {
            $tracked = @(Get-Content $Ledger | ConvertFrom-Json | Where-Object { $_.id -eq $deploymentId -and $_.owner -eq $OwnerTag })
            if ($tracked.Count -eq 0 -or $present[0].properties.model.name -ne $model.model -or $present[0].properties.model.version -ne $model.version -or $present[0].sku.name -ne $model.sku -or $present[0].sku.capacity -ne 100 -or $present[0].properties.provisioningState -ne 'Succeeded') { throw "Existing deployment cannot be reused: $($model.name)" }
            Add-Ledger $deploymentId 'model-deployment' 'verified-existing-benchmark-deployment' 'per-token; no PTU reservation'
            Write-Host "Verified existing benchmark deployment $($model.name)"
            continue
        }
        Add-Ledger $deploymentId 'model-deployment' 'creation-requested' 'per-token; no PTU reservation'
        $deploymentBody = @{sku=@{name=$model.sku; capacity=100}; properties=@{model=@{format='OpenAI'; name=$model.model; version=$model.version}; raiPolicyName='Microsoft.DefaultV2'; versionUpgradeOption='NoAutoUpgrade'}}
        $deployment = Invoke-Arm 'PUT' $deploymentId '2025-06-01' $deploymentBody
        Add-Ledger $deploymentId 'model-deployment' $deployment.properties.provisioningState 'per-token; no PTU reservation'
        Write-Host "Deployment $($model.name): $($deployment.properties.provisioningState)"
    }
    Add-Ledger $StorageId 'storage-account' 'creation-requested' 'metered retained bytes and transactions'
    $storageResource = Invoke-AzJson @('storage', 'account', 'create', '-g', $Group, '-n', $Storage, '-l', $Location, '--sku', 'Standard_LRS', '--kind', 'StorageV2', '--https-only', 'true', '--min-tls-version', 'TLS1_2', '--allow-blob-public-access', 'false', '--allow-shared-key-access', 'false', '--tags', "benchmarkOwner=$OwnerTag")
    Add-Ledger $StorageId 'storage-account' $storageResource.provisioningState 'metered retained bytes and transactions'
    $null = Invoke-Arm 'PUT' $ContainerScope '2023-05-01' @{properties = @{publicAccess = 'None'}}
    Add-Ledger $ContainerScope 'private-blob-container' 'created' 'included in storage usage'
    $identityResource = Invoke-AzJson @('identity', 'create', '-g', $Group, '-n', $Identity, '-l', $Location, '--tags', "benchmarkOwner=$OwnerTag")
    Add-Ledger $IdentityId 'user-assigned-managed-identity' 'created' 'no standalone fixed charge'
    foreach ($grant in @(
        @{role='Cognitive Services OpenAI User'; scope=$FoundryId; principal=$identityResource.principalId; type='ServicePrincipal'},
        @{role='Storage Blob Data Contributor'; scope=$ContainerScope; principal=$identityResource.principalId; type='ServicePrincipal'}
    )) {
        $assignment = Invoke-AzJson @('role', 'assignment', 'create', '--assignee-object-id', $grant.principal, '--assignee-principal-type', $grant.type, '--role', $grant.role, '--scope', $grant.scope)
        Add-Ledger $assignment.id 'role-assignment' 'created' 'no standalone fixed charge'
    }
    } else {
        $identityResource = Invoke-AzJson @('identity', 'show', '-g', $Group, '-n', $Identity)
        $storageResource = Invoke-AzJson @('storage', 'account', 'show', '-g', $Group, '-n', $Storage)
        if ($identityResource.tags.benchmarkOwner -ne $OwnerTag -or $storageResource.tags.benchmarkOwner -ne $OwnerTag) { throw 'Dedicated resource ownership mismatch' }
        $models = @(Invoke-AzJson @('cognitiveservices', 'account', 'deployment', 'list', '-g', $Group, '-n', $Account) | Where-Object { $_.name -like "$ResourcePrefix-*-$DeploymentSuffix" } | ForEach-Object {
            if ($_.properties.provisioningState -ne 'Succeeded') { throw 'Benchmark deployment is not ready' }
            @{name=$_.name; model=$_.properties.model.name; version=$_.properties.model.version; sku=$_.sku.name; capacity=$_.sku.capacity}
        })
        if ($models.Count -ne 4) { throw 'Expected exactly four ready benchmark deployments' }
    }
    $operator = Invoke-AzJson @('ad', 'signed-in-user', 'show', '--query', 'id')
    $operatorGrant = Invoke-AzJson @('role', 'assignment', 'create', '--assignee-object-id', $operator, '--assignee-principal-type', 'User', '--role', 'Storage Blob Data Contributor', '--scope', $ContainerScope)
    Add-Ledger $operatorGrant.id 'operator-storage-role' 'created' 'no standalone fixed charge'
    $config = @{subscription=$Subscription; tenant=$Tenant; resource_group=$Group; foundry=$Account; location=$Location; endpoint="https://$Account.openai.azure.com/openai/v1/"; storage_endpoint="https://$Storage.blob.core.windows.net"; identity_client_id=$identityResource.clientId; deployments=$models; owner_tag=$OwnerTag}
    if (Test-Path -LiteralPath $ConfigPath) { throw 'Refusing to overwrite benchmark configuration' }
    [IO.File]::WriteAllText($ConfigPath, ($config | ConvertTo-Json -Depth 50), [Text.UTF8Encoding]::new($false))
    Write-Evidence 'provisioned.json' $config
    Write-Host 'Provisioned. Existing Foundry local-auth policy has not been changed.'
    return
}

if ($Action -eq 'RestoreSecurity') {
    Set-LocalAuth $true
    $baseline = Get-Content (Join-Path $Evidence 'baseline.json') -Raw | ConvertFrom-Json -Depth 100
    $after = @(Invoke-AzJson @('cognitiveservices', 'account', 'deployment', 'list', '-g', $Group, '-n', $Account))
    foreach ($before in $baseline.deployments) {
        $match = @($after | Where-Object name -eq $before.name)
        if ($match.Count -ne 1) { throw "Original deployment missing: $($before.name)" }
        if ($match[0].sku.name -ne $before.sku.name -or $match[0].sku.capacity -ne $before.sku.capacity -or $match[0].properties.model.version -ne $before.properties.model.version) { throw "Original deployment drift: $($before.name)" }
    }
    Write-Evidence "security-restored-$([DateTime]::UtcNow.ToString('yyyyMMddTHHmmss')).json" @{disableLocalAuth=$true; existing_deployments_verified=$baseline.deployments.Count; deployments=$after}
    return
}

$config = Get-Content $ConfigPath -Raw | ConvertFrom-Json -Depth 50
if ($Action -eq 'Run') {
    $containerName = "aci-$ResourcePrefix-$RunId"
    $containerId = "$BaseId/providers/Microsoft.ContainerInstance/containerGroups/$containerName"
    $existingContainers = @(Invoke-AzJson @('container', 'list', '-g', $Group))
    if ($existingContainers.name -contains $containerName) { throw 'Run container already exists; use a unique run ID' }
    $files = @('benchmark.py', 'run_benchmark.py', 'requirements.txt', 'config.json')
    foreach ($file in $files) {
        $null = Invoke-AzJson @('storage', 'blob', 'upload', '--account-name', $Storage, '--container-name', 'benchmark', '--name', "source/$RunId/$file", '--file', (Join-Path $PSScriptRoot $file), '--auth-mode', 'login', '--overwrite', 'false')
    }
    $bootstrap = @'
import os, pathlib, subprocess, sys
subprocess.run([sys.executable,'-m','pip','install','--disable-pip-version-check','azure-identity==1.25.3','azure-storage-blob==12.30.3'], check=True)
from azure.identity import ManagedIdentityCredential
from azure.storage.blob import BlobServiceClient
credential=ManagedIdentityCredential(client_id=os.environ['AZURE_CLIENT_ID'])
service=BlobServiceClient(os.environ['STORAGE_ENDPOINT'],credential=credential)
container=service.get_container_client('benchmark')
pathlib.Path('/work').mkdir(exist_ok=True)
os.chdir('/work')
for name in ['benchmark.py','run_benchmark.py','requirements.txt','config.json']:
    pathlib.Path(name).write_bytes(container.download_blob('source/'+os.environ['RUN_ID']+'/'+name).readall())
service.close()
credential.close()
subprocess.run([sys.executable,'-m','pip','install','--disable-pip-version-check','-r','requirements.txt'],check=True)
os.execv(sys.executable,[sys.executable,'-u','run_benchmark.py','--phase',os.environ['PHASE'],'--repeats',os.environ['REPEATS'],'--run-id',os.environ['RUN_ID']])
'@
    $encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($bootstrap))
    $keyResult = Invoke-AzJson @('cognitiveservices', 'account', 'keys', 'list', '-g', $Group, '-n', $Account)
    $variables = @(
        @{name='AZURE_CLIENT_ID'; value=$config.identity_client_id},
        @{name='STORAGE_ENDPOINT'; value=$config.storage_endpoint},
        @{name='RUN_ID'; value=$RunId}, @{name='PHASE'; value=$Phase},
        @{name='REPEATS'; value=[string]$Repeats}, @{name='PYTHONUNBUFFERED'; value='1'},
        @{name='BENCHMARK_API_KEY'; secureValue=$keyResult.key1}
    )
    $identities = @{}
    $identities[$IdentityId] = @{}
    $body = @{location=$Location; tags=@{benchmarkOwner=$OwnerTag; benchmarkRun=$RunId}; identity=@{type='UserAssigned'; userAssignedIdentities=$identities}; properties=@{
        osType='Linux'; restartPolicy='Never'; containers=@(@{name='benchmark'; properties=@{
            image='python:3.13-slim-bookworm'; command=@('python','-u','-c',"import base64;exec(base64.b64decode('$encoded'))");
            resources=@{requests=@{cpu=1; memoryInGB=2}}; environmentVariables=$variables
        }})
    }}
    Add-Ledger $containerId 'container-instance' 'creation-requested' 'billed while allocated/running; eligible for deletion after evidence export'
    Set-LocalAuth $false
    try {
        $created = Invoke-Arm 'PUT' $containerId '2023-05-01' $body
        Add-Ledger $containerId 'container-instance' $created.properties.provisioningState 'billed while allocated/running; eligible for deletion after evidence export'
        Write-Host "Created $containerName; run ID $RunId; phase $Phase; repeats $Repeats"
    } catch {
        Set-LocalAuth $true
        throw
    } finally { $keyResult=$null; $variables=$null; $body=$null }
    return
}

if ($Action -eq 'Collect') {
    $destination = Join-Path $Evidence 'download'
    [IO.Directory]::CreateDirectory($destination) | Out-Null
    $null = Invoke-AzJson @('storage', 'blob', 'download-batch', '--account-name', $Storage, '--source', 'benchmark', '--destination', $destination, '--pattern', "runs/$RunId/*", '--auth-mode', 'login', '--overwrite', 'false', '--no-progress')
    Write-Host "Evidence downloaded under $destination"
    return
}

$containers = @(Invoke-AzJson @('container', 'list', '-g', $Group))
$containers | Select-Object name, provisioningState, @{n='state';e={$_.instanceView.state}}, @{n='containerState';e={$_.containers[0].instanceView.currentState.state}}, @{n='exitCode';e={$_.containers[0].instanceView.currentState.exitCode}} | Format-Table