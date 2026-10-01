# Regional And Workload Results

**EU Data Zone Standard and Global Standard show workload-dependent differences in completion time, first-content latency and tail latency.** The [interactive report](https://faustinopalma.github.io/foundry-latency-benchmark/) provides the full results, with deployment types compared within each model and test condition.

## Results

The September 30 - October 1, 2026 measurements comprise 35,193 model calls across 224 conditions, with 100 attempted workflows per condition. Correctness requires the exact expected synthetic output. Timing summaries use correct workflows; failures remain in the attempted denominator.

| Window | Correct / attempted workflows | Technical-error calls | Conditions |
| --- | ---: | ---: | ---: |
| Overnight | 15,993 / 16,000 | 7 | 160 |
| Morning | 6,395 / 6,400 | 5 | 64 |
| Total | 22,388 / 22,400 | 12 | 224 |

No incorrect outputs, refusals or HTTP 429 responses were observed. Seven first-call failures prevented their dependent second calls. Warmups are excluded.

## Test Design

| Dimension | Conditions |
| --- | --- |
| Models | GPT-5.4 `2026-03-05`; GPT-5.4 Mini `2026-03-17` |
| Deployment types | EU Data Zone Standard; Global Standard |
| Client and resource regions | Sweden Central; Italy North, varied independently |
| APIs | Chat Completions; Responses |
| Geographic baseline | Streaming and non-streaming; two dependent calls; approximately 1,500 input / 100 output tokens per call; overnight and morning |
| Input-length sweep | Approximately 1,500 / 8,000 / 32,000 input tokens; 100 output tokens |
| Output-length sweep | Approximately 100 / 500 / 1,500 output tokens; 8,000 input tokens |
| Concurrency sweep | 1 / 4 / 8 / 16 simultaneous clients per deployment |
| Cache treatment | Shared versus unique prefix; approximately 8,000 input tokens |

The workload sweeps use Sweden Central clients and resources, streaming, and the overnight window. They run after the geographic baseline, without overlapping its load. The matrix varies selected dimensions rather than every possible combination.

Requests use Managed Identity, reused connections, disabled SDK retries and quota-aware admission pacing at 100,000 tokens per minute per deployment. The synthetic task validates selection, arithmetic and routing. Protocol `schema-constrained-note-v2` fixes an audit-note field through a single-value schema enum to control output length; these are constrained outputs.

## Measurement

- **Completion time:** client-observed request duration; two-call workflow duration also includes local preparation and validation.
- **First-content latency:** invocation to the first nonempty streamed text event. Service time to first token (TTFT) uses a different observation boundary.
- **Estimated visible-token interval:** first-to-last content duration divided by locally counted visible tokens minus one. Stream events can contain multiple tokens, so this is not directly measured service time between tokens (TBT).
- **P50 / P90 / P95:** the 50th, 90th and 95th percentiles; P50 is the median and the higher percentiles describe slower responses.
- **Cache hit:** positive service-reported `cached_tokens`, independently of the requested prefix treatment.

Percentile intervals use 1,000 bootstrap resamples. Case-matched mean differences use 5,000 resamples and require both workflows to be correct. The 95% intervals are exploratory, without correction for temporal dependence or multiple comparisons.

## Scope

The two windows cover an overnight run and one morning run, with fresh processes and warmups in the morning. Concurrency is measured in synchronized bursts. Results describe these synthetic workloads and observation windows; processing location inside the service and general answer quality were not measured. The repository contains aggregate results; recomputation requires the underlying observations.

References: [deployment types](https://learn.microsoft.com/azure/foundry/foundry-models/concepts/deployment-types), [latency metrics](https://learn.microsoft.com/azure/foundry/openai/how-to/latency).
