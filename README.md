# Foundry Latency Benchmark

**EU Data Zone Standard and Global Standard show workload-dependent latency differences.** This benchmark measures completion time, time to first visible content and response-time variability across regions, APIs and load conditions.

**[Explore the interactive report](https://faustinopalma.github.io/foundry-latency-benchmark/)** or open [the local HTML](preview.html) without a server.

The September 30 - October 1, 2026 campaign recorded **22,400 workflows: 22,388 passed exact synthetic-output validation and 12 had technical failures**, across 224 conditions with 100 attempts each.

## Test Scope

- GPT-5.4 and GPT-5.4 Mini, with deployment types compared within each model.
- Chat Completions and Responses, streaming and non-streaming, with clients and Foundry resources in Sweden Central and Italy North.
- Overnight and morning geographic comparisons; overnight input-length, output-length, concurrency and prompt-cache sweeps.

## Results And Methods

- [Complete campaign](CAMPAIGN.md): results, experimental design, measurement definitions and limitations.
- [Earlier benchmark](REPORT.md): the initial two-call workflow experiment.
- [Network experiment](NETWORK_REPORT.md): connection reuse, transport latency and measurement boundaries.

## Offline Checks

Requires Python 3.13. Tests use the standard library and make no Azure calls.

```sh
python -m unittest test_benchmark -v
```
