# Foundry Latency Benchmark

**EU Data Zone versus Global Standard:** deployment differences depend on workload, API and observation window. At approximately 1,500 output tokens, all four within-model/API comparisons have paired mean-duration intervals including zero; this is not proof of equivalence. First-content and tail latency can still differ. Model families are evaluated separately.

Open the [published interactive report](https://faustinopalma.github.io/foundry-latency-benchmark/) or the [self-contained local report](preview.html): **22,400 measured workflows, 22,388 exact correct outcomes and 12 technical failures across 224 fully sampled conditions**. Each condition has 100 attempts. The completed revised campaign includes overnight and morning geography, plus overnight context, output-length, concurrency and prompt-cache sweeps. Read the [campaign results and methods](CAMPAIGN.md) for denominators, interpretation and limitations.

The report contains only aggregate synthetic-test results. Filters keep models and time windows separate. It works locally without a server; project-specific GitHub Pages serves the same file at `/foundry-latency-benchmark/`, independently of the profile's main site.

## Earlier Experiments

The earlier 960-workflow benchmark observed faster GPT-5.4 EU deployments and slower Responses completion on EU deployments for that controlled workload. It completed 957 correct outcomes with zero HTTP/SDK errors. The API-key comparison remains untested. Read [the historical findings](REPORT.md) for numerical results, limits and resource disposition. Those observations are not pooled with the expanded campaign.

A separate [network experiment](NETWORK_REPORT.md) measured 240 lightweight requests from the PC and Azure, plus 320 Azure model calls. Observable TCP RTT during inference averaged 1.72 ms from Azure; connection setup averaged 16.28 ms. PC lightweight requests averaged 69.11 ms with reuse and 272.50 ms on new connections. All network evidence was verified before deleting the recreated temporary execution group; internal gateway-to-model routing remains unmeasured.

The expanded campaign adds Sweden Central/Italy North runners and resources, a second geographic window and concurrency up to 16 per deployment. The original later windows were cancelled before execution; the morning replacement started on October 1 at 07:30 Europe/Rome. Two windows do not establish 24-hour stability.

## Repository Contents And Privacy

This repository contains aggregate results from synthetic tests, workload generators, measurement code, analyzers and tests. Environment configuration, Azure CLI credential caches, raw responses, resource inventories and verified evidence archives are excluded from Git. Historical evidence was verified by the operator, but is not distributed with this repository; readers can inspect the method and generate their own observations.

The published operational scripts load environment-specific values from `.private/environment.json`; [environment.example.json](environment.example.json) contains no real identifiers. The generated runtime `config.json` is also ignored. Sanitization changed naming, configuration loading and explanatory text after the experiments; archived executed sources remain unchanged in private evidence. The shared cleanup script blocks all baseline capacity drift and contains no environment-specific exceptions.

## Historical Main-Run Scope

- Two dependent model calls per workflow. The second call receives the actual first response and verifies the selected order against a reference catalog.
- GPT-5.4 version 2026-03-05 and GPT-5.4 Mini version 2026-03-17, each deployed with GlobalStandard and DataZoneStandard in the existing Sweden Central Foundry resource.
- Chat Completions and Responses; streaming and non-streaming; store=false; strict equivalent JSON schemas; reasoning effort none; low verbosity; 512 output-token limit.
- Real user-assigned Managed Identity from an Azure Container Instance. API-key comparison is currently unavailable: attempts to change disableLocalAuth were followed by fresh reads still reporting true. The underlying enforcement mechanism has not been established.
- Four concurrent workers, one per deployment. Within a deployment, calls are sequential and the four conditions are shuffled for each case. This is a low-concurrency latency experiment, not a throughput-capacity or SLA test.
- The main run schedules 60 measured workflows per condition: 16 conditions, 960 workflows, 1,920 calls if every first call succeeds. An additional 16 warm-up workflows are excluded from statistics.
- The calibrated pilot observed 1,501-1,505 input tokens and 100-101 output tokens per call. Actual usage is preserved in every observation. A unique request marker near the start reduces prompt-cache reuse; cached tokens are measured rather than assumed absent.

## Measurements

Each observation preserves the submitted wire request and its SHA256, service response ID, exact output, expected answer, usage, completion status, refusal/error state and allowlisted response headers. No authorization headers, keys or bearer tokens are persisted.

Workflow duration starts before preparing the first request and ends after validating the second response. Each call also has a separate client-side duration, header-arrival time and measured token-provider overhead. Subtracting summed call durations from workflow duration exposes local preparation and validation overhead. SDK automatic retries are disabled. Failures remain in the denominator; an unsuccessful first call prevents the dependent second call.

Streaming first-content latency is the time to the first non-empty visible text event. Metadata events and empty chunks do not count. Content-event gaps are not token gaps: an event can contain multiple tokens. The normalized visible-token interval is an estimate based on visible-content span and local tokenization, not a directly observed per-token timestamp. Non-streaming observations cannot identify first-token or between-token latency.

Correctness requires valid JSON, the exact schema and exact field values for both calls. Successful transport alone is insufficient. Usage availability and completed generation are also required for a successful workflow. Arithmetic, selected catalog entry, routing, delivery time, stage and the deterministic audit note are validated.

## Evidence And Analysis

The runner writes each workflow independently to the private benchmark blob container and local container disk. Completed-run exports compare all local files with the cloud inventory and contents, then transfer a ZIP through the authenticated ACI management channel. Every transfer block and the complete ZIP are checked with SHA256. Failed transfers are retained separately and never accepted as evidence. Generated evidence includes resource identifiers and may include sensitive outputs from future workloads; do not publish it without a separate content review.

The main ZIP contains a storage-verification receipt with a hash for each cloud file. Extracted source files and the run manifest preserve the exact implementation and package versions used. The original pilot used a smaller output schema; use the calibrated pilot or main run when comparing workload dimensions.

Analysis computes pooled statistics from individual observations, not averages of per-batch percentiles. Latency summaries for correct workflows are accompanied by attempted/correct/error counts. Paired comparisons join matching case indexes and report left-minus-right differences. Exploratory 95% paired bootstrap intervals require at least 20 pairs; they do not correct temporal dependence or multiple comparisons and are not production guarantees.

## Local Checks And New Runs

Use Python 3.13 and PowerShell 7. The unit tests use the Python standard library and make no Azure calls. A fresh clone can run them before installing cloud dependencies:

```powershell
python -m venv .venv
& '.\.venv\Scripts\python.exe' -m unittest test_benchmark -v
```

For a new Azure run, install [requirements.txt](requirements.txt), create `.private/environment.json` from the example and fill every environment-specific field. Select an existing Foundry resource in Sweden Central, unique storage and identity names, an ownership tag and a distinct execution resource group. The retained resource group must already exist. Use names dedicated to this experiment; do not point the scripts at production resources. Verify model availability, quota and permission to create resources and scoped role assignments before provisioning.

```powershell
& '.\.venv\Scripts\python.exe' -m pip install -r requirements.txt
New-Item -ItemType Directory -Path .private -Force | Out-Null
Copy-Item environment.example.json .private/environment.json
$env:AZURE_CONFIG_DIR = Join-Path $PWD '.azure'
```

Authenticate Azure CLI to the intended tenant using this isolated configuration directory. After editing the private configuration, `azure_benchmark.ps1 -Action Provision` creates the benchmark deployments, storage, identity, scoped roles and runtime configuration. Inspect the recorded inventory and complete private-network setup with `private_benchmark.ps1 -Operation Network`. Use the Managed Identity workflow below; the older `azure_benchmark.ps1 -Action Run` path assumes public storage access and API-key authentication and was not the path used for the reported results.

```powershell
& '.\.venv\Scripts\python.exe' -m unittest test_benchmark -v
.\private_benchmark.ps1 -Operation Run -BenchmarkPhase main -Count 60 -BenchmarkRun <new-unique-run-id> -Authentication managed_identity
.\private_benchmark.ps1 -Operation Collect -BenchmarkRun <run-id>
& '.\.venv\Scripts\python.exe' analyze_benchmark.py evidence/<run-id>/results/<run-id>
```

Do not rerun Provision against existing resources. Every run ID and evidence path is write-once. Container export requires a running execution container; retain it until the ZIP is verified. The execution container intentionally stays alive after the model workload to support export and therefore must be stopped or deleted afterward.

The dedicated execution resource group was deleted after the main run, recreated for the network follow-up, then deleted again after its verified export. Recreate the private network before starting a new run. Analyze your own exported evidence with the included analyzers; the historical raw datasets are not included in a clone. The completed main run's authoritative private derived artifacts are analysis-v2.json and analysis-v2.md; earlier analysis files were superseded without changing raw observations.

`cleanup_benchmark.ps1 -PrimaryRun <run-id>` validates evidence and resource ownership without deleting resources. Add `-Delete` only after reviewing its checks. Network-campaign cleanup expects the documented 60 probe pairs, 20 repeats per inference condition and a separately captured pre-run baseline; it is not a general-purpose resource deletion tool. The anonymized scripts were validated locally for syntax, configuration mapping and unit-test behavior; no new Azure deployment was performed for publication.

## Historical Resources And Security

The deletion statements in this section concern the earlier main and network experiments. The expanded campaign has separate resources and evidence gates described in [CAMPAIGN.md](CAMPAIGN.md); publishing the report does not stop or delete Azure resources.

Azure CLI uses the isolated `.azure/` directory; its credential caches are excluded from Git and must not be shared. `.private/`, `config.json` and `evidence/` contain local-only operational material. Do not override their ignore rules with forced staging.

Benchmark resources comprise model deployments, private evidence storage and a user-assigned identity. Environment-specific names and resource identifiers are excluded from the public results.

The deleted execution group contained only benchmark execution/network infrastructure: ACI containers, VNet, delegated runner subnet, endpoint subnet, NAT gateway and public IP for outbound connectivity, storage Private Endpoint, private DNS zone and VNet link. Newly created fixed-cost infrastructure was removed after evidence export and preservation checks.

The storage account remains publicNetworkAccess=Disabled, allowBlobPublicAccess=false and allowSharedKeyAccess=false. Its private endpoint was approved during execution and removed with the temporary group. Foundry remains disableLocalAuth=true. Runtime MI has Cognitive Services OpenAI User scoped to the Foundry resource and Storage Blob Data Contributor scoped to the benchmark blob container. The operator's blob role does not bypass the private network boundary.

The append-only ledger is evidence/resources.jsonl. evidence/baseline.json is the original snapshot. Creation-requested records are intent, not proof of successful creation; actual inventories and postconditions establish state. Standard model deployments are token-metered, not provisioned-throughput reservations. Retained storage incurs byte/transaction charges. NAT, public IP, Private Endpoint and allocated execution containers have fixed-time charges and are cleanup targets.

## Official References

- [Deployment routing and data boundaries](https://learn.microsoft.com/azure/foundry/foundry-models/concepts/deployment-types)
- [Client/server latency metrics and influencing factors](https://learn.microsoft.com/azure/foundry/openai/how-to/latency)
- [Responses API behavior](https://learn.microsoft.com/azure/foundry/openai/how-to/responses)
- [Reasoning-token behavior](https://learn.microsoft.com/azure/foundry/openai/how-to/reasoning)

GlobalStandard does not imply that every request runs in the United States. DataZoneStandard does not guarantee a fixed latency advantage. A difference measured here is conditional on model, workload, time, deployment, client location and configuration.
