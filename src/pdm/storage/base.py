"""Storage interface shared by the scorer (writes) and the dashboard (reads).

Three logical collections:

telemetry      time-series of raw readings enriched with model outputs (hot path, TTL'd)
machine_state  one row per machine: latest snapshot = the "digital twin" state
alerts         status transitions and anomalies, with an acknowledge workflow
"""

from __future__ import annotations

from typing import Any, Protocol

from pdm.schema import RAW_COLUMNS, SENSOR_COLUMNS, SETTING_COLUMNS

TelemetryItem = dict[str, Any]
MachineState = dict[str, Any]
Alert = dict[str, Any]


def raw_vector_from_item(item: TelemetryItem) -> list[float]:
    """Rebuild the canonical RAW_COLUMNS vector from a stored telemetry item."""
    s = item.get("settings") or {}
    v = item.get("sensors") or {}
    return [float(s[c]) for c in SETTING_COLUMNS] + [float(v[c]) for c in SENSOR_COLUMNS]


assert len(RAW_COLUMNS) == len(SETTING_COLUMNS) + len(SENSOR_COLUMNS)


class TelemetryStore(Protocol):
    # ---- telemetry
    def put_telemetry_batch(self, items: list[TelemetryItem]) -> None: ...

    def get_telemetry(self, machine_id: str, limit: int = 300) -> list[TelemetryItem]:
        """Most recent ``limit`` items in *chronological* order."""
        ...

    # ---- machine state (digital twin)
    def upsert_machine_state(self, state: MachineState) -> None: ...

    def get_machine_state(self, machine_id: str) -> MachineState | None: ...

    def list_machine_states(self) -> list[MachineState]: ...

    # ---- alerts
    def put_alert(self, alert: Alert) -> None: ...

    def list_alerts(self, status: str | None = None, limit: int = 200) -> list[Alert]:
        """Newest first."""
        ...

    def ack_alert(self, machine_id: str, ts: int, user: str = "operator") -> None: ...


def get_store(settings=None) -> TelemetryStore:
    """Factory: pick the adapter from settings/env."""
    from pdm.config import Settings

    settings = settings or Settings.from_env()
    if settings.backend == "dynamodb":
        from pdm.storage.dynamodb import DynamoStore

        return DynamoStore(
            telemetry_table=settings.telemetry_table,
            machine_state_table=settings.machine_state_table,
            alerts_table=settings.alerts_table,
            region=settings.aws_region,
        )
    from pdm.storage.local import SQLiteStore

    return SQLiteStore(settings.local_db_path)
