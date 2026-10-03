"""Root entrypoint for hosted deployments (Streamlit Community Cloud, and similar).

Streamlit runs **one** file as the app, and hosted platforms expect it at a well-known path in
the repository root — which is why this file exists. It is a thin launcher for the real console
at ``services/dashboard/app.py``; no logic lives here.

Deploying to Streamlit Community Cloud is then:

    1. share.streamlit.io -> New app -> pick this repo and branch
    2. Main file path: streamlit_app.py     (it will be in the dropdown)
    3. Deploy

No secrets are required: the launcher enables the self-provisioning, read-only public demo by
default (see :mod:`pdm.demo` and ``docs/public-demo.md``), so a fresh deployment comes up with a
populated simulated fleet. Set ``PDM_PUBLIC_DEMO`` to ``0`` — in the environment or in the app's
secrets — to serve the normal operator console instead, which needs a reachable DynamoDB or a
pre-seeded local store.

Locally this behaves exactly like running the dashboard directly:

    streamlit run streamlit_app.py
"""

from __future__ import annotations

import os
import runpy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DASHBOARD = ROOT / "services" / "dashboard"
APP = DASHBOARD / "app.py"

# Mirror the import path that services/dashboard/app.py sets up for itself, so it behaves the
# same whether it is launched directly or through this launcher.
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(DASHBOARD))

# Being reached through the hosted entrypoint implies "show the demo" unless told otherwise.
# This is only a default: an explicit PDM_PUBLIC_DEMO (environment or Streamlit secrets) wins,
# because common.flag() reads the environment first and this is setdefault.
os.environ.setdefault("PDM_HOSTED", "1")
os.environ.setdefault("PDM_BACKEND", "local")
os.environ.setdefault("PDM_MODEL_DIR", str(ROOT / "artifacts" / "model"))
os.environ.setdefault("PDM_LOCAL_DB", str(ROOT / "artifacts" / "pdm_public_demo.sqlite"))
os.environ.setdefault("PDM_DATA_DIR", str(ROOT / "data" / "cmapss"))

runpy.run_path(str(APP), run_name="__main__")
