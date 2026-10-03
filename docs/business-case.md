# Business case

## The problem leadership actually has

> "Machines fail without warning. Every hour a line is down costs us money. Tell us *when* a
> machine will fail and whether paying for a predictive-maintenance platform is worth it."

Three things have to be true for the answer to be yes:

1. The model has to see failures coming far enough ahead to schedule work (lead time).
2. The alerting has to be trustworthy enough that people act on it (precision).
3. The avoided downtime has to exceed the cost of the platform and the planned interventions (ROI).

## What the platform delivers

| Question | Where it is answered | Mechanism |
| --- | --- | --- |
| Which machines will fail, and when? | Fleet overview, Machine twin | LightGBM RUL (median) with an 80 % conformal band; test RMSE ≈ 14 cycles on the official C-MAPSS FD001 protocol. |
| Is anything behaving abnormally *now*? | Machine twin, Alerts | Mahalanobis distance from a healthy baseline, EWMA-smoothed; ≈ 1 % false-positive rate per cycle on healthy data, 89 % of the last 25 cycles before failure flagged. |
| What should the crews do this week? | Maintenance & ROI | Deadlines from conservative (p10) RUL minus a safety margin; capacity-aware greedy scheduler across crews; CSV export. |
| Is it worth it? | Maintenance & ROI | Interactive ROI model (below) using the plant's own cost assumptions. |

## ROI model

```
events          = machines × unplanned failures per machine-year
captured        = events × capture rate                (model recall at the chosen thresholds)
saving / event  = (unplanned outage h − planned outage h) × downtime cost per h
gross saving    = captured × saving / event
net benefit     = gross saving − platform cost per year
payback (months)= platform cost / gross saving × 12
```

### Worked example (dashboard defaults)

| Assumption | Value |
| --- | --- |
| Machines in scope | 20 |
| Unplanned failures per machine-year (baseline) | 1.2 |
| Share caught early by PdM (capture rate) | 70 % |
| Unplanned outage | 24 h |
| Planned maintenance | 6 h |
| Downtime cost | $15,000 / h |
| Platform cost per year (cloud + support + sensor retrofit amortised) | $60,000 |

| Result | Value |
| --- | --- |
| Failures caught early | 16.8 / year |
| Saving per caught failure | (24 − 6) × 15,000 = **$270,000** |
| Gross saving | **$4.54 M / year** |
| Net benefit | **$4.48 M / year** |
| Payback | **< 1 month** |
| Downtime avoided | 302 h / year |

Even at a tenth of that downtime cost ($1,500/h) the net benefit is ≈ $390 k/year against a
$60 k platform — the case is robust to the assumption most people argue about. The
dashboard lets finance change every input live.

### What the cloud bill looks like

The demo stack costs ≈ $75 / month (see `docs/architecture.md`). A production deployment
for a few hundred assets at 1 message/minute lands in the low hundreds of dollars per month;
the dominant costs are Kinesis shards and DynamoDB writes, both of which scale linearly and
predictably. Compared with a single hour of unplanned downtime it is noise.

## Why Japan / Industry X

Manufacturing is ~20 % of Japan's GDP and the asset base is ageing while the skilled
maintenance workforce shrinks. Predictive maintenance is the entry point of most Industry X
programmes because the ROI is tangible, the data already exists (PLC/SCADA/CM systems) and
success builds the data foundation (ingestion, time-series, twin) that later use cases —
quality prediction, energy optimisation, autonomous scheduling — reuse.

## Limitations (say them before the client does)

* The demo uses **simulated** turbofan data; real assets need re-training on their own
  failure history. The pipeline is asset-agnostic; the model is not.
* Capture rate and lead time depend on thresholds — those are **risk-appetite** decisions for
  maintenance + finance, not data-science defaults.
* The ROI model counts only direct downtime cost. Scrap, overtime, expedited parts, SLA
  penalties and safety incidents are real but left out to keep the case conservative.
