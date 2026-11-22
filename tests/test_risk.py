"""Tests for the EBITDA simulation and the backtest statistics. No network needed."""

from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from covenant_desk.report import stressed_leverage, worst_ltm_decline
from covenant_desk.risk import (
    FirmState,
    GrowthModel,
    breach_probability,
    christoffersen_independence,
    consecutive_pairs,
    fit,
    kupiec_pof,
    simulate_ltm,
    yoy_growth,
)


def quarter_ends(n: int, start=date(2015, 3, 31)) -> list[date]:
    out, d = [], start
    for _ in range(n):
        out.append(d)
        d = (pd.Timestamp(d) + pd.offsets.QuarterEnd(1)).date()
    return out


def test_yoy_growth_handles_seasonality_and_negative_base():
    dates = quarter_ends(8)
    q = pd.Series([100, 50, 100, 50, 110, 55, 110, -50], index=dates, dtype=float)
    g = yoy_growth(q)
    assert g[dates[4]] == pytest.approx(0.10)  # 110 vs 100, same quarter last year
    assert g[dates[7]] == pytest.approx(-2.0)  # -50 vs 50: fell by twice its base


def test_consecutive_pairs_skip_gaps():
    dates = quarter_ends(5)
    g = pd.Series([0.1, 0.2, 0.3, 0.4], index=[dates[0], dates[1], dates[3], dates[4]])
    p = consecutive_pairs(g)
    assert list(p.curr) == [0.2, 0.4]  # the gap between dates[1] and dates[3] is skipped
    assert p.origin == [dates[0], dates[3]]


def test_kupiec_matches_hand_calculation():
    # 10 hits in 100 at p=5%:
    # ln L(0.05) = 90 ln 0.95 + 10 ln 0.05 = -34.574;  ln L(0.10) = 90 ln 0.9 + 10 ln 0.1 = -32.508
    # LR = -2 (-34.574 + 32.508) = 4.131, chi2(1) p-value = 0.042.
    lr, p = kupiec_pof(10, 100, 0.05)
    assert lr == pytest.approx(4.1308, abs=1e-3)
    assert p == pytest.approx(0.0421, abs=1e-3)
    assert kupiec_pof(5, 100, 0.05)[1] == pytest.approx(1.0)


def test_christoffersen_detects_clustering():
    clustered = [np.array([0] * 40 + [1] * 5 + [0] * 55)]
    scattered = [np.array(([0] * 19 + [1]) * 5)]
    assert christoffersen_independence(clustered)[1] < 0.01
    assert christoffersen_independence(scattered)[1] > 0.3


def test_simulation_without_volatility_is_deterministic():
    model = GrowthModel(mu=0.10, phi=0.0, lam=0.9, nu=5.0, beta=0.0, pooled_var=0.0)
    state = FirmState(date(2026, 9, 30), np.array([100.0, 100, 100, 100]), 0.10, 0.0, debt=1000)
    paths = simulate_ltm(model, state, n_paths=10)
    # Each quarter grows 10% on the same quarter last year: LTM goes 410, 420, 430, 440.
    assert paths[0].tolist() == pytest.approx([410, 420, 430, 440])
    assert breach_probability(paths, debt=1000, max_ratio=2.5) == 0.0  # 1000/410 = 2.44x
    assert breach_probability(paths, debt=1000, max_ratio=2.4) == 1.0


def test_negative_ebitda_counts_as_breach():
    paths = np.array([[100.0, -5.0], [100.0, 100.0]])
    assert breach_probability(paths, debt=100, max_ratio=5) == 0.5


def test_fit_recovers_known_parameters():
    rng = np.random.default_rng(1)
    growth = {}
    for f in range(20):
        dates = quarter_ends(60, start=date(2000, 3, 31) + timedelta(days=0))
        g = [0.05]
        for _ in range(59):
            g.append(0.05 + 0.5 * (g[-1] - 0.05) + 0.1 * rng.standard_t(5) * np.sqrt(3 / 5))
        growth[f"F{f}"] = pd.Series(g, index=dates)
    m = fit(growth)
    assert m.phi == pytest.approx(0.5, abs=0.08)
    assert m.mu == pytest.approx(0.05, abs=0.03)
    assert m.nu <= 10  # fat tails detected


def test_stress_helpers():
    dates = quarter_ends(9)
    s = pd.Series([100, 100, 100, 100, 100, 80, 60, 50, 70], index=dates, dtype=float)
    assert worst_ltm_decline(s) == pytest.approx(-0.5)  # 100 -> 50
    assert stressed_leverage(300, 100, -0.5) == pytest.approx(6.0)
    assert stressed_leverage(300, 100, -1.2) == float("inf")
