"""Telemetry message contract and sensor metadata.

The MQTT payload published by every machine (and replayed by the simulator) is::

    topic:   factory/<site>/<line>/<machine_id>/telemetry
    payload: {
        "machine_id": "GT-001", "site": "nagoya", "line": "L1",
        "ts": "2026-10-03T10:00:00.000Z", "cycle": 57,
        "settings": {"op1": -0.0007, "op2": -0.0004, "op3": 100.0},
        "sensors":  {"s1": 518.67, ..., "s21": 23.419}
    }

Sensor names follow the NASA C-MAPSS turbofan dataset so that the ML model can be trained
offline on the public data and applied unchanged to the live stream.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, Field, field_validator

SETTING_COLUMNS: list[str] = ["op1", "op2", "op3"]
SENSOR_COLUMNS: list[str] = [f"s{i}" for i in range(1, 22)]
RAW_COLUMNS: list[str] = SETTING_COLUMNS + SENSOR_COLUMNS

# Sensors that carry degradation signal under the single operating condition of FD001.
# (The remaining ones are constant or near-constant in that sub-dataset.)
INFORMATIVE_SENSORS: list[str] = [
    "s2", "s3", "s4", "s7", "s8", "s9", "s11", "s12", "s13", "s14", "s15", "s17", "s20", "s21",
]  # fmt: skip

# Human-readable metadata used by the digital-twin view.
SENSOR_META: dict[str, dict[str, str]] = {
    "s1": {"name": "T2", "desc": "Total temperature at fan inlet", "unit": "°R", "station": "Fan"},
    "s2": {"name": "T24", "desc": "Total temperature at LPC outlet", "unit": "°R", "station": "LPC"},
    "s3": {"name": "T30", "desc": "Total temperature at HPC outlet", "unit": "°R", "station": "HPC"},
    "s4": {"name": "T50", "desc": "Total temperature at LPT outlet", "unit": "°R", "station": "LPT"},
    "s5": {"name": "P2", "desc": "Pressure at fan inlet", "unit": "psia", "station": "Fan"},
    "s6": {"name": "P15", "desc": "Total pressure in bypass duct", "unit": "psia", "station": "Bypass"},
    "s7": {"name": "P30", "desc": "Total pressure at HPC outlet", "unit": "psia", "station": "HPC"},
    "s8": {"name": "Nf", "desc": "Physical fan speed", "unit": "rpm", "station": "Fan"},
    "s9": {"name": "Nc", "desc": "Physical core speed", "unit": "rpm", "station": "Core"},
    "s10": {"name": "epr", "desc": "Engine pressure ratio (P50/P2)", "unit": "-", "station": "Core"},
    "s11": {"name": "Ps30", "desc": "Static pressure at HPC outlet", "unit": "psia", "station": "HPC"},
    "s12": {"name": "phi", "desc": "Fuel flow / Ps30", "unit": "pps/psi", "station": "Combustor"},
    "s13": {"name": "NRf", "desc": "Corrected fan speed", "unit": "rpm", "station": "Fan"},
    "s14": {"name": "NRc", "desc": "Corrected core speed", "unit": "rpm", "station": "Core"},
    "s15": {"name": "BPR", "desc": "Bypass ratio", "unit": "-", "station": "Bypass"},
    "s16": {"name": "farB", "desc": "Burner fuel-air ratio", "unit": "-", "station": "Combustor"},
    "s17": {"name": "htBleed", "desc": "Bleed enthalpy", "unit": "-", "station": "HPC"},
    "s18": {"name": "Nf_dmd", "desc": "Demanded fan speed", "unit": "rpm", "station": "Fan"},
    "s19": {"name": "PCNfR_dmd", "desc": "Demanded corrected fan speed", "unit": "rpm", "station": "Fan"},
    "s20": {"name": "W31", "desc": "HPT coolant bleed", "unit": "lbm/s", "station": "HPT"},
    "s21": {"name": "W32", "desc": "LPT coolant bleed", "unit": "lbm/s", "station": "LPT"},
}

TOPIC_TEMPLATE = "factory/{site}/{line}/{machine_id}/telemetry"
TOPIC_FILTER = "factory/+/+/+/telemetry"


def utc_now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def iso_to_epoch_ms(ts: str) -> int:
    """Parse an ISO-8601 timestamp (with or without 'Z') to epoch milliseconds."""
    s = ts.replace("Z", "+00:00")
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp() * 1000)


class TelemetryMessage(BaseModel):
    """One sensor snapshot for one machine."""

    machine_id: str = Field(min_length=1, max_length=64)
    site: str = "default"
    line: str = "default"
    ts: str = Field(default_factory=utc_now_iso)
    cycle: int = Field(ge=1)
    settings: dict[str, float]
    sensors: dict[str, float]

    @field_validator("sensors")
    @classmethod
    def _all_sensors_present(cls, v: dict[str, float]) -> dict[str, float]:
        missing = [c for c in SENSOR_COLUMNS if c not in v]
        if missing:
            raise ValueError(f"missing sensors: {missing}")
        return v

    @field_validator("settings")
    @classmethod
    def _all_settings_present(cls, v: dict[str, float]) -> dict[str, float]:
        missing = [c for c in SETTING_COLUMNS if c not in v]
        if missing:
            raise ValueError(f"missing settings: {missing}")
        return v

    @property
    def topic(self) -> str:
        return TOPIC_TEMPLATE.format(site=self.site, line=self.line, machine_id=self.machine_id)

    @property
    def ts_ms(self) -> int:
        return iso_to_epoch_ms(self.ts)

    def raw_vector(self) -> list[float]:
        """Settings + sensors in canonical RAW_COLUMNS order (model input row)."""
        return [self.settings[c] for c in SETTING_COLUMNS] + [self.sensors[c] for c in SENSOR_COLUMNS]
