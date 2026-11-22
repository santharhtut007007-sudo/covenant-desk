"""Breach-risk report: `uv run covenant-desk-risk`.

For each borrower: current gross leverage (XBRL proxy), one-year EBITDA-at-Risk, and the
probability that leverage crosses screening thresholds within four quarters, under today's
credit conditions and under 2020-style credit stress. Also deterministic stress tests and the
model backtest. Results are saved to data/risk_summary.csv and data/backtest.json.
"""

from __future__ import annotations

import json
from datetime import date

import numpy as np
import pandas as pd

from covenant_desk import config
from covenant_desk.covenant import GTX_LEVERAGE_COVENANT
from covenant_desk.market import StressSignal, fred_series
from covenant_desk.pipeline import load_panel
from covenant_desk.risk import (
    backtest_one_quarter,
    backtest_one_year_event,
    breach_probability,
    brier,
    ebitda_at_risk,
    firm_state,
    fit,
    simulate_ltm,
    summarise_hits,
    yoy_growth,
)
from covenant_desk.xbrl import ltm

SCREEN_THRESHOLDS = (4.5, 6.0)  # typical maintenance-covenant levels for leveraged loans
COVID_STRESS_DATE = date(2020, 3, 31)  # credit-market stress at the start of COVID


def worst_ltm_decline(ltm_series: pd.Series) -> float:
    """Worst fall in LTM EBITDA over any 4-quarter window in the borrower's own history."""
    s = ltm_series.sort_index()
    worst = 0.0
    for i in range(4, len(s)):
        base = s.iloc[i - 4]
        if base > 0 and 330 <= (s.index[i] - s.index[i - 4]).days <= 400:
            worst = min(worst, s.iloc[i] / base - 1)
    return worst


def stressed_leverage(debt: float, ltm_now: float, decline: float) -> float:
    after = ltm_now * (1 + decline)
    return float("inf") if after <= 0 else debt / after


def main() -> None:
    panel = load_panel()
    growth = {k: yoy_growth(v["quarterly_ebitda"]) for k, v in panel.items()}
    baa = StressSignal(fred_series("BAA10Y"), "Moody's BAA - 10y Treasury spread")
    model = fit(growth, stress=baa.ratio)
    covid_ratio = baa.ratio(COVID_STRESS_DATE)
    today_ratio = baa.ratio(date.today())

    print(f"\nModel (pooled, all data): mu={model.mu:.3f} phi={model.phi:.3f} "
          f"lambda={model.lam} nu={model.nu} beta={model.beta}")
    print(f"Credit stress ratio today {today_ratio:.2f}; at {COVID_STRESS_DATE}: {covid_ratio:.2f}")

    rows = []
    for firm, data in panel.items():
        q = data["quarterly_ebitda"]
        ltm_series = ltm(q)
        debt_series = data["debt"]
        if ltm_series.empty or debt_series.empty:
            continue
        asof = ltm_series.index[-1]
        debt = float(debt_series[debt_series.index <= asof].iloc[-1])
        current = float(ltm_series.iloc[-1])
        state = firm_state(model, q, debt, asof=asof)
        if state is None or current <= 0:
            continue
        base = simulate_ltm(model, state)
        stressed = simulate_ltm(model, state, stress_override=covid_ratio)
        worst = worst_ltm_decline(ltm_series)
        row = {
            "firm": firm, "asof": asof, "stale": (date.today() - asof).days > 200,
            "ltm_ebitda_m": round(current / 1e6), "gross_debt_m": round(debt / 1e6),
            "leverage": round(debt / current, 2),
            "ebitda_at_risk_5pct": round(ebitda_at_risk(base, current, 0.05), 3),
            "worst_hist_4q_decline": round(worst, 3),
            "leverage_after_worst": round(stressed_leverage(debt, current, worst), 2),
        }
        for t in SCREEN_THRESHOLDS:
            row[f"p_lev_gt_{t}"] = round(breach_probability(base, debt, t), 3)
            row[f"p_lev_gt_{t}_credit_stress"] = round(breach_probability(stressed, debt, t), 3)
        rows.append(row)
    table = pd.DataFrame(rows).sort_values("leverage")
    pd.set_option("display.width", 220)
    pd.set_option("display.max_columns", 30)
    print("\nScreening (debt held constant, next 4 quarters):")
    print(table.to_string(index=False))

    # GTX against its actual covenant.
    gtx = panel.get("GTX")
    if gtx is not None:
        q, debt_s = gtx["quarterly_ebitda"], gtx["debt"]
        asof = ltm(q).index[-1]
        debt = float(debt_s[debt_s.index <= asof].iloc[-1])
        state = firm_state(model, q, debt, asof=asof)
        limit = GTX_LEVERAGE_COVENANT.max_ratio
        p_now = breach_probability(simulate_ltm(model, state), debt, limit)
        p_stress = breach_probability(simulate_ltm(model, state, stress_override=covid_ratio), debt, limit)
        print(f"\nGTX vs its {limit:.2f}x springing covenant: P(leverage > {limit}x within 4 quarters) "
              f"= {p_now:.2%} today, {p_stress:.2%} under 2020-style credit stress. "
              f"The test applies only if the revolver is >35% drawn (0% at last 10-Q).")

    # Backtests.
    df_q, summ_q, _ = backtest_one_quarter(growth, stress=baa.ratio)
    ex2020 = summarise_hits(df_q[df_q["date"].map(lambda d: d.year) != 2020])
    df_y = backtest_one_year_event(panel, growth, baa.ratio)
    result = {"one_quarter": summ_q, "one_quarter_ex_2020": ex2020,
              "one_year_drop_30pct": brier(df_y)}
    print("\nBacktest:", json.dumps(result, indent=1, default=float))

    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    table.to_csv(config.DATA_DIR / "risk_summary.csv", index=False)
    (config.DATA_DIR / "backtest.json").write_text(json.dumps(result, indent=1, default=float))
    print(f"\nSaved {config.DATA_DIR / 'risk_summary.csv'} and backtest.json")
    np.set_printoptions()


if __name__ == "__main__":
    main()
