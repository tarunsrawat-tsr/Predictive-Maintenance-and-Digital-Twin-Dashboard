"""Single-process demo: simulator -> scorer -> local SQLite store, no cloud required.

This is the same StreamScorer the Lambda runs, just fed directly instead of via IoT Core/Kinesis.

    python scripts/demo.py --machines 12 --interval 2 --warmup 150
    PDM_BACKEND=local streamlit run services/dashboard/app.py

``--warmup N`` first replays N cycles quickly with back-dated timestamps so the dashboard has
history the moment it opens, then continues in real time.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from pdm.cmapss import download  # noqa: E402
from pdm.config import Settings  # noqa: E402
from pdm.model import load_bundle  # noqa: E402
from pdm.scoring import StreamScorer  # noqa: E402
from pdm.simulator import FleetSimulator  # noqa: E402
from pdm.storage.local import SQLiteStore  # noqa: E402

log = logging.getLogger("pdm.demo")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--machines", type=int, default=12)
    ap.add_argument("--interval", type=float, default=2.0, help="seconds per cycle in real time")
    ap.add_argument("--warmup", type=int, default=150, help="cycles to back-fill before going live")
    ap.add_argument("--dataset", default="FD001")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument(
        "--db", default=os.environ.get("PDM_LOCAL_DB", str(ROOT / "artifacts" / "pdm_local.sqlite"))
    )
    ap.add_argument("--model", default=os.environ.get("PDM_MODEL_DIR", str(ROOT / "artifacts" / "model")))
    ap.add_argument("--data-dir", default=os.environ.get("PDM_DATA_DIR", str(ROOT / "data" / "cmapss")))
    ap.add_argument("--reset", action="store_true", help="wipe the local store first")
    ap.add_argument("--max-ticks", type=int, default=0)
    args = ap.parse_args()

    logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s %(message)s")
    download(args.data_dir)
    settings = Settings.from_env()
    store = SQLiteStore(args.db)
    if args.reset:
        store.reset()
    model = load_bundle(args.model)
    notified: list[dict] = []
    scorer = StreamScorer(model, store, settings, notify_sink=lambda alerts: notified.extend(alerts))

    # Warm-up with back-dated virtual timestamps.
    sim = FleetSimulator(
        n_machines=args.machines,
        dataset=args.dataset,
        seed=args.seed,
        data_dir=args.data_dir,
        virtual_tick_seconds=args.interval,
        virtual_start=time.time() - args.warmup * args.interval,
    )
    t0 = time.time()
    for batch in sim.stream(max_ticks=args.warmup):
        scorer.process(batch)
    log.info(
        "warm-up: %d cycles x %d machines scored in %.1fs (%d alerts)",
        args.warmup,
        args.machines,
        time.time() - t0,
        len(store.list_alerts()),
    )

    # Go live on the wall clock.
    sim.virtual_tick_seconds = 0.0
    ticks = 0
    while not args.max_ticks or ticks < args.max_ticks:
        t = time.time()
        summary = scorer.process(sim.step())
        ticks += 1
        if summary["alerts"]:
            log.info("tick %d: %s", sim.tick, summary)
        if ticks % 30 == 0:
            states = store.list_machine_states()
            crit = sum(s["status"] == "critical" for s in states)
            log.info(
                "tick %d: %d machines, %d critical, %d open alerts",
                sim.tick,
                len(states),
                crit,
                len(store.list_alerts(status="open")),
            )
        time.sleep(max(0.0, args.interval - (time.time() - t)))


if __name__ == "__main__":
    main()
