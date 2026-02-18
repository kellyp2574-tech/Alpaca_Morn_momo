"""Scoring logic for premarket candidates."""


def score_candidate(gap_pct: float, pm_vol_float: float, relvol: float) -> float:
    """Return a composite score combining gap, float churn, and relative volume."""
    return 0.5 * pm_vol_float + 0.3 * gap_pct + 0.2 * relvol
