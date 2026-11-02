# Development

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

## Checks

CI runs these on Python 3.11 to 3.13, on Linux, macOS, and Windows:

```sh
ruff check . && ruff format --check .
pyright
pytest                       # -m "not slow" skips the end-to-end demo run
```

The suite recovers known `sigma`/`kappa` from synthetic curves, checks the fit
under noise, verifies LPT against Graham's 4/3 bound, compares `model` output
on committed sweep data against a golden report, covers every spec error and
exit code, and fuzzes the spec loader and log harvester.

## Releases

Bump `__version__` in `conctune/__init__.py`, then push a matching `vX.Y.Z`
tag. The release workflow builds the sdist and wheel and attaches them to a
GitHub release.
