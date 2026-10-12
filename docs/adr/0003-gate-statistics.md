# ADR 0003: Regression gates decide by non-inferiority, with intervals matched to the score type

- Status: accepted (replaces the first gate design from layer 10)
- Date: 2026-10-12

## Context

A gate compares a candidate run with a baseline over the same items and blocks a PR when quality
drops by more than an allowed amount. The first version used one normal-approximation interval
for every score type and treated "interval includes 0" as noise, which meant:

- **Low power became a silent pass.** With few items the interval is wide, includes 0, and the gate
  passed even when the observed drop was well over the threshold.
- **Binary scores got overconfident intervals.** Measured by simulation: when few items flip, the
  "95%" normal interval covered the true difference only 47-86% of the time (zero-width intervals
  whenever no item happened to flip).
- **Many gates per PR inflated false alarms**: five independent 5% tests fail a clean PR ~23% of
  the time.
- **p95 on small samples is mostly noise**: below 59 items the sample p95 is close to the single
  slowest value.

Evidence: arXiv 2411.00640 (Miller, "Adding Error Bars to Evals"), 2408.04667 (run-to-run
non-determinism up to 15% accuracy), 2605.30315 (resolution of paired LLM comparisons), and the
research notes summarised in the PR.

## Decision

1. **Non-inferiority logic** (`api/gates.py::decide`). With an interval on *worsening*: fail if its
   lower end exceeds the allowed drop, pass if its upper end is within it, otherwise
   **inconclusive**, with a rough count of items that would decide it.
2. **Interval per score type** (`stats.py::paired_delta`): binary scores use the exact McNemar test
   and Newcombe's hybrid score interval (bounded, ~95% coverage in simulation); other scores use a
   paired t interval with n-1 degrees of freedom.
3. **Bonferroni across a PR**: `POST /v1/gates:check` computes every interval at 1 - 0.05/m for m
   applicable gates, keeping the family-wise false-fail rate at 5%.
4. **Quantile gates need enough items**: a p-quantile gate is inconclusive below
   ceil(log 0.05 / log q) items (59 for p95, 5 for p50).

## Consequences

- Small datasets now say "inconclusive" instead of "pass". The triage demo grew from 30 to 120
  tickets: 120 is the smallest size at which an unchanged prompt can be certified within a 0.05
  tolerance at the 97.5% level used for two gates.
- CI must decide what to do with inconclusive (`forgeeval gate --fail-on-inconclusive`).
- Medians and non-binary pass rates still use the point estimate; a paired bootstrap would give
  them intervals (not done).
- Not done yet: anytime-valid early stopping, A/A noise calibration, judge-reliability correction.
