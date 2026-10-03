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
    "cmapss": "RUL model trained on NASA C-MAPSS FD001",
    "synthetic": "RUL model trained on a synthetic fallback fleet (no network at build time)",
    "existing": "RUL model loaded from the existing bundle in artifacts/model",
}


def live_enabled() -> bool:
    """Whether the fleet keeps degrading while the page is open (off = frozen snapshot)."""
    return flag("PDM_PUBLIC_DEMO_LIVE", default=True)


@st.cache_resource(show_spinner="Preparing the public demo: model, fleet replay and store…")
def session():
    """Build (once per process) the self-contained demo session."""
    from pdm.demo import bootstrap_from_env

    return bootstrap_from_env(live=live_enabled())


def render_banner() -> None:
    """Explain, on every page, what a visitor is looking at."""
    info = session().summary()
    label = MODEL_LABELS.get(info.get("model_source", ""), "simulated fleet")
    liveness = (
        "The fleet keeps degrading in real time."
        if info.get("live")
        else "This is a frozen snapshot; restart the demo to resume playback."
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
