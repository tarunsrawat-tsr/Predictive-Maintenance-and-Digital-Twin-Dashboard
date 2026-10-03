"""Read-only "public demo" wiring for the dashboard.

When ``PDM_PUBLIC_DEMO=1`` the console bootstraps its own model, data and store (see
:mod:`pdm.demo`) and runs without any external simulator, AWS account or login. The store is
only ever written by the ingestion-side scorer, never by a dashboard interaction, so the
exposed surface is read-only by construction.
"""

from __future__ import annotations

import streamlit as st
from common import flag

# Model provenance -> how it is described to a visitor.
MODEL_LABELS = {
    "shipped": "RUL model trained on NASA C-MAPSS FD001",
    "cmapss": "RUL model trained on NASA C-MAPSS FD001",
    "synthetic": "RUL model trained on a synthetic fallback fleet (no network at build time)",
    "existing": "RUL model loaded from the existing bundle in artifacts/model",
}


def live_enabled() -> bool:
    """Whether the fleet keeps degrading while the page is open (off = frozen snapshot)."""
    return flag("PDM_PUBLIC_DEMO_LIVE", default=True)


def thread_enabled() -> bool:
    """Whether to drive playback from a background thread instead of the render loop.

    Off by default, and deliberately so: a thread that keeps scoring regardless of viewers
    stops the container from ever looking idle, which is exactly what gets an app throttled on
    a shared free tier. ``--headless`` sets ``PDM_PUBLIC_DEMO_THREAD=1`` when it genuinely
    needs ingestion to continue with nobody watching.
    """
    return flag("PDM_PUBLIC_DEMO_THREAD", default=False)


@st.cache_resource(show_spinner="Preparing the public demo: model, fleet replay and store…")
def session():
    """Build (once per process) the self-contained demo session."""
    from pdm.demo import bootstrap_from_env

    if not live_enabled():
        mode: bool | str = False
    else:
        mode = "thread" if thread_enabled() else "render"
    return bootstrap_from_env(live=mode)


def advance() -> int:
    """Advance playback by at most one cycle. Called from the dashboard's data loaders."""
    return session().pump()


def render_banner() -> None:
    """Explain, on every page, what a visitor is looking at."""
    info = session().summary()
    label = MODEL_LABELS.get(info.get("model_source", ""), "simulated fleet")
    liveness = (
        "The fleet keeps degrading while this page is open, and stops the moment nobody is "
        "watching — so the demo costs nothing when idle."
        if info.get("playback")
        else "This is a frozen snapshot; no simulation is running."
    )
    st.markdown(
        f"""
<div class="pdm-public-banner">
  <div class="pdm-public-title">🧪 Public demo — simulated fleet, read-only</div>
  <div class="pdm-public-body">
    {info["machines"]} virtual machines are being replayed through the same scoring pipeline the
    AWS Lambda runs (<b>{label}</b>), with predictions, health states and alerts written to a
    local store. This is a reference implementation on NASA C-MAPSS turbofan data, not a real
    plant and not a production deployment. {liveness}
  </div>
</div>
""",
        unsafe_allow_html=True,
    )
