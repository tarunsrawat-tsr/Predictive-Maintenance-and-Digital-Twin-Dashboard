"""Plotly figures for the digital-twin dashboard."""

from __future__ import annotations

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from common import STATUS_COLORS

from pdm.schema import SENSOR_META

PLOT_BG = "rgba(0,0,0,0)"
GRID = "#1e293b"
FONT = "#e2e8f0"


def _layout(fig: go.Figure, height: int = 320, **kw) -> go.Figure:
    layout = dict(
        paper_bgcolor=PLOT_BG,
        plot_bgcolor=PLOT_BG,
        font=dict(color=FONT, size=12),
        margin=dict(l=10, r=10, t=36, b=10),
        height=height,
        legend=dict(orientation="h", y=1.08, x=0),
    )
    layout.update(kw)
    fig.update_layout(**layout)
    fig.update_xaxes(gridcolor=GRID, zeroline=False)
    fig.update_yaxes(gridcolor=GRID, zeroline=False)
    return fig


# ---------------------------------------------------------------------- fleet level
def plant_floor(states: pd.DataFrame) -> go.Figure:
    """A schematic plant floor: one lane per production line, machines as nodes."""
    fig = go.Figure()
    if states.empty:
        return _layout(fig, 260, title="Plant floor")
    lines = sorted(states["line"].unique())
    lane_h = 1.0
    for i, line in enumerate(lines):
        y = -i * lane_h
        fig.add_shape(
            type="rect", x0=-0.6, x1=max(6, states["line"].value_counts().max()) - 0.4, y0=y - 0.42, y1=y + 0.42,
            line=dict(color=GRID), fillcolor="rgba(30,41,59,0.45)", layer="below",
        )  # fmt: skip
        fig.add_annotation(
            x=-0.55,
            y=y + 0.32,
            text=f"<b>Line {line}</b>",
            showarrow=False,
            xanchor="left",
            font=dict(size=11, color="#94a3b8"),
        )
    for line_idx, line in enumerate(lines):
        sub = states[states["line"] == line].reset_index(drop=True)
        y = -line_idx * lane_h
        fig.add_trace(
            go.Scatter(
                x=sub.index,
                y=[y] * len(sub),
                mode="markers+text",
                text=sub["machine_id"],
                textposition="bottom center",
                textfont=dict(size=10, color=FONT),
                marker=dict(
                    size=18 + 22 * (sub["health_index"].fillna(0) / 100.0),
                    color=[STATUS_COLORS.get(s, STATUS_COLORS["unknown"]) for s in sub["status"]],
                    line=dict(width=2, color="#0b1220"),
                    symbol="hexagon",
                ),
                customdata=np.stack(
                    [
                        sub["status"],
                        sub["health_index"],
                        sub["rul_p50"],
                        sub["rul_p10"],
                        sub["rul_p90"],
                        sub["anomaly_score"],
                        sub["cycle"],
                    ],
                    axis=1,
                ),
                hovertemplate=(
                    "<b>%{text}</b><br>status: %{customdata[0]}<br>health: %{customdata[1]:.0f}%"
                    "<br>RUL p50: %{customdata[2]:.0f} (p10 %{customdata[3]:.0f} – p90 %{customdata[4]:.0f})"
                    "<br>anomaly: %{customdata[5]:.2f}<br>cycle: %{customdata[6]}<extra></extra>"
                ),
                showlegend=False,
            )
        )
    fig.update_xaxes(visible=False)
    fig.update_yaxes(visible=False, range=[-(len(lines) - 1) * lane_h - 0.7, 0.7])
    return _layout(
        fig, 90 + 95 * len(lines), title="Plant floor · digital twin (marker size = health, colour = status)"
    )


def fleet_rul_bars(states: pd.DataFrame, warn: float, crit: float) -> go.Figure:
    fig = go.Figure()
    if states.empty:
        return _layout(fig, 300, title="Remaining useful life by machine")
    df = states.sort_values("rul_p50")
    fig.add_trace(
        go.Bar(
            x=df["rul_p50"],
            y=df["machine_id"],
            orientation="h",
            marker_color=[STATUS_COLORS.get(s, "#64748b") for s in df["status"]],
            error_x=dict(
                type="data",
                symmetric=False,
                array=(df["rul_p90"] - df["rul_p50"]).clip(lower=0),
                arrayminus=(df["rul_p50"] - df["rul_p10"]).clip(lower=0),
                color="#94a3b8",
                thickness=1.2,
            ),  # fmt: skip
            hovertemplate="<b>%{y}</b><br>RUL p50 %{x:.0f} cycles<extra></extra>",
        )
    )
    fig.add_vline(
        x=crit,
        line=dict(color=STATUS_COLORS["critical"], dash="dot"),
        annotation_text="critical",
        annotation_position="top",
    )
    fig.add_vline(
        x=warn,
        line=dict(color=STATUS_COLORS["warning"], dash="dot"),
        annotation_text="warning",
        annotation_position="top",
    )
    fig.update_xaxes(title="predicted RUL (cycles) with p10–p90 band")
    return _layout(fig, max(260, 26 * len(df) + 80), title="Remaining useful life by machine")


# ---------------------------------------------------------------------- machine level
def rul_trajectory(tele: pd.DataFrame, warn: float, crit: float, cap: float, x: str = "cycle") -> go.Figure:
    fig = go.Figure()
    if tele.empty:
        return _layout(fig, 320, title="RUL prediction")
    fig.add_trace(
        go.Scatter(x=tele[x], y=tele["rul_p90"], line=dict(width=0), showlegend=False, hoverinfo="skip")
    )
    fig.add_trace(
        go.Scatter(
            x=tele[x],
            y=tele["rul_p10"],
            fill="tonexty",
            fillcolor="rgba(56,189,248,0.18)",
            line=dict(width=0),
            name="p10–p90 (80% conformal band)",
            hoverinfo="skip",
        )  # fmt: skip
    )
    fig.add_trace(
        go.Scatter(x=tele[x], y=tele["rul_p50"], name="RUL p50", line=dict(color="#38bdf8", width=2.5))
    )
    fig.add_trace(
        go.Scatter(
            x=tele[x],
            y=tele["health_index"],
            name="health index (%)",
            yaxis="y2",
            line=dict(color="#a78bfa", width=1.5, dash="dot"),
        )
    )
    fig.add_hline(y=warn, line=dict(color=STATUS_COLORS["warning"], dash="dot"))
    fig.add_hline(y=crit, line=dict(color=STATUS_COLORS["critical"], dash="dot"))
    fig.update_yaxes(title="RUL (cycles)", range=[0, cap * 1.05])
    fig.update_layout(
        yaxis2=dict(title="health %", overlaying="y", side="right", range=[0, 105], showgrid=False)
    )
    fig.update_xaxes(title=x)
    return _layout(fig, 340, title="Remaining useful life · prediction over time")


def anomaly_timeline(tele: pd.DataFrame, warn: float, crit: float, x: str = "cycle") -> go.Figure:
    fig = go.Figure()
    if tele.empty:
        return _layout(fig, 260, title="Anomaly score")
    colors = np.where(
        tele["anomaly_score"] >= crit,
        STATUS_COLORS["critical"],
        np.where(tele["anomaly_score"] >= warn, STATUS_COLORS["warning"], "#38bdf8"),
    )
    fig.add_trace(go.Bar(x=tele[x], y=tele["anomaly_score"], marker_color=colors, name="anomaly score"))
    fig.add_hline(y=warn, line=dict(color=STATUS_COLORS["warning"], dash="dot"), annotation_text="warning")
    fig.add_hline(y=crit, line=dict(color=STATUS_COLORS["critical"], dash="dot"), annotation_text="critical")
    fig.update_yaxes(title="Mahalanobis / healthy p99")
    fig.update_xaxes(title=x)
    return _layout(fig, 260, title="Anomaly score · distance from healthy baseline")


def sensor_trends(tele: pd.DataFrame, sensors: list[str], x: str = "cycle") -> go.Figure:
    from plotly.subplots import make_subplots

    n = max(1, len(sensors))
    cols = 2 if n > 1 else 1
    rows = int(np.ceil(n / cols))
    titles = [f"{s} · {SENSOR_META[s]['name']} ({SENSOR_META[s]['unit']})" for s in sensors]
    fig = make_subplots(
        rows=rows, cols=cols, subplot_titles=titles, vertical_spacing=0.12, horizontal_spacing=0.08
    )
    for i, s in enumerate(sensors):
        r, c = divmod(i, cols)
        if s not in tele:
            continue
        fig.add_trace(
            go.Scatter(
                x=tele[x],
                y=tele[s],
                mode="lines",
                line=dict(width=1, color="#64748b"),
                name=s,
                showlegend=False,
            ),
            row=r + 1,
            col=c + 1,
        )
        roll = tele[s].rolling(10, min_periods=1).mean()
        fig.add_trace(
            go.Scatter(
                x=tele[x],
                y=roll,
                mode="lines",
                line=dict(width=2.2, color="#38bdf8"),
                name=f"{s} (rolling)",
                showlegend=False,
            ),
            row=r + 1,
            col=c + 1,
        )
    fig.update_annotations(font_size=11)
    return _layout(fig, 180 * rows + 60, title="Sensor trends (raw + 10-cycle rolling mean)")


def health_gauge(value: float, status: str) -> go.Figure:
    fig = go.Figure(
        go.Indicator(
            mode="gauge+number",
            value=value,
            number=dict(suffix="%", font=dict(size=30)),
            gauge=dict(
                axis=dict(range=[0, 100], tickwidth=1, tickcolor="#94a3b8"),
                bar=dict(color=STATUS_COLORS.get(status, "#64748b"), thickness=0.3),
                bgcolor="rgba(0,0,0,0)",
                borderwidth=0,
                steps=[
                    dict(range=[0, 30], color="rgba(239,68,68,0.18)"),
                    dict(range=[30, 60], color="rgba(245,158,11,0.18)"),
                    dict(range=[60, 100], color="rgba(34,197,94,0.18)"),
                ],
            ),
            title=dict(text="Health index", font=dict(size=13)),
        )
    )
    return _layout(fig, 210, margin=dict(l=20, r=20, t=40, b=0))


# ---------------------------------------------------------------------- engine schematic twin
# Station positions along the gas path (x) for the schematic; y is used to fan-out labels.
_STATION_X = {
    "Fan": 0.9,
    "Bypass": 1.6,
    "LPC": 2.3,
    "HPC": 3.4,
    "Combustor": 4.5,
    "HPT": 5.3,
    "LPT": 6.2,
    "Core": 3.9,
}


def engine_twin(latest: pd.Series, zscores: dict[str, float], sensors: list[str]) -> go.Figure:
    """Simplified turbofan cross-section with live sensor read-outs coloured by deviation."""
    fig = go.Figure()
    # Nacelle / casing
    fig.add_shape(
        type="rect",
        x0=0.3,
        x1=7.4,
        y0=-1.6,
        y1=1.6,
        line=dict(color="#334155", width=2),
        fillcolor="rgba(30,41,59,0.35)",
        layer="below",
    )
    # Core flow path
    fig.add_shape(
        type="rect",
        x0=1.9,
        x1=6.9,
        y0=-0.75,
        y1=0.75,
        line=dict(color="#475569", width=1.5),
        fillcolor="rgba(51,65,85,0.5)",
        layer="below",
    )
    # Fan
    fig.add_shape(
        type="rect",
        x0=0.7,
        x1=1.1,
        y0=-1.45,
        y1=1.45,
        line=dict(color="#38bdf8"),
        fillcolor="rgba(56,189,248,0.35)",
    )
    # Compressor stages (triangles narrowing)
    for i, x in enumerate(np.linspace(2.0, 3.7, 6)):
        h = 0.68 - i * 0.06
        fig.add_shape(
            type="rect",
            x0=x,
            x1=x + 0.14,
            y0=-h,
            y1=h,
            line=dict(color="#a5b4fc"),
            fillcolor="rgba(165,180,252,0.45)",
        )
    # Combustor
    fig.add_shape(
        type="rect",
        x0=4.1,
        x1=4.9,
        y0=-0.5,
        y1=0.5,
        line=dict(color="#fb923c"),
        fillcolor="rgba(251,146,60,0.55)",
    )
    # Turbines
    for i, x in enumerate(np.linspace(5.05, 6.4, 5)):
        h = 0.4 + i * 0.07
        fig.add_shape(
            type="rect",
            x0=x,
            x1=x + 0.14,
            y0=-h,
            y1=h,
            line=dict(color="#f472b6"),
            fillcolor="rgba(244,114,182,0.45)",
        )
    # Shaft
    fig.add_shape(type="line", x0=0.7, x1=6.8, y0=0, y1=0, line=dict(color="#94a3b8", width=3))
    # Nozzle
    fig.add_shape(
        type="path",
        path="M 6.9,-0.75 L 7.4,-0.45 L 7.4,0.45 L 6.9,0.75 Z",
        line=dict(color="#475569"),
        fillcolor="rgba(71,85,105,0.5)",
    )
    for name, x in [("Fan", 0.9), ("LPC/HPC", 2.85), ("Combustor", 4.5), ("HPT/LPT", 5.75), ("Nozzle", 7.15)]:
        fig.add_annotation(x=x, y=-1.85, text=name, showarrow=False, font=dict(size=10, color="#94a3b8"))

    # Sensor read-outs, alternating above/below, coloured by |z|
    slots: dict[str, int] = {}
    for s in sensors:
        meta = SENSOR_META[s]
        x = _STATION_X.get(meta["station"], 3.9)
        k = slots.get(meta["station"], 0)
        slots[meta["station"]] = k + 1
        side = 1 if k % 2 == 0 else -1
        level = 2.1 + 0.45 * (k // 2)
        z = zscores.get(s, 0.0)
        color = (
            STATUS_COLORS["critical"]
            if abs(z) >= 3
            else STATUS_COLORS["warning"]
            if abs(z) >= 2
            else "#cbd5e1"
        )
        val = latest.get(s, np.nan)
        fig.add_annotation(
            x=x, y=side * level, ax=x, ay=side * 1.65, axref="x", ayref="y",
            text=f"<b>{meta['name']}</b> {val:,.2f} {meta['unit']}<br><span style='font-size:10px'>z={z:+.1f}</span>",
            showarrow=True, arrowhead=0, arrowcolor="#475569", arrowwidth=1,
            font=dict(size=10, color=color), bgcolor="rgba(15,23,42,0.9)", bordercolor=color, borderwidth=1, borderpad=3,
        )  # fmt: skip
    ymax = 2.1 + 0.45 * (max(slots.values(), default=1) // 2) + 0.6
    fig.update_xaxes(visible=False, range=[0, 7.7])
    fig.update_yaxes(visible=False, range=[-ymax, ymax], scaleanchor="x", scaleratio=1)
    return _layout(
        fig, 430, title="Asset twin · live read-outs (colour = deviation from healthy baseline, z-score)"
    )


# ---------------------------------------------------------------------- maintenance
def maintenance_gantt(plan: pd.DataFrame) -> go.Figure:
    import plotly.express as px

    if plan.empty:
        return _layout(go.Figure(), 260, title="Maintenance plan")
    color_map = {
        "P1": STATUS_COLORS["critical"],
        "P2": STATUS_COLORS["warning"],
        "P3": STATUS_COLORS["healthy"],
    }
    fig = px.timeline(
        plan, x_start="start", x_end="end", y="machine_id", color="priority", color_discrete_map=color_map,
        hover_data={"crew": True, "slack_hours": True, "due": True, "rul_p10": True, "start": False, "end": False},
        category_orders={"priority": ["P1", "P2", "P3"]},
    )  # fmt: skip
    fig.add_trace(
        go.Scatter(
            x=plan["due"],
            y=plan["machine_id"],
            mode="markers",
            name="deadline (p10 RUL − margin)",
            marker=dict(symbol="line-ns-open", size=16, color="#e2e8f0", line=dict(width=2)),
        )  # fmt: skip
    )
    fig.update_yaxes(autorange="reversed", title="")
    fig.update_xaxes(title="")
    return _layout(
        fig,
        max(280, 28 * len(plan) + 100),
        title="Capacity-aware maintenance plan (bars = planned window, ticks = deadline)",
    )


def roi_waterfall(r: dict) -> go.Figure:
    fig = go.Figure(
        go.Waterfall(
            orientation="v",
            measure=["relative", "relative", "total"],
            x=["Avoided downtime (gross)", "Platform cost", "Net annual benefit"],
            y=[r["gross_saving"], -r["platform_cost"], 0],
            text=[f"${r['gross_saving']:,.0f}", f"-${r['platform_cost']:,.0f}", f"${r['net_benefit']:,.0f}"],
            textposition="outside",
            connector=dict(line=dict(color="#475569")),
            increasing=dict(marker=dict(color=STATUS_COLORS["healthy"])),
            decreasing=dict(marker=dict(color=STATUS_COLORS["critical"])),
            totals=dict(marker=dict(color="#38bdf8")),
        )
    )
    fig.update_yaxes(title="USD / year", tickprefix="$", separatethousands=True)
    return _layout(fig, 340, title="Business case · annual value")
