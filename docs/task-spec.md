# Describing your task

A task is one TOML file. Minimum:

```toml
name = "mine"

[run]
cmd = "make -j{j}"      # {j} is replaced with the worker count
                        # {python} with the interpreter running conctune
cwd = "."               # relative to this file; default: the file's directory
timeout = 900           # seconds per run
env = { PYTHONHASHSEED = "0" }   # extra environment variables; {j} works here too
shell = true            # false: split cmd into argv and run it without a shell
carriers = 1            # optional: kernel threads an M:N runtime runs on
```

That alone gets you the sweep, the USL fit, and the hardware ceiling.

`{j}` must appear in `cmd` or in an `env` value. For a tool that reads its
worker count from the environment, put it in `env`
(`env = { OMP_NUM_THREADS = "{j}" }`) rather than prefixing `cmd` with
`VAR={j}`, which only POSIX shells parse.

## User-level and virtual threads

`{j}` need not be a process or kernel-thread count. For asyncio tasks,
goroutines, Java virtual threads, or Node promises, make it the number of tasks
in flight (a semaphore or pool size) and set `carriers` to the kernel threads
the runtime multiplexes them onto: 1 for asyncio and Node, `GOMAXPROCS` for Go,
`jdk.virtualThreadScheduler.parallelism` for Java. The hardware ceiling then
counts carriers instead of cores, and the load-balance check is skipped, since
tasks sharing a carrier queue for it in bursts that the check would misread as
imbalance. Sweep geometrically and far past the core count: a task that mostly
waits costs a fraction of a core. See [model.md](model.md#user-level-and-virtual-threads).

## Cases

Adding a `[cases]` section unlocks the span ceiling, the startup ceiling, and
the load-balance analysis:

```toml
[cases]
list    = "./run.py --list"           # one case id per line
one     = "./run.py --case {case}"    # run exactly one case
startup = "./run.py --noop"           # boot a worker, do no work (measures B)
sample  = 0                           # 0 = all cases, N = random sample
```

If the workload already times its own units of work, harvest them from its log
instead: one run rather than one process per case, and the real times rather
than reconstructions:

```toml
[cases]
log          = "/tmp/run.log.shard*"      # glob, parsed after a profiling run
case_pattern = '^PASS \[(?P<group>[^\]]+)\] (?P<name>.+?)\s+\((?P<ms>\d+)ms\)'
boot_pattern = 'ready in (?P<ms>\d+)ms'   # optional: per-worker boot, B
profile_jobs = 1                          # workers for the profiling run
```

The pattern needs a named group for the duration (`ms`, `sec` or `s`); every
other named group is joined to form the case id.

`startup` matters more than it looks. Each case is timed in its own process, so
every measurement carries one boot; without `startup` to subtract it, the work
total comes out above the CPU time the parallel run actually burns and the
scheduling bounds land above makespans you already achieved. conctune detects
that and says so, but measuring it is better.

Even with `startup`, a case run in a fresh process pays a cold start the real
run does not (imports, cold caches, a clock that has not ramped up). For cases
under a second that can dominate. When the case times sum to more than 1.25x
the whole N=1 run, the report flags `work_overstated` and the hardware ceiling
falls back to the N=1 wall time. Harvest times from a log for short cases.

## Sweep

```toml
[sweep]
points = [1, 2, 4, 6, 8, 12, 16]   # omit to use the machine's suggestion
repeats = 3
randomize = true                   # shuffle the whole run matrix
warmup = 1
```

## Examples

- `examples/demo.toml` with `examples/demo_workload.py`: a synthetic workload
  with a deliberate fat case, a per-worker boot cost, and a switchable
  scheduler (`--schedule static|pull`), so you can watch each diagnosis fire.
  Run it from the repo root: `conctune auto --task examples/demo.toml`.
- `examples/asyncio.toml` with `examples/asyncio_workload.py`: user-level
  threads, asyncio tasks on one event loop (`carriers = 1`). Each task waits
  on a simulated remote call, then computes on the loop, so the optimum is set
  by the wait-to-compute ratio, not the core count. Takes about three minutes.
- `examples/nvim-tests.toml`: harvests case and boot times from a real test
  suite's log. Its `cwd` is a placeholder; point it at your own checkout. Its
  command uses POSIX shell syntax, so it needs Linux or macOS.
