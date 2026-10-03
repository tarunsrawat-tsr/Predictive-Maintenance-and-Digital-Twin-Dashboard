"""Run the AWS-free public demo: a populated digital twin with no setup and no cloud account.

    python scripts/public_demo.py            # provision, then serve the dashboard
    python scripts/public_demo.py --static   # frozen snapshot (no live playback)
    python scripts/public_demo.py --seed-only
    python scripts/public_demo.py --headless # run only the ingestion side

The first run trains a model (real C-MAPSS if the dataset can be downloaded, otherwise a
deterministic synthetic fleet) and replays a fleet through the production scoring pipeline to
seed the store. Later runs reuse both, resuming the replay where it stopped.

The dashboard is served read-only: acknowledgement is the only write path in the console and it
is disabled. See docs/public-demo.md.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from pdm.demo import (  # noqa: E402
    DEFAULT_CYCLES,
    DEFAULT_LIVE_SECONDS,
    DEFAULT_MACHINES,
    DEFAULT_SEED,
    PublicDemo,
)

log = logging.getLogger("pdm.public_demo.cli")
APP = ROOT / "services" / "dashboard" / "app.py"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Self-contained public demo of the predictive-maintenance console.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    ap.add_argument("--machines", type=int, default=DEFAULT_MACHINES, help="size of the virtual fleet")
    ap.add_argument("--cycles", type=int, default=DEFAULT_CYCLES, help="cycles replayed to seed history")
    ap.add_argument(
        "--interval", type=float, default=DEFAULT_LIVE_SECONDS, help="seconds per cycle when live"
    )
    ap.add_argument("--cycle-seconds", type=float, default=None, help="virtual seconds per replayed cycle")
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED, help="RNG seed (the demo is deterministic)")
    ap.add_argument(
        "--rounds", type=int, default=800, help="max boosting rounds when training the demo model"
    )
    ap.add_argument("--no-download", action="store_true", help="never fetch C-MAPSS; use synthetic data")
    ap.add_argument("--reseed", action="store_true", help="rebuild the demo store from scratch")
    ap.add_argument("--seed-only", action="store_true", help="provision model + store, then exit")
    ap.add_argument("--no-prewarm", action="store_true", help="let the dashboard seed itself on first load")
    ap.add_argument("--headless", action="store_true", help="run the live loop only, no dashboard")
    ap.add_argument("--static", action="store_true", help="serve a frozen snapshot instead of live playback")
    ap.add_argument("--address", default="0.0.0.0", help="dashboard bind address")
    ap.add_argument("--port", type=int, default=8501, help="dashboard port")
    return ap.parse_args(argv)


def _provision(args: argparse.Namespace, *, live: bool) -> PublicDemo:
    return PublicDemo.bootstrap(
        machines=args.machines,
        cycles=args.cycles,
        cycle_seconds=args.cycle_seconds or DEFAULT_CYCLES and max(60.0, 30 * 3600 / max(args.cycles, 1)),
        seed=args.seed,
        rounds=args.rounds,
        allow_download=not args.no_download,
        reseed=args.reseed,
        live=live,
        live_seconds=args.interval,
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s %(message)s")

    if args.headless:
        demo = _provision(args, live=True)
        log.info("headless: %s", json.dumps(demo.summary()))
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            log.info("stopping")
        finally:
            demo.stop_live()
        return 0

    if not args.no_prewarm or args.seed_only:
        # Provisioning here (rather than on first page load) means a visitor is never greeted by
        # a cold start, and the cursor file lets the dashboard resume the replay exactly.
        demo = _provision(args, live=False)
        log.info("demo ready: %s", demo.seed_summary.describe())

    if args.seed_only:
        return 0

    env = os.environ.copy()
    env["PDM_PUBLIC_DEMO"] = "1"
    env["PDM_PUBLIC_DEMO_LIVE"] = "0" if args.static else "1"
    env.setdefault("PDM_BACKEND", "local")
    # The dashboard re-bootstraps in its own process; hand it the same geometry.
    env["PDM_DEMO_MACHINES"] = str(args.machines)
    env["PDM_DEMO_CYCLES"] = str(args.cycles)
    env["PDM_DEMO_LIVE_SECONDS"] = str(args.interval)
    env["PDM_DEMO_ALLOW_DOWNLOAD"] = "0" if args.no_download else "1"

    cmd = [
        sys.executable, "-m", "streamlit", "run", str(APP),
        "--server.address", args.address,
        "--server.port", str(args.port),
        "--server.headless", "true",
    ]  # fmt: skip
    log.info("serving the public demo on http://%s:%d", args.address, args.port)
    try:
        return subprocess.run(cmd, env=env, cwd=ROOT).returncode
    except KeyboardInterrupt:  # pragma: no cover - Ctrl+C on the child
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
