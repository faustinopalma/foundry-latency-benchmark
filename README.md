# Foundry Latency Benchmark

Latency measurements for EU Data Zone Standard versus Global Standard, compared separately for GPT-5.4 and GPT-5.4 Mini.

**[Explore the interactive report](https://faustinopalma.github.io/foundry-latency-benchmark/)** or open [the local HTML](preview.html) without a server.

## Results And Methods

- [Regional and workload results](CAMPAIGN.md): geography, input/output length, concurrency and caching.
- [Two-call workflows](REPORT.md): completion times and exact-output validation.
- [Network measurements](NETWORK_REPORT.md): connection reuse and transport latency.

## Offline Checks

Requires Python 3.13. Tests use the standard library and make no Azure calls.

```sh
python -m unittest test_benchmark -v
```
