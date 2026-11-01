"""Fuzz the spec loader and log harvester: TaskError is the only allowed failure."""

from __future__ import annotations

import random
import shutil

from conctune import task

from .conftest import FIXTURES, REPO

SEEDS = range(300)
ALPHABET = "[]{}=\"'\n #.,()?<>P\\0123456789abcjmsx_-"


def _mutate(text: str, rng: random.Random) -> str:
    chars = list(text)
    for _ in range(rng.randint(1, 6)):
        op = rng.random()
        at = rng.randrange(len(chars) + 1)
        if op < 0.4 and chars:
            del chars[min(at, len(chars) - 1)]
        elif op < 0.8:
            chars.insert(at, rng.choice(ALPHABET))
        elif chars:
            chars[min(at, len(chars) - 1)] = rng.choice(ALPHABET)
    return "".join(chars)


def test_spec_loader_raises_only_task_error(tmp_path):
    sources = [
        (REPO / "examples" / "demo.toml").read_text(),
        (REPO / "examples" / "nvim-tests.toml").read_text(),
    ]
    path = tmp_path / "spec.toml"
    for seed in SEEDS:
        rng = random.Random(seed)
        path.write_text(_mutate(rng.choice(sources), rng))
        try:
            task.load_spec(path)
        except task.TaskError:
            pass


def test_harvester_raises_only_task_error(tmp_path):
    lines = []
    for log in sorted((FIXTURES / "harvest").iterdir()):
        lines += log.read_text().splitlines()
    for seed in SEEDS:
        rng = random.Random(seed)
        work = tmp_path / str(seed)
        work.mkdir()
        mutated = [_mutate(line, rng) if rng.random() < 0.7 else line for line in lines]
        (work / "run.log.shard1").write_text("\n".join(mutated) + "\n")
        spec = task.TaskSpec(
            name="f",
            run_cmd="{python} -c pass {j}",
            cwd=work,
            log_glob="run.log.shard*",
            case_pattern=(
                r"^(?:PASS|FAIL)\s+\[(?P<group>[^\]]+)\]\s+(?P<name>.+?)\s+"
                r"\((?:(?P<ms>\d+)ms|(?P<sec>[\d.]+)sec|(?P<s>[\d.]+)s)\)"
            ),
            boot_pattern=r"ready in (?P<ms>\d+)ms",
        )
        try:
            task.profile_from_log(spec, log=lambda _: None)
        except task.TaskError:
            pass
        shutil.rmtree(work)
