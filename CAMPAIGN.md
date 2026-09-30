# Regional and workload latency campaign

## Results

**The EU Data Zone versus Global Standard difference is workload-dependent.** At approximately 1,500 output tokens, the four within-model/API comparisons have close completion medians and paired mean-difference intervals that include zero. This does not establish equivalence, and first-content or tail latency can differ. Compare deployment types within a fixed model, API, workload, time window, runner region, resource region and delivery mode; no cross-model ranking is intended.

The [interactive report](https://faustinopalma.github.io/foundry-latency-benchmark/) contains the complete revised campaign: **22,400 attempted workflows, 22,388 exact correct outcomes, 12 technical failures and 224 conditions with 100 attempts each**. There were no observed incorrect-output, refusal or HTTP 429 calls in the measured campaign. Failed workflows remain in the denominator; correctness refers to the synthetic schema and expected values, not general reasoning quality. Earlier calibration and historical experiments are excluded.

| Window | Correct / attempted workflows | Technical-error calls | Conditions |
| --- | ---: | ---: | ---: |
| Overnight | 15,993 / 16,000 | 7 | 160 |
| Morning | 6,395 / 6,400 | 5 | 64 |
| Complete revised campaign | 22,388 / 22,400 | 12 | 224 |

### Long Outputs And Startup Delay

The output-length sweep uses Sweden Central runners and resources, streaming, approximately 8,000 input tokens, and 100 attempted single-call workflows per condition. At approximately 1,500 output tokens:

| Fixed model / API | EU workflow P50, s | Global workflow P50, s | Paired mean EU minus Global, ms | Exploratory 95% interval, ms |
| --- | ---: | ---: | ---: | --- |
| GPT-5.4 / Chat Completions | 12.004 | 11.827 | -735.0 | -1,715.6 to 37.7 |
| GPT-5.4 / Responses | 11.971 | 11.688 | 73.6 | -328.8 to 415.7 |
| GPT-5.4 Mini / Chat Completions | 16.674 | 16.914 | -133.8 | -431.2 to 160.6 |
| GPT-5.4 Mini / Responses | 14.328 | 14.389 | -71.0 | -409.4 to 270.1 |

These rows are separate deployment comparisons. Their P50 differences are not the paired mean differences used for the intervals. Every workflow in these eight conditions was correct.

For GPT-5.4 Chat Completions, median first-visible-content latency remains lower on EU Data Zone: 924.5 ms versus 1,293.5 ms at the longest output. The estimated visible-token interval medians are 7.27 versus 7.09 ms. A startup difference therefore represents a smaller fraction of a longer response; its absolute duration need not disappear. Workflow P95 remains different, at 13.741 versus 15.444 s. GPT-5.4 Mini was already close between deployment types at shorter outputs, so a large short-output gap is not universal.

Service-reported mean output counts match between deployment types: approximately 1,498.5 tokens for Chat Completions and 1,499.5 for Responses in the longest conditions. Positive cached tokens occurred in 98/100 Responses calls per deployment type for GPT-5.4, and 98/100 EU versus 100/100 Global calls for Mini; Chat Completions reported no cache hits. The sweep describes observed configurations and cache behavior, not an isolated hardware decoding comparison.

Global routing may introduce a different processing path, but the actual serving location and internal network contribution were not measured. Client first-content latency includes service waiting, prompt processing, buffering and network effects. The measurements support relative dilution of startup overhead; they do not identify physical distance as its cause.

## Completed Schedule

The revised schedule retained the overnight window and exactly one morning geographic window on 2026-10-01 at 07:30 Europe/Rome (05:30 UTC). The original pending windows were cancelled on both runners before execution. The replacement preserved 100 attempted workflows per condition, request protocol and admission pacing, with 3,200 measured workflows per runner. Both morning processes exited successfully. Four final exports cover the complete revised matrix; the five observations missing from the preliminary snapshot are included.

| Batch / window | Scheduled start (UTC) | Europe/Rome | Effective state |
| --- | --- | --- | --- |
| Original / window-0 | 2026-09-30 19:25:34.069812 | Sep 30, 21:25:34 | Completed on both clients |
| Original / window-1 | 2026-10-01 07:25:34.069812 | Oct 1, 09:25:34 | Cancelled before starting |
| Original / window-2 | 2026-10-01 19:25:34.069812 | Oct 1, 21:25:34 | Cancelled before starting |
| Replacement / window-1 | 2026-10-01 05:30:00 | Oct 1, 07:30:00 | Completed on both runners |

Sweden Central completed the overnight run, including the advanced sweeps, at 2026-10-01 04:42:43.842229 UTC; Italy North completed at 2026-09-30 20:42:08.282490 UTC. The morning runs completed at 06:47:01.063048 UTC and 06:46:55.482025 UTC, respectively. UTC timestamps are canonical; the displayed local schedule uses Europe/Rome.

The original schedulers exited with signal 15 at 05:06:41.920378 UTC and 05:06:46.567154 UTC after overnight completion. Their manifests and schedules remain immutable; additional verified schedule-revision records document cancellation and replacement. A nonzero scheduler exit here is distinct from a failed measured workflow. The replacement uses fresh process connections and warmups, so a temporal comparison cannot isolate time of day from restart effects. Request sources and campaign functions other than scheduling/main were unchanged from the successful pilot.

Actual window-0 start was `2026-09-30T19:25:34.070113+00:00` on the Sweden Central client and `2026-09-30T19:25:34.070083+00:00` on the Italy North client, corresponding to 0.301 ms and 0.271 ms of scheduler lateness. These are orchestration-start timestamps, not inference latency. Both runners' manifests and all three schedule records matched their private Blob copies, and executed source bytes matched their manifest hashes.

The corrected pilot's measured interval was 2026-09-30 18:59:02.691558 to 19:19:33.361973 UTC. Source hashes, actual submitted requests, exact planned case coverage, UTC intervals, output correctness and observed cache behavior passed the original launch gate: 352/352 measured workflows were correct, with zero technical errors and 650 evidence files verified against Blob storage. Both pilot batches' containers were stopped after verified exports and subsequently removed with the campaign execution infrastructure.

The initial calibration completed 496 measured workflows on 2026-09-30 from 18:34:57 to 18:54:54 UTC: 464 were correct and 32 failed exact output-note validation. These failures and both original pilot archives remain preserved:

| First-pilot group | Correct / attempted workflows | Observed limitation |
| --- | ---: | --- |
| Geographic matrix | 128 / 128 | Two attempts per condition; insufficient for latency conclusions |
| Context sweeps | 48 / 48 | All three input sizes executed |
| Generation, approximately 100 tokens | 16 / 16 | Requested size achieved |
| Generation, approximately 500 and 1,500 tokens | 0 / 32 | HTTP 200 and correct business fields, but wrong audit-note length/content |
| Concurrency sweeps | 240 / 240 | Includes 128 workflows at concurrency 16; no HTTP 429 observed |
| Cache treatments | 32 / 32 | Positive cached tokens in 14/16 shared-prefix calls and 0/16 unique-prefix calls |

The unsuccessful output-length calibration remains preserved under protocol `prompt-note-v1`. Protocol `schema-constrained-note-v2` constrains only the synthetic `validation_note` to a single-value string enum. Business selection, arithmetic and routing still require model computation and exact validation. The added schema tokens are offset by reducing neutral message padding. This measures schema-constrained synthetic output, not unconstrained prose generation; constrained decoding can affect performance. Analyses refuse to pool these two protocols. The second pilot covers geography, 32K context, all output lengths, concurrency 16 and both cache treatments.

The resource regions are Sweden Central and Italy North. West Europe rejected creation of a new Foundry resource with `RequestDisallowedByAzure` (location ineligible). Capacity is 100,000 TPM per deployment, reflecting the shared EU-area quota allocation for the requested models. The four preexisting benchmark deployments are unchanged.

## Experimental Design

| Axis | Conditions |
| --- | --- |
| Client region | Sweden Central; Italy North |
| Foundry resource region | Sweden Central; Italy North |
| Model version | GPT-5.4 `2026-03-05`; GPT-5.4 Mini `2026-03-17` |
| Deployment type | Global Standard; Data Zone Standard EU |
| Capacity | 100,000 TPM per deployment, dynamic quota disabled |
| API | Chat Completions; Responses |
| Geographic baseline | Streaming and nonstreaming, two sequential calls, approximately 1,500 input tokens and 100 output tokens per call |
| Temporal coverage | Overnight baseline plus one morning baseline; the original three-window, 24-hour plan was superseded |
| Context sweep | Approximately 1,500; 8,000; 32,000 input tokens, approximately 100 output tokens |
| Generation sweep | Approximately 100; 500; 1,500 output tokens, approximately 8,000 input tokens |
| Concurrency sweep | 1; 4; 8; 16 clients per deployment, synchronized batches |
| Cache treatment | Unique prefix versus shared prefix, approximately 8,000 input tokens |
| Full-campaign sample size | 100 attempted workflows per condition; warmups stored separately |

The geographic baseline runs on both client regions against all eight deployments in every window. The context, generation, concurrency and cache sweeps run in the first window on the Sweden Central client against the four Sweden Central deployments, using both APIs with streaming. Advanced sweeps start only after both regional clients have finished the geographic phase. This avoids overlapping the load experiment with regional comparisons. It is a controlled set of sweeps, not the full Cartesian product of every axis.

The revised campaign executed 22,400 measured workflows and 35,193 model calls, excluding warmups: 16,000 overnight workflows plus 6,400 in the replacement window. Seven first-stage failures skipped their dependent second call; unsuccessful workflows remain in the attempted denominator. Cancelled windows contribute neither observations nor technical failures. The pilot uses two attempts for sequential conditions and at least one full batch at each requested concurrency level. The workload is synthetic and does not include a production application or retrieval pipeline.

Global Standard can route processing worldwide; Data Zone Standard confines processing to its data zone. Changing the Foundry resource region does not prove that model processing moved to that region. Client regions and resource regions are recorded separately.

## Measurement and Evidence

- Calls and workflows record `started_utc` and `ended_utc` as timezone-aware ISO 8601 timestamps. UTC is the canonical timezone.
- Each window records its scheduled start, actual start, actual end, schedule lateness and completion state. Future windows remain scheduled until executed.
- Request latency uses `time.perf_counter`; preparation, batch synchronization and admission pacing are reported separately. Two-stage workflow duration includes local orchestration.
- Each concurrent worker owns its own SDK client and managed-identity credential. Connections are reused within each profile; SDK request retries are disabled. Authentication callbacks remain measured.
- The pilot and full campaign record actual API input, output, cached and reasoning token usage. Local message and schema token counts are recorded separately. Protocol v2 compensates additional schema tokens relative to the original schema, keeping the combined local budget stable across output sizes; API framing is still excluded. Calibrated compact expected outputs are approximately 94, 492 and 1,492 local tokens; service serialization can differ.
- A shared-prefix treatment is not classified as a cache hit unless the service returns positive `cached_tokens`.
- Streaming measurements observe nonempty text events. Events are not individual tokens; first-visible-content and estimated token intervals must retain their distinct definitions.
- First-visible-content latency is measured from client invocation to the first nonempty text event. Estimated visible-token interval divides the first-to-last content span by locally tokenized visible tokens minus one, and requires multiple content events. It is not a direct server time-between-tokens (TBT) measurement. Service time-to-first-token (TTFT) has a different observation boundary.
- API responses provide usage counts, including output, cached and reasoning tokens when available. Chat Completions uses `usage.completion_tokens`; Responses uses `usage.output_tokens`. The response does not supply these client timing measurements as ready-made TTFT/TBT values.
- HTTP success, output correctness, refusals, technical failures and rate-limit responses remain separate. Latency summaries use validated correct completions with explicit attempted denominators.
- Admission pacing is quota-aware and included in evidence. Synchronized bursts preserve the requested concurrency. This is a burst experiment, not an unbounded arrival-rate load test.
- Per-record Blob writes are immutable. Raw requests, expected answers, actual outputs, source hashes, dependency versions, deployment metadata and UTC timestamps remain in private evidence. Export compares local records byte-for-byte against Blob storage before producing a SHA-256-verified archive.

## Operations

### Final Publication

Final export compares the complete local JSON inventory with Blob Storage and verifies each record byte-for-byte. The transfer verifies individual blocks and the complete archive SHA-256. The public generator rechecks every receipt entry and its hash before applying an explicit field allowlist and aggregating observations. Final exports require recorded process status; intentionally terminated original schedulers additionally require a verified schedule-revision receipt.

```powershell
python build_preview.py evidence/campaign-20260930a-sc.zip evidence/campaign-20260930a-it.zip evidence/campaign-20261001m-sc.zip evidence/campaign-20261001m-it.zip --complete --template preview.template.html --output preview.html
```

`--complete` requires exactly four final exports and every case index from 0 through 99 in each of the 224 conditions of this revised two-window matrix. Missing cases, duplicate observations, extra conditions or partial snapshots fail the gate. This option encodes the published campaign's scope; it is not a generic completeness assertion for other designs. Original manifests retain cancelled windows; their older manifest-only analysis must not be used to infer additional missing failures.

The self-contained HTML contains aggregate statistics, actual denominators and archive hashes; raw observations, outputs and runtime configuration remain private. A fresh clone can inspect methods, aggregates and offline tests, but cannot independently recompute the published numbers without the private evidence. Percentile intervals use 1,000 bootstrap resamples; paired mean differences use 5,000. Timing summaries include validated correct observations, and paired comparisons require both matched workflows to be correct. Intervals are exploratory, without correction for multiple comparisons or temporal dependence.

The project-specific GitHub Pages workflow runs offline tests and uploads only this report as `index.html`. It publishes under `/foundry-latency-benchmark/`; it does not update the profile's main site. Intermediate snapshots remain private historical evidence and are not the publication data source.

### Campaign Commands

Campaign configuration is stored under the ignored `.private/` directory. Credentials stay in the ignored `.azure/` directory. Do not publish private configuration, raw evidence or resource identifiers. The scripts refuse run-ID reuse and do not overwrite historical evidence.

```powershell
./campaign_azure.ps1 -Operation Provision
./campaign_azure.ps1 -Operation Run -BatchId <unique-pilot-id> -Pilot
./campaign_azure.ps1 -Operation Status -BatchId <unique-pilot-id>
./campaign_azure.ps1 -Operation Collect -BatchId <unique-pilot-id> -Pilot
./campaign_azure.ps1 -Operation StopFinished -BatchId <verified-completed-batch-id>
./campaign_azure.ps1 -Operation Metrics -BatchId <unique-pilot-id> -StartUtc <start-UTC-ISO-8601> -EndUtc <end-UTC-ISO-8601>
./campaign_azure.ps1 -Operation Run -BatchId <unique-campaign-id> -StartUtc <future-UTC-ISO-8601> -PilotBatchId <verified-pilot-id> -PythonPath <python-executable>
```

The full-launch gate requires both regional pilot ZIPs, successful process exits, exact planned case coverage, at least one correct calibration per condition, zero technical errors, observed shared-prefix caching, no unique-prefix caching, valid UTC intervals and matching source hashes. Blob receipt inventories and content hashes are revalidated directly from the archives. The first protocol cannot authorize the full campaign. Raw HTTP request bodies are checked against their planned canonical hashes before launch.

`StopFinished` validates both batch archives and their complete Blob inventories before stopping either container. It requires successful process exits and completed summaries, verifies each receipt hash and container ownership, and records the observed stopped state. It does not delete storage, identities, deployments or networking. Failed or partial batches require a separate evidence-preserving review instead of this success-only shortcut.

Analyze the two verified exports together with `analyze_benchmark.py <first-results-root> --campaign --peer-root <second-results-root> --label <unique-analysis-label>`. Group summaries retain both region axes and each window's measured UTC interval. Per-call and workflow P50/P90/P95 intervals require at least 20 correct observations. Paired regional, deployment-type and API comparisons match case indexes within the same remaining conditions; bootstrap intervals are exploratory and do not correct temporal dependence or multiple comparisons.

Azure Monitor collection preserves the queried UTC interval, metric definitions and raw one-minute series by deployment for `AzureOpenAITTLTInMS`, `AzureOpenAITimeToResponse`, `AzureOpenAINormalizedTBTInMS`, `AzureOpenAINormalizedTTFTInMS`, `GeneratedTokens` and `ProcessedPromptTokens`. Metric availability does not guarantee populated data immediately after a call. Gateway/service metric boundaries differ from client first-visible-content and end-to-end duration; values must not be equated directly.

Start the full campaign only after the pilot evidence has been inspected. The containers perform scheduling independently of the workstation. A technical deadline bounds each window and the overall process; there is no spending cap. Storage stays private, while authenticated model inference uses public Foundry endpoints through NAT. No local-authentication keys or PTU reservations are introduced.

At 100,000 TPM per deployment, the first window's planned admission schedule is approximately 9.2 hours; inference and persistence can add time. The replacement contains only the geographic baseline, approximately 1.3 hours of planned admissions. Each window has a 10-hour technical deadline. The default scheduler retains three starts 12 hours apart; the replacement explicitly sets `first_window_index=1` and `window_count=1`. Actual lateness and partial completion remain explicit. No PTU experiment has been executed, and no further experiments are scheduled.

### Final Resource State

On October 1, 2026, both campaign execution resource groups were deleted after verification of all eight campaign and pilot archives, their complete receipt-backed inventories, exact resource ownership and network dependencies. Resource-group absence was checked after deletion. This removed eight containers, two NAT gateways, two public IP addresses, two Private Endpoints and their dedicated virtual networks, interfaces and private DNS zones. The earlier experiment's execution group was also confirmed absent. Original campaign exits of -15 were accepted only with the preserved cancellation receipts and the verified complete revised observation matrix.

Private Blob evidence and local ZIP archives were retained. Storage public network access, shared-key access and public Blob access remain disabled. The original Foundry resource, its deployment settings and the retained identity were verified unchanged. The two campaign Foundry resources retain only Global Standard and Data Zone Standard deployments, without provisioned-throughput reservations. No further inference is scheduled. Storage capacity and applicable transactions remain chargeable; this is not a zero-cost retention claim. Future access to private Blob data requires an authorized private network path, since the temporary endpoints were removed.

Container provisioning success is not evidence that a test ran. Use immutable window records and the final process/summary records to determine actual progress. Finished containers keep a loopback export server alive and continue to incur compute charges until explicitly stopped or removed after evidence collection. NAT gateways, public IPs and private endpoints also incur charges while retained.

The historical cleanup script has checks specific to the earlier network experiment and must not be used as a campaign cleanup gate. Campaign cleanup must first verify both exports, completion/partial-state records, exact ownership tags and unchanged original deployment settings. Only the campaign resource groups and the campaign-specific role assignments/private connections are eligible; the original Foundry resource, retained identity and evidence storage remain excluded.

## Sources

- [Foundry deployment types](https://learn.microsoft.com/azure/foundry/foundry-models/concepts/deployment-types)
- [Azure OpenAI latency](https://learn.microsoft.com/azure/foundry/openai/how-to/latency)
- [Azure region access restrictions](https://learn.microsoft.com/azure/azure-resource-manager/troubleshooting/error-region-access-policy)
- [ACI virtual network requirements](https://learn.microsoft.com/azure/container-instances/container-instances-virtual-network-concepts)
