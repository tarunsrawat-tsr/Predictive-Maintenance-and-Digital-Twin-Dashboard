from __future__ import annotations

import os
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
for p in ("src", "services/scorer", "services/dashboard", "services/simulator", "ml"):
    sys.path.insert(0, str(ROOT / p))

from pdm.synthetic import synthetic_cmapss  # noqa: E402

os.environ.setdefault("AWS_DEFAULT_REGION", "ap-northeast-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")


@pytest.fixture(scope="session")
def synth_train() -> np.ndarray:
    """A small C-MAPSS-like run-to-failure array (the generator the public demo falls back to)."""
    return synthetic_cmapss(n_units=12, seed=0)


@pytest.fixture(scope="session")
def synth_data_dir(tmp_path_factory, synth_train) -> Path:
    """Write the synthetic data in the C-MAPSS file layout so loaders/simulators can use it."""
    d = tmp_path_factory.mktemp("cmapss")
    np.savetxt(d / "train_FD001.txt", synth_train, fmt="%.4f")
    # test split: truncate each unit and emit RUL
    units = synth_train[:, 0]
    test_rows, ruls = [], []
    rng = np.random.default_rng(1)
    for u in np.unique(units):
        rows = synth_train[units == u]
        cut = int(rng.integers(40, len(rows) - 5))
        test_rows.append(rows[:cut])
        ruls.append(len(rows) - cut)
    np.savetxt(d / "test_FD001.txt", np.vstack(test_rows), fmt="%.4f")
    np.savetxt(d / "RUL_FD001.txt", np.array(ruls), fmt="%d")
    return d


@pytest.fixture(scope="session")
def model_dir(tmp_path_factory, synth_data_dir) -> Path:
    """Train a tiny bundle on the synthetic data (fast) and return its directory."""
    from train import main as train_main

    out = tmp_path_factory.mktemp("model") / "bundle"
    train_main(["--data-dir", str(synth_data_dir), "--out", str(out), "--rounds", "60", "--window", "10"])
    return out


@pytest.fixture(scope="session")
def bundle(model_dir):
    from pdm.model import ModelBundle

    return ModelBundle.load(model_dir)
