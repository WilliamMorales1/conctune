# Usage

## Stages

`conctune auto --task <spec>` runs all four stages. Or run them one at a time:

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
`machine.json`, `task.json`, `runs.csv`, and `report.txt` (or `report.json`
with `--json`). Every stage reads the
previous stage's files, so you can re-analyse without re-running anything, or
hand-edit a profile and see what changes.

## Flags

- `--out DIR` sets the results directory (default `conctune-out/<task name>`).
- `--machine FILE` uses a saved host profile instead of probing.
- `--runs FILE` and `--task-profile FILE` (model) analyse a `runs.csv` and
  `task.json` from anywhere; `--task` is then optional.
- `--points 1,2,4,8` and `--repeats N` (sweep, auto) bound the cost.
- `--seed N` makes the run order reproducible.
- `--no-randomize` (sweep, auto) runs in order, which is not advised.
- `--json` (model, auto) prints a machine-readable report on stdout with progress on stderr.
- `--short` (model, auto) prints a one-line verdict such as `N=8 binding=hardware r2=1.00` (`binding=none` when no ceiling is reached within the sweep), then one `note:` or `warn:` line each, and still saves the full report.

## Exit codes

| Exit | Meaning |
| --- | --- |
| 0 | success |
| 1 | nothing to analyse (`model` without sweep data or a successful N=1 run, `task` without `[cases]`), or an unwritable `--out` |
| 2 | task spec error: missing file, invalid TOML, or a bad field; the message names the field |
| 130 | interrupted |

## Platforms

Run CPU, which the hardware ceiling rests on, comes from the best source each
platform has:

| Platform | CPU source | Counts |
| --- | --- | --- |
| Linux, delegated cgroup v2 (a systemd user session gives one) | `cgroup` | the run's process tree only |
| Linux, no delegation | `proc_stat` | every process on the machine |
| macOS | `rusage` | the run's process tree, minus processes it detaches |
| Windows | `system_times` | every process on the machine |

The report says when its source could skew the hardware ceiling. Frequency
governor and turbo checks are Linux only; on Windows the load figure is CPU
busy time sampled over half a second, since there is no load average.

Commands run through the platform shell: `/bin/sh` on Linux and macOS,
`cmd.exe` on Windows. Write specs in syntax both understand, or keep one spec
per platform.
