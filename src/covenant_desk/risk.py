"""Probability of a covenant breach: an EBITDA simulation model and its statistical backtest.

The model, in words
-------------------
Work with quarterly EBITDA compared with the same quarter one year earlier. This removes
seasonality, which matters for cruise lines and airlines:

    g_t = (E_t - E_{t-4}) / |E_{t-4}|          year-over-year growth of quarter t

Growth is persistent (a bad year tends to stay bad for a while), so it follows an AR(1):

    g_t = mu + phi * (g_{t-1} - mu) + sigma_t * z_t

* sigma_t^2 = EWMA_t * m_t^beta
    - EWMA_t: exponentially weighted average of the borrower's past squared residuals.
      Volatility clusters: calm periods are followed by calm, crises by crises.
    - m_t: market stress ratio known when the forecast is made (VIX vs its history).
      Company data cannot warn of a sudden shock. The market can.
      beta = 0 switches this off; the data choose beta by maximum likelihood.
* z_t: Student-t shock with nu degrees of freedom, scaled to variance 1 (fat tails).

mu (robust: the median, so post-COVID rebounds do not inflate it) and phi are estimated on
the pooled universe, because individual companies have too few quarters. lambda (EWMA decay),
nu and beta are chosen by maximum likelihood on the pooled residuals.

Breach probability: simulate the next four quarters many times, recompute LTM EBITDA and
leverage (debt held constant), and count paths that break the limit. Negative LTM EBITDA
counts as a breach.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date

import numpy as np
import pandas as pd
from scipy import stats

GROWTH_CLIP = 3.0  # cap |g| at 300%: tiny bases make ratios explode
LAMBDA_GRID = (0.5, 0.6, 0.7, 0.8, 0.85, 0.9, 0.95)
NU_GRID = (2.5, 3.0, 4.0, 5.0, 7.0, 10.0, 30.0)
BETA_GRID = (0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0)

StressRatio = Callable[[date], float]  # date -> market stress ratio (1.0 = normal)


# --------------------------------------------------------------------------- data prep

def yoy_growth(quarterly: pd.Series) -> pd.Series:
    """Year-over-year growth of each quarter versus the same quarter a year earlier."""
    q = quarterly.dropna().sort_index()
    out = {}
    dates = list(q.index)
    for i, d in enumerate(dates):
        for j in range(max(0, i - 6), i):  # find the date about one year earlier
            if 350 <= (d - dates[j]).days <= 380:
                base = q.iloc[j]
                if abs(base) > 0.02 * q.abs().mean():  # skip near-zero bases
                    out[d] = float(np.clip((q.iloc[i] - base) / abs(base), -GROWTH_CLIP, GROWTH_CLIP))
                break
    return pd.Series(out, dtype="float64")


@dataclass
class Pairs:
    """Consecutive quarters: forecast origin (t-1) and outcome (t)."""

    prev: np.ndarray  # g_{t-1}
    curr: np.ndarray  # g_t
    origin: list  # date of t-1, when the forecast is made
    when: list  # date of t


def consecutive_pairs(g: pd.Series) -> Pairs:
    prev, curr, origin, when = [], [], [], []
    items = list(g.items())
    for (d0, v0), (d1, v1) in itertools.pairwise(items):
        if 80 <= (d1 - d0).days <= 100:
            prev.append(v0)
            curr.append(v1)
            origin.append(d0)
            when.append(d1)
    return Pairs(np.array(prev), np.array(curr), origin, when)


# --------------------------------------------------------------------------- the model

def _std_t_logpdf(x: np.ndarray, nu: float) -> np.ndarray:
    """Log density of a Student-t rescaled to variance 1."""
    scale = np.sqrt((nu - 2) / nu)
    return stats.t.logpdf(x / scale, nu) - np.log(scale)


def std_t_ppf(p, nu: float):
    """Quantile of the variance-1 Student-t."""
    return stats.t.ppf(p, nu) * np.sqrt((nu - 2) / nu)


@dataclass
class GrowthModel:
    mu: float
    phi: float
    lam: float
    nu: float
    beta: float
    pooled_var: float
    stress: StressRatio | None = field(default=None, repr=False)

    def mean(self, g_prev):
        return self.mu + self.phi * (g_prev - self.mu)

    def ewma_path(self, residuals: np.ndarray) -> np.ndarray:
        """EWMA variance before each residual; the last element is the next-quarter forecast."""
        s2 = np.empty(len(residuals) + 1)
        s2[0] = self.pooled_var  # start from the universe-wide variance
        for i, e in enumerate(residuals):
            s2[i + 1] = self.lam * s2[i] + (1 - self.lam) * e * e
        return s2

    def stress_multiplier(self, origins) -> np.ndarray:
        if self.stress is None or self.beta == 0:
            return np.ones(len(origins))
        return np.array([self.stress(d) for d in origins]) ** self.beta


def _residuals(pairs: dict[str, Pairs], mu: float, phi: float) -> dict[str, np.ndarray]:
    return {f: p.curr - (mu + phi * (p.prev - mu)) for f, p in pairs.items()}


def fit(growth_by_firm: dict[str, pd.Series], before: date | None = None,
        stress: StressRatio | None = None, robust_mean: bool = True) -> GrowthModel:
    """Estimate the pooled model, optionally using only quarters strictly before a date."""
    pairs = {}
    for firm, g in growth_by_firm.items():
        g = g[g.index < before] if before is not None else g
        p = consecutive_pairs(g)
        if len(p.prev) >= 4:
            pairs[firm] = p
    x_all = np.concatenate([p.prev for p in pairs.values()])
    y_all = np.concatenate([p.curr for p in pairs.values()])

    if robust_mean:
        mu = float(np.median(y_all))
        phi = float(np.polyfit(x_all - mu, y_all - mu, 1)[0])
    else:
        phi, a = np.polyfit(x_all, y_all, 1)
        mu = float(a / (1 - phi))
    phi = float(np.clip(phi, -0.95, 0.95))
    resid = _residuals(pairs, mu, phi)
    pooled_var = float(np.var(np.concatenate(list(resid.values()))))

    betas = BETA_GRID if stress is not None else (0.0,)
    best = (-np.inf, 0.9, 5.0, 0.0)
    for lam in LAMBDA_GRID:
        base = GrowthModel(mu, phi, lam, 5.0, 0.0, pooled_var)
        ewma = {f: base.ewma_path(e)[:-1] for f, e in resid.items()}
        for beta in betas:
            m = GrowthModel(mu, phi, lam, 5.0, beta, pooled_var, stress)
            var = {f: ewma[f] * m.stress_multiplier(pairs[f].origin) for f in resid}
            z = np.concatenate([resid[f] / np.sqrt(var[f]) for f in resid])
            log_det = 0.5 * sum(np.log(v).sum() for v in var.values())
            for nu in NU_GRID:
                ll = _std_t_logpdf(z, nu).sum() - log_det
                if ll > best[0]:
                    best = (ll, lam, nu, beta)
    _, lam, nu, beta = best
    return GrowthModel(mu, phi, lam, nu, beta, pooled_var, stress)


@dataclass
class FirmState:
    """What the model needs to know about one borrower on one date."""

    asof: date
    last_four_quarters: np.ndarray  # quarterly EBITDA, oldest first
    last_growth: float  # g at the latest quarter
    next_ewma_var: float  # EWMA variance forecast for the next residual
    debt: float


def firm_state(model: GrowthModel, quarterly: pd.Series, debt: float,
               asof: date | None = None) -> FirmState | None:
    q = quarterly.dropna().sort_index()
    if asof is not None:
        q = q[q.index <= asof]
    g = yoy_growth(q)
    if len(q) < 4 or g.empty or g.index[-1] != q.index[-1]:
        return None
    p = consecutive_pairs(g)
    resid = p.curr - model.mean(p.prev)
    return FirmState(q.index[-1], q.iloc[-4:].to_numpy(dtype=float), float(g.iloc[-1]),
                     float(model.ewma_path(resid)[-1]), float(debt))


def simulate_ltm(model: GrowthModel, state: FirmState, horizon: int = 4,
                 n_paths: int = 20_000, seed: int = 7,
                 stress_override: float | None = None) -> np.ndarray:
    """Simulated LTM EBITDA for each of the next `horizon` quarters, shape (n_paths, horizon).

    Market stress is held at today's level over the horizon (or at `stress_override`).
    """
    rng = np.random.default_rng(seed)
    scale = np.sqrt((model.nu - 2) / model.nu)
    if stress_override is not None:
        mult = stress_override ** model.beta
    else:
        mult = float(model.stress_multiplier([state.asof])[0])
    window = np.tile(state.last_four_quarters, (n_paths, 1))  # rolling last 4 quarters
    g_prev = np.full(n_paths, state.last_growth)
    s2 = np.full(n_paths, state.next_ewma_var)
    ltm = np.empty((n_paths, horizon))
    for k in range(horizon):
        e = np.sqrt(s2 * mult) * rng.standard_t(model.nu, n_paths) * scale
        g = np.clip(model.mean(g_prev) + e, -GROWTH_CLIP, GROWTH_CLIP)
        same_quarter_last_year = window[:, 0]
        new_q = same_quarter_last_year + g * np.abs(same_quarter_last_year)
        window = np.column_stack([window[:, 1:], new_q])
        ltm[:, k] = window.sum(axis=1)
        s2 = model.lam * s2 + (1 - model.lam) * e * e
        g_prev = g
    return ltm


def breach_probability(ltm_paths: np.ndarray, debt: float, max_ratio: float) -> float:
    """Share of paths where leverage exceeds the limit in any quarter (or EBITDA <= 0)."""
    with np.errstate(divide="ignore"):
        lev = np.where(ltm_paths > 0, debt / ltm_paths, np.inf)
    return float((lev > max_ratio).any(axis=1).mean())


def ebitda_at_risk(ltm_paths: np.ndarray, current_ltm: float, level: float = 0.05) -> float:
    """Fall in LTM EBITDA one year ahead that is exceeded with probability `level`."""
    return float(1 - np.quantile(ltm_paths[:, -1], level) / current_ltm)


# --------------------------------------------------------------------------- backtests

def kupiec_pof(hits: int, n: int, p: float) -> tuple[float, float]:
    """Kupiec proportion-of-failures test. Returns (LR statistic, p-value), chi2 with 1 df."""
    if n == 0:
        return float("nan"), float("nan")
    phat = hits / n

    def ll(prob):
        prob = min(max(prob, 1e-12), 1 - 1e-12)
        return (n - hits) * np.log(1 - prob) + hits * np.log(prob)

    lr = -2 * (ll(p) - ll(phat))
    return float(lr), float(stats.chi2.sf(lr, 1))


def christoffersen_independence(sequences: list[np.ndarray]) -> tuple[float, float]:
    """Do hits cluster in time? Pools 0/1 transition counts across firms. (LR, p), chi2 1 df."""
    n = np.zeros((2, 2))
    for s in sequences:
        for a, b in itertools.pairwise(s):
            n[int(a), int(b)] += 1
    n00, n01, n10, n11 = n[0, 0], n[0, 1], n[1, 0], n[1, 1]
    if n01 + n11 == 0:
        return 0.0, 1.0
    pi0 = n01 / max(n00 + n01, 1)
    pi1 = n11 / max(n10 + n11, 1)
    pi = (n01 + n11) / n.sum()

    def xlogy(x, y):
        return x * np.log(y) if x > 0 else 0.0

    l_null = xlogy(n00 + n10, 1 - pi) + xlogy(n01 + n11, pi)
    l_alt = xlogy(n00, 1 - pi0) + xlogy(n01, pi0) + xlogy(n10, 1 - pi1) + xlogy(n11, pi1)
    lr = -2 * (l_null - l_alt)
    return float(lr), float(stats.chi2.sf(lr, 1))


def backtest_one_quarter(growth_by_firm: dict[str, pd.Series], start_year: int = 2015,
                         levels=(0.05, 0.01), stress: StressRatio | None = None,
                         robust_mean: bool = True) -> tuple[pd.DataFrame, dict, dict]:
    """Out-of-sample test of one-quarter-ahead growth quantiles.

    For every firm-quarter from `start_year`, refit on earlier years only, forecast the 5% and
    1% quantiles of g_t, and record a "hit" when the actual g_t falls below. A calibrated
    model has hit rates of 5% and 1%, with hits not clustered in time.
    """
    rows, params = [], {}
    years = sorted({d.year for g in growth_by_firm.values() for d in g.index if d.year >= start_year})
    for year in years:
        model = fit(growth_by_firm, before=date(year, 1, 1), stress=stress, robust_mean=robust_mean)
        params[year] = (round(model.mu, 3), round(model.phi, 3), model.lam, model.nu, model.beta)
        for firm, g in growth_by_firm.items():
            p = consecutive_pairs(g)
            if len(p.prev) == 0:
                continue
            fitted = model.mean(p.prev)
            var = model.ewma_path(p.curr - fitted)[:-1] * model.stress_multiplier(p.origin)
            for i, d in enumerate(p.when):
                if d.year != year:
                    continue
                row = {"firm": firm, "date": d, "actual": p.curr[i]}
                for lv in levels:
                    q = fitted[i] + np.sqrt(var[i]) * std_t_ppf(lv, model.nu)
                    row[f"hit_{lv}"] = int(p.curr[i] < q)
                rows.append(row)
    df = pd.DataFrame(rows).sort_values(["firm", "date"])
    return df, summarise_hits(df, levels), params


def summarise_hits(df: pd.DataFrame, levels=(0.05, 0.01)) -> dict:
    summary = {}
    for lv in levels:
        col = f"hit_{lv}"
        hits, n = int(df[col].sum()), len(df)
        lr_uc, p_uc = kupiec_pof(hits, n, lv)
        seqs = [grp[col].to_numpy() for _, grp in df.groupby("firm")]
        lr_ind, p_ind = christoffersen_independence(seqs)
        summary[lv] = {"n": n, "hits": hits, "expected": round(lv * n, 1),
                       "hit_rate": round(hits / n, 4), "kupiec_p": round(p_uc, 4),
                       "christoffersen_p": round(p_ind, 4),
                       "conditional_coverage_p": round(float(stats.chi2.sf(lr_uc + lr_ind, 2)), 4)}
    return summary


def backtest_one_year_event(panel: dict[str, dict], growth_by_firm: dict[str, pd.Series],
                            stress: StressRatio | None, drop: float = 0.30,
                            start_year: int = 2012, n_paths: int = 10_000) -> pd.DataFrame:
    """Out-of-sample test of the 1-year probability that LTM EBITDA falls by more than `drop`.

    This is the event a covenant cares about: a 30% EBITDA fall moves 3.3x leverage to 4.7x.
    Forecasts are made once a year per firm (at its fourth fiscal quarter), so outcomes do not
    overlap. Each forecast uses a model fitted only on data before the forecast date.
    """
    from covenant_desk.xbrl import ltm as ltm_fn

    models: dict[int, GrowthModel] = {}
    rows = []
    for firm, data in panel.items():
        q = data["quarterly_ebitda"].dropna().sort_index()
        ltm_series = ltm_fn(q)
        origins = [d for d in ltm_series.index if d.year >= start_year]
        origins = origins[3::4] if origins else []  # one forecast a year
        for origin in origins:
            later = ltm_series[ltm_series.index > origin]
            if len(later) < 4 or ltm_series[origin] <= 0:
                continue
            outcome_date = later.index[3]
            if not 330 <= (outcome_date - origin).days <= 400:
                continue
            year = origin.year
            if year not in models:
                models[year] = fit(growth_by_firm, before=origin, stress=stress)
            model = models[year]
            state = firm_state(model, q, debt=1.0, asof=origin)
            if state is None:
                continue
            paths = simulate_ltm(model, state, n_paths=n_paths, seed=year)
            current = float(ltm_series[origin])
            prob = float((paths[:, -1] < (1 - drop) * current).mean())
            happened = int(later.iloc[3] < (1 - drop) * current)
            rows.append({"firm": firm, "origin": origin, "prob": prob, "happened": happened})
    return pd.DataFrame(rows)


def brier(df: pd.DataFrame) -> dict:
    """Brier score of the model vs. always forecasting the historical base rate."""
    model_bs = float(((df["prob"] - df["happened"]) ** 2).mean())
    base_rate = float(df["happened"].mean())
    naive_bs = float(((base_rate - df["happened"]) ** 2).mean())
    return {"n": len(df), "events": int(df["happened"].sum()), "base_rate": round(base_rate, 3),
            "brier_model": round(model_bs, 4), "brier_naive": round(naive_bs, 4),
            "skill": round(1 - model_bs / naive_bs, 3) if naive_bs else float("nan")}
