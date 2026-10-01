# Network Latency Measurements

**Connection reuse avoided about 204 ms of setup on the Windows PC and 16 ms on the Azure runner.** During Azure inference, the observable TCP round-trip time (RTT) averaged 1.72 ms, compared with 1,683 ms for a complete call. The RTT covers the client-to-peer path; internal service routing and processing are outside that measurement.

## Lightweight Requests

Each location ran 60 new-connection and 60 reused-connection requests to `GET /openai/v1/models` without credentials or inference. All 240 returned the expected HTTP 401. New/reused order was shuffled within each pair; warmups are excluded.

Values are milliseconds. Setup is the per-request sum of DNS, TCP and TLS durations.

| Measurement | PC mean | PC P50 | PC P90 | Azure mean | Azure P50 | Azure P90 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| DNS resolution, new connection | 7.52 | 0.89 | 12.63 | 4.67 | 2.50 | 11.19 |
| TCP connect, new connection | 67.27 | 61.61 | 84.56 | 1.93 | 1.51 | 2.36 |
| TLS handshake, new connection | 129.12 | 122.71 | 153.03 | 9.40 | 7.63 | 14.35 |
| Combined DNS + TCP + TLS setup | 203.90 | 193.50 | 247.00 | 16.01 | 14.10 | 23.98 |
| Complete HTTP request, new connection | 272.50 | 267.16 | 322.24 | 17.82 | 16.02 | 26.06 |
| Complete HTTP request, reused connection | 69.11 | 60.44 | 95.66 | 1.93 | 1.42 | 2.87 |

Paired new-minus-reused request duration was +203.39 ms on the PC (95% bootstrap interval +191.59 to +215.66 ms) and +15.89 ms on Azure (+14.38 to +17.49 ms), with 60 pairs each. Complete HTTP duration includes gateway and client overhead as well as transport.

## Inference

Azure ran 320 sequential single-call attempts: 20 per combination of deployment, API and connection mode. **315 outputs were exactly correct; five had valid JSON but incorrect business fields.** All calls returned HTTP 200, with no transport errors. All 160 reused-mode calls reused their sockets; new connections averaged 16.28 ms of setup.

Mean call durations below include all 20 attempts per cell, including incorrect answers. RTT is sampled from the socket after each response.

| Model | Deployment | API | New correct/attempted | Reused correct/attempted | New mean, ms | Reused mean, ms | New RTT mean, ms | Reused RTT mean, ms |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| GPT-5.4 | EU Data Zone Standard | Chat Completions | 20/20 | 20/20 | 1936.41 | 1877.51 | 1.65 | 2.09 |
| GPT-5.4 | EU Data Zone Standard | Responses | 20/20 | 20/20 | 2273.16 | 2139.32 | 1.64 | 2.06 |
| GPT-5.4 | Global Standard | Chat Completions | 20/20 | 20/20 | 2447.98 | 2129.04 | 1.97 | 2.27 |
| GPT-5.4 | Global Standard | Responses | 20/20 | 20/20 | 2043.58 | 1944.40 | 2.04 | 1.56 |
| GPT-5.4 Mini | EU Data Zone Standard | Chat Completions | 20/20 | 20/20 | 1019.61 | 996.14 | 1.62 | 1.56 |
| GPT-5.4 Mini | EU Data Zone Standard | Responses | 19/20 | 19/20 | 1415.88 | 1086.63 | 1.50 | 0.92 |
| GPT-5.4 Mini | Global Standard | Chat Completions | 20/20 | 19/20 | 1322.56 | 1352.15 | 1.48 | 1.20 |
| GPT-5.4 Mini | Global Standard | Responses | 19/20 | 19/20 | 1531.55 | 1409.64 | 1.50 | 2.47 |

Inference-duration differences were noisier than setup measurements: all five conditions with 20 correct pairs had exploratory 95% intervals spanning zero; the other three had 19 pairs, below the interval threshold.

## Method

- September 30, 2026: Windows 11 ARM64 PC at approximately 15:30-15:31 UTC; Linux x86-64 Azure container in Sweden Central at 15:37-15:46 UTC. Location comparisons also differ in time, OS and host.
- Python 3.13, HTTP/1.1, certificate-validated TLS 1.3, no TLS session resumption. DNS/TCP/TLS setup is instrumented separately; DNS includes resolver caching.
- Linux `TCP_INFO.tcpi_rtt` provides a smoothed acknowledgment-based RTT estimate. Windows TCP connect time is only an approximation to RTT.
- Inference uses Managed Identity, GPT-5.4 `2026-03-05` and GPT-5.4 Mini `2026-03-17`, non-streaming strict JSON, reasoning effort `none`, `store=false` and no retries. Input/output usage is 1,501-1,505 / 100-107 tokens per call.
- Call timing includes connection setup and response-body reading. Token acquisition, serialization, validation and persistence are outside the timer. Non-streaming calls provide no first-content or inter-token timing.

These single-call measurements are analyzed independently of workflow results. References: [Python TLS sockets](https://docs.python.org/3.13/library/ssl.html), [Linux TCP_INFO](https://man7.org/linux/man-pages/man7/tcp.7.html), [metric definitions](CAMPAIGN.md#measurement).
