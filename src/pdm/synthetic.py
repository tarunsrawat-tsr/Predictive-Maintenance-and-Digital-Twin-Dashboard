"""Deterministic synthetic C-MAPSS data.

The real dataset is a ~15 MB download (see :mod:`pdm.cmapss`) and is deliberately not vendored
into the repository. The public demo still has to work with no network access and no prior
``make train``, so this module fabricates a small run-to-failure fleet in exactly the NASA file
layout:

    unit, cycle, op1..op3, s1..s21

The qualitative behaviour is preserved: sensors drift monotonically, and the drift accelerates
as a unit approaches failure, so the RUL model and the Mahalanobis anomaly detector both see a
real degradation signal. Operating settings are held constant, mirroring FD001's single
operating condition.

This is demo scaffolding, not data. Numbers derived from it are only meaningful for exercising
the pipeline end to end; use :func:`pdm.cmapss.download` for the real benchmark.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from pdm.cmapss import COLUMNS

__all__ = ["synthetic_cmapss", "write_synthetic_split"]


def synthetic_cmapss(n_units: int = 40, min_len: int = 120, max_len: int = 220, seed: int = 0) -> np.ndarray:
    """Generate a small C-MAPSS-like run-to-failure array with monotone sensor drift.

    Returns an array shaped ``(rows, len(COLUMNS))`` using the dataset column layout.
    """
    rng = np.random.default_rng(seed)
    rows = []
    base = rng.normal(500, 50, size=24)
    base[:3] = [0.0, 0.0, 100.0]  # operating settings
    for u in range(1, n_units + 1):
        n = int(rng.integers(min_len, max_len))
        drift = rng.normal(0, 0.02, size=24)
        for c in range(1, n + 1):
            frac = c / n
            raw = base + drift * (frac**2) * 200 + rng.normal(0, 0.3, size=24)
            raw[:3] = [0.0, 0.0, 100.0]
            rows.append([u, c, *raw])
    arr = np.array(rows, dtype=float)
    assert arr.shape[1] == len(COLUMNS)
    return arr


def write_synthetic_split(
    directory: Path | str,
    *,
    n_units: int = 40,
    seed: int = 0,
    dataset: str = "FD001",
) -> Path:
    """Materialise a synthetic fleet as ``train/test/RUL_<dataset>.txt`` in ``directory``.

    The result is indistinguishable, to :mod:`pdm.cmapss` and :mod:`pdm.simulator`, from a real
    download - which is what lets the demo fall back to it transparently.
    """
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    train_path = directory / f"train_{dataset}.txt"
    if train_path.exists():
        return directory

    train = synthetic_cmapss(n_units=n_units, seed=seed)
    np.savetxt(train_path, train, fmt="%.4f")

    # Test split: truncate each unit before failure and record the true remaining life.
    units = train[:, 0]
    rng = np.random.default_rng(seed + 1)
    test_rows, ruls = [], []
    for u in np.unique(units):
        rows = train[units == u]
        cut = int(rng.integers(40, len(rows) - 5))
        test_rows.append(rows[:cut])
        ruls.append(len(rows) - cut)
    np.savetxt(directory / f"test_{dataset}.txt", np.vstack(test_rows), fmt="%.4f")
    np.savetxt(directory / f"RUL_{dataset}.txt", np.array(ruls), fmt="%d")
    return directory
