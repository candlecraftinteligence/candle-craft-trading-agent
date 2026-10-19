# J. Cadence and cost

The watch scheduler is fixed start-to-start. The CLI default interval is 60.0 seconds, so a five-minute cycle is not the code default. A 300-second sleep-after-completion story does not describe `schedule_after_iteration`.

```json
{
  "scheduler": "fixed_start_to_start",
  "cli_default_interval_seconds": 60.0,
  "five_minute_interval_is_code_default": false,
  "example_duration_seconds": 12.0,
  "example_sleep_seconds": 48.0,
  "overrun_missed_intervals": 1,
  "overrun_next_is_not_finish_plus_interval": true,
  "runtime_cadence": "unverified"
}
```

The offline microbenchmark times freshness checks only. It does not claim Runtime capacity.

Cost projection uses 288 cycles per day only as a what-if for a 300-second cadence, and a 30-day month. Request counts are not billing credits. The code-default 60-second interval would be 1440 cycles per day if that default were the Runtime setting. Runtime cadence is unverified.

Attached request accounting:

```json
{
  "http_attempts": 7,
  "by_provider": {
    "cmc": 2,
    "coinpaprika": 1,
    "binance": 3,
    "bybit": 1,
    "coinglass": 0
  },
  "bytes_read": 3224415,
  "rounds": 1,
  "lowered_reason": "spare shared-IP capacity was not established; one round only",
  "binance_used_weight_1m_header_observations": [
    "19",
    "59",
    "60"
  ],
  "weight_header_meaning": "cumulative IP counter during the calls, not an isolated bill for this process"
}
```

Offline freshness microbenchmark:

```json
{
  "label": "dev_measured_microbenchmark",
  "symbols": 30,
  "repeats": 3,
  "cold_seconds": 0.0029178999830037355,
  "warm_seconds": [
    0.0025465000071562827,
    0.004376799974124879
  ],
  "statuses": [
    "pass"
  ],
  "scanner_invoked": false,
  "runtime_capacity": "unverified"
}
```

Seven live calls had latencies [383, 332, 350, 491, 221, 255, 273] ms. The descriptive mean is 329.3 ms. That mean is not a percentile and not a full-universe cycle time. A five-minute headroom claim for the selected universe was not measured.
