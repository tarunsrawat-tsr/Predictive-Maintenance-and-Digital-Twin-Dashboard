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

from common import REFRESH_SECONDS, inject_css, require_login, settings  # noqa: E402

st.set_page_config(
    page_title="PdM Digital Twin Console",
    page_icon="🏭",
    layout="wide",
    initial_sidebar_state="expanded",
)
inject_css()
require_login()

pages = [
    st.Page("views/fleet.py", title="Fleet overview", icon="🏭", default=True),
    st.Page("views/machine.py", title="Machine twin", icon="⚙️"),
    st.Page("views/alerts.py", title="Alerts", icon="🚨"),
    st.Page("views/maintenance.py", title="Maintenance & ROI", icon="🗓️"),
    st.Page("views/model.py", title="Model card", icon="🧠"),
]

with st.sidebar:
    st.markdown("### 🏭 PdM Digital Twin")
    s = settings()
    backend = "AWS DynamoDB" if s.backend == "dynamodb" else "local demo store"
    st.caption(f"Data source: **{backend}** · region `{s.aws_region}`")
    st.caption(f"Auto-refresh every {REFRESH_SECONDS}s")
    if os.environ.get("PDM_DEMO_MODE"):
        st.info(
            "Demo mode: an in-process simulator is replaying NASA C-MAPSS engines through the scorer.",
            icon="🧪",
        )

nav = st.navigation(pages)
nav.run()
