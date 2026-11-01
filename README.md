# conctune

Work out how many workers a task should run with, and — more usefully — what
is stopping it from going faster.

Four stages, each runnable on its own. Both ends are swappable from the CLI:
`--task` points at any workload spec, `--machine` at any host profile.

```
machine  ->  task  ->  sweep  ->  model
 specs      cases      runs     verdict
```

## Quick start

```sh
python3 -m conctune auto --task examples/demo.toml
```

Or stage by stage:

```sh
python3 -m conctune machine                        # 1. probe the host
python3 -m conctune task    --task mine.toml       # 2. time each case serially
python3 -m conctune sweep   --task mine.toml       # 3. run at several N
python3 -m conctune model   --task mine.toml       # 4. fit and recommend
```

Artifacts land in `conctune-out/<task name>/`: `machine.json`, `task.json`,
`runs.csv`, `report.txt`. Every stage reads the previous stage's files, so you
can re-analyse without re-running anything, or hand-edit a profile and see what
changes.

## Describing your task

A task is one TOML file. Minimum:

```toml
name = "mine"

[run]
cmd = "make -j{j}"      # {j} is replaced with the worker count
```

That alone gets you the sweep and the USL fit. Adding a `[cases]` section
unlocks the span ceiling and the load-balance analysis:

```toml
[cases]
list    = "./run.py --list"           # one case id per line
one     = "./run.py --case {case}"    # run exactly one case
startup = "./run.py --noop"           # boot a worker, do no work (measures B)
sample  = 0                           # 0 = all cases, N = random sample
```

If the workload already times its own units of work, harvest them from its log
instead — one run rather than one process per case, and the real times rather
than reconstructions:

```toml
[cases]
log          = "/tmp/run.log.shard*"      # glob, parsed after a profiling run
case_pattern = '^PASS \[(?P<group>[^\]]+)\] (?P<name>.+?)\s+\((?P<ms>\d+)ms\)'
boot_pattern = 'ready in (?P<ms>\d+)ms'   # optional: per-worker boot, B
profile_jobs = 1                          # workers for the profiling run
```

The pattern needs a named group for the duration (`ms`, `sec` or `s`); every
other named group is joined to form the case id. `examples/nvim-tests.toml` is
a working instance of this against a real test suite.

`startup` matters more than it looks. Each case is timed in its own process, so
every measurement carries one boot; without `startup` to subtract it, the work
total comes out above the CPU time the parallel run actually burns and the
scheduling bounds land above makespans you already achieved. conctune detects
that and says so, but measuring it is better.

See `examples/demo.toml` and `examples/demo_workload.py` for a complete worked
example — a synthetic workload with a deliberate fat case and a switchable
scheduler, so you can watch each diagnosis fire.

## For AI agents

`skills/conctune/SKILL.md` is an agent skill: writing a task spec, bounding
sweep cost, reading the verdict from `model --json`, and what to tell the user
for each binding ceiling. Copy the `skills/conctune` folder into an agent's
skills directory (for Claude Code, `~/.claude/skills/`).

## What it computes

**Three ceilings on useful concurrency.** The smallest one binds, and which one
binds decides what is worth optimising:

| ceiling | formula | means |
|---|---|---|
| startup | `sqrt(work / B)` | worker boot cost, measured directly |
| overhead | `sqrt((1 - sigma) / kappa)` | USL peak, fitted from the sweep |
| hardware | `cores * work / cpu_total` | cores already busy (CPU of the run's process tree) |
| parallelism | `work / span` | longest single case; average parallelism |

**A fitted Universal Scalability Law curve.** `S(N) = N / (1 + sigma(N-1) +
kappa·N(N-1))`, where `sigma` is contention and `kappa` is coherency — the term
that makes scaling go *retrograde*, so that past `N*` more workers means a
slower run. Fitted via Gunther's linearising transform plus a Gauss-Newton
polish, so numpy is the only dependency.

**A load-balance check.** Measured per-case times are fed to a greedy LPT
schedule; the observed makespan is compared against it (with one worker boot
added back, since the simulation places work but does not boot anything). If
the real run trails by more than 25%, scheduling is the problem and the worker
count is not worth tuning yet — a pull queue fixes it and the curve changes
shape afterwards.

**A reliability gate.** Worker counts where the task failed are excluded from
the recommendation regardless of speed, and reported with their failure rate. A
run that does not finish is not an optimum, and heavily oversubscribed runs are
exactly where timing-sensitive workloads start to flake.

**A recommendation from the measurements, not the model.** The ceilings are
estimates; the sweep is ground truth. conctune recommends the cheapest N within
3% of the fastest measured wall time, then reports the binding ceiling as
diagnosis alongside it — and flags the disagreement when the two differ.

## Measurement hygiene

Handled for you:

- run order is randomised across the whole (points x repeats) matrix, so
  thermal drift cannot correlate with the worker count and manufacture fake
  retrograde scaling;
- repeats collapse by median, not mean, because a throttling laptop has a long
  right tail;
- a warmup run is discarded;
- each run starts in its own child cgroup, whose `cpu.stat` counts the whole
  process tree (forkserver workers and daemons the harness never parented) and
  nothing else on the machine. Without a delegated cgroup v2 (a systemd user
  session gives one) it falls back to `/proc/stat` deltas, which count every
  process, and the report says so;
- sweep points are chosen dense below the physical core count and sparse out to
  2x logical, because without points past saturation `kappa` is unidentifiable.

Flagged, not fixed: a busy machine, a ramping frequency governor, turbo boost,
SMT, and low free memory all distort the curve. The report names each one it
finds.

## Reading the result

- **R² below 0.90** — the USL does not describe this workload. Usually load
  imbalance that varies with N, or thermal drift. Fix that before trusting `N*`.
- **kappa ≈ 0** — no retrograde term; scaling is Amdahl-shaped and the ceiling
  is `1/sigma`. Sweep further past the core count if you need to pin it down.
- **fastest point is the largest swept** — the peak may be beyond your range;
  extend `--points`.
- **hardware ceiling below the measured optimum** — the ceilings are
  first-order estimates; workers past them still win, just sub-linearly.
- **hardware ceiling *above* the core count** — the cases block rather than
  compute (waiting on I/O, a language server, a subprocess), so oversubscribing
  pays. The report says by how much.
- **rows marked `(saturated)` in the balance table** — past the physical core
  count (further for a workload that blocks) a case no longer runs at its
  contention-free service time, so the simulated schedule is optimistic there
  and those rows are excluded from the judgement.

## Tests

```sh
python3 tests.py
```

Recovers known `sigma`/`kappa` from synthetic curves, checks the fit under 2%
noise, verifies LPT against Graham's 4/3 bound, and confirms the ceilings bind
in the situations they should.

## Background

Universal Scalability Law: Neil Gunther, *Guerrilla Capacity Planning*.
Work-span model and the `T_1/N + O(T_inf)` bound: Blumofe & Leiserson, Cilk.
List-scheduling bound: Graham (1969). Queueing background: Harchol-Balter,
*Performance Modeling and Design of Computer Systems*. Stragglers: Dean &
Barroso, "The Tail at Scale".
