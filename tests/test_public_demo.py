"""Public demo: self-provisioning, deterministic, resumable and read-only.

These tests never touch the network or the real C-MAPSS download - they exercise the synthetic
fallback and the resume path, which is what a hosted demo actually relies on.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

from pdm.cmapss import COLUMNS
from pdm.config import Settings
from pdm.demo import (
    DEMO_START_FRACTIONS,
    PublicDemo,
    ensure_bundle,
    read_provenance,
    stagger_fleet,
    start_fractions,
)
from pdm.scoring import StreamScorer
from pdm.simulator import FleetSimulator
from pdm.storage.local import SQLiteStore
from pdm.synthetic import synthetic_cmapss, write_synthetic_split

DASH = Path(__file__).resolve().parents[1] / "services" / "dashboard"


# ---------------------------------------------------------------------------- synthetic data
def test_synthetic_cmapss_matches_dataset_layout():
    arr = synthetic_cmapss(n_units=5, seed=0)
    assert arr.shape[1] == len(COLUMNS)
    assert set(np.unique(arr[:, 0])) == {1, 2, 3, 4, 5}
    # Operating settings are constant in FD001 and must stay constant here too.
    assert np.allclose(arr[:, 2:5], [0.0, 0.0, 100.0])

    # Sensors drift away from their as-new values, and the drift accelerates with age
    # (the generator scales it by frac**2), which is what gives the model a signal to learn.
    unit = arr[arr[:, 0] == 1]
    n = len(unit)
    cumulative = [np.abs(unit[int(k * (n - 1) / 4), 5:] - unit[0, 5:]).mean() for k in range(4)]
    assert cumulative == sorted(cumulative), cumulative
    assert cumulative[-1] > 1.0, cumulative


def test_write_synthetic_split_is_idempotent_and_loadable(tmp_path):
    from pdm.cmapss import load_rul, load_split

    d = write_synthetic_split(tmp_path / "syn", n_units=6, seed=3)
    assert (d / "train_FD001.txt").exists()
    train = load_split("train", "FD001", d)
    assert train.shape[1] == len(COLUMNS)
    assert len(load_rul("FD001", d)) == 6
    # A second call must not regenerate (the demo may call it on every boot).
    before = (d / "train_FD001.txt").stat().st_mtime_ns
    assert write_synthetic_split(d, n_units=6, seed=3) == d
    assert (d / "train_FD001.txt").stat().st_mtime_ns == before


# ---------------------------------------------------------------------------- fleet staging
def test_start_fractions_interpolate_to_any_fleet_size():
    assert start_fractions(len(DEMO_START_FRACTIONS)) == pytest.approx(list(DEMO_START_FRACTIONS))
    assert len(start_fractions(5)) == 5
    assert len(start_fractions(30)) == 30
    fr = start_fractions(30)
    assert all(0.0 <= v <= 1.0 for v in fr)
    assert fr == sorted(fr, reverse=True), "oldest machine should be placed first"


def test_stagger_fleet_keeps_machines_inside_their_trajectory(synth_data_dir):
    sim = FleetSimulator(n_machines=6, data_dir=str(synth_data_dir), seed=11)
    stagger_fleet(sim, cycles=30)
    for m in sim.machines:
        assert 0 <= m.pos < len(m.rows)
        # Starting position must leave room for the whole seeded history: a machine that ran off
        # the end of its trajectory mid-replay would be overhauled and reset to cycle 1.
        assert m.pos + 30 <= len(m.rows)


def test_stagger_fleet_spreads_ages(synth_data_dir):
    sim = FleetSimulator(n_machines=6, data_dir=str(synth_data_dir), seed=11)
    stagger_fleet(sim, cycles=30)
    remaining = [len(m.rows) - m.pos for m in sim.machines]
    assert max(remaining) - min(remaining) > 10, "fleet should span a range of ages"


# ---------------------------------------------------------------------------- resume support
def test_simulator_snapshot_round_trip(synth_data_dir):
    sim = FleetSimulator(n_machines=4, data_dir=str(synth_data_dir), seed=5, start_offset="zero")
    for _ in range(7):
        sim.step()
    cursor = sim.snapshot()
    expected = sim.step()

    resumed = FleetSimulator(n_machines=4, data_dir=str(synth_data_dir), seed=999, start_offset="zero")
    assert resumed.restore(cursor) is True
    assert resumed.tick == 7
    actual = resumed.step()
    assert [m.machine_id for m in actual] == [m.machine_id for m in expected]
    assert [m.cycle for m in actual] == [m.cycle for m in expected]
    assert [m.sensors["s3"] for m in actual] == pytest.approx([m.sensors["s3"] for m in expected])


def test_simulator_restore_rejects_stale_cursor(synth_data_dir):
    sim = FleetSimulator(n_machines=4, data_dir=str(synth_data_dir), seed=5)
    assert sim.restore({"machines": [{"unit": 1, "pos": 0}]}) is False  # wrong fleet size
    assert sim.restore({}) is False
    assert sim.restore({"machines": [{"unit": 99999, "pos": 0}] * 4}) is False  # unknown unit
    assert sim.restore({"machines": [{"unit": sim.machines[0].unit, "pos": 10**9}] * 4}) is False


# ---------------------------------------------------------------------------- bootstrap
@pytest.fixture
def demo_kwargs(tmp_path, model_dir, synth_data_dir):
    """A tiny, offline demo: no download, no training (the trained bundle is reused)."""
    return {
        "model_dir": model_dir,
        "data_dir": synth_data_dir,
        "db_path": tmp_path / "demo.sqlite",
        "machines": 4,
        "cycles": 25,
        "seed": 7,
        "allow_download": False,
        "reseed": True,
    }


def test_bootstrap_seeds_a_readable_store(demo_kwargs):
    demo = PublicDemo.bootstrap(**demo_kwargs)
    assert demo.seed_summary.machines == 4
    assert demo.seed_summary.cycles == 25
    states = demo.store.list_machine_states()
    assert len(states) == 4
    for s in states:
        assert s["status"] in {"healthy", "warning", "critical"}
        assert s["rul_p50"] >= 0
        assert s["maintenance_due"]
    assert demo.seed_summary.open_alerts >= 1, "a staged fleet should raise alerts while degrading"


def test_bootstrap_is_deterministic(demo_kwargs):
    first = PublicDemo.bootstrap(**demo_kwargs).store.list_machine_states()
    second = PublicDemo.bootstrap(**demo_kwargs).store.list_machine_states()
    assert [(s["machine_id"], s["status"], s["rul_p50"], s["cycle"]) for s in first] == [
        (s["machine_id"], s["status"], s["rul_p50"], s["cycle"]) for s in second
    ]


def test_bootstrap_reuses_store_and_resumes_replay(demo_kwargs):
    first = PublicDemo.bootstrap(**demo_kwargs)
    first.sim.step()
    first.sim.step()
    first._write_cursor()
    cursor_tick = first.sim.tick
    assert first.cursor_path.exists()

    demo_kwargs["reseed"] = False
    second = PublicDemo.bootstrap(**demo_kwargs)
    assert second.seed_summary.cycles == 0, "existing store must not be reseeded"
    assert second.sim.tick == cursor_tick, "replay should resume where it stopped"


def test_bootstrap_falls_back_to_synthetic_without_network(tmp_path, model_dir):
    """No dataset on disk and downloads disabled -> synthetic fleet + trained model, no error."""
    demo = PublicDemo.bootstrap(
        model_dir=tmp_path / "model",
        data_dir=tmp_path / "absent",
        db_path=tmp_path / "demo.sqlite",
        machines=3,
        cycles=15,
        rounds=25,
        window=10,
        allow_download=False,
        seed=1,
    )
    assert len(demo.store.list_machine_states()) == 3
    assert "synthetic" in demo.provenance["data_dir"]


def test_ensure_bundle_reuses_an_existing_bundle(tmp_path, model_dir, synth_data_dir):
    before = {n: (Path(model_dir) / n).stat().st_mtime_ns for n in ("metadata.json", "rul_p50.txt")}
    provenance = ensure_bundle(model_dir, data_dir=synth_data_dir, allow_download=False)
    assert provenance["source"] == "existing"
    assert provenance["data_source"] == "cmapss"
    assert read_provenance(model_dir)["source"] == "existing"
    # The bundle must be reused, not retrained.
    after = {n: (Path(model_dir) / n).stat().st_mtime_ns for n in ("metadata.json", "rul_p50.txt")}
    assert before == after


def test_ensure_bundle_reports_synthetic_when_it_had_to_train(tmp_path):
    provenance = ensure_bundle(
        tmp_path / "m", data_dir=tmp_path / "none", allow_download=False, rounds=20, window=10
    )
    assert provenance["source"] == "synthetic"
    assert provenance["data_source"] == "synthetic"
    assert Path(provenance["data_dir"], "train_FD001.txt").exists()


# ---------------------------------------------------------------------------- dashboard
def _seed_store(db: Path, bundle, synth_data_dir: Path, *, machines: int = 4, cycles: int = 30) -> None:
    """Populate a store the way the demo does, with the oldest machine staged near failure."""
    store = SQLiteStore(db)
    settings = Settings(backend="local")
    sim = FleetSimulator(
        n_machines=machines,
        dataset="FD001",
        seed=3,
        data_dir=str(synth_data_dir),
        virtual_tick_seconds=600,
    )
    stagger_fleet(sim, cycles, fractions=[0.99, 0.8, 0.45, 0.1][:machines])
    scorer = StreamScorer(bundle, store, settings)
    for batch in sim.stream(max_ticks=cycles):
        scorer.process(batch)


def _reload_dashboard_modules() -> None:
    """Make the dashboard re-read the environment, and drop Streamlit's global caches.

    ``common``/``public_mode`` capture their public-demo flag at import time, and Streamlit
    caches ``store()``/``settings()`` process-wide (keyed by function identity, so a re-import
    still collides). Without this, one test silently reads another test's database.
    """
    import streamlit as st

    for name in ("common", "public_mode"):
        sys.modules.pop(name, None)
    st.cache_resource.clear()
    st.cache_data.clear()


@pytest.fixture
def public_demo_env(tmp_path, model_dir, synth_data_dir, bundle, monkeypatch):
    db = tmp_path / "public.sqlite"
    _seed_store(db, bundle, synth_data_dir)
    assert SQLiteStore(db).list_alerts(status="open"), "fixture needs open alerts to test the guard"

    monkeypatch.setenv("PDM_PUBLIC_DEMO", "1")
    monkeypatch.setenv("PDM_PUBLIC_DEMO_LIVE", "0")  # no background thread inside a test
    monkeypatch.setenv("PDM_BACKEND", "local")
    monkeypatch.setenv("PDM_LOCAL_DB", str(db))
    monkeypatch.setenv("PDM_MODEL_DIR", str(model_dir))
    monkeypatch.setenv("PDM_DATA_DIR", str(synth_data_dir))
    monkeypatch.setenv("PDM_DEMO_MACHINES", "4")
    monkeypatch.setenv("PDM_DEMO_CYCLES", "30")
    monkeypatch.setenv("PDM_DEMO_ALLOW_DOWNLOAD", "0")
    monkeypatch.chdir(DASH)
    monkeypatch.syspath_prepend(str(DASH))
    _reload_dashboard_modules()
    return db


def test_flag_reads_environment_and_defaults(tmp_path, monkeypatch):
    """Hosted runtimes configure the demo through secrets; locally it is an env var."""
    monkeypatch.chdir(tmp_path)  # no .streamlit/secrets.toml here, so secrets lookup must not fail
    monkeypatch.syspath_prepend(str(DASH))
    monkeypatch.delenv("PDM_PUBLIC_DEMO", raising=False)
    _reload_dashboard_modules()
    import common

    assert common.flag("PDM_PUBLIC_DEMO") is False
    assert common.flag("PDM_PUBLIC_DEMO", default=True) is True
    for raw, expected in (("1", True), ("true", True), ("0", False), ("false", False), ("no", False)):
        monkeypatch.setenv("PDM_PUBLIC_DEMO", raw)
        assert common.flag("PDM_PUBLIC_DEMO") is expected, raw


def test_public_demo_page_renders_read_only(public_demo_env, monkeypatch):
    pytest.importorskip("streamlit")
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(DASH / "app.py"), default_timeout=120)
    at.run()
    assert not at.exception, [e.value for e in at.exception]

    banner = "\n".join(m.value for m in at.markdown)
    assert "Public demo" in banner
    assert "read-only" in banner

    # The console's public demo must not be gated behind the operator password.
    assert not at.text_input, "public demo should not render a login form"


def test_public_demo_alert_page_disables_acknowledgement(public_demo_env):
    """Acknowledge is the console's only write path; it must be inert for the public."""
    pytest.importorskip("streamlit")
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(DASH / "views" / "alerts.py"), default_timeout=120)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    assert at.title[0].value.startswith("🚨")

    acks = [b for b in at.button if "Acknowledge" in b.label]
    assert acks, "expected the acknowledge control to be rendered"
    assert all(b.disabled for b in acks), "acknowledgement must be disabled in the public demo"
    # The inert public variant carries no selection count (nothing can be selected).
    assert all("(" not in b.label for b in acks)


def test_alert_page_allows_acknowledgement_outside_the_public_demo(public_demo_env, monkeypatch):
    """Guard against the read-only change silently disabling the operator console."""
    pytest.importorskip("streamlit")
    from streamlit.testing.v1 import AppTest

    monkeypatch.delenv("PDM_PUBLIC_DEMO", raising=False)
    _reload_dashboard_modules()
    at = AppTest.from_file(str(DASH / "views" / "alerts.py"), default_timeout=120)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    # The operator console keeps the interactive control (selection count in the label) rather
    # than the inert variant the public demo renders. It is disabled only because no row is
    # selected yet, which is the pre-existing behaviour.
    acks = [b for b in at.button if "Acknowledge" in b.label]
    assert acks and "(" in acks[0].label
