# Public demo

A one-command, AWS-free instance of the console that anyone can open: no cloud account, no
dataset download, no training step, no login, and nothing to clean up afterwards.

```bash
make setup         # or: pip install -e ".[dashboard]"
make public-demo   # -> http://localhost:8501
```

This is the fastest way to see the whole system work. The AWS deployment, the MQTT gateway and
the Terraform stack are all bypassed, but the *scoring* path is not: the same
`pdm.scoring.StreamScorer` the Lambda runs is what produces every number on every page.

## What you get

- **A populated console on first load** — 12 virtual machines, 90 cycles (~30 h of virtual
  history) of sensor data, RUL predictions, health states and a populated alert log.
- **A fleet with a realistic age mix.** Machines are staged across their own trajectories so the
  plant floor shows a few assets in the critical band, some ageing and the rest comfortable,
  rather than 12 identical machines. Degradation *during* playback is what generates the alert
  history.
- **Live playback.** Once the seed history is written, one new cycle arrives every 6 seconds and
  the console auto-refreshes, so the digital twin behaves like the streaming system.
- **Read-only by construction.** Acknowledgement is the only write path in the console and it is
  disabled; the store is written exclusively by the ingestion-side scorer.

## What happens on the first run

| Step | Behaviour |
|---|---|
| Model | Reuses `artifacts/model` if a complete bundle is already there. Otherwise trains one from NASA C-MAPSS FD001 (≈15 MB download, a few seconds). If the download is unavailable, it trains on a deterministic synthetic fleet instead, so the demo works fully offline. |
| Data | `data/cmapss/` for the real dataset, `data/synthetic/` for the fallback — both git-ignored. |
| Store | `artifacts/pdm_public_demo.sqlite`, seeded by replaying the fleet through the scorer. |
| Cursor | `artifacts/pdm_public_demo.sqlite.cursor.json` records where each machine is in its trajectory, so restarting the demo resumes the replay instead of jumping backwards. |

The second run reuses all of it and starts in about a second. `--reseed` rebuilds the store from
scratch; the demo is deterministic given `--seed` (default `42`).

## Options

```text
python scripts/public_demo.py [options]

--machines N        size of the virtual fleet            (default 12)
--cycles N          cycles of history written at seeding (default 90)
--interval SECONDS  wall-clock seconds per live cycle    (default 6)
--cycle-seconds S   virtual seconds per replayed cycle   (default: 30 h spread over --cycles)
--rounds N          boosting rounds if a model must be trained (default 800, early-stopped)
--seed N            RNG seed; the demo is deterministic  (default 42)
--no-download       never fetch C-MAPSS; use synthetic data even if the network is available
--reseed            wipe and rebuild the demo store
--static            serve a frozen snapshot instead of live playback
--seed-only         provision the model + store, then exit (useful for CI or a warm image)
--no-prewarm        let the dashboard provision itself on first page load
--headless          run the live scoring loop only, with no dashboard
--address / --port  bind address and port                (default 0.0.0.0:8501)
```

### Environment variables

Useful when the app is started directly (`streamlit run services/dashboard/app.py`) rather than
through the CLI, e.g. on a hosted platform. All are optional.

| Variable | Default | Purpose |
|---|---|---|
| `PDM_PUBLIC_DEMO` | unset | Enables public mode: self-provisioning, read-only, no login. Read from the environment **or** Streamlit secrets. |
| `PDM_PUBLIC_DEMO_LIVE` | `1` | `0` serves a frozen snapshot instead of live playback. |
| `PDM_DEMO_MACHINES`, `PDM_DEMO_CYCLES` | `12`, `90` | Fleet geometry used when the app provisions itself. |
| `PDM_DEMO_CYCLE_SECONDS` | `1200` | Virtual seconds per replayed cycle. |
| `PDM_DEMO_LIVE_SECONDS` | `6` | Wall-clock seconds per live cycle. |
| `PDM_DEMO_ALLOW_DOWNLOAD` | `1` | `0` forces the synthetic fallback. |
| `PDM_LOCAL_DB`, `PDM_MODEL_DIR` | demo defaults | Override where the store and model bundle live. |

## Publishing it

The demo binds `0.0.0.0:8501` and needs no configuration for CORS or XSRF (see
`.streamlit/config.toml`), so it can sit behind any reverse proxy.

**Docker** — the existing dashboard image works unchanged; override the backend it ships with:

```bash
make train        # the image needs a model bundle in artifacts/model
docker build -f services/dashboard/Dockerfile -t pdm-dashboard .
docker run --rm -p 8501:8501 \
  -e PDM_BACKEND=local \
  -e PDM_PUBLIC_DEMO=1 \
  -e PDM_PUBLIC_DEMO_LIVE=1 \
  pdm-dashboard
```

**Streamlit Community Cloud** — point the app at `services/dashboard/app.py` and set
`PDM_PUBLIC_DEMO = "1"` in the app's secrets. Note that the free tier does not install the
`[dashboard]` extra from `pyproject.toml`; add a `requirements.txt` (the contents of
`services/dashboard/requirements.txt` are sufficient) so `streamlit`, `plotly` and `pandas` are
present. Set `PDM_DEMO_ALLOW_DOWNLOAD = "0"` if you would rather not depend on the dataset
download at boot.

**Any host or VM** — `make public-demo` under a process manager, or run
`python scripts/public_demo.py --seed-only` as a build step and `--headless` plus a separate
`streamlit run` if you prefer the ingestion side to be its own service.

## How this differs from the other run modes

| | `make demo` | `make public-demo` | AWS deployment |
|---|---|---|---|
| Prerequisites | `make setup` + `make train` + two terminals | `make setup` | Terraform, AWS account, IoT certs |
| Provisioning | manual | automatic (download → train → seed) | Terraform |
| Ingestion | separate simulator process | in-process, resumable | IoT Core → Kinesis → Lambda |
| Durability | none | replay cursor survives restarts | DynamoDB / S3 |
| Audience | developer | anyone with the link | operators |

## Limitations

Everything in the root [README](../README.md#limitations) applies, plus:

- The fleet is **staged** for presentation: where each machine sits in its life is a deliberate
  choice, not a sample from a population. The model and the scoring pipeline are untouched.
- The free-tier and container deployments above use ephemeral storage, so the demo re-provisions
  on a cold start (roughly 10 s with the download, ~1 s from the synthetic fallback).
- The synthetic fallback exists so the demo never fails to start; metrics from a bundle trained
  on it are not meaningful as model performance.
- Live playback writes to SQLite on every cycle. That is comfortably within what a single
  container can do, but it is not a load test and there is no multi-user isolation.
