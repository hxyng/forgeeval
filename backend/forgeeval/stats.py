"""Statistics for comparing two runs over the same dataset version.

Both runs score the same items, so the comparison is paired: for each item scored in both runs
we take the difference, and the 95% confidence interval of the mean difference tells us whether
a change is larger than run-to-run noise. Pairing removes item difficulty from the variance,
which is why it detects real regressions with far fewer items than comparing two averages.
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass

Z_95 = 1.959964


@dataclass(frozen=True)
class PairedDelta:
    n: int
    mean: float
    ci95_low: float
    ci95_high: float

    @property
    def significant(self) -> bool:
        """The interval excludes zero. Needs at least two pairs."""
        return self.n >= 2 and (self.ci95_low > 0 or self.ci95_high < 0)


def paired_delta(pairs: Sequence[tuple[float, float]]) -> PairedDelta | None:
    """Mean of (candidate - baseline) with a normal-approximation 95% interval."""
    if not pairs:
        return None
    diffs = [c - b for b, c in pairs]
    n = len(diffs)
    mean = sum(diffs) / n
    if n < 2:
        return PairedDelta(n, mean, mean, mean)
    var = sum((d - mean) ** 2 for d in diffs) / (n - 1)
    half = Z_95 * math.sqrt(var / n)
    return PairedDelta(n, mean, mean - half, mean + half)


def direction(delta: float, higher_is_better: bool, significant: bool) -> str:
    """'improved' / 'regressed' only when the change is significant; otherwise 'unchanged'."""
    if not significant or delta == 0:
        return "unchanged"
    better = delta > 0 if higher_is_better else delta < 0
    return "improved" if better else "regressed"
