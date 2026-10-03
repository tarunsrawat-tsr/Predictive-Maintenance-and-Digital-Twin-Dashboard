"""Predictive Maintenance & Digital Twin console (Streamlit).

Run locally against the demo store:   PDM_BACKEND=local streamlit run services/dashboard/app.py
In AWS (ECS Fargate):                 PDM_BACKEND=dynamodb + table names from Terraform outputs.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import streamlit as st

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1] / "src"))

from common import PUBLIC_DEMO, REFRESH_SECONDS, inject_css, require_login, settings  # noqa: E402

st.set_page_config(
    page_title="PdM Digital Twin Console",
    page_icon="🏭",
    layout="wide",
    initial_sidebar_state="expanded",
)
inject_css()

if PUBLIC_DEMO:
    # The public demo is self-contained: it provisions its own model, replay data and store, and
    # is never gated behind the operator password.
    import public_mode  # noqa: E402

    public_mode.session()
else:
    require_login()

# Page paths are anchored to this file rather than passed as relative strings: Streamlit
# resolves a relative st.Page against the *entrypoint* script's directory, so a launcher at the
# repository root (see streamlit_app.py) would not find services/dashboard/views/*.py.
PAGES = [
    (HERE / "views" / "fleet.py", "Fleet overview", "🏭", True),
    (HERE / "views" / "machine.py", "Machine twin", "⚙️", False),
    (HERE / "views" / "alerts.py", "Alerts", "🚨", False),
    (HERE / "views" / "maintenance.py", "Maintenance & ROI", "🗓️", False),
    (HERE / "views" / "model.py", "Model card", "🧠", False),
]

pages = [st.Page(str(path), title=title, icon=icon, default=default) for path, title, icon, default in PAGES]


with st.sidebar:
    st.markdown("### 🏭 PdM Digital Twin")
    s = settings()
    backend = "AWS DynamoDB" if s.backend == "dynamodb" else "local demo store"
    st.caption(f"Data source: **{backend}** · region `{s.aws_region}`")
    st.caption(f"Auto-refresh every {REFRESH_SECONDS}s")
    if PUBLIC_DEMO:
        st.info(
            "Public demo: **read-only**. A simulated C-MAPSS fleet is scored by the real "
            "pipeline in this process; no AWS account or plant data is involved.",
            icon="🧪",
        )
    elif os.environ.get("PDM_DEMO_MODE"):
        st.info(
            "Demo mode: an in-process simulator is replaying NASA C-MAPSS engines through the scorer.",
            icon="🧪",
        )

if PUBLIC_DEMO:
    public_mode.render_banner()

nav = st.navigation(pages)
nav.run()
