"""NASA C-MAPSS turbofan degradation dataset loader.

Dataset: A. Saxena and K. Goebel (2008). "Turbofan Engine Degradation Simulation Data Set",
NASA Prognostics Data Repository, NASA Ames Research Center.

Each sub-dataset (FD001..FD004) ships as whitespace-separated text files:
    train_FD00X.txt  run-to-failure trajectories (unit, cycle, op1..op3, s1..s21)
    test_FD00X.txt   trajectories truncated before failure
    RUL_FD00X.txt    true remaining useful life at the last cycle of each test unit
"""

from __future__ import annotations

import io
import os
import urllib.request
import zipfile
from pathlib import Path

import numpy as np

from pdm.schema import RAW_COLUMNS

COLUMNS = ["unit", "cycle", *RAW_COLUMNS]

# Public mirrors of the dataset. The first is a GitHub repo that vendors the original NASA zip
# contents; the second is the NASA Open Data portal download.
MIRRORS = [
    "https://codeload.github.com/hankroark/Turbofan-Engine-Degradation/zip/refs/heads/master",
    "https://data.nasa.gov/download/ff5v-kuh6/application%2Fzip",
]

DEFAULT_DATA_DIR = Path(os.environ.get("PDM_DATA_DIR", "data/cmapss"))


def download(data_dir: Path | str = DEFAULT_DATA_DIR, force: bool = False) -> Path:
    """Download and extract the C-MAPSS text files into ``data_dir`` (idempotent)."""
    data_dir = Path(data_dir)
    if not force and (data_dir / "train_FD001.txt").exists():
        return data_dir
    data_dir.mkdir(parents=True, exist_ok=True)
    last_err: Exception | None = None
    for url in MIRRORS:
        try:
            with urllib.request.urlopen(url, timeout=120) as resp:  # noqa: S310
                blob = resp.read()
            with zipfile.ZipFile(io.BytesIO(blob)) as zf:
                for member in zf.namelist():
                    name = Path(member).name
                    if name.endswith(".txt") and ("FD00" in name or name == "readme.txt"):
                        (data_dir / name).write_bytes(zf.read(member))
            if (data_dir / "train_FD001.txt").exists():
                return data_dir
        except Exception as exc:  # pragma: no cover - network dependent
            last_err = exc
    raise RuntimeError(f"Could not download C-MAPSS from any mirror: {last_err}")


def _read_txt(path: Path) -> np.ndarray:
    arr = np.loadtxt(path)
    if arr.ndim == 1:
        arr = arr[None, :]
    return arr[:, : len(COLUMNS)]


def load_split(split: str, dataset: str = "FD001", data_dir: Path | str = DEFAULT_DATA_DIR) -> np.ndarray:
    """Return a float array shaped (rows, 26) with columns ``COLUMNS`` for ``split`` in {train,test}."""
    data_dir = Path(data_dir)
    path = data_dir / f"{split}_{dataset}.txt"
    if not path.exists():
        download(data_dir)
    return _read_txt(path)


def load_rul(dataset: str = "FD001", data_dir: Path | str = DEFAULT_DATA_DIR) -> np.ndarray:
    """True RUL for each unit in the test split (ordered by unit id)."""
    path = Path(data_dir) / f"RUL_{dataset}.txt"
    if not path.exists():
        download(data_dir)
    return np.loadtxt(path).astype(float).ravel()


def add_rul_labels(train: np.ndarray, cap: float | None = 125.0) -> np.ndarray:
    """Return per-row RUL for a run-to-failure ``train`` array (RUL = max_cycle - cycle).

    With ``cap`` the target becomes piecewise-linear (flat at ``cap`` early in life), the
    standard formulation in C-MAPSS literature: degradation is not observable early on.
    """
    units = train[:, 0]
    cycles = train[:, 1]
    rul = np.empty_like(cycles)
    for u in np.unique(units):
        mask = units == u
        rul[mask] = cycles[mask].max() - cycles[mask]
    if cap is not None:
        rul = np.minimum(rul, cap)
    return rul


def iter_units(arr: np.ndarray):
    """Yield (unit_id, rows) with rows sorted by cycle, for each unit in ``arr``."""
    units = arr[:, 0]
    for u in np.unique(units):
        rows = arr[units == u]
        rows = rows[np.argsort(rows[:, 1])]
        yield int(u), rows
