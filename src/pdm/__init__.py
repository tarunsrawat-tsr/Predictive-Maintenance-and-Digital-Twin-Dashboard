"""pdm - shared library for the Predictive Maintenance & Digital Twin platform.

Modules
-------
schema     Telemetry message contract (MQTT payload) and sensor metadata.
cmapss     NASA C-MAPSS dataset loader.
features   Rolling-window feature engineering shared by training *and* inference.
model      RUL regressors (LightGBM p10/p50/p90) + Mahalanobis anomaly detector bundle.
health     Health index, alert rules, maintenance scheduling.
storage    Store adapters (DynamoDB for AWS, SQLite for tests/demo).
simulator  Replay engine that turns the dataset into a live sensor stream.
"""

__version__ = "0.1.0"
