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

The 30-symbol freshness microbenchmark is not the selected-universe benchmark. The configured-workload benchmark below counts scanner kline and optional reads and times a local freshness plus 2d resample stage. It does not call the network and does not claim five-minute headroom.

Cost projection uses 288 cycles per day only as a what-if for a 300-second cadence, and a 30-day month. Request counts are not billing credits. The code-default 60-second interval would be 1440 cycles per day if that default were the Runtime setting. Runtime cadence is unverified.

Configured-universe local benchmark:

```json
{
  "label": "synthetic_workload",
  "live_network": "not_executed",
  "scanner_invoked": false,
  "symbols": 2,
  "configured_requests": 22,
  "local_stage_passes": 2,
  "five_minute_headroom": "not_measured"
}
```
