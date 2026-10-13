"""Statistics for comparing two runs over the same dataset version.

Both runs score the same items, so the comparison is paired: for each item scored in both runs
we take the difference, and a confidence interval for the mean difference says whether a change
is larger than run-to-run noise. Pairing removes item difficulty from the variance.

The interval depends on the score type, because one formula does not fit all of them at the
sample sizes we see (20-500 items):

- **Binary scores** (exact match, pass/fail): only discordant items carry information. We use the
  exact McNemar test for the p-value and Newcombe's hybrid score interval for the difference of
  paired proportions, which stays inside [-1, 1] and behaves when few items flip. A normal
  approximation is badly overconfident here (5 of 20 flipping one way is not significant).
- **Everything else** (judge scores, latency): paired t interval with n-1 degrees of freedom; the
  normal 1.96 is too narrow at small n.

``confidence`` is configurable so a PR checking several gates can widen every interval
(Bonferroni) and keep the family-wise false-alarm rate at 5%.
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass

from scipy import stats as sps


@dataclass(frozen=True)
class PairedDelta:
    n: int
    mean: float
    ci_low: float
    ci_high: float
    p_value: float | None
    method: str  # "mcnemar-newcombe" | "paired-t" | "single-pair"
    confidence: float

    @property
    def significant(self) -> bool:
        """The interval excludes zero. Needs at least two pairs."""
        return self.n >= 2 and (self.ci_low > 0 or self.ci_high < 0)


def _is_binary(pairs: Sequence[tuple[float, float]]) -> bool:
    return all(b in (0.0, 1.0) and c in (0.0, 1.0) for b, c in pairs)


def _wilson(successes: int, n: int, z: float) -> tuple[float, float]:
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def _paired_binary(pairs: Sequence[tuple[float, float]], confidence: float) -> PairedDelta:
    n = len(pairs)
    both = sum(1 for b, c in pairs if b == 1 and c == 1)
    lost = sum(1 for b, c in pairs if b == 1 and c == 0)  # baseline passed, candidate failed
    gained = sum(1 for b, c in pairs if b == 0 and c == 1)
    neither = n - both - lost - gained
    delta = (gained - lost) / n

    discordant = lost + gained
    p_value = 1.0 if discordant == 0 else float(sps.binomtest(gained, discordant, 0.5).pvalue)

    # Newcombe (1998), method 10: Wilson intervals for each marginal, combined with the
    # correlation between them.
    z = float(sps.norm.ppf(1 - (1 - confidence) / 2))
    p_cand, p_base = (both + gained) / n, (both + lost) / n
    l1, u1 = _wilson(both + gained, n, z)
    l2, u2 = _wilson(both + lost, n, z)
    denom = (both + lost) * (gained + neither) * (both + gained) * (lost + neither)
    phi = 0.0 if denom == 0 else (both * neither - lost * gained) / math.sqrt(denom)
    d_low = math.sqrt(
        max(0.0, (p_cand - l1) ** 2 - 2 * phi * (p_cand - l1) * (u2 - p_base) + (u2 - p_base) ** 2)
    )
    d_high = math.sqrt(
        max(0.0, (u1 - p_cand) ** 2 - 2 * phi * (u1 - p_cand) * (p_base - l2) + (p_base - l2) ** 2)
    )
    return PairedDelta(
        n=n,
        mean=delta,
        ci_low=max(-1.0, delta - d_low),
        ci_high=min(1.0, delta + d_high),
        p_value=p_value,
        method="mcnemar-newcombe",
        confidence=confidence,
    )


def _t_p_value(mean: float, se: float, n: int) -> float:
    if se == 0:  # every difference identical: no doubt either way
        return 1.0 if mean == 0 else 0.0
    return float(2 * sps.t.sf(abs(mean / se), n - 1))


def _paired_t(diffs: Sequence[float], confidence: float) -> PairedDelta:
    n = len(diffs)
    mean = sum(diffs) / n
    var = sum((d - mean) ** 2 for d in diffs) / (n - 1)
    se = math.sqrt(var / n)
    t_crit = float(sps.t.ppf(1 - (1 - confidence) / 2, n - 1))
    return PairedDelta(
        n=n,
        mean=mean,
        ci_low=mean - t_crit * se,
        ci_high=mean + t_crit * se,
        p_value=_t_p_value(mean, se, n),
        method="paired-t",
        confidence=confidence,
    )


def paired_delta(
    pairs: Sequence[tuple[float, float]], confidence: float = 0.95
) -> PairedDelta | None:
    """Mean of (candidate - baseline) over (baseline, candidate) pairs, with an interval suited to
    the score type."""
    if not pairs:
        return None
    if len(pairs) == 1:
        d = pairs[0][1] - pairs[0][0]
        return PairedDelta(1, d, d, d, None, "single-pair", confidence)
    if _is_binary(pairs):
        return _paired_binary(pairs, confidence)
    return _paired_t([c - b for b, c in pairs], confidence)


def direction(delta: float, higher_is_better: bool, significant: bool) -> str:
    """'improved' / 'regressed' only when the change is significant; otherwise 'unchanged'."""
    if not significant or delta == 0:
        return "unchanged"
    better = delta > 0 if higher_is_better else delta < 0
    return "improved" if better else "regressed"


def min_items_for_quantile(q: float, confidence: float = 0.95) -> int:
    """Smallest n for which the sample maximum is a one-sided upper confidence bound on the
    q-quantile: P(all n samples fall below the q-quantile) = q^n <= 1 - confidence.
    For p95 at 95% that is 59 items; below it a p95 is effectively the noisiest single value."""
    return math.ceil(math.log(1 - confidence) / math.log(q))


# ---------- anytime-valid confidence sequences (early stopping) ----------


@dataclass(frozen=True)
class SequentialInterval:
    n: int
    mean: float
    low: float
    high: float
    alpha: float


def confidence_sequence(diffs: Sequence[float], alpha: float) -> SequentialInterval | None:
    """Anytime-valid confidence sequence for the mean of paired differences in [-1, 1].

    Predictable plug-in empirical-Bernstein CS of Waudby-Smith & Ramdas (2023, "Estimating means
    of bounded random variables by betting"): valid simultaneously at every n, so a run may be
    checked after each item and stopped as soon as the interval is decisive without inflating the
    error rate. Unlike Hoeffding-style bounds it tightens when the differences have low variance
    (most items unchanged), which is the usual case for prompt changes.

    Differences are mapped to [0, 1] with x = (d + 1) / 2 and the interval is mapped back.
    """
    if not diffs:
        return None
    xs = [(d + 1) / 2 for d in diffs]
    if any(x < 0 or x > 1 for x in xs):
        raise ValueError("confidence_sequence needs differences in [-1, 1]")
    log_term = math.log(2 / alpha)
    cap = 0.5
    sum_lam = sum_lam_x = sum_v_psi = 0.0
    sum_sq = 0.25  # prior pseudo-observation for the variance estimate
    sum_x = 0.5  # prior pseudo-observation for the mean estimate
    for i, x in enumerate(xs, start=1):
        mean_prev = sum_x / i  # predictable: uses observations before i
        var_prev = sum_sq / i
        lam = min(math.sqrt(2 * log_term / (var_prev * i * math.log(i + 1))), cap)
        psi = (-math.log(1 - lam) - lam) / 4
        sum_v_psi += 4 * (x - mean_prev) ** 2 * psi
        sum_lam += lam
        sum_lam_x += lam * x
        sum_x += x
        sum_sq += (x - sum_x / (i + 1)) ** 2
    centre = sum_lam_x / sum_lam
    half = (log_term + sum_v_psi) / sum_lam
    low, high = max(0.0, centre - half), min(1.0, centre + half)
    mean = sum(diffs) / len(diffs)
    return SequentialInterval(len(diffs), mean, 2 * low - 1, 2 * high - 1, alpha)
