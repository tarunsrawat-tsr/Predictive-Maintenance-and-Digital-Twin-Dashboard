"""Render every dashboard page headlessly against a populated local store."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from pdm.config import Settings
from pdm.scoring import StreamScorer
from pdm.simulator import FleetSimulator
from pdm.storage.local import SQLiteStore

DASH = Path(__file__).resolve().parents[1] / "services" / "dashboard"
PAGES = ["views/fleet.py", "views/machine.py", "views/alerts.py", "views/maintenance.py", "views/model.py"]


@pytest.fixture(scope="module")
def populated_db(tmp_path_factory, bundle, synth_data_dir) -> Path:
    db = tmp_path_factory.mktemp("dash") / "pdm.sqlite"
    store = SQLiteStore(db)
    scorer = StreamScorer(bundle, store, Settings(backend="local"))
    sim = FleetSimulator(n_machines=4, data_dir=str(synth_data_dir), seed=5, virtual_tick_seconds=60)
    for batch in sim.stream(max_ticks=60):
        scorer.process(batch)
    return db


@pytest.mark.parametrize("page", PAGES)
def test_page_renders_without_exception(page, populated_db, model_dir, monkeypatch):
    pytest.importorskip("streamlit")
    from streamlit.testing.v1 import AppTest

    monkeypatch.setenv("PDM_BACKEND", "local")
    monkeypatch.setenv("PDM_LOCAL_DB", str(populated_db))
    monkeypatch.setenv("PDM_MODEL_DIR", str(model_dir))
    monkeypatch.chdir(DASH)
    monkeypatch.syspath_prepend(str(DASH))
    # common.py caches settings/store per process; make sure this test's env wins.
    import common

    common.settings.clear()
    common.store.clear()
    common.model_bundle.clear()

    at = AppTest.from_file(str(DASH / page), default_timeout=90)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    assert at.title, "page should render a title"


def test_page_handles_empty_store(tmp_path, model_dir, monkeypatch):
    """A brand-new deployment with no telemetry must not crash the fleet page."""
    pytest.importorskip("streamlit")
    from streamlit.testing.v1 import AppTest

    monkeypatch.setenv("PDM_BACKEND", "local")
    monkeypatch.setenv("PDM_LOCAL_DB", str(tmp_path / "empty.sqlite"))
    monkeypatch.setenv("PDM_MODEL_DIR", str(model_dir))
    monkeypatch.chdir(DASH)
    monkeypatch.syspath_prepend(str(DASH))
    import common

    common.settings.clear()
    common.store.clear()
    common.model_bundle.clear()
    at = AppTest.from_file(str(DASH / "views/fleet.py"), default_timeout=90)
    at.run()
    assert not at.exception
    assert at.warning, "expected the 'no machine state yet' notice"
    assert os.environ["PDM_BACKEND"] == "local"
