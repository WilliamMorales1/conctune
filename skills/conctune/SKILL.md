---
name: conctune
description: Find the worker count a parallel task should run at, and what limits its scaling, with the conctune CLI (TOML task spec in, measured sweep and verdict out). Use when asked how many jobs, workers, threads, or shards to use (make -j, pytest -n, xargs -P, a test runner's parallelism), why adding workers stopped helping, or to tune or diagnose concurrency of a command on Linux, macOS, or Windows.
---

# conctune

conctune runs a command at several worker counts, fits the Universal Scalability Law to the timings, and reports the cheapest worker count within 3% of the fastest measured run plus the ceiling that limits scaling (startup, overhead, parallelism, or hardware). Runs on Linux, macOS, and Windows; commands go through `/bin/sh` or `cmd.exe`.

Run it as `conctune` if installed (`pipx install conctune` or `uv tool install conctune`), otherwise `python3 -m conctune` from the repo root.

## Workflow

1. Find how the workload takes a worker count: a flag (`make -j N`, `pytest -n N`, `xargs -P N`) or an env var (put it in `[run].env` as `JOBS = "{j}"`, not a `JOBS={j}` prefix, which breaks on Windows). If it has neither, conctune cannot drive it; say so instead of guessing.
2. Time one serial run first (`time CMD` at 1 worker). The full sweep costs about `points x repeats` runs, each between that time and a fraction of it. Over about 30 minutes in total, cut points or repeats and tell the user the estimate.
3. Write the spec to `NAME.toml` (see Task spec). Only `[run].cmd` is required.
4. Run all stages. The sweep can take minutes, so run it in the background and wait for it to exit:

```sh
conctune auto --task NAME.toml --seed 0 --short
```

5. Read the verdict. `--short` prints only the verdict on stdout (progress goes to stderr) and still saves the full report:

```
N=8 binding=hardware r2=0.98 scheduling_suspect unreliable=16 work_overstated
note: the fastest point is the largest swept (N=8); the peak may lie beyond it -- extend --points and re-run
warn: turbo boost is on -- ...
```

The first line is `N=<recommended> binding=<ceiling or none> r2=<fit R^2>`, followed by flags only when they apply: `scheduling_suspect`, `unreliable=<worker counts>`, `work_overstated`. Each `note:` line comes from the recommendation and each `warn:` line from the machine probe; pass both on. `conctune model --task NAME.toml --short` re-analyses the saved runs without running anything.

For ceiling values and details, use JSON instead (`--json` on `auto` or `model`):

```sh
conctune model --task NAME.toml --json | jq '.analysis | {
  n: .recommended_n.n, notes: .recommended_n.notes, binding, nearest_ceiling,
  ceilings: (.ceilings | map_values(.n)), r2: .fit.r2,
  scheduling_suspect: .imbalance.scheduling_suspect,
  unreliable, work_overstated }'
```

The human report is also saved to `conctune-out/NAME/report.txt`.

## Task spec

```toml
name = "unit-tests"

[run]
cmd = "pytest -q -n {j}"   # {j} = worker count; required here or in env. {python} = conctune's interpreter. {tmp} = temp dir
cwd = "."                  # relative to this file; default: the file's directory
timeout = 900              # seconds per run
env = { PYTHONHASHSEED = "0" }   # values may use {j}
carriers = 1               # optional; only for user-level threads, see below

[sweep]                    # optional; omit points to use the machine's suggestion
points = [1, 2, 4, 6, 8, 12, 16]
repeats = 3
```

For user-level threads (asyncio tasks, goroutines, Java virtual threads, Node), `{j}` is the in-flight task limit and `carriers` is the kernel threads the runtime runs them on: 1 for asyncio and Node, `GOMAXPROCS` for Go, `jdk.virtualThreadScheduler.parallelism` for Java. With it set, the hardware ceiling counts carriers instead of logical CPUs, `.analysis.carriers` echoes it, and `.imbalance` is null (the load-balance check does not apply). Sweep geometrically past the core count, e.g. `--points 1,4,16,64,256,1024`.

Start with only `[run]`. That gives the sweep, the USL fit, and the hardware ceiling. Add `[cases]` when the user wants to know why it stops scaling, since it unlocks the parallelism ceiling, the startup ceiling, and the load-balance check. Two ways:

- Log harvest (prefer it when the workload already logs per-case times): one profiling run, then regexes over its log.

```toml
[cases]
log = "/tmp/run.log*"      # glob, relative to cwd unless absolute
case_pattern = '^PASS (?P<name>\S+) \((?P<ms>\d+)ms\)'   # needs ms, sec or s
boot_pattern = 'worker ready in (?P<ms>\d+)ms'           # optional, gives B
profile_jobs = 1
```

- Per-case commands: one process per case, so only for suites of tens of cases, or set `sample = N`. Each case pays a cold start the real run does not, which inflates sub-second cases; prefer log harvest for those.

```toml
[cases]
list = "./run --list"            # one case id per line
one = "./run --case {case}"      # {case} is shell-quoted
startup = "./run --noop"         # boot a worker, do no work; always give it if possible
```

Without `startup`, each case time includes one boot, so total work is overstated (the report flags `work_overstated`). The flag also fires when the case times sum to over 1.25x the whole N=1 run, even with `startup` given; then the parallelism ceiling and load-balance table are unreliable, and the hardware ceiling falls back to the N=1 wall time.

## Reading the verdict

`recommended_n.n` is the answer and comes from measurements, not the model. `binding` is the diagnosis: the smallest ceiling, which tells the user what to change. It is null (`binding=none`) when speedup still climbs at the largest swept N and every ceiling lies beyond it; `nearest_ceiling` then names the smallest one.

| `binding` | Means | Tell the user |
| --- | --- | --- |
| `startup` | worker boot B dominates | reuse or pool workers, move setup out of the per-worker path, batch small cases |
| `overhead` | coordination grows with N; past `fit.n_star` runs get slower | same as startup; never run past the peak |
| `parallelism` | the longest case sets a floor | split the fat cases, or schedule them first |
| `hardware` | cores are busy with useful work | more workers will not help; make each case cheaper |
| `none` | no ceiling reached within the sweep | rerun with `--points` extended toward `nearest_ceiling`; the N so far is a lower bound |

Before reporting, check these and pass them on (in `--short` output they are the flags and `note:` lines):

- `recommended_n.notes`: read every note. "fastest point is the largest swept" means rerun with larger `--points`.
- `fit.r2` below 0.90: the USL does not describe this workload, so trust the measured table, not `n_star`.
- `imbalance.scheduling_suspect` true: the real run trails a greedy schedule of the same cases by over 25%, after the schedule is stretched by the run's CPU growth (a clock that drops under load is not imbalance). A pull queue or longest-first ordering is the fix, and worker count is not worth tuning until then.
- `unreliable`: worker counts where some runs failed. They are never recommended; report the failure rate, because it often means timing-sensitive tests flake under load.
- A hardware ceiling above the logical CPU count means cases block (I/O, subprocesses), so oversubscribing pays.
- A hardware ceiling detail ending in "CPU from /proc/stat (or GetSystemTimes) counts every process on the machine" means CPU was measured machine-wide (Linux without a cgroup, or Windows); other load on the machine lowered that ceiling.
- A detail ending in "CPU from getrusage misses processes the run detaches" (macOS) means daemons the workload spawned went uncounted; the ceiling may read high.

## Hygiene

- Keep the machine otherwise idle during the sweep; do not build or test anything else alongside it.
- Machine warnings in the report (busy machine, powersave governor, turbo, low memory) distort the curve. Pass them on.
- Pass `--seed` so a rerun uses the same run order. Keep run order randomised (do not pass `--no-randomize`) so thermal drift does not fake retrograde scaling.
- Re-analyse without rerunning: `conctune model --task NAME.toml`. Rerun only `sweep` after changing points: `conctune sweep --task NAME.toml --points 1,4,8,16,24`, then `model`.

## Errors

| Exit | Cause |
| --- | --- |
| 0 | success |
| 1 | no sweep data or no successful N=1 run (`model`), no `[cases]` (`task`), or an unwritable `--out` |
| 2 | spec error: missing file, invalid TOML, or a bad field; the message names the field, e.g. `[run].cmd must contain the placeholder {j}` |
| 130 | interrupted |

`[cases].case_pattern matched nothing` prints the log's first line: fix the regex against it. Test it with `grep -P` on the log before rerunning.

## Examples

`examples/demo.toml` with `examples/demo_workload.py` is a synthetic workload with a fat case, a per-worker boot cost, and a switchable scheduler (`--schedule static|pull`). `examples/asyncio.toml` sizes asyncio tasks on one event loop (`carriers = 1`); copy it for user-level threads. `examples/nvim-tests.toml` harvests case and boot times from a real test suite's log; copy it when the workload logs its own times.
