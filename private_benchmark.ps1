param(
    [ValidateSet('Network', 'Run', 'Status', 'Collect')][string]$Operation = 'Status',
    [ValidateSet('pilot', 'main', 'sensitivity', 'network', 'campaign')][string]$BenchmarkPhase = 'pilot',
    [ValidateSet('managed_identity', 'both')][string]$Authentication = 'managed_identity',
    [ValidateRange(1,60)][int]$Count = 1,
    [ValidatePattern('^[a-z0-9-]{1,40}$')][string]$BenchmarkRun = 'pilot-20260930a',
    [string]$CampaignConfigPath,
    [ValidateSet('swedencentral','italynorth')][string]$ClientRegion = 'swedencentral',
    [switch]$CampaignPilot,
    [ValidatePattern('^$|^[a-z0-9-]{1,40}$')][string]$SnapshotLabel
)

if ($SnapshotLabel -and $Operation -ne 'Collect') { throw 'Snapshots are supported only for Collect' }
. (Join-Path $PSScriptRoot 'azure_benchmark.ps1') -Action Status
if ($CampaignConfigPath) {
    $campaignSettings = Get-Content -LiteralPath $CampaignConfigPath -Raw | ConvertFrom-Json -AsHashtable
    $ExecutionGroup = $campaignSettings.client_groups[$ClientRegion]
    $OwnerTag = $campaignSettings.owner_tag
    $ResourcePrefix = $campaignSettings.resource_prefix
    $Location = $ClientRegion
    if ($ExecutionGroup -eq $Group -or $ExecutionGroup -notmatch '^rg-latency-campaign-' -or $OwnerTag -notmatch '^latency-campaign-' -or $ResourcePrefix -ne 'latency') { throw 'Invalid campaign isolation settings' }
    if ($Authentication -ne 'managed_identity') { throw 'Campaign execution requires managed identity' }
}
if ($BenchmarkPhase -eq 'campaign' -and -not $CampaignConfigPath) { throw 'A campaign configuration is required' }
$ExecutionBase = "/subscriptions/$Subscription/resourceGroups/$ExecutionGroup"
$VnetId = "$ExecutionBase/providers/Microsoft.Network/virtualNetworks/vnet-$ResourcePrefix-benchmark"
$RunnerSubnet = "$VnetId/subnets/runners"
$EndpointSubnet = "$VnetId/subnets/endpoints"
$ZoneId = "$ExecutionBase/providers/Microsoft.Network/privateDnsZones/privatelink.blob.core.windows.net"
$ContainerName = "aci-$ResourcePrefix-$BenchmarkRun"
$RunContainerId = "$ExecutionBase/providers/Microsoft.ContainerInstance/containerGroups/$ContainerName"

if ($Operation -eq 'Network') {
    $groups = @(Invoke-AzJson @('group', 'list'))
    $found = @($groups | Where-Object name -eq $ExecutionGroup)
    if ($found.Count -gt 0 -and $found[0].tags.benchmarkOwner -ne $OwnerTag) { throw 'Execution group is not owned by this benchmark' }
    $null = Invoke-AzJson @('group', 'create', '-n', $ExecutionGroup, '-l', $Location, '--tags', "benchmarkOwner=$OwnerTag")
    Add-Ledger $ExecutionBase 'dedicated-resource-group' 'created-or-verified' 'only benchmark resources; network and compute have fixed hourly charges'
    $inventory = @(Invoke-AzJson @('resource', 'list', '-g', $ExecutionGroup))
    $steps = @(
        @{id="$ExecutionBase/providers/Microsoft.Network/publicIPAddresses/pip-$ResourcePrefix-benchmark"; command=@('network','public-ip','create','-g',$ExecutionGroup,'-n',"pip-$ResourcePrefix-benchmark",'-l',$Location,'--sku','Standard','--allocation-method','Static','--tags',"benchmarkOwner=$OwnerTag")},
        @{id="$ExecutionBase/providers/Microsoft.Network/natGateways/nat-$ResourcePrefix-benchmark"; command=@('network','nat','gateway','create','-g',$ExecutionGroup,'-n',"nat-$ResourcePrefix-benchmark",'-l',$Location,'--public-ip-addresses',"pip-$ResourcePrefix-benchmark",'--tags',"benchmarkOwner=$OwnerTag")},
        @{id=$VnetId; command=@('network','vnet','create','-g',$ExecutionGroup,'-n',"vnet-$ResourcePrefix-benchmark",'-l',$Location,'--address-prefixes','10.97.0.0/16','--subnet-name','runners','--subnet-prefixes','10.97.1.0/24','--tags',"benchmarkOwner=$OwnerTag")},
        @{id=$ZoneId; command=@('network','private-dns','zone','create','-g',$ExecutionGroup,'-n','privatelink.blob.core.windows.net','--tags',"benchmarkOwner=$OwnerTag")}
    )
    foreach ($step in $steps) {
        if ($inventory.id -notcontains $step.id) {
            Add-Ledger $step.id 'benchmark-network-resource' 'creation-requested' 'see Azure meter for resource type'
            $null = Invoke-AzJson $step.command
            Add-Ledger $step.id 'benchmark-network-resource' 'created' 'see Azure meter for resource type'
        }
    }
    $null = Invoke-AzJson @('network','vnet','subnet','update','-g',$ExecutionGroup,'--vnet-name',"vnet-$ResourcePrefix-benchmark",'-n','runners','--delegations','Microsoft.ContainerInstance/containerGroups','--nat-gateway',"nat-$ResourcePrefix-benchmark")
    $null = Invoke-AzJson @('network','vnet','subnet','create','-g',$ExecutionGroup,'--vnet-name',"vnet-$ResourcePrefix-benchmark",'-n','endpoints','--address-prefixes','10.97.2.0/24')
    $links = @(Invoke-AzJson @('network','private-dns','link','vnet','list','-g',$ExecutionGroup,'-z','privatelink.blob.core.windows.net'))
    if ($links.name -notcontains 'benchmark-link') {
        $null = Invoke-AzJson @('network','private-dns','link','vnet','create','-g',$ExecutionGroup,'-z','privatelink.blob.core.windows.net','-n','benchmark-link','--virtual-network',$VnetId,'--registration-enabled','false')
    }
    $endpointId = "$ExecutionBase/providers/Microsoft.Network/privateEndpoints/pe-$ResourcePrefix-storage"
    if ($inventory.id -notcontains $endpointId) {
        $null = Invoke-AzJson @('network','private-endpoint','create','-g',$ExecutionGroup,'-n',"pe-$ResourcePrefix-storage",'-l',$Location,'--subnet',$EndpointSubnet,'--private-connection-resource-id',$StorageId,'--group-id','blob','--connection-name',"$ResourcePrefix-benchmark-storage",'--tags',"benchmarkOwner=$OwnerTag")
        Add-Ledger $endpointId 'storage-private-endpoint' 'created' 'fixed hourly and data processing charges'
    }
    $null = Invoke-AzJson @('network','private-endpoint','dns-zone-group','create','-g',$ExecutionGroup,'--endpoint-name',"pe-$ResourcePrefix-storage",'-n','default','--private-dns-zone',$ZoneId,'--zone-name','blob')
    $privateEndpoint = Invoke-AzJson @('network','private-endpoint','show','-g',$ExecutionGroup,'-n',"pe-$ResourcePrefix-storage")
    if ($privateEndpoint.provisioningState -ne 'Succeeded' -or $privateEndpoint.privateLinkServiceConnections[0].privateLinkServiceConnectionState.status -ne 'Approved') { throw 'Private endpoint is not ready and approved' }
    Write-Evidence "private-network-$([DateTime]::UtcNow.ToString('yyyyMMddTHHmmss')).json" @{group=$ExecutionGroup; endpoint=$privateEndpoint; resources=@(Invoke-AzJson @('resource','list','-g',$ExecutionGroup))}
    Write-Host 'Private network ready and storage connection approved.'
    return
}

if ($Operation -eq 'Run') {
    $containers = @(Invoke-AzJson @('container','list','-g',$ExecutionGroup))
    if ($containers.name -contains $ContainerName) { throw 'Run ID already exists' }
    $source = @{}
    foreach ($name in @('benchmark.py','run_benchmark.py','run_network.py','requirements.txt','config.json','export_evidence.py')) { $source[$name] = [IO.File]::ReadAllText((Join-Path $PSScriptRoot $name)) }
    if ($BenchmarkPhase -eq 'campaign') {
        $source['run_campaign.py'] = [IO.File]::ReadAllText((Join-Path $PSScriptRoot 'run_campaign.py'))
        $source['campaign.json'] = [IO.File]::ReadAllText($CampaignConfigPath)
    }
    $memoryStream = [IO.MemoryStream]::new()
    $gzip = [IO.Compression.GZipStream]::new($memoryStream,[IO.Compression.CompressionLevel]::Optimal,$true)
    $sourceBytes = [Text.Encoding]::UTF8.GetBytes(($source | ConvertTo-Json -Compress -Depth 30))
    $gzip.Write($sourceBytes)
    $gzip.Dispose()
    $payload = [Convert]::ToBase64String($memoryStream.ToArray())
    $memoryStream.Dispose()
    $bootstrap = @'
import base64,gzip,json,os,pathlib,subprocess,sys,http.server,threading
pathlib.Path('/work').mkdir(exist_ok=True)
os.chdir('/work')
for name,content in json.loads(gzip.decompress(base64.b64decode(os.environ.pop('SOURCE_PAYLOAD')))).items():
    pathlib.Path(name).write_text(content,encoding='utf-8')
finished=threading.Event()
class Handler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path=='/wait':
            ready=finished.wait(7500)
            self.send_response(200 if ready else 504)
            self.end_headers()
            self.wfile.write(b'completed' if ready else b'timeout')
        else:
            super().do_GET()
def execute():
    try:
        subprocess.run([sys.executable,'-m','pip','install','--disable-pip-version-check','-r','requirements.txt'],check=True)
        if os.environ['PHASE']=='campaign':
            command=[sys.executable,'-u','run_campaign.py','--run-id',os.environ['RUN_ID'],'--client-region',os.environ['CLIENT_REGION']]
            if os.environ['CAMPAIGN_PILOT']=='True':
                command.append('--pilot')
            code=subprocess.call(command,timeout=172800)
        elif os.environ['PHASE']=='network':
            code=subprocess.call([sys.executable,'-u','run_network.py','--cloud','--samples','60','--inference-repeats',os.environ['REPEATS'],'--run-id',os.environ['RUN_ID']],timeout=2100)
        else:
            code=subprocess.call([sys.executable,'-u','run_benchmark.py','--phase',os.environ['PHASE'],'--repeats',os.environ['REPEATS'],'--run-id',os.environ['RUN_ID']])
    except Exception as error:
        print(type(error).__name__,str(error),flush=True)
        code=99
    from datetime import datetime,timezone
    pathlib.Path('process-status.json').write_text(json.dumps({'exit_code':code,'ended_utc':datetime.now(timezone.utc).isoformat()}),encoding='utf-8')
    print('PROCESS_EXIT_CODE='+str(code),flush=True)
    finished.set()
threading.Thread(target=execute,daemon=True).start()
http.server.ThreadingHTTPServer(('127.0.0.1',8765),Handler).serve_forever()
'@
    $encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($bootstrap))
    if ($Authentication -eq 'both') {
        Set-LocalAuth $false
        try {
            $keys = Invoke-AzJson @('cognitiveservices','account','keys','list','-g',$Group,'-n',$Account)
        } catch {
            Set-LocalAuth $true
            throw
        }
    }
    $variables = @(@{name='AZURE_CLIENT_ID';value=$config.identity_client_id},@{name='SOURCE_PAYLOAD';value=$payload},@{name='RUN_ID';value=$BenchmarkRun},@{name='PHASE';value=$BenchmarkPhase},@{name='REPEATS';value=[string]$Count},@{name='PYTHONUNBUFFERED';value='1'},@{name='BENCHMARK_AUTH';value=$Authentication})
    $variables += @(@{name='CLIENT_REGION';value=$ClientRegion},@{name='CAMPAIGN_PILOT';value=[string][bool]$CampaignPilot})
    if ($Authentication -eq 'both') { $variables += @{name='BENCHMARK_API_KEY';secureValue=$keys.key1} }
    $identities = @{}
    $identities[$IdentityId] = @{}
    $compute = if ($BenchmarkPhase -eq 'campaign') { @{cpu=4;memoryInGB=8} } else { @{cpu=1;memoryInGB=2} }
    $body = @{location=$Location;tags=@{benchmarkOwner=$OwnerTag;benchmarkRun=$BenchmarkRun};identity=@{type='UserAssigned';userAssignedIdentities=$identities};properties=@{osType='Linux';restartPolicy='Never';subnetIds=@(@{id=$RunnerSubnet});containers=@(@{name='benchmark';properties=@{image='python:3.13-slim-bookworm';command=@('python','-u','-c',"import base64;exec(base64.b64decode('$encoded'))");resources=@{requests=$compute};environmentVariables=$variables}})}}
    Add-Ledger $RunContainerId 'private-container-instance' 'creation-requested' 'fixed compute charges until stopped or deleted'
    try {
        $created = Invoke-Arm 'PUT' $RunContainerId '2023-05-01' $body
        Add-Ledger $RunContainerId 'private-container-instance' $created.properties.provisioningState 'fixed compute charges until stopped or deleted'
        Write-Host "Started creation of $ContainerName in $ExecutionGroup"
    } catch {
        if ($Authentication -eq 'both') { Set-LocalAuth $true }
        throw
    } finally { $keys=$null; $variables=$null; $body=$null }
    return
}

if ($Operation -eq 'Collect') {
    $exportName = if ($SnapshotLabel) { "$BenchmarkRun-snapshot-$SnapshotLabel" } else { $BenchmarkRun }
    $zipPath = Join-Path $Evidence "$exportName.zip"
    $extractPath = Join-Path $Evidence $exportName
    if ((Test-Path $zipPath) -or (Test-Path $extractPath)) { throw 'Export destination already exists; use a new snapshot label or preserve the existing final export' }
    $transcript = Join-Path $Evidence "$exportName-export-$([DateTime]::UtcNow.ToString('yyyyMMddTHHmmss')).txt"
    if (Test-Path $transcript) { throw 'Export transcript already exists' }
    $exportBuffer = [IO.MemoryStream]::new()
    $exportGzip = [IO.Compression.GZipStream]::new($exportBuffer,[IO.Compression.CompressionLevel]::Optimal,$true)
    $exportText = [IO.File]::ReadAllText((Join-Path $PSScriptRoot 'export_evidence.py'))
    if ($SnapshotLabel) { $exportText = "import os`nos.environ['SNAPSHOT_LABEL'] = '$SnapshotLabel'`n" + $exportText }
    $exportGzip.Write([Text.Encoding]::UTF8.GetBytes($exportText))
    $exportGzip.Dispose()
    $exportSource = [Convert]::ToBase64String($exportBuffer.ToArray()).Replace('+','-').Replace('/','_')
    $exportBuffer.Dispose()
    $exportCommand = "python -c exec(__import__('gzip').decompress(__import__('base64').urlsafe_b64decode('$exportSource')))"
    $session = Invoke-Arm 'POST' "$RunContainerId/containers/benchmark/exec" '2023-05-01' @{command=$exportCommand;terminalSize=@{rows=24;cols=160}}
    $socket = [Net.WebSockets.ClientWebSocket]::new()
    $timeout = [Threading.CancellationTokenSource]::new([TimeSpan]::FromMinutes(130))
    $received = [IO.MemoryStream]::new()
    $archive = [IO.MemoryStream]::new()
    $pending = ''
    $expectedIndex = 0
    $expectedArchiveHash = $null
    $ended = $false
    try {
        $null = $socket.ConnectAsync([uri]$session.webSocketUri,$timeout.Token).GetAwaiter().GetResult()
        $passwordBytes = [Text.Encoding]::UTF8.GetBytes($session.password)
        $null = $socket.SendAsync([ArraySegment[byte]]::new($passwordBytes),[Net.WebSockets.WebSocketMessageType]::Text,$true,$timeout.Token).GetAwaiter().GetResult()
        $session=$null
        $passwordBytes=$null
        $buffer = [byte[]]::new(32768)
        while ($socket.State -eq [Net.WebSockets.WebSocketState]::Open) {
            $frame = $socket.ReceiveAsync([ArraySegment[byte]]::new($buffer),$timeout.Token).GetAwaiter().GetResult()
            if ($frame.MessageType -eq [Net.WebSockets.WebSocketMessageType]::Close) { break }
            $received.Write($buffer,0,$frame.Count)
            $pending += [Text.Encoding]::UTF8.GetString($buffer,0,$frame.Count)
            $exportLimit = if ($BenchmarkPhase -eq 'campaign') { 536870912 } else { 52428800 }
            if ($received.Length -gt $exportLimit) { throw "Export exceeded $exportLimit byte limit" }
            $header = [regex]::Match($pending,'EVIDENCE_BEGIN ([a-f0-9]{64})')
            if ($header.Success) { $expectedArchiveHash = $header.Groups[1].Value }
            if ($pending.Contains('ACK>')) {
                $packet = [regex]::Match($pending,'CHUNK (\d+) ([a-f0-9]{64}) ([A-Za-z0-9_=-]+) ENDCHUNK')
                $valid = $false
                if ($packet.Success -and [int]$packet.Groups[1].Value -eq $expectedIndex) {
                    try {
                        $chunkBytes = [Convert]::FromBase64String($packet.Groups[3].Value.Replace('-','+').Replace('_','/'))
                        $chunkHash = [Convert]::ToHexString([Security.Cryptography.SHA256]::HashData($chunkBytes)).ToLowerInvariant()
                        $valid = $chunkHash -eq $packet.Groups[2].Value
                    } catch { $valid = $false }
                }
                $answer = "RETRY`n"
                if ($valid) {
                    $archive.Write($chunkBytes)
                    $answer = "NEXT $expectedIndex`n"
                    $expectedIndex++
                }
                $answerBytes = [Text.Encoding]::UTF8.GetBytes($answer)
                $null = $socket.SendAsync([ArraySegment[byte]]::new($answerBytes),[Net.WebSockets.WebSocketMessageType]::Text,$true,$timeout.Token).GetAwaiter().GetResult()
                $pending = ''
            }
            if ($pending.Contains('EVIDENCE_END')) { $ended=$true; break }
        }
        [IO.File]::WriteAllBytes($transcript,$received.ToArray())
        $zipBytes = $archive.ToArray()
    } finally { $socket.Dispose(); $timeout.Dispose(); $received.Dispose(); $archive.Dispose(); $session=$null; $passwordBytes=$null }
    if (-not $ended -or -not $expectedArchiveHash -or $expectedIndex -eq 0) { throw 'Complete export markers not found' }
    $actual = [Convert]::ToHexString([Security.Cryptography.SHA256]::HashData($zipBytes)).ToLowerInvariant()
    if ($actual -ne $expectedArchiveHash) { throw 'Export SHA256 mismatch' }
    if (Test-Path $zipPath) { throw 'Verified archive already exists; refusing to overwrite' }
    [IO.File]::WriteAllBytes($zipPath,$zipBytes)
    [IO.Compression.ZipFile]::ExtractToDirectory($zipPath,$extractPath)
    Write-Host "Verified export $exportName SHA256 $actual"
    return
}

$containers = @(Invoke-AzJson @('container','list','-g',$ExecutionGroup))
$containers | Select-Object name,provisioningState,@{n='state';e={$_.containers[0].instanceView.currentState.state}},@{n='detail';e={$_.containers[0].instanceView.currentState.detailStatus}} | Format-Table