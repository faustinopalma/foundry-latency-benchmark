# Foundry Latency Benchmark: Findings

## Conclusion

Under the tested workload, GPT-5.4 was faster with EU DataZoneStandard than GlobalStandard, and Responses was slower than Chat Completions on the EU deployments. The size and direction of the differences depended on model, API and streaming mode. The results do not establish a universal EU advantage or a fixed Responses overhead. API-key versus Managed Identity latency remains untested because local authentication could not be enabled on the test Foundry resource.

All 960 planned measured workflows ran, with 60 attempts per condition. Of these, 957 produced both exact expected answers. All 1,917 executed model calls returned HTTP 200; three first calls produced incorrect field values, so their dependent second calls were intentionally not executed. There were zero observed HTTP/SDK errors. The 979 raw evidence files were compared with their private-storage copies, exported and hash-verified before deleting the dedicated execution resource group.

These are synthetic two-call workflow measurements, not a production latency guarantee. The primary comparison is non-streaming. Streaming provides a separate demonstration that first-content latency and total completion time can rank APIs differently. This report publishes aggregate results from the synthetic benchmark; raw evidence and infrastructure identifiers are excluded.

A later [network isolation experiment](NETWORK_REPORT.md) measured 240 lightweight requests and 320 additional single model calls. Azure client-to-peer TCP RTT averaged 1.72 ms and new-connection setup averaged 16.28 ms during inference. Internal gateway-to-model transport remains unmeasured. That follow-up's results and cleanup are separate from the original two-call dataset below.

## Primary Results

Each workflow contains two dependent calls. All conditions below use a real Azure Managed Identity, reasoning effort none, store=false and equivalent strict structured output. Durations describe correct workflows; the correctness denominator includes every attempt. Values are seconds.

| Model | Deployment | API | Correct/attempted | Mean | P50 | P90 |
| --- | --- | --- | ---: | ---: | ---: | ---: |
| GPT-5.4 | EU DataZoneStandard | Chat Completions | 60/60 | 3.393 | 3.367 | 4.122 |
| GPT-5.4 | GlobalStandard | Chat Completions | 60/60 | 4.242 | 4.094 | 5.118 |
| GPT-5.4 | EU DataZoneStandard | Responses | 60/60 | 3.937 | 3.731 | 4.994 |
| GPT-5.4 | GlobalStandard | Responses | 60/60 | 4.457 | 4.135 | 5.390 |
| GPT-5.4 Mini | EU DataZoneStandard | Chat Completions | 59/60 | 2.188 | 2.426 | 2.695 |
| GPT-5.4 Mini | GlobalStandard | Chat Completions | 60/60 | 3.182 | 3.225 | 4.066 |
| GPT-5.4 Mini | EU DataZoneStandard | Responses | 59/60 | 3.318 | 2.748 | 4.676 |
| GPT-5.4 Mini | GlobalStandard | Responses | 60/60 | 3.212 | 2.918 | 4.555 |

The following differences pair the same synthetic case across conditions, including only pairs where both workflows were correct. They are case-matched, not simultaneous measurements. The exploratory 95% bootstrap intervals do not correct temporal dependence or multiple comparisons.

| Non-streaming comparison | Valid pairs | Mean difference, ms | 95% interval, ms | Interpretation |
| --- | ---: | ---: | --- | --- |
| GPT-5.4: EU minus Global, Chat | 60 | -849 | [-1225, -535] | EU faster in this run |
| GPT-5.4: EU minus Global, Responses | 60 | -519 | [-1012, -107] | EU faster in this run |
| Mini: EU minus Global, Chat | 59 | -997 | [-1221, -775] | EU faster in this run |
| Mini: EU minus Global, Responses | 59 | +106 | [-251, +498] | No clear difference established |
| GPT-5.4 EU: Responses minus Chat | 60 | +544 | [+255, +852] | Responses slower in this run |
| Mini EU: Responses minus Chat | 59 | +1130 | [+766, +1513] | Responses slower in this run |
| GPT-5.4 Global: Responses minus Chat | 60 | +215 | [-261, +745] | No clear difference established |
| Mini Global: Responses minus Chat | 60 | +30 | [-253, +337] | No clear difference established |

For GPT-5.4, the EU mean reduction was approximately 20.0% with Chat Completions and 11.6% with Responses. Mini's non-streaming Responses result provides no basis for promising an EU latency advantage. The private full analysis covers 16 conditions and 16 paired comparisons, including streaming results and explicit failure rows. The tables here show the primary comparisons and EU streaming measurements.

## Reading Duration Metrics

Time to first token (TTFT) measures the delay until generation starts delivering tokens; time between tokens (TBT) describes subsequent token delivery intervals. A complete non-streaming two-call workflow cannot be converted into either metric by division. Dividing by two gives only an average call-duration estimate if local orchestration overhead is accounted for; dividing by output tokens mixes waiting, generation, transport and local overhead.

P50 and P90 are the 50th and 90th percentiles of the observations. Averaging percentiles or standard deviations from separate batches generally does not recover the statistic of the pooled dataset. This experiment preserves individual observations and computes pooled statistics directly, while reporting correctness denominators alongside latency.

## First Content Versus Completion

Streaming measurements below are per call, pooled across stages 1 and 2 of correct workflows. First content means the first non-empty visible text event at the client. It excludes earlier metadata events and does not claim to be the API gateway's TTFT metric.

| EU deployment | API | Correct calls included | Mean first content, ms | Mean complete call, ms | Mean two-call workflow, ms |
| --- | --- | ---: | ---: | ---: | ---: |
| GPT-5.4 | Chat Completions | 120 | 1107 | 1472 | 2949 |
| GPT-5.4 | Responses | 120 | 783 | 1853 | 3710 |
| GPT-5.4 Mini | Chat Completions | 120 | 869 | 1016 | 2037 |
| GPT-5.4 Mini | Responses | 120 | 666 | 1586 | 3177 |

Responses produced visible content earlier but completed later in these EU streaming comparisons. This directly demonstrates why a total-duration table cannot be interpreted as a first-token table. It also shows that faster first content does not necessarily improve the time until a complete structured answer is available.

SSE event timestamps are not individual token timestamps: an event can contain multiple tokens, and buffering or API delivery behavior can affect the observed gaps. The dataset includes a normalized visible-content interval estimate, but it cannot establish intrinsic model TBT or identify a backend implementation cause. Non-streaming samples have no measured first-content or inter-token time.

## Correctness And Authentication

GPT-5.4 completed 480/480 workflows correctly. Mini completed 477/480 correctly. All three Mini failures occurred in stage 1 of case 7: expected priority normal, route standard and service_days 3 became urgent, express and 1. JSON and schema remained valid; these were incorrect business fields, not formatting or network failures. The second call was skipped after each failed first call. Three second calls therefore remain unexecuted rather than failed. The failing conditions were non-streaming Mini EU Chat Completions, Mini EU Responses and Mini Global streaming Responses.

During the measured campaign, the client-side MI token-provider callback averaged 0.0467 ms per call and its maximum was 0.5504 ms. In a separate diagnostic before warm-up, the first acquisition with a new credential took 211.8 ms; later new-credential observations can still benefit from platform-side token caching. The client reused credentials and connections during the measured workload. These measurements isolate token retrieval in this Python client; they do not quantify the complete difference between bearer-token and API-key validation at the service.

## Method And Evidence

- Main run: main-20260930a, 30 September 2026, 11:59:34-12:16:06 UTC, including warm-up within this interval.
- One Linux x86-64 Azure Container Instance in Sweden Central, 1 vCPU and 2 GiB; four workers, each sequential within its own deployment. Condition order was shuffled within each case block. Faster workers finished earlier, so comparisons do not remove all time-of-day/backend-load effects.
- GPT-5.4 version 2026-03-05 and GPT-5.4 Mini version 2026-03-17; GlobalStandard and EU DataZoneStandard; capacity 100 each; no PTU reservation. Existing deployments were not used for inference.
- Python 3.13 container; OpenAI SDK 3.19.0, azure-identity 1.25.3, azure-storage-blob 12.30.3, httpx 0.28.1 and tiktoken 0.12.0. Exact executed source and package manifest are inside the archive.
- Approximately 1,501-1,505 input tokens and 100-101 output tokens per call. A deterministic audit-note field calibrated output length; the rest of the output requires catalog selection, arithmetic and routing. This is a narrow correctness test, not a general model-quality evaluation.
- Measured usage: 2,881,168 input tokens, 193,467 output tokens, zero reported reasoning tokens and zero cached tokens. Including 32 warm-up calls: 1,949 calls, 2,929,262 input tokens and 196,715 output tokens. The two separate pilots are excluded from these totals.
- Automatic SDK retries were disabled. The two-call workflow includes local request preparation and validation; mean local overhead beyond summed call timers was 4.96 ms. Storage writes occur after the timed workflow.
- All 979 raw main-run files matched the private blob inventory and contents. The verification receipt and archive are retained privately. Archive SHA256: 7089b631623a4537346954bd6fe95793dca27a98a0332fef72e8c87a8a237178. This hash identifies the preserved archive; it does not make the unpublished evidence independently inspectable.
- The earlier main-run analysis.json/analysis.md artifacts contain an incomplete comparison list and are superseded by analysis-v2.json/analysis-v2.md. Raw observations were unchanged. Main-run validation passed 12 unit tests plus a full-dataset check of 16 conditions, 16 comparisons and three explicit failures; the current test suite includes two additional network tests.

No tests of API-key authentication, high concurrency, provisioned throughput, other client regions in the main campaign, other reasoning settings or multi-agent/RAG orchestration were completed. These two-call measurements cannot predict the latency of a larger orchestration workflow. No exact Azure invoice cost is claimed from token counts alone.

## Resource Disposition

The dedicated execution resource group was deleted after evidence verification; a subsequent read confirmed it no longer exists. Its execution containers, NAT gateway, public IP, Private Endpoint, VNet and private DNS resources are no longer retained. A private append-only resource ledger records creation attempts, successful operations and verified deletion.

The four benchmark model deployments are retained. Standard deployment inference is token-metered; retained benchmark storage still incurs storage/transaction charges. The complete verified main-run archive is preserved privately. Storage public network access, anonymous Blob access and shared-key access remain disabled.

## Documentation Basis

[Microsoft's latency guidance](https://learn.microsoft.com/azure/foundry/openai/how-to/latency) distinguishes complete response time, first response and normalized token-generation metrics, and identifies model, prompt size, generated tokens and load as influencing factors. Client timers and API-gateway metrics have different boundaries.

[Deployment-type documentation](https://learn.microsoft.com/azure/foundry/foundry-models/concepts/deployment-types) describes dynamic global routing and data-zone-constrained routing. It does not promise that EU is always faster or that Global always processes in the United States. [Responses documentation](https://learn.microsoft.com/azure/foundry/openai/how-to/responses) and [reasoning guidance](https://learn.microsoft.com/azure/foundry/openai/how-to/reasoning) provide configuration context; this experiment does not attribute the observed API difference to an undocumented implementation mechanism.
