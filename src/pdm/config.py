"""Runtime configuration (12-factor: everything comes from environment variables)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _env_float(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


def _env_int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


@dataclass(frozen=True)
class HealthThresholds:
    """Business rules that turn model outputs into operational states.

    RUL values are in *cycles* (one C-MAPSS cycle == one operating cycle of the machine).
    """

    rul_cap: float = 125.0  # piecewise-linear RUL cap used for training & health index
    rul_warning: float = 50.0  # RUL (p50) below which a machine is "warning"
    rul_critical: float = 20.0  # RUL (p50) below which a machine is "critical"
    anomaly_warning: float = 1.0  # anomaly score (distance / healthy threshold) -> warning
    anomaly_critical: float = 2.0  # anomaly score -> critical
    safety_margin_cycles: float = 10.0  # plan maintenance this many cycles before p10 RUL hits zero
    # Hysteresis: a machine only *improves* its status once the metric clears the threshold by
    # this much, which stops alert flapping when a prediction hovers around a threshold.
    hysteresis_rul: float = 8.0
    hysteresis_anomaly: float = 0.25
    # EWMA smoothing factor applied to the raw anomaly score before thresholding (SPC-style
    # EWMA chart). 1.0 disables smoothing. Lower = fewer false alarms, slower detection.
    anomaly_ewma_alpha: float = 0.3

    @classmethod
    def from_env(cls) -> HealthThresholds:
        return cls(
            rul_cap=_env_float("PDM_RUL_CAP", 125.0),
            rul_warning=_env_float("PDM_RUL_WARNING", 50.0),
            rul_critical=_env_float("PDM_RUL_CRITICAL", 20.0),
            anomaly_warning=_env_float("PDM_ANOMALY_WARNING", 1.0),
            anomaly_critical=_env_float("PDM_ANOMALY_CRITICAL", 2.0),
            safety_margin_cycles=_env_float("PDM_SAFETY_MARGIN_CYCLES", 10.0),
            hysteresis_rul=_env_float("PDM_HYSTERESIS_RUL", 8.0),
            hysteresis_anomaly=_env_float("PDM_HYSTERESIS_ANOMALY", 0.25),
            anomaly_ewma_alpha=_env_float("PDM_ANOMALY_EWMA_ALPHA", 0.3),
        )


@dataclass(frozen=True)
class PlantEconomics:
    """Assumptions used to convert cycles into calendar time and money.

    Defaults model a plant where one machine cycle is an 8-hour shift, unplanned downtime
    costs 15,000 USD/hour and lasts 24h, while planned maintenance takes 6h.
    Everything is overridable in the dashboard's ROI calculator.
    """

    cycle_hours: float = 8.0
    downtime_cost_per_hour: float = 15_000.0
    unplanned_outage_hours: float = 24.0
    planned_outage_hours: float = 6.0
    maintenance_crews: int = 2

    @classmethod
    def from_env(cls) -> PlantEconomics:
        return cls(
            cycle_hours=_env_float("PDM_CYCLE_HOURS", 8.0),
            downtime_cost_per_hour=_env_float("PDM_DOWNTIME_COST_PER_HOUR", 15_000.0),
            unplanned_outage_hours=_env_float("PDM_UNPLANNED_OUTAGE_HOURS", 24.0),
            planned_outage_hours=_env_float("PDM_PLANNED_OUTAGE_HOURS", 6.0),
            maintenance_crews=_env_int("PDM_MAINTENANCE_CREWS", 2),
        )


@dataclass(frozen=True)
class Settings:
    """Top-level settings shared by the scorer Lambda and the dashboard."""

    backend: str = "local"  # "dynamodb" | "local"
    aws_region: str = "ap-northeast-1"
    telemetry_table: str = "pdm-telemetry"
    machine_state_table: str = "pdm-machine-state"
    alerts_table: str = "pdm-alerts"
    local_db_path: str = "artifacts/pdm_local.sqlite"
    model_dir: str = "artifacts/model"
    model_s3_uri: str = ""  # s3://bucket/prefix  (optional; falls back to model_dir)
    firehose_stream: str = ""  # optional archive to S3 data lake
    sns_topic_arn: str = ""  # optional critical-alert fan-out
    telemetry_ttl_days: int = 7
    thresholds: HealthThresholds = field(default_factory=HealthThresholds)
    economics: PlantEconomics = field(default_factory=PlantEconomics)

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            backend=_env("PDM_BACKEND", "local"),
            aws_region=_env("AWS_REGION", _env("AWS_DEFAULT_REGION", "ap-northeast-1")),
            telemetry_table=_env("PDM_TELEMETRY_TABLE", "pdm-telemetry"),
            machine_state_table=_env("PDM_MACHINE_STATE_TABLE", "pdm-machine-state"),
            alerts_table=_env("PDM_ALERTS_TABLE", "pdm-alerts"),
            local_db_path=_env("PDM_LOCAL_DB", "artifacts/pdm_local.sqlite"),
            model_dir=_env("PDM_MODEL_DIR", "artifacts/model"),
            model_s3_uri=_env("PDM_MODEL_S3_URI", ""),
            firehose_stream=_env("PDM_FIREHOSE_STREAM", ""),
            sns_topic_arn=_env("PDM_SNS_TOPIC_ARN", ""),
            telemetry_ttl_days=_env_int("PDM_TELEMETRY_TTL_DAYS", 7),
            thresholds=HealthThresholds.from_env(),
            economics=PlantEconomics.from_env(),
        )
