# Foundry Network Latency Measurements

## Conclusion

The observable client-to-Foundry transport was small compared with model-call duration from Azure Sweden Central: the Linux TCP round-trip time (RTT) estimate averaged 1.72 ms across 320 inference calls, while a complete call averaged 1,683 ms. A new connection added approximately 16 ms of measured DNS, TCP and TLS setup. These measurements do not expose internal gateway-to-model routing, queueing, safety processing or generation, so the remaining time cannot be labeled pure model compute.

From the local Windows PC, an unauthenticated lightweight request to the same hostname averaged 69.11 ms on an existing connection and 272.50 ms on a new connection. Reusing the connection saved 203.39 ms on average. From Azure, the corresponding request took 1.93 ms and 17.82 ms. The local TCP handshake averaged 67.27 ms, providing a rough RTT-scale estimate; a kernel RTT measurement was available only on Linux.

All 240 measured lightweight requests returned the expected HTTP 401. All 320 measured inference calls returned HTTP 200 with no observed transport errors; 315 answers were exactly correct and five contained incorrect field values. The 451 Azure evidence files matched their private Blob copies and were exported before the temporary execution resource group was deleted. The nine existing model deployments and checked security settings were preserved.

## Lightweight Requests

Each location ran 60 new-connection requests and 60 requests on an already-open connection. The request was `GET /openai/v1/models` without credentials, deliberately returning a small HTTP 401 response without model inference. Every reused request genuinely reused its socket. Both clients reached the same TCP peer; its address is omitted from this anonymized report. All observed new connections negotiated TLS 1.3 without TLS session resumption. Each location also made one excluded warm-up request.

All values below are milliseconds. Each row has 60 observations. Setup is the per-request sum of DNS, TCP and TLS durations; its percentiles are computed from those sums.

| Measurement | PC mean | PC P50 | PC P90 | Azure mean | Azure P50 | Azure P90 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| DNS resolution, new connection | 7.52 | 0.89 | 12.63 | 4.67 | 2.50 | 11.19 |
| TCP connect, new connection | 67.27 | 61.61 | 84.56 | 1.93 | 1.51 | 2.36 |
| TLS handshake, new connection | 129.12 | 122.71 | 153.03 | 9.40 | 7.63 | 14.35 |
| Combined DNS + TCP + TLS setup | 203.90 | 193.50 | 247.00 | 16.01 | 14.10 | 23.98 |
| Complete HTTP request, new connection | 272.50 | 267.16 | 322.24 | 17.82 | 16.02 | 26.06 |
| Complete HTTP request, reused connection | 69.11 | 60.44 | 95.66 | 1.93 | 1.42 | 2.87 |

The complete reused HTTP request includes network transit, gateway processing and client overhead. It is not a pure RTT measurement. On Azure's reused probe connection, the kernel RTT estimate averaged 1.74 ms (P50 1.65, P90 2.07). On new probe connections, the pre-TLS kernel estimate averaged 1.90 ms, close to the independently timed 1.93 ms TCP connect.

Within each sample pair, new/reused order was shuffled. Paired new-minus-reused request duration was +203.39 ms on the PC (60 pairs; exploratory 95% bootstrap interval +191.59 to +215.66 ms) and +15.89 ms on Azure (60 pairs; +14.38 to +17.49 ms). These intervals describe this short measurement window, without correcting temporal dependence. The PC and Azure windows were different, so their difference is an observational comparison rather than a simultaneous controlled location experiment.

## Inference Cross-Check

Azure also ran 20 attempts for each combination of four deployments, two APIs and two connection modes: 320 independent single calls, plus eight excluded warm-up calls. These calls use stage 1 of the original synthetic task, not its two-call workflow. Calls were sequential with shuffled conditions in each case block; the original benchmark used four workers. Absolute durations from the two campaigns therefore have different experimental contexts.

Across the 320 measured calls, the kernel RTT estimate was mean 1.72 ms, P50 1.41 ms, P90 2.87 ms and P95 3.81 ms. All 160 reused-mode calls genuinely reused their sockets. The 160 new-mode calls had mean setup 16.28 ms (P50 13.69; P90 24.41), consisting of DNS 6.25 ms, TCP 2.11 ms and TLS 7.93 ms on average.

The table reports mean complete-call time for all 20 attempts in each cell, including incorrect answers. Correct/attempted counts explicitly distinguish answer validity from HTTP success. RTT columns are kernel estimates sampled after each response, not the request's header wait.

| Model | Deployment | API | New correct/attempted | Reused correct/attempted | New mean, ms | Reused mean, ms | New RTT mean, ms | Reused RTT mean, ms |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| GPT-5.4 | EU DataZoneStandard | Chat Completions | 20/20 | 20/20 | 1936.41 | 1877.51 | 1.65 | 2.09 |
| GPT-5.4 | EU DataZoneStandard | Responses | 20/20 | 20/20 | 2273.16 | 2139.32 | 1.64 | 2.06 |
| GPT-5.4 | GlobalStandard | Chat Completions | 20/20 | 20/20 | 2447.98 | 2129.04 | 1.97 | 2.27 |
| GPT-5.4 | GlobalStandard | Responses | 20/20 | 20/20 | 2043.58 | 1944.40 | 2.04 | 1.56 |
| GPT-5.4 Mini | EU DataZoneStandard | Chat Completions | 20/20 | 20/20 | 1019.61 | 996.14 | 1.62 | 1.56 |
| GPT-5.4 Mini | EU DataZoneStandard | Responses | 19/20 | 19/20 | 1415.88 | 1086.63 | 1.50 | 0.92 |
| GPT-5.4 Mini | GlobalStandard | Chat Completions | 20/20 | 19/20 | 1322.56 | 1352.15 | 1.48 | 1.20 |
| GPT-5.4 Mini | GlobalStandard | Responses | 19/20 | 19/20 | 1531.55 | 1409.64 | 1.50 | 2.47 |

End-to-end inference differences between new and reused connections were much noisier than the directly measured setup durations. All five condition comparisons with 20 correct pairs had exploratory 95% intervals spanning zero. The remaining three had 19 correct pairs, below the analyzer's preexisting 20-pair interval threshold. The inference-duration differences must not be interpreted as measurements of connection setup or an established per-condition speedup.

The five incorrect answers all occurred on Mini, case 7, in the conditions with 19/20 correct above. Their JSON and schema were valid but their field values did not match the deterministic expected answer. HTTP success is counted separately. All eight warm-up model answers were correct and are excluded from measured statistics.

## Interpretation

- Connection reuse has a directly observed benefit: it avoids approximately 204 ms of setup on this PC and 16 ms on this Azure runner. The similar paired lightweight-request differences independently support this result.
- For two sequential small request/response exchanges over an existing connection, twice the lightweight baseline is approximately 138 ms from the PC and 3.9 ms from Azure. This is only an order-of-magnitude illustration: the probe has no inference payload or generation and cannot be subtracted as an exact correction from an actual workflow.
- Subsecond differences between EU/Global or Chat/Responses in the original Azure experiment cannot reasonably be explained solely by the approximately 1-2 ms observable access-path RTT found here. This follow-up sampled a later period and does not prove the earlier route was identical. Internal routing, backend load and service processing remain unseparated.
- A small RTT to the public gateway does not locate the model execution. GlobalStandard and DataZoneStandard use the same resource hostname in this experiment; their backend paths may differ beyond the observed TCP peer.
- Header wait averaged 1,674.52 ms across inference calls. It includes remote processing and network effects. The 0.033 ms mean body-read time mostly reflects data already buffered when headers were parsed; it does not establish that transferring the response over the network took 0.033 ms.
- These non-streaming calls measure neither time to first token (TTFT) nor time between tokens (TBT). No exact model-compute latency or service-side authentication comparison is inferred.

## Measurement Boundaries

The client uses Python's `http.client.HTTPSConnection` with explicitly instrumented DNS lookup, TCP connect and certificate-validated TLS handshake. It negotiates HTTP/1.1 and uses direct sockets; environment HTTP proxy settings are not used. VPN routing and transparent network intermediaries, if present, are not bypassed by this setting. DNS includes operating-system/resolver caching; no cache flush was attempted. TCP connect includes host scheduling; TLS includes cryptographic work, certificate validation and protocol exchanges.

Linux `TCP_INFO.tcpi_rtt` is read from the live socket and converted from microseconds to milliseconds. It is a smoothed TCP acknowledgment-based RTT estimate to the peer, not an independent fresh RTT sample for each response and not a measure of the peer-to-model network. Windows has no value through this Linux interface; unavailable measurements remain null rather than zero. TCP setup timing on Windows supplies only an approximation.

The complete-call timer starts immediately before the HTTP request and ends after reading the response body. It includes connection setup when needed. Token acquisition, request construction/serialization, answer validation and evidence storage are outside this timer. The timing decomposition was checked for every measured Azure request. Managed Identity token retrieval averaged 0.0513 ms across measured inference calls after warm-up; server-side validation was not isolated. Retry count was zero.

GPT-5.4 version 2026-03-05 and GPT-5.4 Mini version 2026-03-17 retained their GlobalStandard/DataZoneStandard deployments at capacity 100. Requests used reasoning effort none, non-streaming output, store=false, low verbosity, strict JSON and a 512 output-token cap. Input usage ranged from 1,501 to 1,505 tokens and output from 100 to 107. Measured totals were 480,939 input tokens and 32,310 output tokens, with zero reported cached or reasoning tokens. Each case has deterministic input data with a varying request marker to reduce caching; paired payloads are case-equivalent, not byte-identical.

## Evidence And Cleanup

Raw datasets, manifests, cloud receipts, runtime configuration and exact executed sources are retained privately. They are excluded from this repository because they contain environment identifiers. The tables publish aggregate measurements; the included code supports collecting new evidence, but a fresh clone cannot independently recompute the historical results.

- PC run: network-local-20260930a, 30 September 2026, approximately 15:30:42-15:31:03 UTC; Windows 11 ARM64, Python 3.13.13, OpenSSL 3.0.19.
- Azure run: network-20260930a, approximately 15:37:15-15:46:38 UTC; Linux x86-64 ACI, Sweden Central, 1 vCPU/2 GiB, Python 3.13.15, OpenSSL 3.0.20. The two vantage points therefore also differ in OS, host and TLS implementation version.
- Foundry inference used its public endpoint via the runner's NAT gateway. The private endpoint was for evidence storage only. This is not a Foundry Private Link versus public-path comparison.
- The private derived analysis contains distributions, pair counts, confidence intervals, usage and the five explicit incorrect outputs. The archived executed source matches the hash recorded before execution. Public-source sanitization later changed the identifying User-Agent string without modifying measurement logic.
- Cloud verification covered 451 files, consisting of 120 measured probes, 320 measured model calls, eight model warm-ups, one probe warm-up, one manifest and one completion record. Local raw files matched the private cloud inventory and contents. The recorded process exit code was zero.
- Private verified archive SHA256: `cf475d09fe78ad4199e18c6c7dd0acdcccc337a73242b9525f5e0f553adec730`. Individual transfer chunks, archive contents and raw-file receipt hashes were verified. The PC evidence remains a local dataset, without a separate cloud-copy verification claim. The hash identifies the private archive; it is not a substitute for access to its contents.
- The recreated execution resource group was deleted after export and preservation checks. A fresh read confirmed absence. The nine baseline deployments retained model versions, capacities, SKUs and RAI policies. Foundry retained disableLocalAuth=true and its public-network setting; storage retained publicNetworkAccess=Disabled, allowBlobPublicAccess=false and allowSharedKeyAccess=false. The final-state receipt and resource ledger remain private.
- The four benchmark model deployments and private evidence storage remain. With the execution group's private endpoint removed, a new authorized private path is required to access the cloud evidence; the verified export is available locally. Standard models have token-based inference charges and retained storage has storage/transaction charges; no exact invoice amount was calculated.

## Reproduce Analysis

These commands require separately collected local evidence in the expected run folders under `evidence/`; the historical datasets are not distributed. They make no inference or provisioning calls. Derived output paths are write-once; choose a fresh filename when rerunning. Use `analyze_network.py --help` to supply your own run IDs and analysis output path.

```powershell
& '.\.venv\Scripts\python.exe' -m unittest test_benchmark -v
& '.\.venv\Scripts\python.exe' analyze_network.py --output evidence/network-analysis-review.json
```

References: [Python TLS sockets](https://docs.python.org/3.13/library/ssl.html), [Linux TCP_INFO socket option](https://man7.org/linux/man-pages/man7/tcp.7.html), [Microsoft Foundry latency guidance](https://learn.microsoft.com/azure/foundry/openai/how-to/latency). The original two-call experiment is documented separately in [REPORT.md](REPORT.md).