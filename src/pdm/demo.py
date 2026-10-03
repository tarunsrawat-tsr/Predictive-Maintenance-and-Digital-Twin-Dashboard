"""One-command, AWS-free bootstrap for the public demo.

A fresh checkout needs ``make setup && make train`` plus two terminals before the dashboard
shows anything, and it needs an AWS account for the real ingestion path. That is fine for a
developer but useless as a *public* demo - something a stranger should be able to open from a
link and immediately see a populated digital twin.

This module removes every one of those prerequisites:

1. **Model** - reuse ``artifacts/model`` if present, otherwise train one. The real C-MAPSS
   download is attempted first (so the demo shows the genuine benchmark model); if there is no
   network, a deterministic synthetic fleet from :mod:`pdm.synthetic` is used instead. Either
   way the demo starts.
2. **Data** - replay a fleet through the *real* :class:`~pdm.scoring.StreamScorer`, so the store
   the dashboard reads is produced by exactly the same code path as the Lambda.
3. **Liveness** - optionally keep scoring in a background thread, so the console behaves like the
   streaming system rather than a screenshot.

Nothing here imports or calls AWS. Everything is deterministic given ``seed``.

The resulting store is written in place and is *never* mutated by a dashboard action, which is
what makes it safe to expose publicly in read-only mode.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import sys
import threading
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from pdm.cmapss import download
from pdm.config import Settings
from pdm.model import ModelBundle, load_bundle
from pdm.scoring import StreamScorer
from pdm.simulator import FleetSimulator
from pdm.storage.local import SQLiteStore
from pdm.synthetic import write_synthetic_split

log = logging.getLogger("pdm.public_demo")

ROOT = Path(__file__).resolve().parents[2]
REQUIRED_ARTIFACTS = ("metadata.json", "rul_p50.txt", "interval.json", "anomaly.npz")
PROVENANCE_FILE = "demo_provenance.json"

DEFAULT_MODEL_DIR = "artifacts/model"
DEFAULT_DB = "artifacts/pdm_public_demo.sqlite"
DEFAULT_DATA_DIR = "data/cmapss"

#: Demo geometry. 12 machines over ~30 h of virtual history is enough for every dashboard page
#: to be interesting (a mix of healthy/ageing/near-failure assets and a populated alert log).
DEFAULT_MACHINES = 12
#: Kept well below the shortest C-MAPSS FD001 trajectory (128 cycles) so no machine runs to
#: failure while the history is being seeded.
DEFAULT_CYCLES = 90
DEFAULT_CYCLE_SECONDS = 1200.0
#: Virtual span of the seeded history (used by the CLI to pick a cycle length for ``--cycles``).
DEFAULT_HISTORY_HOURS = 30.0
DEFAULT_SEED = 42
DEFAULT_LIVE_SECONDS = 6.0

#: Where each machine starts within the life it has left after the seeded history, as a fraction
#: of that window (``0`` = brand new, ``1`` = about to fail). Staging the fleet this way means
#: the console opens on a realistic spread - a couple of machines in the critical band, a few
#: ageing, the rest comfortable - and the degradation *during* the replay is what produces a
#: believable alert history. It is a presentation choice for simulated data, not a property of
#: the model, and it is derived from each machine's own trajectory length so no machine is
#: pushed past failure (which would trigger a mid-replay overhaul).
DEMO_START_FRACTIONS = (0.96, 0.90, 0.84, 0.72, 0.60, 0.50, 0.44, 0.36, 0.28, 0.20, 0.12, 0.05)


# ---------------------------------------------------------------------------- model provisioning
def bundle_present(model_dir: Path | str) -> bool:
    """True when ``model_dir`` holds a complete, loadable bundle."""
    model_dir = Path(model_dir)
    return all((model_dir / name).exists() for name in REQUIRED_ARTIFACTS)


def _train_module():
    """Import ``ml/train.py`` by path (it is a script, not an installed module)."""
    path = ROOT / "ml" / "train.py"
    spec = importlib.util.spec_from_file_location("pdm_train", path)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise ImportError(f"cannot load training script at {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["pdm_train"] = module
    spec.loader.exec_module(module)
    return module


def read_provenance(model_dir: Path | str) -> dict:
    path = Path(model_dir) / PROVENANCE_FILE
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):  # pragma: no cover - corrupt file
        return {}


def ensure_bundle(
    model_dir: Path | str,
    *,
    data_dir: Path | str,
    dataset: str = "FD001",
    rounds: int = 800,
    window: int = 20,
    allow_download: bool = True,
    synthetic_units: int = 40,
    seed: int = DEFAULT_SEED,
    force: bool = False,
) -> dict:
    """Guarantee a usable model bundle, training one if necessary.

    Returns a provenance dict: ``source`` is ``existing`` | ``cmapss`` | ``synthetic``, and
    ``data_dir`` points at the directory the simulator should replay.
    """
    model_dir = Path(model_dir)
    data_dir = Path(data_dir)
    have_bundle = bundle_present(model_dir) and not force

    # 1. Resolve the trajectory data the simulator will replay: an existing download, a fresh
    #    download, or the deterministic synthetic fleet. This step can never fail the demo.
    data_source = "synthetic"
    if (data_dir / f"train_{dataset}.txt").exists():
        data_source = "cmapss"
    elif allow_download:
        try:
            download(data_dir)
            data_source = "cmapss"
        except Exception as exc:  # network is optional by design
            log.warning("C-MAPSS download unavailable (%s); using synthetic data", exc)

    if data_source == "synthetic":
        data_dir = write_synthetic_split(
            Path(data_dir).parent / "synthetic", dataset=dataset, seed=seed, n_units=synthetic_units
        )
        log.info("no C-MAPSS data available - generated a synthetic fleet in %s", data_dir)

    # 2. Train only when there is nothing to reuse.
    if have_bundle:
        log.info("reusing the model bundle in %s", model_dir)
    else:
        log.info("training a demo model (%s data, rounds=%d) -> %s", data_source, rounds, model_dir)
        _train_module().main(
            [
                "--dataset", dataset,
                "--data-dir", str(data_dir),
                "--out", str(model_dir),
                "--rounds", str(rounds),
                "--window", str(window),
            ]
        )  # fmt: skip

    provenance = {
        "source": "existing" if have_bundle else data_source,
        "data_source": data_source,
        "model_dir": str(model_dir),
        "data_dir": str(data_dir),
    }
    model_dir.mkdir(parents=True, exist_ok=True)
    (model_dir / PROVENANCE_FILE).write_text(json.dumps(provenance, indent=2))
    return provenance


# ---------------------------------------------------------------------------- fleet staging
def start_fractions(n_machines: int) -> list[float]:
    """Spread :data:`DEMO_START_FRACTIONS` over ``n_machines`` (interpolated when counts differ)."""
    base = np.asarray(DEMO_START_FRACTIONS, dtype=float)
    if n_machines == len(base):
        return base.tolist()
    return np.interp(np.linspace(0.0, len(base) - 1, n_machines), np.arange(len(base)), base).tolist()


def stagger_fleet(sim: FleetSimulator, cycles: int, fractions: list[float] | None = None) -> None:
    """Give the seeded fleet a spread of ages instead of drawing every machine young.

    Each machine is placed ``fraction`` of the way through the life it has left *after* the
    seeded history, i.e. ``pos = fraction * (life - cycles)``. Machines placed near 1 are close
    to failure when the demo opens, machines near 0 are barely run in, and because the start
    position is always inside the trajectory no machine fails (and is overhauled) mid-replay.
    """
    fractions = fractions if fractions is not None else start_fractions(len(sim.machines))
    for machine, fraction in zip(sim.machines, fractions, strict=True):
        life = len(machine.rows)
        window = max(0, life - cycles)
        machine.pos = int(max(0, min(fraction * window, life - 1)))


# ---------------------------------------------------------------------------- demo session
@dataclass
class SeedSummary:
    machines: int
    cycles: int
    open_alerts: int
    total_alerts: int
    statuses: dict[str, int]
    seconds: float

    def to_dict(self) -> dict:
        return {
            "machines": self.machines,
            "cycles": self.cycles,
            "open_alerts": self.open_alerts,
            "total_alerts": self.total_alerts,
            "statuses": self.statuses,
            "seconds": round(self.seconds, 1),
        }

    def describe(self) -> str:
        mix = ", ".join(f"{k}={v}" for k, v in sorted(self.statuses.items()))
        return (
            f"{self.machines} machines x {self.cycles} cycles in {self.seconds:.1f}s "
            f"({mix}; {self.open_alerts} open alerts)"
        )


class PublicDemo:
    """A seeded demo store plus an optional in-process live scoring loop."""

    def __init__(
        self,
        *,
        settings: Settings,
        model: ModelBundle,
        store: SQLiteStore,
        sim: FleetSimulator,
        scorer: StreamScorer,
        provenance: dict,
        seed_summary: SeedSummary,
        model_dir: Path,
        db_path: Path,
        data_dir: Path,
    ) -> None:
        self.settings = settings
        self.model = model
        self.store = store
        self.sim = sim
        self.scorer = scorer
        self.provenance = provenance
        self.seed_summary = seed_summary
        self.model_dir = model_dir
        self.db_path = db_path
        self.data_dir = data_dir
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------ construction
    @classmethod
    def bootstrap(
        cls,
        *,
        model_dir: Path | str | None = None,
        data_dir: Path | str | None = None,
        db_path: Path | str | None = None,
        machines: int = DEFAULT_MACHINES,
        cycles: int = DEFAULT_CYCLES,
        cycle_seconds: float = DEFAULT_CYCLE_SECONDS,
        seed: int = DEFAULT_SEED,
        dataset: str = "FD001",
        rounds: int = 800,
        window: int = 20,
        allow_download: bool = True,
        reseed: bool = False,
        live: bool = False,
        live_seconds: float = DEFAULT_LIVE_SECONDS,
    ) -> PublicDemo:
        """Provision (model, data, store) and optionally start the live loop.

        Seeding is skipped when the store already holds machine state, so restarting the demo is
        instant. Pass ``reseed=True`` to rebuild it from scratch.
        """
        model_dir = Path(model_dir or os.environ.get("PDM_MODEL_DIR") or DEFAULT_MODEL_DIR)
        data_dir = Path(data_dir or os.environ.get("PDM_DATA_DIR") or DEFAULT_DATA_DIR)
        db_path = Path(db_path or os.environ.get("PDM_LOCAL_DB") or DEFAULT_DB)

        # The scorer and the dashboard both build Settings from the environment; pin the paths
        # here so a demo launched from one process is seen identically by the other.
        os.environ.setdefault("PDM_BACKEND", "local")
        os.environ["PDM_MODEL_DIR"] = str(model_dir)
        os.environ["PDM_LOCAL_DB"] = str(db_path)

        provenance = ensure_bundle(
            model_dir,
            data_dir=data_dir,
            dataset=dataset,
            rounds=rounds,
            window=window,
            allow_download=allow_download,
            seed=seed,
        )
        replay_dir = Path(provenance["data_dir"])

        settings = Settings.from_env()
        store = SQLiteStore(db_path)
        model = load_bundle(model_dir)

        sim = FleetSimulator(
            n_machines=machines,
            dataset=dataset,
            seed=seed,
            data_dir=str(replay_dir),
            virtual_tick_seconds=cycle_seconds,
            virtual_start=time.time() - cycles * cycle_seconds,
        )
        scorer = StreamScorer(model, store, settings)

        demo = cls(
            settings=settings,
            model=model,
            store=store,
            sim=sim,
            scorer=scorer,
            provenance=provenance,
            seed_summary=SeedSummary(machines=0, cycles=0, open_alerts=0, total_alerts=0, statuses={}, seconds=0.0),
            model_dir=model_dir,
            db_path=db_path,
            data_dir=replay_dir,
        )  # fmt: skip

        existing = store.list_machine_states()
        if existing and not reseed:
            # Resume mid-trajectory so a restarted demo never looks like it went backwards.
            resumed = sim.restore(demo._read_cursor())
            demo.seed_summary = cls._summarize(store, cycles=0, seconds=0.0)
            log.info(
                "reusing demo store %s (%s)%s",
                db_path,
                demo.seed_summary.describe(),
                " - replay resumed from cursor" if resumed else "",
            )
        else:
            if existing:
                # --reseed must rebuild from scratch: scoring on top of an old store would leak
                # the previous run's history into every machine's feature window.
                store.reset()
            stagger_fleet(sim, cycles)
            demo.seed_summary = cls._seed(store, scorer, sim, cycles=cycles)
            demo._write_cursor()
            log.info("seeded demo store %s (%s)", db_path, demo.seed_summary.describe())

        if live:
            demo.start_live(live_seconds)
        return demo

    # ------------------------------------------------------------------ seeding
    @staticmethod
    def _summarize(store: SQLiteStore, *, cycles: int, seconds: float) -> SeedSummary:
        states = store.list_machine_states()
        return SeedSummary(
            machines=len(states),
            cycles=cycles,
            open_alerts=len(store.list_alerts(status="open")),
            total_alerts=len(store.list_alerts()),
            statuses=dict(Counter(s.get("status", "unknown") for s in states)),
            seconds=seconds,
        )

    @classmethod
    def _seed(
        cls, store: SQLiteStore, scorer: StreamScorer, sim: FleetSimulator, *, cycles: int
    ) -> SeedSummary:
        t0 = time.time()
        for batch in sim.stream(max_ticks=cycles):
            scorer.process(batch)
        return cls._summarize(store, cycles=sim.tick, seconds=time.time() - t0)

    # ------------------------------------------------------------------ live loop
    def start_live(self, interval_seconds: float = DEFAULT_LIVE_SECONDS) -> None:
        """Continue replaying on the wall clock in a daemon thread."""
        if self._thread and self._thread.is_alive():
            return
        # Switch the simulator off its virtual clock: live cycles are stamped with real time.
        self.sim.virtual_tick_seconds = 0.0
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._live_loop, args=(interval_seconds,), name="pdm-public-demo-live", daemon=True
        )
        self._thread.start()
        log.info("live demo loop started (one cycle every %.1fs)", interval_seconds)

    def _live_loop(self, interval_seconds: float) -> None:
        while not self._stop.is_set():
            t0 = time.time()
            try:
                self.scorer.process(self.sim.step())
                self._write_cursor()
            except Exception:  # pragma: no cover - a demo must not die on one bad tick
                log.exception("live tick failed")
            self._stop.wait(max(0.0, interval_seconds - (time.time() - t0)))

    def stop_live(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None
        self._write_cursor()

    @property
    def is_live(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    # ------------------------------------------------------------------ replay cursor
    @property
    def cursor_path(self) -> Path:
        return Path(f"{self.db_path}.cursor.json")

    def _read_cursor(self) -> dict:
        try:
            return json.loads(self.cursor_path.read_text())
        except (OSError, ValueError):
            return {}

    def _write_cursor(self) -> None:
        """Atomically persist where each machine is in its trajectory."""
        payload = {"saved_at": int(time.time() * 1000), **self.sim.snapshot()}
        tmp = self.cursor_path.with_suffix(".cursor.json.tmp")
        try:
            tmp.write_text(json.dumps(payload))
            os.replace(tmp, self.cursor_path)
        except OSError:  # pragma: no cover - read-only filesystem: demo still works, just not resumable
            log.debug("could not persist replay cursor to %s", self.cursor_path, exc_info=True)

    # ------------------------------------------------------------------ introspection
    def summary(self) -> dict:
        data = self._summarize(self.store, cycles=self.seed_summary.cycles, seconds=self.seed_summary.seconds)
        return {
            **data.to_dict(),
            "live": self.is_live,
            "model_source": self.provenance.get("source", "unknown"),
            "db_path": str(self.db_path),
            "model_dir": str(self.model_dir),
        }


def bootstrap_from_env(*, live: bool = False, reseed: bool = False, **overrides) -> PublicDemo:
    """Bootstrap using the ``PDM_DEMO_*`` environment variables, with explicit overrides on top."""
    params = {
        "machines": int(os.environ.get("PDM_DEMO_MACHINES", DEFAULT_MACHINES)),
        "cycles": int(os.environ.get("PDM_DEMO_CYCLES", DEFAULT_CYCLES)),
        "cycle_seconds": float(os.environ.get("PDM_DEMO_CYCLE_SECONDS", DEFAULT_CYCLE_SECONDS)),
        "live_seconds": float(os.environ.get("PDM_DEMO_LIVE_SECONDS", DEFAULT_LIVE_SECONDS)),
        "allow_download": os.environ.get("PDM_DEMO_ALLOW_DOWNLOAD", "1") not in ("0", "false", "False"),
    }
    params.update({k: v for k, v in overrides.items() if v is not None})
    return PublicDemo.bootstrap(live=live, reseed=reseed, **params)
