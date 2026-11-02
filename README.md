# conctune

conctune runs a command at several worker counts, fits the Universal
Scalability Law to the timings, and tells you the worker count to use plus
what is stopping the task from going faster: worker startup, coordination
overhead, the longest single case, or busy cores. Point it at `make -j`,
`pytest -n`, `xargs -P`, or any command that takes a worker count.

```
CEILINGS ON USEFUL CONCURRENCY
  startup       N <=    4.3   sqrt(work / B), measured boot cost  <== BINDING
                worker boot B=0.241s against 4.5s of work
  parallelism   N <=    5.5   work / span
                work=4.5s, span=0.8s (longest single case is 18.2% of all work)
  overhead      N <=    6.5   USL peak, sqrt((1-sigma)/kappa)
                coherency kappa=0.02263, contention sigma=0.0361
  hardware      N <=    7.2   cpus * work / cpu_total
                at N=8: 5.0s CPU for 4.5s of work (90% of burned cycles useful)

========================================================================
 RECOMMENDED WORKER COUNT: 8
   cheapest N within 3% of the best measured wall
========================================================================
   fastest measured : N=8 at 1.216s (3.17x over serial)
   knee of the curve: N=4
   binding constraint: startup (estimates N <= 4.3)
   ! the fastest point is the largest swept (N=8); the peak may lie beyond it -- extend --points and re-run
   ! the startup ceiling estimates N<=4.3, below the measured optimum of 8 -- the ceiling is a first-order estimate and the workers past it still win, just sub-linearly
```

Requires Python 3.11+ and numpy, on Linux, macOS, or Windows.

## Install

```sh
pipx install git+https://github.com/WilliamMorales1/conctune
# or: uv tool install git+https://github.com/WilliamMorales1/conctune
```

From a clone: `pipx install .`, or run without installing via `python3 -m conctune ...` (numpy must be importable).

## Quick start

```sh
conctune auto --task examples/demo.toml
```

A task is one TOML file naming the command, with `{j}` for the worker count:

```toml
name = "mine"

[run]
cmd = "make -j{j}"
```

Results land in `conctune-out/<task name>/`.

## Docs

- [Usage](docs/usage.md): stages, flags, output files, exit codes, platform notes.
- [Task spec](docs/task-spec.md): `[run]`, `[cases]`, `[sweep]`, async runtimes, examples.
- [Model](docs/model.md): the four ceilings, USL fit, load balance, reading the report.
- [Development](docs/development.md): dev setup, checks, releases.
- [Agent skill](skills/conctune/SKILL.md): copy `skills/conctune` into an agent's skills directory.

## License

MIT
