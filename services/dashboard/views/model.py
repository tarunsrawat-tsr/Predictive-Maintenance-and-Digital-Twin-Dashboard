from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from charts import _layout
from common import model_bundle, settings

from pdm.features import N_FEATURES
from pdm.schema import INFORMATIVE_SENSORS

st.title("🧠 Model card")

bundle = model_bundle()
if bundle is None:
    st.warning("Model bundle not available to the dashboard (set PDM_MODEL_DIR or PDM_MODEL_S3_URI).")
    st.stop()

meta = bundle.metadata
metrics = meta.get("metrics", {})
th = settings().thresholds

st.markdown(
    f"""
**Task.** Predict remaining useful life (RUL, in cycles) and flag anomalous behaviour for rotating equipment from 21 sensor channels.

**Training data.** NASA C-MAPSS `{meta.get("dataset", "?")}` run-to-failure trajectories
({metrics.get("n_train_units", "?")} engines, {metrics.get("n_train_rows", "?"):,} cycles). Trained {str(meta.get("trained_at", ""))[:19]} UTC.

**Features.** {N_FEATURES} features: cycle count + last / rolling-mean / rolling-std / slope over a {bundle.window}-cycle window for
{len(INFORMATIVE_SENSORS)} informative sensors. The identical NumPy code runs offline and inside the Lambda, so there is no training/serving skew.

**RUL model.** LightGBM regressor ({metrics.get("best_iteration", "?")} trees) on a piecewise-linear target capped at {bundle.rul_cap:.0f} cycles.
**Uncertainty.** Split-conformal 80% interval (p10–p90) calibrated on {metrics.get("n_calibration_units", "?")} held-out engines, bucketed by predicted RUL.
**Anomaly model.** Mahalanobis distance to a healthy baseline (first 35% of life); score 1.0 = 99th percentile of healthy data.
"""
)

c = st.columns(5)
c[0].metric(
    "Test RMSE (capped)",
    f"{metrics.get('test_rmse_capped', float('nan')):.1f} cycles",
    help="Official FD001 test protocol: last cycle of each of 100 test engines vs. true RUL (capped at 125).",
)
c[1].metric("Test RMSE (uncapped)", f"{metrics.get('test_rmse_uncapped', float('nan')):.1f}")
c[2].metric(
    "NASA PHM08 score",
    f"{metrics.get('test_nasa_score_capped', float('nan')):,.0f}",
    help="Asymmetric score; late predictions are penalised more. Lower is better.",
)
c[3].metric(
    "Interval coverage (test)",
    f"{100 * metrics.get('test_coverage_p10_p90', float('nan')):.0f}%",
    delta="target 80%",
    delta_color="off",
)
c[4].metric(
    "Anomaly: early vs late life",
    f"{metrics.get('anomaly_score_early_life_mean', 0):.2f} → {metrics.get('anomaly_score_end_of_trace_mean', 0):.2f}",
)

left, right = st.columns(2)
with left:
    imp = pd.DataFrame(metrics.get("top_features_gain", []))
    if not imp.empty:
        fig = go.Figure(
            go.Bar(x=imp["gain"][::-1], y=imp["feature"][::-1], orientation="h", marker_color="#38bdf8")
        )
        fig.update_xaxes(title="split gain")
        st.plotly_chart(
            _layout(fig, 420, title="Top features (gain)"), width="stretch", config={"displayModeBar": False}
        )
with right:
    ci = bundle.interval
    edges = [e for e in ci.edges]
    labels = [
        f"{int(edges[i])}–{int(edges[i + 1]) if edges[i + 1] < 1e8 else '∞'}" for i in range(len(ci.lo))
    ]
    fig = go.Figure()
    fig.add_trace(go.Bar(x=labels, y=ci.hi, name="p90 offset", marker_color="#22c55e"))
    fig.add_trace(go.Bar(x=labels, y=ci.lo, name="p10 offset", marker_color="#ef4444"))
    fig.update_layout(barmode="relative")
    fig.update_xaxes(title="predicted RUL bucket (cycles)")
    fig.update_yaxes(title="offset added to p50")
    st.plotly_chart(
        _layout(fig, 420, title="Conformal interval offsets by RUL bucket"),
        width="stretch",
        config={"displayModeBar": False},
    )

st.subheader("Operating thresholds")
st.table(
    pd.DataFrame(
        {
            "rule": [
                "RUL p50 < warning",
                "RUL p50 < critical",
                "anomaly ≥ warning",
                "anomaly ≥ critical",
                "maintenance deadline",
            ],
            "value": [
                f"{th.rul_warning:.0f} cycles",
                f"{th.rul_critical:.0f} cycles",
                f"{th.anomaly_warning:.1f}",
                f"{th.anomaly_critical:.1f}",
                f"p10 RUL − {th.safety_margin_cycles:.0f} cycles",
            ],
        }
    )
)

st.subheader("Limitations & intended use")
st.markdown(
    """
- Trained on **simulated** turbofan degradation under one operating condition (FD001). Deploying to real assets requires
  re-training on that asset class's history; the pipeline (features → LightGBM → conformal → thresholds) is asset-agnostic.
- The RUL cap means the model says "≥ 125 cycles", not an exact number, early in life. That is by design: early-life wear is unobservable.
- Conformal intervals are marginal (80% on average), not per-machine guarantees.
- Thresholds encode risk appetite; tune them with maintenance and finance, not data science alone.
"""
)
with st.expander("Raw metadata"):
    st.json({k: v for k, v in meta.items() if k != "feature_names"})
