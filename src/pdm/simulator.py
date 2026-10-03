"""Replay engine: turns C-MAPSS trajectories into a live fleet of machines.

Each simulated machine is bound to one C-MAPSS unit and emits one cycle per tick. When a unit
reaches the end of its recorded trajectory (i.e. it failed in the dataset), the machine is
"overhauled": a new unit is drawn and the cycle counter restarts, so the fleet keeps running
indefinitely and the dashboard shows a mix of young, ageing and near-failure assets.

Transport is pluggable: the engine only yields ``TelemetryMessage`` objects; the MQTT publisher
(services/simulator) and the in-process demo both consume this generator.
"""

from __future__ import annotations

import random
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime

import numpy as np

from pdm.cmapss import iter_units, load_split
from pdm.schema import RAW_COLUMNS, SENSOR_COLUMNS, SETTING_COLUMNS, TelemetryMessage, utc_now_iso

DEFAULT_SITE = "nagoya"
DEFAULT_LINES = ["L1", "L2", "L3"]


@dataclass
class MachineReplay:
    machine_id: str
    site: str
    line: str
    unit: int
    rows: np.ndarray  # (n, 26): unit, cycle, raw...
    pos: int = 0  # index of the next row to emit
    overhauls: int = 0

    @property
    def remaining(self) -> int:
        return len(self.rows) - self.pos


@dataclass
class FleetSimulator:
    n_machines: int = 10
    dataset: str = "FD001"
    split: str = "train"  # "train" has full run-to-failure trajectories -> machines visibly fail
    site: str = DEFAULT_SITE
    lines: list[str] = field(default_factory=lambda: list(DEFAULT_LINES))
    seed: int = 42
    noise_std: float = 0.0  # optional extra gaussian noise (fraction of per-sensor std)
    start_offset: str = "random"  # "random" | "zero"
    machine_prefix: str = "GT"
    data_dir: str | None = None
    # Virtual clock: when > 0, timestamps advance by this many seconds per tick starting at
    # ``virtual_start`` (UTC epoch seconds, default: now). When 0 the wall clock is used.
    virtual_tick_seconds: float = 0.0
    virtual_start: float | None = None
    machines: list[MachineReplay] = field(default_factory=list, init=False)
    tick: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        self._rng = random.Random(self.seed)
        arr = (
            load_split(self.split, self.dataset, self.data_dir)
            if self.data_dir
            else load_split(self.split, self.dataset)
        )
        self._units = {u: rows for u, rows in iter_units(arr)}
        self._pool = list(self._units)
        self._rng.shuffle(self._pool)
        self._sensor_std = arr[:, 2:].std(axis=0)
        if self.virtual_start is None:
            self.virtual_start = time.time()
        for i in range(self.n_machines):
            mid = f"{self.machine_prefix}-{i + 1:03d}"
            line = self.lines[i % len(self.lines)]
            self.machines.append(self._new_replay(mid, line))

    # ------------------------------------------------------------------ helpers
    def _draw_unit(self) -> int:
        if not self._pool:
            self._pool = list(self._units)
            self._rng.shuffle(self._pool)
        return self._pool.pop()

    def _new_replay(self, machine_id: str, line: str, overhauls: int = 0) -> MachineReplay:
        unit = self._draw_unit()
        rows = self._units[unit]
        pos = 0
        if self.start_offset == "random":
            # Start somewhere in the first 80% of life so the fleet is staggered.
            pos = self._rng.randint(0, int(len(rows) * 0.8))
        return MachineReplay(machine_id, self.site, line, unit, rows, pos, overhauls)

    def _now_iso(self) -> str:
        if self.virtual_tick_seconds > 0:
            t = (self.virtual_start or 0.0) + self.tick * self.virtual_tick_seconds
            return datetime.fromtimestamp(t, UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        return utc_now_iso()

    def _to_message(self, m: MachineReplay) -> TelemetryMessage:
        row = m.rows[m.pos]
        raw = row[2:].astype(float).copy()
        if self.noise_std > 0:
            raw += (
                np.random.default_rng(self.tick * 1000 + m.pos).normal(0, self.noise_std, len(raw))
                * self._sensor_std
            )
        settings = {c: float(raw[RAW_COLUMNS.index(c)]) for c in SETTING_COLUMNS}
        sensors = {c: float(raw[RAW_COLUMNS.index(c)]) for c in SENSOR_COLUMNS}
        return TelemetryMessage(
            machine_id=m.machine_id,
            site=m.site,
            line=m.line,
            ts=self._now_iso(),
            cycle=int(row[1]) - int(m.rows[0, 1]) + 1,
            settings=settings,
            sensors=sensors,
        )

    # ------------------------------------------------------------------ public API
    def step(self) -> list[TelemetryMessage]:
        """Advance every machine by one cycle and return the messages to publish."""
        out = []
        for i, m in enumerate(self.machines):
            if m.pos >= len(m.rows):
                # Failure reached -> overhaul with a fresh unit, restart at cycle 1.
                fresh = self._new_replay(m.machine_id, m.line, m.overhauls + 1)
                fresh.pos = 0
                self.machines[i] = m = fresh
            out.append(self._to_message(m))
            m.pos += 1
        self.tick += 1
        return out

    # ------------------------------------------------------------------ resume support
    def snapshot(self) -> dict:
        """Serialisable replay cursor: where each machine is in its trajectory.

        Lets a long-running replay survive a process restart (the public demo persists this
        next to its store so a visitor never sees cycle counters jump backwards).
        """
        return {
            "tick": self.tick,
            "machines": [
                {
                    "machine_id": m.machine_id,
                    "site": m.site,
                    "line": m.line,
                    "unit": m.unit,
                    "pos": m.pos,
                    "overhauls": m.overhauls,
                }
                for m in self.machines
            ],
        }

    def restore(self, cursor: dict) -> bool:
        """Resume from a :meth:`snapshot`. Returns False if the cursor cannot be applied.

        A stale or mismatched cursor (different fleet size or dataset) is ignored rather than
        raising, so callers can simply reseed.
        """
        entries = (cursor or {}).get("machines") or []
        if len(entries) != len(self.machines):
            return False
        restored: list[MachineReplay] = []
        for current, entry in zip(self.machines, entries, strict=True):
            unit = entry.get("unit")
            if unit not in self._units:
                return False
            rows = self._units[unit]
            pos = int(entry.get("pos", 0))
            if not 0 <= pos <= len(rows):
                return False
            restored.append(
                MachineReplay(
                    current.machine_id,
                    current.site,
                    current.line,
                    int(unit),
                    rows,
                    pos,
                    int(entry.get("overhauls", 0)),
                )
            )
        self.machines = restored
        self.tick = int(cursor.get("tick", 0))
        # Keep the draw pool free of units that are already running.
        in_use = {m.unit for m in self.machines}
        self._pool = [u for u in self._pool if u not in in_use]
        return True

    def true_rul(self, machine_id: str) -> int | None:
        """Ground-truth RUL of the current unit (for evaluation/demo overlays only)."""
        for m in self.machines:
            if m.machine_id == machine_id:
                return max(0, len(m.rows) - m.pos)
        return None

    def stream(self, max_ticks: int | None = None) -> Iterator[list[TelemetryMessage]]:
        n = 0
        while max_ticks is None or n < max_ticks:
            yield self.step()
            n += 1
