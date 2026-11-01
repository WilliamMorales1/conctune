"""A synthetic user-level-thread workload for conctune: asyncio tasks on one loop.

Each case waits on a simulated remote call, then does a little CPU work on the
event loop. N is the number of tasks in flight, not the number of kernel
threads: there is exactly one carrier, the loop's thread. The optimum is set by
the ratio of waiting to computing (Little's law), not by the core count.

    --jobs N     run every case with at most N tasks in flight
    --log PATH   append one "<case> <ms>ms" line per case
"""

import argparse
import asyncio
import hashlib
import time

CASES = 160
WAIT = 0.20  # remote call: the loop is free while a task waits
COMPUTE = 0.008  # parsing the reply: holds the loop, nothing else runs
# A downstream with a fixed number of connections. Past this many tasks in
# flight the extra ones just queue, the way a database pool caps them.
POOL = 48


def burn(seconds: float) -> None:
    end = time.perf_counter() + seconds
    digest = b"seed"
    while time.perf_counter() < end:
        for _ in range(200):
            digest = hashlib.sha256(digest).digest()


async def case(index: int, limit: asyncio.Semaphore, pool: asyncio.Semaphore, out):
    async with limit:
        start = time.perf_counter()
        async with pool:
            await asyncio.sleep(WAIT)
        burn(COMPUTE)
        if out:
            out.write(f"case{index:03d} {(time.perf_counter() - start) * 1000:.1f}ms\n")


async def run(jobs: int, log: str | None) -> None:
    limit, pool = asyncio.Semaphore(jobs), asyncio.Semaphore(POOL)
    out = open(log, "w", encoding="utf-8") if log else None
    try:
        await asyncio.gather(*(case(i, limit, pool, out) for i in range(CASES)))
    finally:
        if out:
            out.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--jobs", type=int, required=True)
    parser.add_argument("--log")
    args = parser.parse_args()
    asyncio.run(run(args.jobs, args.log))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
