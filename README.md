# conctune

conctune runs a command at several worker counts, fits the Universal
Scalability Law to the timings, and tells you the worker count to use plus
what is stopping the task from going faster: worker startup, coordination
overhead, the longest single case, or busy cores. Point it at `make -j`,
`pytest -n`, `xargs -P`, or any command that takes a worker count.

```
CEILINGS ON USEFUL CONCURRENCY
  hardware      N <=    4.1   cores * work / cpu_total  <== BINDING
                at N=4: 4.4s CPU for 4.5s of work (100% of burned cycles useful)
  startup       N <=    4.3   sqrt(work / B), measured boot cost
                worker boot B=0.241s against 4.5s of work
  parallelism   N <=    5.5   work / span
                work=4.5s, span=0.8s (longest single case is 18.2% of all work)
  overhead      N <=    6.5   USL peak, sqrt((1-sigma)/kappa)
                coherency kappa=0.02263, contention sigma=0.0361

========================================================================
 RECOMMENDED WORKER COUNT: 8
   cheapest N within 3% of the best measured wall
========================================================================
   fastest measured : N=8 at 1.216s (3.17x over serial)
   knee of the curve: N=4
   binding constraint: hardware (estimates N <= 4.1)
```

Requires Linux, Python 3.11+, and numpy. A delegated cgroup v2 (a systemd
user session gives one) is optional: with it, run CPU counts only the task's
own processes; without it, conctune falls back to `/proc/stat` and says so.

## Install

From GitHub:

```sh
pipx install git+https://github.com/WilliamMorales1/conctune
# or: uv tool install git+https://github.com/WilliamMorales1/conctune
```

From source:

```sh
git clone https://github.com/WilliamMorales1/conctune
cd conctune
pipx install .               # or: uv tool install .
```

Or run it straight from a clone with no install: `python3 -m conctune ...`
(numpy must be importable).

## Quick start

```sh
conctune auto --task examples/demo.toml
```

Or stage by stage:

```sh
conctune machine                        # 1. probe the host
conctune task    --task mine.toml       # 2. time each case serially
conctune sweep   --task mine.toml       # 3. run at several N
conctune model   --task mine.toml       # 4. fit and recommend
```

```
machine  ->  task  ->  sweep  ->  model
 specs      cases      runs     verdict
```

Both ends are swappable: `--task` points at any workload spec, `--machine` at
any host profile. Results land in `conctune-out/<task name>/`:
`machine.json`, `task.json`, `runs.csv`, `report.txt`. Every stage reads the
previous stage's files, so you can re-analyse without re-running anything, or
hand-edit a profile and see what changes.

Useful flags: `--points 1,2,4,8` and `--repeats N` (sweep, auto) bound the
cost; `--seed N` makes the run order reproducible; `--no-randomize` (sweep,
auto) runs in order, which is not advised; `--json` (model, auto) prints a
machine-readable report on stdout with progress on stderr; `--short` (model,
auto) prints a one-line verdict such as `N=8 binding=hardware r2=1.00`, then
one `note:` or `warn:` line each, and still saves the full report.

## Describing your task

A task is one TOML file. Minimum:

```toml
name = "mine"

[run]
cmd = "make -j{j}"      # {j} is replaced with the worker count
cwd = "."               # relative to this file; default: the file's directory
timeout = 900           # seconds per run
```

That alone gets you the sweep, the USL fit, and the hardware ceiling. Adding a
`[cases]` section unlocks the span ceiling, the startup ceiling, and the
load-balance analysis:

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
- `examples/nvim-tests.toml`: harvests case and boot times from a real test
  suite's log. Its `cwd` is a placeholder; point it at your own checkout.

## Exit codes

| Exit | Meaning |
| --- | --- |
| 0 | success |
| 1 | nothing to analyse (`model` without sweep data or a successful N=1 run, `task` without `[cases]`), an unwritable `--out`, or not Linux |
| 2 | task spec error: missing file, invalid TOML, or a bad field; the message names the field |
| 130 | interrupted |

## How it works

Four ceilings on useful concurrency (startup, overhead, hardware,
parallelism), the smallest of which binds; a USL fit; a load-balance check
against a greedy LPT schedule; and a recommendation taken from the
measurements, not the model. [docs/model.md](docs/model.md) explains each one,
the measurement hygiene the sweep applies, and how to read the report.

## For AI agents

`skills/conctune/SKILL.md` is an agent skill: writing a task spec, bounding
sweep cost, reading the verdict from `--short` or `--json`, and what to tell the user
for each binding ceiling. Copy the `skills/conctune` folder into an agent's
skills directory (for Claude Code, `~/.claude/skills/`).

## Development

Pick one environment manager. Each gives an editable install plus the dev
tools (pytest, ruff, pyright).

uv (uses the committed `uv.lock`):

```sh
uv sync                      # creates .venv with conctune and the dev group
uv run pytest
uv run conctune --version
```

conda (or mamba):

```sh
conda create -n conctune -c conda-forge python=3.11 numpy pytest ruff pyright
conda activate conctune
pip install -e . --no-deps
pytest
```

venv:

```sh
python3 -m venv .venv
. .venv/bin/activate         # fish: . .venv/bin/activate.fish
pip install -e . pytest ruff pyright
pytest
```

Checks CI runs on Python 3.11 to 3.13:

```sh
ruff check . && ruff format --check .
pyright
pytest                       # -m "not slow" skips the end-to-end demo run
```

The suite recovers known `sigma`/`kappa` from synthetic curves, checks the fit
under noise, verifies LPT against Graham's 4/3 bound, compares `model` output
on committed sweep data against a golden report, covers every spec error and
exit code, and fuzzes the spec loader and log harvester.

Releases: bump `__version__` in `conctune/__init__.py`, then push a matching
`vX.Y.Z` tag. The release workflow builds the sdist and wheel and attaches them
to a GitHub release.

## License

MIT
