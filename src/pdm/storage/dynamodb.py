"""DynamoDB adapter.

Table design (all PAY_PER_REQUEST, provisioned by Terraform in infra/terraform/storage.tf):

pdm-telemetry      PK machine_id (S)  SK ts (N, epoch ms)   TTL attribute: ttl
pdm-machine-state  PK machine_id (S)
pdm-alerts         PK machine_id (S)  SK ts (N)             GSI status-ts-index (status, ts)

Why DynamoDB and not Amazon Timestream? Timestream for LiveAnalytics closed to new customers on
2025-06-20; its successor (Timestream for InfluxDB) is instance-based and VPC-bound. The access
patterns here ("latest N points for a machine", "current state of every machine") map directly
onto a partition-key + sort-key design, keep the stack fully serverless, and cost ~0 at demo
scale. Long-term/analytical history goes to S3 via Kinesis Firehose (see scorer).
"""

from __future__ import annotations

import time
from decimal import Decimal
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key

from pdm.storage.base import Alert, MachineState, TelemetryItem


def to_ddb(obj: Any) -> Any:
    """Recursively convert floats to Decimal (DynamoDB does not accept float)."""
    if isinstance(obj, float):
        return Decimal(str(obj))
    if isinstance(obj, dict):
        return {k: to_ddb(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [to_ddb(v) for v in obj]
    return obj


def from_ddb(obj: Any) -> Any:
    """Recursively convert Decimal back to int/float."""
    if isinstance(obj, Decimal):
        return int(obj) if obj == obj.to_integral_value() else float(obj)
    if isinstance(obj, dict):
        return {k: from_ddb(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [from_ddb(v) for v in obj]
    return obj


class DynamoStore:
    def __init__(
        self,
        telemetry_table: str,
        machine_state_table: str,
        alerts_table: str,
        region: str | None = None,
        ttl_days: int = 7,
    ):
        ddb = boto3.resource("dynamodb", region_name=region)
        self.telemetry = ddb.Table(telemetry_table)
        self.state = ddb.Table(machine_state_table)
        self.alerts = ddb.Table(alerts_table)
        self.ttl_days = ttl_days

    # ------------------------------------------------------------------ telemetry
    def put_telemetry_batch(self, items: list[TelemetryItem]) -> None:
        ttl = int(time.time()) + self.ttl_days * 86400
        with self.telemetry.batch_writer(overwrite_by_pkeys=["machine_id", "ts"]) as bw:
            for it in items:
                bw.put_item(Item=to_ddb({**it, "ttl": ttl}))

    def get_telemetry(self, machine_id: str, limit: int = 300) -> list[TelemetryItem]:
        resp = self.telemetry.query(
            KeyConditionExpression=Key("machine_id").eq(machine_id),
            ScanIndexForward=False,
            Limit=limit,
        )
        items = [from_ddb(i) for i in resp.get("Items", [])]
        items.reverse()
        return items

    # ------------------------------------------------------------------ machine state
    def upsert_machine_state(self, state: MachineState) -> None:
        self.state.put_item(Item=to_ddb(state))

    def get_machine_state(self, machine_id: str) -> MachineState | None:
        resp = self.state.get_item(Key={"machine_id": machine_id})
        item = resp.get("Item")
        return from_ddb(item) if item else None

    def list_machine_states(self) -> list[MachineState]:
        items: list[dict] = []
        kwargs: dict = {}
        while True:
            resp = self.state.scan(**kwargs)
            items.extend(resp.get("Items", []))
            if "LastEvaluatedKey" not in resp:
                break
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
        return sorted((from_ddb(i) for i in items), key=lambda s: s["machine_id"])

    # ------------------------------------------------------------------ alerts
    def put_alert(self, alert: Alert) -> None:
        self.alerts.put_item(Item=to_ddb(alert))

    def list_alerts(self, status: str | None = None, limit: int = 200) -> list[Alert]:
        if status:
            resp = self.alerts.query(
                IndexName="status-ts-index",
                KeyConditionExpression=Key("status").eq(status),
                ScanIndexForward=False,
                Limit=limit,
            )
            return [from_ddb(i) for i in resp.get("Items", [])]
        items: list[dict] = []
        kwargs: dict = {}
        while True:
            resp = self.alerts.scan(**kwargs)
            items.extend(resp.get("Items", []))
            if "LastEvaluatedKey" not in resp or len(items) >= limit:
                break
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
        items.sort(key=lambda a: a["ts"], reverse=True)
        return [from_ddb(i) for i in items[:limit]]

    def ack_alert(self, machine_id: str, ts: int, user: str = "operator") -> None:
        self.alerts.update_item(
            Key={"machine_id": machine_id, "ts": int(ts)},
            UpdateExpression="SET #s = :s, acknowledged_by = :u, acknowledged_at = :t",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":s": "acknowledged", ":u": user, ":t": int(time.time() * 1000)},
        )
