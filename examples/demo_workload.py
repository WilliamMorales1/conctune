#!/usr/bin/env python3
"""A synthetic workload for exercising conctune.

Deliberately shaped to show every failure mode: one fat case that sets the
span, a per-worker import cost that acts as B, and a choice of scheduler so
the load-balance analysis has something to catch.

    --list                 print case ids, one per line
    --case ID              run exactly one case
    --jobs N               run all cases across N workers
    --noop                 boot a worker and exit (measures startup)
    --schedule static|pull partitioning strategy
"""

import argparse
import hashlib
import sys
import time
from concurrent.futures import ProcessPoolExecutor

# Case id -> nominal cost in seconds. One case is 8x the others: that is the
# span, and no worker count can finish faster than it.
CASES = {f"case{i:02d}": 0.10 for i in range(24)}
CASES["case00"] = 0.80
CASES["case01"] = 0.40

WORKER_BOOT = 0.15  # stands in for imports / model loading / connection setup


def burn(seconds: float) -> None:
    """Spin the CPU for a wall-clock duration (real work, not sleep)."""
    end = time.perf_counter() + seconds
    digest = b"seed"
    while time.perf_counter() < end:
        for _ in range(500):
            digest = hashlib.sha256(digest).digest()


def boot() -> None:
    burn(WORKER_BOOT)


def run_case(case: str) -> tuple[str, float]:
    start = time.perf_counter()
    burn(CASES[case])
    return case, time.perf_counter() - start


_booted = False


def _worker(case: str):
    global _booted
    if not _booted:  # once per process, like a real import cost
        boot()
        _booted = True
    return run_case(case)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--case")
    parser.add_argument("--jobs", type=int)
    parser.add_argument("--noop", action="store_true")
    parser.add_argument("--schedule", choices=("static", "pull"), default="pull")
    args = parser.parse_args()

    if args.list:
        print("\n".join(sorted(CASES)))
        return 0

    if args.noop:
        boot()
        return 0

    if args.case:
        boot()
        case, elapsed = run_case(args.case)
        print(f"{case}\t{elapsed:.6f}")
        return 0

    if args.jobs:
        cases = sorted(CASES)
        with ProcessPoolExecutor(max_workers=args.jobs) as pool:
            if args.schedule == "pull":
                # Workers take the next case when free.
                results = list(pool.map(_worker, cases))
            else:
                # Static round-robin: each worker gets a fixed slice up front,
                # so one fat slice strands everyone else at the end.
                chunk = max(1, len(cases) // args.jobs)
                results = list(pool.map(_worker, cases, chunksize=chunk))
        print(f"{len(results)} cases on {args.jobs} workers", file=sys.stderr)
        return 0

    parser.error("one of --list, --case, --jobs, --noop is required")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
