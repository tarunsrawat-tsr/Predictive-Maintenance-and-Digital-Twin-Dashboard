"""Public demo: self-provisioning, deterministic, resumable and read-only.

These tests never touch the network or the real C-MAPSS download - they exercise the synthetic
fallback and the resume path, which is what a hosted demo actually relies on.
"""

from __future__ import annotations

import re
import sys
import time
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
    resolve_playback,
    stagger_fleet,
    start_fractions,
)
from pdm.scoring import StreamScorer
from pdm.simulator import FleetSimulator
from pdm.storage.local import SQLiteStore
from pdm.synthetic import synthetic_cmapss, write_synthetic_split

ROOT = Path(__file__).resolve().parents[1]
DASH = ROOT / "services" / "dashboard"


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


def test_ensure_bundle_reports_synthetic_when_it_had_to_train(tmp_path, monkeypatch):
    # Hide the committed bundle so this exercises the training path.
    monkeypatch.setattr("pdm.demo.SHIPPED_MODEL_DIR", tmp_path / "no-shipped-bundle")
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


# ---------------------------------------------------------------------------- playback modes
def test_resolve_playback_accepts_the_documented_modes():
    assert resolve_playback(False) == (False, None)
    assert resolve_playback(None) == (False, None)
    assert resolve_playback(True) == (True, "thread")  # legacy alias
    assert resolve_playback("render") == (True, "render")
    assert resolve_playback(" thread ") == (True, "thread")
    with pytest.raises(ValueError):
        resolve_playback("background")


def test_render_mode_starts_no_background_thread(demo_kwargs):
    """A hosted demo must not hold a thread alive: that is what stops a container idling."""
    demo_kwargs["live"] = "render"
    demo = PublicDemo.bootstrap(**demo_kwargs)
    assert demo.playback is True
    assert demo.mode == "render"
    assert demo.is_live is False, "render mode must not start the thread"
    assert demo._thread is None


def test_frozen_demo_never_advances(demo_kwargs):
    demo_kwargs["live"] = False
    demo = PublicDemo.bootstrap(**demo_kwargs)
    assert demo.playback is False
    before = demo.sim.tick
    demo._last_pump = time.time() - 10_000  # pretend a very long time has passed
    assert demo.pump() == 0
    assert demo.sim.tick == before


def test_pump_is_a_no_op_until_a_cycle_is_due(demo_kwargs):
    """Calling it on every rerun must be free: only one timestamp comparison."""
    demo_kwargs["live"] = "render"
    demo = PublicDemo.bootstrap(**demo_kwargs)
    assert demo.pump() == 0, "the first call only arms the timer"
    assert demo.pump() == 0, "and nothing is due immediately afterwards"
    assert demo.sim.tick == demo.seed_summary.cycles, "no cycles consumed while idle"


def test_pump_advances_when_due_and_bounds_catch_up(demo_kwargs):
    demo_kwargs["live"] = "render"
    demo = PublicDemo.bootstrap(**demo_kwargs)
    demo.pump()  # arm

    demo._last_pump = time.time() - demo.live_seconds  # exactly one cycle overdue
    assert demo.pump() == 1

    # Returning after a long absence must not trigger a burst of scoring.
    demo._last_pump = time.time() - demo.live_seconds * 1000
    assert demo.pump(max_catchup=3) == 3


def test_pump_survives_a_failing_tick(demo_kwargs, monkeypatch):
    """A bad tick must never break a page render."""
    demo_kwargs["live"] = "render"
    demo = PublicDemo.bootstrap(**demo_kwargs)
    demo.pump()

    def boom(_batch):
        raise RuntimeError("scoring exploded")

    monkeypatch.setattr(demo.scorer, "process", boom)
    demo._last_pump = time.time() - demo.live_seconds * 5
    assert demo.pump() == 0  # swallows the error, reports nothing scored


# ---------------------------------------------------------------------------- shipped bundle
def test_shipped_bundle_is_complete_and_loadable():
    """Guards the committed demo bundle: a missing file only shows up on a hosted deploy.

    Hosted deployments have ephemeral storage and no training step, so if this bundle is broken
    the public demo cannot start at all.
    """
    from pdm.demo import REQUIRED_ARTIFACTS, SHIPPED_MODEL_DIR

    assert SHIPPED_MODEL_DIR.is_dir(), f"{SHIPPED_MODEL_DIR} should be committed to the repo"
    for name in REQUIRED_ARTIFACTS:
        assert (SHIPPED_MODEL_DIR / name).exists(), f"{name} missing from the shipped bundle"

    from pdm.model import load_bundle

    bundle = load_bundle(SHIPPED_MODEL_DIR)
    metrics = bundle.metadata["metrics"]
    assert bundle.window == metrics["window"]
    # The bundle must be the real C-MAPSS model the README documents, not a synthetic one:
    # a fallback-trained bundle reports a suspiciously low RMSE which would be misleading.
    assert metrics["dataset"] == "FD001"
    assert 12.0 < metrics["test_rmse_capped"] < 17.0, metrics["test_rmse_capped"]


def test_ensure_bundle_installs_shipped_bundle_without_training(tmp_path, synth_data_dir, monkeypatch):
    def _no_training():
        raise AssertionError("training must not run when the committed bundle is available")

    monkeypatch.setattr("pdm.demo._train_module", _no_training)
    model_dir = tmp_path / "fresh-model"
    provenance = ensure_bundle(model_dir, data_dir=synth_data_dir, allow_download=False)

    assert provenance["source"] == "shipped"
    assert (model_dir / "rul_p50.txt").exists()
    assert read_provenance(model_dir)["source"] == "shipped"


def test_install_shipped_bundle_reports_failure_when_absent(tmp_path, monkeypatch):
    monkeypatch.setattr("pdm.demo.SHIPPED_MODEL_DIR", tmp_path / "nope")
    from pdm.demo import install_shipped_bundle

    assert install_shipped_bundle(tmp_path / "dest") is False
    assert not (tmp_path / "dest" / "rul_p50.txt").exists()


def test_frozen_demo_does_not_poll_at_all(tmp_path, monkeypatch):
    """A snapshot must not pay for periodic reruns - that is the whole point of freezing it."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(DASH))
    monkeypatch.setenv("PDM_PUBLIC_DEMO", "1")
    monkeypatch.setenv("PDM_HOSTED", "1")
    monkeypatch.delenv("PDM_DASHBOARD_REFRESH_SECONDS", raising=False)

    monkeypatch.setenv("PDM_PUBLIC_DEMO_LIVE", "1")
    _reload_dashboard_modules()
    import common

    assert common.DEMO_PLAYBACK is True
    assert common.REFRESH_SECONDS == 10  # hosted default when playing
    assert common.REFRESH_EVERY == "10s"

    monkeypatch.setenv("PDM_PUBLIC_DEMO_LIVE", "0")
    _reload_dashboard_modules()
    import common as frozen

    assert frozen.DEMO_PLAYBACK is False
    assert frozen.REFRESH_SECONDS == 0
    assert frozen.REFRESH_EVERY is None, "a frozen demo must not schedule reruns"

    # An explicit interval still wins over the frozen default.
    monkeypatch.setenv("PDM_DASHBOARD_REFRESH_SECONDS", "30")
    _reload_dashboard_modules()
    import common as explicit

    assert explicit.REFRESH_SECONDS == 30


def test_hosted_default_enables_the_public_demo(tmp_path, monkeypatch):
    """A hosted deployment must work with no secrets, but an explicit opt-out still wins."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(DASH))
    monkeypatch.delenv("PDM_PUBLIC_DEMO", raising=False)
    monkeypatch.setenv("PDM_HOSTED", "1")
    _reload_dashboard_modules()
    import common

    assert common.PUBLIC_DEMO is True
    monkeypatch.setenv("PDM_PUBLIC_DEMO", "0")
    _reload_dashboard_modules()
    import common as reloaded

    assert reloaded.PUBLIC_DEMO is False, "explicit PDM_PUBLIC_DEMO must override PDM_HOSTED"


def test_root_entrypoint_renders_and_resolves_every_page(public_demo_env):
    """Hosted platforms run one root file; it must still find all five pages.

    ``st.Page()`` raises at construction when a path cannot be resolved, and relative page paths
    resolve against the *entrypoint's* directory — so a clean render of the root launcher is
    precisely the regression test for pages vanishing on a hosted deploy.
    """
    pytest.importorskip("streamlit")
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(ROOT / "streamlit_app.py"), default_timeout=180)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    assert at.title[0].value.startswith("🏭")
    assert at.metric, "the fleet page should render metrics through the root entrypoint"


def test_hosted_entrypoint_has_deployment_manifests():
    """Streamlit Cloud reads these from the repository root; the app cannot start without them.

    pyproject.toml is not enough: hosted platforms do not install optional extras, so the
    dashboard dependencies must also be listed in requirements.txt.
    """
    requirements = (ROOT / "requirements.txt").read_text()
    for package in ("streamlit", "plotly", "pandas", "lightgbm", "numpy"):
        assert package in requirements, f"{package} missing from the root requirements.txt"
    assert "libgomp1" in (ROOT / "packages.txt").read_text()


def test_packages_txt_names_only_valid_apt_packages():
    """``packages.txt`` is fed to ``apt-get install`` line by line.

    Every line is treated as a package name, so a comment (or anything else that is not a valid
    Debian package name) aborts the install with "Unable to locate package #" and the hosted
    deploy dies during dependency processing. That is why this file carries no commentary — the
    explanation lives here instead.
    """
    lines = (ROOT / "packages.txt").read_text().splitlines()
    assert lines, "packages.txt must name at least one package"
    valid = re.compile(r"^[a-z0-9][a-z0-9+.-]*$")
    for line in lines:
        assert valid.match(line), (
            f"packages.txt line {line!r} is not a valid apt package name; "
            "apt-get would fail on it (comments are not allowed in this file)"
        )


def test_alert_page_allows_acknowledgement_outside_the_public_demo(public_demo_env, monkeypatch):
    """Guard against the read-only change silently disabling the operator console."""
    pytest.importorskip("streamlit")
    from streamlit.testing.v1 import AppTest

    monkeypatch.delenv("PDM_PUBLIC_DEMO", raising=False)
    # The root entrypoint sets this process-wide during its AppTest run.
    monkeypatch.delenv("PDM_HOSTED", raising=False)
    _reload_dashboard_modules()
    at = AppTest.from_file(str(DASH / "views" / "alerts.py"), default_timeout=120)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    # The operator console keeps the interactive control (selection count in the label) rather
    # than the inert variant the public demo renders. It is disabled only because no row is
    # selected yet, which is the pre-existing behaviour.
    acks = [b for b in at.button if "Acknowledge" in b.label]
    assert acks and "(" in acks[0].label
