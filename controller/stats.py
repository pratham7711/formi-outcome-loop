"""Small, deterministic statistics. No randomness anywhere in a decision path.

The workhorse is `stratified_diff`: a Cochran–Mantel–Haenszel-style comparison of two arms'
success rates across strata (day x config version x cohort). Stratifying is what makes it safe
to compare arms whose traffic shares changed over time or that were only live in some cohorts:
arms are only ever compared against each other *inside* the same stratum.
"""
from __future__ import annotations

import math
from dataclasses import dataclass


def phi(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def z_for_p(p: float) -> float:
    """Inverse normal CDF (Acklam's rational approximation, |err| < 1.2e-9)."""
    if not 0 < p < 1:
        raise ValueError(p)
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02, 1.383577518672690e+02,
         -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02, 6.680131188771972e+01,
         -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00, -2.549732539343734e+00,
         4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00, 3.754408661907416e+00]
    lo = 0.02425
    if p < lo:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
            ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    if p > 1 - lo:
        return -z_for_p(1 - p)
    q = p - 0.5
    r = q * q
    return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / \
        (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1)


def wilson(k: float, n: float, z: float = 1.96) -> tuple[float, float]:
    if n <= 0:
        return 0.0, 1.0
    p = k / n
    den = 1 + z * z / n
    c = (p + z * z / (2 * n)) / den
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return max(0.0, c - h), min(1.0, c + h)


@dataclass
class Diff:
    """Arm A minus arm B, pooled over strata."""
    diff: float          # weighted mean difference in success rate (absolute)
    se: float
    z: float
    n_a: float           # trials in arm A that entered the comparison
    n_b: float
    rate_b: float        # arm B's pooled rate over the same strata (for relative lift)
    strata: int

    @property
    def lift(self) -> float:
        return self.diff / self.rate_b if self.rate_b > 0 else 0.0

    def ci(self, z: float = 1.96) -> tuple[float, float]:
        return self.diff - z * self.se, self.diff + z * self.se


EMPTY = Diff(0.0, float("inf"), 0.0, 0, 0, 0.0, 0)


def stratified_diff(pairs, min_n: float = 5) -> Diff:
    """pairs: iterable of ((succ_a, n_a), (succ_b, n_b)), one per stratum.

    Inverse-variance weighting with the pooled within-stratum rate for the variance (the null),
    which keeps tiny strata from dominating. Strata where either arm has < min_n trials are skipped.
    """
    sw = swd = 0.0
    na = nb = sb = 0.0
    k = 0
    for (sa, ta), (sb_, tb) in pairs:
        if ta < min_n or tb < min_n:
            continue
        p = (sa + sb_) / (ta + tb)
        p = min(max(p, 1e-3), 1 - 1e-3)
        var = p * (1 - p) * (1 / ta + 1 / tb)
        w = 1.0 / var
        sw += w
        swd += w * (sa / ta - sb_ / tb)
        na += ta
        nb += tb
        sb += sb_
        k += 1
    if sw == 0:
        return EMPTY
    d = swd / sw
    se = math.sqrt(1.0 / sw)
    return Diff(d, se, d / se, na, nb, sb / nb if nb else 0.0, k)


def binom_upper_z(k: float, n: float, p0: float) -> float:
    """z-score of k successes in n trials against null rate p0 (one-sided, upper)."""
    if n <= 0:
        return 0.0
    p0 = min(max(p0, 1e-4), 1 - 1e-4)
    return (k - n * p0) / math.sqrt(n * p0 * (1 - p0))


def bonferroni_z(alpha: float, m: int) -> float:
    """One-sided critical z for family-wise alpha over m comparisons."""
    return z_for_p(1 - alpha / max(m, 1))
