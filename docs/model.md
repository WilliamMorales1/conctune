# How conctune works

Theory behind the report: the four ceilings, the USL fit, the load-balance check, and how the sweep keeps its measurements honest. The [README](../README.md) covers installing and running it.

## What it computes

**Four ceilings on useful concurrency.** The smallest one binds, and which one
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
  process, and the report says so. macOS uses `getrusage(RUSAGE_CHILDREN)`,
  which counts the run's tree but misses processes it detaches; Windows uses
  `GetSystemTimes` deltas, which, like `/proc/stat`, count every process;
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

## Background

Universal Scalability Law: Neil Gunther, *Guerrilla Capacity Planning*.
Work-span model and the `T_1/N + O(T_inf)` bound: Blumofe & Leiserson, Cilk.
List-scheduling bound: Graham (1969). Queueing background: Harchol-Balter,
*Performance Modeling and Design of Computer Systems*. Stragglers: Dean &
Barroso, "The Tail at Scale".
