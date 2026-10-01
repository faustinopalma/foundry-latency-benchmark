# Two-Call Workflow Results

**EU Data Zone Standard had lower mean completion times in three of the four non-streaming model/API comparisons.** The Mini Responses comparison had no clear difference within its exploratory interval.

This dataset contains 960 attempted workflows across 16 streaming/non-streaming conditions, with 60 attempts each: **957 correct workflows, three incorrect first-call outputs and zero HTTP/SDK errors**. All 1,917 executed calls returned HTTP 200; the three dependent second calls were skipped after failed validation.

## Completion Times

Each workflow consists of two dependent calls. The table covers non-streaming conditions; durations are seconds and include only correct workflows. The denominator includes every attempt.

| Model | Deployment | API | Correct/attempted | Mean | P50 | P90 |
| --- | --- | --- | ---: | ---: | ---: | ---: |
| GPT-5.4 | EU Data Zone Standard | Chat Completions | 60/60 | 3.393 | 3.367 | 4.122 |
| GPT-5.4 | Global Standard | Chat Completions | 60/60 | 4.242 | 4.094 | 5.118 |
| GPT-5.4 | EU Data Zone Standard | Responses | 60/60 | 3.937 | 3.731 | 4.994 |
| GPT-5.4 | Global Standard | Responses | 60/60 | 4.457 | 4.135 | 5.390 |
| GPT-5.4 Mini | EU Data Zone Standard | Chat Completions | 59/60 | 2.188 | 2.426 | 2.695 |
| GPT-5.4 Mini | Global Standard | Chat Completions | 60/60 | 3.182 | 3.225 | 4.066 |
| GPT-5.4 Mini | EU Data Zone Standard | Responses | 59/60 | 3.318 | 2.748 | 4.676 |
| GPT-5.4 Mini | Global Standard | Responses | 60/60 | 3.212 | 2.918 | 4.555 |

The following comparisons match the same case across deployment types, retaining pairs where both workflows were correct. Negative values favor EU Data Zone Standard.

| Model / API | Correct pairs | Mean EU minus Global, ms | Exploratory 95% interval, ms |
| --- | ---: | ---: | --- |
| GPT-5.4 / Chat Completions | 60 | -849 | -1225 to -535 |
| GPT-5.4 / Responses | 60 | -519 | -1012 to -107 |
| GPT-5.4 Mini / Chat Completions | 59 | -997 | -1221 to -775 |
| GPT-5.4 Mini / Responses | 59 | +106 | -251 to +498 |

## Method

- September 30, 2026, 11:59-12:16 UTC, including warmup; Linux container in Sweden Central, 1 vCPU / 2 GiB. Four workers, sequential within each deployment, with condition order shuffled by case.
- GPT-5.4 `2026-03-05` and GPT-5.4 Mini `2026-03-17`; Managed Identity, reasoning effort `none`, `store=false`, strict structured output and disabled SDK retries.
- Approximately 1,501-1,505 input and 100-101 output tokens per call; zero reported cached or reasoning tokens. A deterministic audit note controls output length, while selection, arithmetic and routing are validated against expected values.
- Workflow timing includes local preparation and validation; storage writes occur afterward. See [metric definitions](CAMPAIGN.md#measurement) for percentiles and streaming timing boundaries.

The three incorrect outputs had valid JSON and schema but wrong business fields. Bootstrap intervals are case-matched rather than simultaneous and do not correct temporal dependence or multiple comparisons. This dataset is analyzed independently of the regional and workload matrix.
