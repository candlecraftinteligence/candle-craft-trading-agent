# Resource and phase observations

## Verified DEV gap

The supervisor's earlier scanner statistics and cycle duration do not attribute
all lifecycle, owner continuation, storage and reporting work. The current
receipt's duration reaches presentation but excludes its own persistence. The
historical 135-second scanner / 240-second full-cycle medians therefore do not
establish a SQLite bottleneck. Historical Runtime RAM/disk points are not current
capacity evidence. This correction adds observations, not a bottleneck claim.

## Timing contract

`PhaseProfile` has 19 fixed phase names, cumulative call counts and elapsed call
time using `time.perf_counter`. Clock implementation and resolution are retained.
Uncalled phases have N/A duration, active calls are IN_PROGRESS, and invalid,
failed or regressing clocks produce Unverified / N/A. Raised calls are counted
without replacing their original exceptions, including cancellation. A returned
call does not imply successful business processing. Nested boundaries overlap;
never sum phase totals. Awaited I/O is included; these are not CPU times.

The observed boundaries cover resource sampling, iteration-start receipt,
universe/lifecycle priority, scanner, lifecycle, owner continuation, Telegram,
symbol health, portfolio selection, JSON output, the combined scan core,
watch-state persistence, primary scan storage, manifest, watch output, admin
report, console emission and failed-iteration recording. Console formatting,
queue bookkeeping, legacy alert handling and other work can remain outside the
individual boundaries while contributing to elapsed iteration time. Primary scan
storage includes payload construction and the existing storage call; it is not
isolated SQLite engine latency. Service startup/final cleanup and final receipt /
journal persistence are not represented as measured inside the final profile.

Scan JSON contains a snapshot before final JSON output; active core calls remain
IN_PROGRESS. SQLite watch payloads, manifests and watch output retain their own
explicit earlier boundaries. The current receipt and session journal contain the
final profile after manifest/report/presentation, before final observation
persistence. Earlier artifacts are not rewritten to make them appear later.
`watch_session_id` and `iteration_id` link these observations. Only one profile
exists per iteration; no per-symbol history or unbounded timing list is collected.

## Resource and operational-limit contract

One point per iteration records process RSS, physical memory and system-disk
availability. Windows physical memory uses GlobalMemoryStatusEx. A bounded
GetSystemDirectoryW lookup selects the system root; GetDriveTypeW must prove
DRIVE_FIXED before disk_usage is called. Remote, unknown, removable, RAM-disk and
malformed roots are not sampled. No configured database path or S: path is
inspected. Other platforms retain supported RSS with system memory/disk N/A.
Unsupported APIs, bad responses and timestamp failures are explicit Unverified /
N/A. Ordinary collector failures do not stop scanning; cancellation and critical
lease/receipt integrity errors retain their existing distinct behavior.

Reuse the existing planning floor: greater of 10 GiB and 10% of that volume.
Being above this floor is not capacity approval. Volume identity, restore space,
at least two comparable growth observations separated by 24 hours, and the
existing minimum ten-day planning horizon remain required by the readiness
policy. This sampler does not satisfy that policy or inspect database/WAL growth.
No new RAM safety threshold is invented: one available-memory point and one RSS
point do not establish peaks, sustained headroom or safe concurrency. A Runtime
capacity/hardware upgrade may be necessary; software diagnostics do not eliminate
the supplied historical hardware limitation.

Resource advisories do not alter operational scan status, signal policy or failure
backoff. Scan SUCCESS describes operational phases, not capacity safety. No
cleanup, pruning, compression, throttling or restart action is performed.

## Diagnostic collection budget

The session journal has a hard 32 MiB append budget. Ordinary admission reserves
256 bytes for an exact terminal exhaustion marker, including its newline. The
first nonfitting record is omitted and a bounded marker is flushed/fsynced when
space permits. Its exact contract, database identity and record kind are checked
using a bounded final-line read. Long ordinary rows remain valid. The marker
survives a helper's abrupt exit before receipt persistence and prevents smaller
rows or restarts from resuming recording. A legacy file already too near the cap
is suspended by its persistent size, even if a marker cannot fit. Existing files
larger than the cap are preserved without further append, not shrunk.

The receipt retains BUDGET_EXHAUSTED, incomplete recording coverage, first observed
omission time and bounded omission counters. Counts are observed lower bounds:
abrupt exit can precede receipt persistence. Coverage refers only to this recording
budget, not scan coverage or historical integrity. Startup and both successful and
failed iterations emit at most one budget advisory per session. Advisory-output
failure cannot replace scan failures. The bounded current receipt continues;
unknown, torn or missing historical journals still fail closed. No evidence is
deleted or automatically rotated. Resuming journal history needs a separately
approved archival/generation procedure; restarting alone is not that procedure.

The receipt remains at most 32 KiB with one nonrecursive prior session. Existing
scan JSON, SQLite payloads, manifests and watch-output retention policies are
unchanged; their incremental bytes must also enter Runtime capacity planning.

## Bounded DEV evidence

Corrected profile/resource/session subset: 65 passed. Expanded watch, command,
owner, provenance, stream-service, cadence, memory and inventory subset:
**238 passed in 45.97 seconds**, including durable-marker regressions. An earlier
preflight's three findings were corrected: cross-session budget suspension,
startup/failure-path advisories, and timestamp failure isolation. Final full
pytest, exact-head review and CI remain pending.

An alternating 100-pair filesystem-only DEV probe compared committed A's session
writer with E's writer plus actual local resource sampling, 19 synthetic phase
calls and profile serialization. Both use the same disk and flush/fsync contract;
there is no scanner, database, network or other artifact write in this probe.

| Measured probe | A baseline | E observations |
| --- | ---: | ---: |
| Median pair duration | 10.816 ms | 11.499 ms |
| p95 pair duration | 18.362 ms | 18.059 ms |
| Final current receipt | 1,128 bytes | 6,979 bytes |
| Journal after 100 finalizations and startup | 89,294 bytes | 628,430 bytes |

QPC resolution was 0.1 microsecond; the synthetic profile JSON was 4,521 bytes.
The median increment was 0.683 ms in this bounded probe; p95 variation does not
demonstrate a speed improvement. Other artifact duplication and Runtime behavior
are excluded. At this synthetic journal size, a 32 MiB budget is roughly 18 days
at five-minute iterations, or 3.7 days at the CLI's one-minute default, before
accounting for extra session boundaries. These are projections, not observed
Runtime growth or recommended retention periods. Plan an approved archive before
exhaustion if longer history is required; never discard protected evidence.

At 2026-10-23 21:57:44 UTC this DEV point reported 7.86 GiB total physical memory,
1.66 GiB available, 78% physical load, 60.5 MiB process RSS, and 95.1 GiB free on
the fixed C: system volume. This is not Runtime or sustained headroom evidence.
The older 4 GB / 221 MiB / 1.51 GB Runtime figures remain historical. No cleanup
was necessary or attempted on DEV. Historical C: growth, cache/log attribution,
current Runtime capacity and matched Runtime cadence remain Unverified.

Primary API contracts: [Python elapsed timer](https://docs.python.org/3.11/library/time.html#time.perf_counter),
[physical memory](https://learn.microsoft.com/en-us/windows/win32/api/sysinfoapi/nf-sysinfoapi-globalmemorystatusex),
[system directory](https://learn.microsoft.com/en-us/windows/win32/api/sysinfoapi/nf-sysinfoapi-getsystemdirectoryw),
[drive classification](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-getdrivetypew).
