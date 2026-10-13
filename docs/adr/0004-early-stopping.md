# ADR 0004: Stop evaluation runs early with anytime-valid confidence sequences

- Status: accepted
- Date: 2026-10-13

## Context

Every item in a run costs a model call (and a judge call per rubric evaluator). A prompt change
that breaks most answers is obvious long before all items run, but checking a fixed-n interval
repeatedly and stopping when it looks decisive inflates false decisions: the more often you peek,
the more likely noise crosses the line (arXiv 2606.15474 found a rolling z-test false-alarming on
75% of drift-free streams).

## Decision

1. **Anytime-valid confidence sequence** (`stats.confidence_sequence`): the predictable plug-in
   empirical-Bernstein sequence of Waudby-Smith & Ramdas (2023) for means of bounded variables,
   applied to paired differences mapped from [-1, 1] to [0, 1]. It is valid simultaneously at every
   sample size, so the run may be checked as often as we like, and it tightens when most items are
   unchanged. Simulated time-uniform coverage under continuous monitoring: 99.2-100% at a 95% target.
2. **Fixed random order**: each run item gets a random `position`; jobs are enqueued in that order
   and the check reads only the longest contiguous finished prefix, so slow or retried items
   (which may correlate with outcome) can't bias the decision.
3. **Who decides**: the reaper (every ~5 s, one worker at a time) evaluates eligible gates —
   `max_drop_vs_baseline` on the mean or pass rate of an evaluator scored in [0, 1]. Any decisive
   fail stops the run; an early pass needs every applicable gate to be eligible and decisively
   passed. Unstarted items are cancelled.
4. **Error budget**: 1% for in-flight decisions, 4% for the fixed-n test of runs that go the
   distance (union bound, 5% total), each split across gates (Bonferroni). Runs that complete keep
   nearly all their fixed-n precision.
5. **Baselines** for early stop (and for gates generally) skip any run that failed a gate check;
   otherwise a regression becomes the next run's baseline and hides later regressions (found while
   testing this feature).

## Consequences

- Simulation (120 items, 0.05 tolerance): a total regression is decided after ~23 items, a 40%
  breakage after ~72; small effects and unchanged prompts run to the end. On the compose stack with
  5 ms fake models, the 5 s check interval let ~60 items finish first, so it stopped at ~62/120;
  with real models taking seconds per item the check runs relatively much earlier.
- Early stopping can't certify "no regression" quickly at tight tolerances: proving a drop is
  under 5 points needs hundreds of items. It is a tool for catching bad changes cheaply.
- Latency, cost and quantile gates are not decided sequentially (unbounded or not a mean); they
  block early passes but not early fails.
