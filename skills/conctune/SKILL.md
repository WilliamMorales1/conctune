---
name: conctune
description: Find the worker count a parallel task should run at, and what limits its scaling, with the conctune CLI (TOML task spec in, measured sweep and verdict out). Use when asked how many jobs, workers, threads, or shards to use (make -j, pytest -n, xargs -P, a test runner's parallelism), why adding workers stopped helping, or to tune or diagnose concurrency of a command on Linux.
---

# conctune

conctune runs a command at several worker counts, fits the Universal Scalability Law to the timings, and reports the cheapest worker count within 3% of the fastest measured run plus the ceiling that limits scaling (startup, overhead, parallelism, or hardware). Linux only: it reads `/proc` and `/sys`.

Run it as `conctune` if installed, otherwise `python3 -m conctune` from the repo root.

## Workflow

1. Find how the workload takes a worker count: a flag (`make -j N`, `pytest -n N`, `xargs -P N`) or an env var (`JOBS=N ./run.sh`). If it has neither, conctune cannot drive it; say so instead of guessing.
2. Time one serial run first (`time CMD` at 1 worker). The full sweep costs about `points x repeats` runs, each between that time and a fraction of it. Over about 30 minutes in total, cut points or repeats and tell the user the estimate.
3. Write the spec to `NAME.toml` (see Task spec). Only `[run].cmd` is required.
4. Run all stages. The sweep can take minutes, so run it in the background and wait for it to exit:

```sh
conctune auto --task NAME.toml --seed 0
```

5. Read the verdict as JSON. `auto --json` prints only the report on stdout (progress goes to stderr); `model --json` re-analyses the saved runs without running anything:

```sh
conctune model --task NAME.toml --json | jq '.analysis | {
  n: .recommended_n.n, notes: .recommended_n.notes, binding,
  ceilings: (.ceilings | map_values(.n)), r2: .fit.r2,
  scheduling_suspect: .imbalance.scheduling_suspect,
  unreliable, work_overstated }'
```

The human report is also saved to `conctune-out/NAME/report.txt`.

## Task spec

```toml
name = "unit-tests"

[run]
cmd = "pytest -q -n {j}"   # {j} = worker count; required
cwd = "."                  # relative to this file; default: the file's directory
timeout = 900              # seconds per run
env = { PYTHONHASHSEED = "0" }

[sweep]                    # optional; omit points to use the machine's suggestion
points = [1, 2, 4, 6, 8, 12, 16]
repeats = 3
```

Start with only `[run]`. That gives the sweep, the USL fit, and the hardware ceiling. Add `[cases]` when the user wants to know why it stops scaling, since it unlocks the parallelism ceiling, the startup ceiling, and the load-balance check. Two ways:

- Log harvest (prefer it when the workload already logs per-case times): one profiling run, then regexes over its log.

```toml
[cases]
log = "/tmp/run.log*"      # glob, relative to cwd unless absolute
case_pattern = '^PASS (?P<name>\S+) \((?P<ms>\d+)ms\)'   # needs ms, sec or s
boot_pattern = 'worker ready in (?P<ms>\d+)ms'           # optional, gives B
profile_jobs = 1
```

- Per-case commands: one process per case, so only for suites of tens of cases, or set `sample = N`.

```toml
[cases]
list = "./run --list"            # one case id per line
one = "./run --case {case}"      # {case} is shell-quoted
startup = "./run --noop"         # boot a worker, do no work; always give it if possible
```

Without `startup`, each case time includes one boot, so total work is overstated (the report flags `work_overstated`).

## Reading the verdict

`recommended_n.n` is the answer and comes from measurements, not the model. `binding` is the diagnosis: the smallest ceiling, which tells the user what to change.

| `binding` | Means | Tell the user |
| --- | --- | --- |
| `startup` | worker boot B dominates | reuse or pool workers, move setup out of the per-worker path, batch small cases |
| `overhead` | coordination grows with N; past `fit.n_star` runs get slower | same as startup; never run past the peak |
| `parallelism` | the longest case sets a floor | split the fat cases, or schedule them first |
| `hardware` | cores are busy with useful work | more workers will not help; make each case cheaper |

Before reporting, check these and pass them on:

- `recommended_n.notes`: read every note. "fastest point is the largest swept" means rerun with larger `--points`.
- `fit.r2` below 0.90: the USL does not describe this workload, so trust the measured table, not `n_star`.
- `imbalance.scheduling_suspect` true: the real run trails a greedy schedule of the same cases by over 25%. A pull queue or longest-first ordering is the fix, and worker count is not worth tuning until then.
- `unreliable`: worker counts where some runs failed. They are never recommended; report the failure rate, because it often means timing-sensitive tests flake under load.
- A hardware ceiling above the core count means cases block (I/O, subprocesses), so oversubscribing pays.
- A hardware ceiling detail ending in "CPU from /proc/stat counts every process on the machine" means no cgroup was available; other load on the machine lowered that ceiling.

## Hygiene

- Keep the machine otherwise idle during the sweep; do not build or test anything else alongside it.
- Machine warnings in the report (busy machine, powersave governor, turbo, low memory) distort the curve. Pass them on.
- Pass `--seed` so a rerun uses the same run order.
- Re-analyse without rerunning: `conctune model --task NAME.toml`. Rerun only `sweep` after changing points: `conctune sweep --task NAME.toml --points 1,4,8,16,24`, then `model`.

## Errors

| Exit | Cause |
| --- | --- |
| 0 | success |
| 1 | no sweep data or no successful N=1 run (`model`), or no `[cases]` (`task`) |
| 2 | spec error: the message names the field, e.g. `[run].cmd must contain the placeholder {j}` |
| 130 | interrupted |

`[cases].case_pattern matched nothing` prints the log's first line: fix the regex against it. Test it with `grep -P` on the log before rerunning.

## Examples

`examples/demo.toml` with `examples/demo_workload.py` is a synthetic workload with a fat case, a per-worker boot cost, and a switchable scheduler (`--schedule static|pull`). `examples/nvim-tests.toml` harvests case and boot times from a real test suite's log; copy it when the workload logs its own times.
