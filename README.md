# Covenant Desk

Automated monitoring of financial covenants in leveraged credit agreements, built from SEC filings.

> Work in progress. Educational project, not investment or credit advice.

## The problem

A leveraged loan comes with **financial covenants**: rules such as "gross debt may not exceed
4.70x EBITDA". Credit analysts check them every quarter, for every borrower. To do that they
read a 300-page credit agreement, find the exact definitions, pull the numbers from the latest
10-Q, and recalculate. It is slow and easy to get wrong, mainly because **"EBITDA" in a credit
agreement is not the EBITDA in the income statement.** The agreement defines its own version,
with a long list of add-backs.

Covenant Desk automates this workflow:

1. Find a borrower's credit agreement and every amendment on SEC EDGAR
2. Extract the covenant terms, each with its exact citation *(AI agent, Phase 3)*
3. Compute compliance and headroom from XBRL financial data *(deterministic Python)*
4. Estimate the probability of a breach *(statistical model, Phase 4)*
5. Re-run automatically when a new 10-Q is filed and alert on deteriorating names *(Phase 5)*

**Design rule:** agents read and cite. Code calculates. Every number traces back to either a
quoted clause or a tested function.

## Case study: Garrett Motion (GTX)

This continues my [GTX LBO project](https://github.com/santharhtut007007-sudo/lbo-valuation-project).

GTX's current loan is the **Amended and Restated Credit Agreement** (8-K Exhibit 10.1,
31 Jan 2025), amended in Aug 2025 and May 2026. Its one financial covenant, Section 6.13:

> "...so long as on the last day of any such fiscal quarter the aggregate amount of Revolving
> Loans and Letters of Credit outstanding [...] exceeds 35.0% of the aggregate amounts of all
> Revolving Commitments [...], Holdings will not permit the Consolidated Gross Total Leverage
> Ratio to exceed 4.70 to 1.00."

What a credit analyst reads in that sentence:

- **Springing covenant.** It is tested only if the revolver is more than 35% drawn.
- **Revolver-only.** A breach is a default only for revolving lenders. The term loans are
  *covenant-lite* (no maintenance test), as is typical for recent leveraged loans.
- **Gross, not net, leverage.** Cash is not subtracted.
- **Generous EBITDA.** The definition adds back restructuring, "business optimization",
  litigation costs, and **run-rate cost savings expected within 24 months, with no cap**.
  These items make covenant EBITDA higher than reported EBITDA.

### Current result

```
uv run covenant-desk GTX
```

| Quarter | LTM EBITDA | Gross debt | Leverage | Cushion |
|---|---|---|---|---|
| 2025-12-31 | $611M | $1,418M | 2.32x | 51% |
| 2026-03-31 | $650M | $1,417M | 2.18x | 54% |
| 2026-06-30 | $677M | $1,367M | 2.02x | 57% |
| 2026-09-30 | $692M | $1,366M | 1.97x | 58% |

At 1.97x against a 4.70x limit, EBITDA could fall about 58% at today's debt before a breach.

**And this quarter the covenant is not tested at all.** The Q3 2026 10-Q shows the $630M
revolver with no borrowings or letters of credit outstanding (0% usage, below the 35%
trigger). The code checks that this quote really appears in the 10-Q.

### Which agreement is in force?

GTX has filed 27 loan-related EX-10 exhibits since 2018. Covenant Desk classifies each one
and rebuilds the chain:

| Filed | Document | Role |
|---|---|---|
| 2018-10-01 | Credit Agreement (spin-off from Honeywell) | superseded |
| 2020-06-12 | First Amendment: COVID "Relief Period" for the leverage and interest-coverage covenants | superseded |
| 2020-10-09 | Debtor-in-possession credit agreement (Chapter 11) | bankruptcy loan |
| 2021-04-30 | New Credit Agreement on exit from Chapter 11 (Amendments No. 1-5 follow) | superseded |
| **2025-01-31** | **Restatement Agreement (amended and restated credit agreement)** | **base in force** |
| 2025-08-07 | Amendment No. 1 | in force, no covenant change |
| 2026-05-20 | Amendment No. 2 | in force; flagged by the rule-based check, but it only mentions the First Lien Leverage Ratio for **pricing**, not the covenant |

That last row shows why a rules-only system is not enough. A keyword match cannot tell a
pricing grid from a covenant. Phase 3's Contract Agent makes that call, with a quote.

EBITDA here is a **proxy**: net income + interest + taxes + D&A + restructuring, rebuilt from
XBRL. The agreement's further add-backs (run-rate savings, etc.) are not in XBRL, so true
covenant EBITDA would be equal or higher and true leverage equal or lower.

## The borrower universe (18 leveraged issuers)

`uv run python -m covenant_desk.pipeline` runs every borrower in [`universe.csv`](universe.csv):
auto suppliers (GTX and peers), rental, cruise and leisure, gaming, airlines, media, industrials.

| Result | Count |
|---|---|
| Agreement in force identified | 18 of 18 |
| ...of which the base is a long amendment carrying a **conformed copy** | 13 |
| Current XBRL leverage proxy (latest quarter, not stale) | 12 |
| Stale or incomplete data, flagged rather than shown as current | 6 (CPS, CAR, HTZ, CNK, SABR, URI) |
| Proxy known to be invalid (rental fleet depreciation) | 4 (CAR, HTZ, HRI, URI) |

Gross leverage proxy, latest quarter (before the agreements' extra add-backs):

| Borrower | Quarter | Gross leverage |
|---|---|---|
| VC | 2026-09 | 0.96x |
| DAN | 2026-09 | 1.45x |
| GTX | 2026-09 | 1.97x |
| CCL | 2026-08 | 3.29x |
| ADNT | 2026-09 | 3.30x |
| CZR | 2026-09 | 3.40x |
| GT | 2026-09 | 5.16x |
| DCH | 2026-09 | 5.21x |
| NCLH | 2026-09 | 6.58x |
| AAL | 2026-09 | 8.41x |
| IHRT | 2026-09 | 8.75x |

These are screening numbers, not covenant tests. Each borrower's covenant (if any), its
definitions and its add-backs still have to be extracted from its agreement, which is the
Contract Agent's job in Phase 3.

### What scaling to 18 borrowers taught

Every one of these was found by running the universe and checking numbers that looked wrong:

1. **Most borrowers never refile the base agreement.** They file large amendments that
   attach the whole agreement, as amended, as an annex. Treating those as the base fixed 13 of 18.
2. **Lenders name the same thing differently:** "Credit and Guaranty Agreement", "Loan
   Agreement", "Revolving Credit Agreement".
3. **A silent zero is the worst bug.** The first version treated missing interest expense as
   $0, which inflated EBITDA without any warning. Core lines (net income, interest, taxes,
   D&A) are now required. A missing one makes that quarter "unknown", never zero.
4. **Companies change XBRL tags over time.** Some now report only *net* interest
   (`InterestIncomeExpenseNonoperatingNet`, negative when it is an expense), so the code supports
   sign-flipped fallbacks.
5. **One-off gains distort EBITDA.** Dana's Q1 2026 net income included a $1.1B gain from
   selling a business (discontinued operations). The first run showed 0.51x leverage. Removing
   discontinued operations, as the agreement's definition does, gives 1.45x.
6. **Sector matters.** Rental companies (Avis, Hertz, Herc, United Rentals) report fleet
   depreciation outside the standard D&A tag, so a generic proxy is wrong for them.

## The Contract Agent (Phase 3): a local LLM, $0, no data leaves the machine

Banks often cannot send loan documents to an outside AI service. The agent runs on a **local
open-source model through [Ollama](https://ollama.com)**: no API key, no cost, fully private.
The design point is that a *small* model is enough when code does the heavy lifting and checks
every answer.

```
uv run covenant-desk-eval --model qwen2.5vl:7b
```

### How it works

1. **Retrieval (code).** Split the 100,000-170,000-word agreement into numbered sections and
   defined terms. Rank sections by heading ("Financial Covenant", "... Leverage Ratio",
   "Liquidity"), the canonical covenant sentence ("will not permit ... to exceed 4.70 to
   1.00"), and cross-references ("the Financial Covenant means the covenant set forth in
   Section 5.04"). The model sees 1-3 short passages, not the whole agreement.
2. **Extraction (model).** Fill a strict schema: metric, direction, threshold, springing
   condition, facilities protected, section, exact quote. The model may ask for up to four
   defined terms, which a code tool looks up (a small agent loop).
3. **Verification (code).** Six independent checks, each catching a different kind of mistake:

| Check | Catches |
|---|---|
| quote is in the agreement word for word | paraphrased or invented text |
| threshold number is in the quote | wrong number ($2.5B written for $250M) |
| section exists | wrong citation |
| direction matches wording ("exceed" means maximum, "less than" means minimum) | inverted covenants |
| springing wording in the section matches the springing flag | missed "only tested when..." conditions |
| metric matches the ratio's name | a Loan-to-Value cap labelled as minimum liquidity |

4. **One retry.** Failed checks go back to the model with the exact error. Anything still
   failing is marked **unverified for human review**, never silently accepted.

### Evaluation

[Answer key](evaluation/gold_covenants.json): 14 borrowers, 14 covenants plus 3 covenant-lite
agreements. Each label has a quote, and a test checks every quote against the filing. The key
was drafted with Claude Code and still needs a human review (`reviewed_by` is empty). The 4
rental companies are excluded because the rule-based classifier picks a fleet-financing document
for them.

| Metric (95% Wilson CI) | qwen2.5vl 7B | gemma3 4B |
|---|---|---|
| Covenant-lite call | 14/14 (0.79-1.00) | 13/14 |
| Recall | 11/14 (0.52-0.92) | **14/14** (0.79-1.00) |
| Precision | **11/12** (0.65-0.99) | 14/20 (0.48-0.86) |
| Threshold correct, when found | 11/11 | 14/14 |
| Springing correct, when found | 9/11 | 9/14 |
| **Verified -> fully correct** | **7/7** (0.65-1.00) | 9/10 (0.60-0.98) |
| Time for 14 agreements (M4 Pro) | 2.1 min | 3.6 min |
| Cost | $0 | $0 |

**What this shows:**

- **The verifier is what makes a small model usable.** Unverified answers include real errors
  (Goodyear's paraphrased quote, missed springing conditions at Cooper-Standard and Goodyear).
  Verified answers have been right every time so far for the 7B model.
- **Each check was added because of an observed failure:** the first version's "verified"
  answers were right only 5 of 7 times; adding the direction, springing and metric checks made it 7 of 7.
- **Small models have typical blind spots:** "will not: (a) Permit X to be greater than..."
  (a negation stated far from the clause), and minimum-liquidity tests not recognised as covenants
  until shown an example.
- The sample is small, so the intervals are wide. A larger answer key is the most valuable next step.

### Data lessons from this phase

- **Redlined conformed copies.** Amendments show deleted words with strikethrough. Plain
  HTML-to-text keeps them, producing "July 1, 2024 through DecemberMarch 31, 20242026". The
  text extractor now drops struck-through text, so the agreement reads *as amended*.
- **Covenant-lite is common.** Adient's, Caesars' and Sabre's agreements in force have no
  maintenance covenant at all. Saying so correctly counts as a right answer.
- **Not all covenants are leverage ratios:** minimum liquidity (American Airlines $2.0B;
  Norwegian $250M), Loan-to-Value (Norwegian 0.70), debt-to-capital (Carnival 0.65), springing
  fixed-charge coverage in ABLs (Cooper-Standard, iHeart).

## Breach probability (Phase 4)

`uv run covenant-desk-risk` simulates each borrower's next four quarters and backtests the model.

### The model

Quarterly EBITDA is compared with the same quarter a year earlier, which removes seasonality.
That year-over-year growth `g` follows

```
g_t = mu + phi (g_{t-1} - mu) + sigma_t z_t
sigma_t^2 = EWMA of past squared residuals  x  (credit spread / its history)^beta
z_t ~ Student-t(nu), scaled to variance 1
```

- **AR(1):** bad years tend to persist (phi about 0.3).
- **EWMA volatility:** crises cluster.
- **Credit-spread scaling:** Moody's BAA minus 10-year Treasury, from FRED. Company data are
  reported late and cannot warn of a sudden shock; credit markets can.
- **Student-t shocks:** maximum likelihood picks nu = 3, so tails are very fat.
- mu (median), phi, lambda, nu and beta are estimated on the pooled universe, because single
  companies have too few quarters. Negative EBITDA is allowed, and a negative LTM counts as a breach.

20,000 paths per borrower give the distribution of LTM EBITDA and leverage (debt held constant).

### Does it work? Out-of-sample backtests

Every forecast uses a model fitted only on earlier data.

**One quarter ahead: 5% and 1% quantiles of growth, 545 firm-quarters (2015-2026)**

| Model | 5% hit rate | Kupiec p | Christoffersen p | Misses in 2020 |
|---|---|---|---|---|
| A: OLS mean, EWMA, Student-t | 8.1% | 0.002 | 0.08 | 19 |
| B: robust (median) mean | 7.3% | 0.019 | 0.03 | 18 |
| C: B + VIX scaling | 7.5% | 0.012 | 0.04 | 18 |
| **D: B + credit-spread scaling (chosen)** | **7.2%** | 0.030 | **0.19** | **14** |
| D, excluding 2020 | **5.03%** (25 / 497) | **0.98** | 0.81 | - |

- **Outside 2020 the 5% quantile is almost exact.** At 1% it is slightly too thin (2.0%, p = 0.046).
- **2020 breaks every version.** A single-company model cannot foresee a shock that hits every
  borrower at once. The credit spread reduces the damage (14 misses instead of 18, and no
  clustering), but it does not fix it.
- **The VIX did not help out of sample.** Fitted on pre-2020 data, the likelihood chose beta = 0:
  the VIX had never predicted EBITDA before COVID. The credit spread already had beta = 1 before 2020.
- **The robust mean matters:** an OLS mean of about 15% a year (inflated by 2021 rebounds)
  falls to about 4.5%. The OLS version would understate breach risk.

**One year ahead: probability that LTM EBITDA falls more than 30%**

A 30% fall is what takes 3.3x leverage to 4.7x. 125 non-overlapping forecasts, 18 events.

| | Result |
|---|---|
| Brier skill vs. always forecasting the base rate | -0.05 (95% bootstrap CI -0.28 to +0.11) |
| AUC (ranking) | 0.55 (CI 0.39 to 0.71); 0.69 excluding forecasts made just before COVID |
| Large misses (event happened, forecast < 10%) | 6, of which 4 were forecasts made in late 2019, just before COVID (CCL, CNK, AAL, GT) |

**Honest conclusion:** the model is calibrated for short-horizon risk in normal markets and gives
a consistent stress framework. **It is not a reliable one-year breach predictor on its own**, and
125 forecasts are too few to prove otherwise. That gives Phase 3 a measurable goal: does
information an agent reads from filings (guidance, liquidity commentary, amendment activity)
improve the Brier score and AUC?

### Current results

| Borrower | Leverage | 1-yr EBITDA-at-Risk (5%) | P(lev > 6.0x, 4Q) | ...under 2020 credit stress |
|---|---|---|---|---|
| VC | 0.96x | 37% | 0.6% | 4.0% |
| DAN | 1.45x | 57%* | 2.8% | 12.5% |
| GTX | 1.97x | 5% | 0.0% | 0.3% |
| CCL | 3.29x | 15% | 0.5% | 3.6% |
| ADNT | 3.30x | 2% | 0.0% | 0.4% |
| CZR | 3.40x | 9% | 0.1% | 1.4% |
| GT | 5.16x | 27% | 17.9% | 35.7% |
| DCH | 5.21x | 16% | 7.2% | 23.3% |

NCLH (6.58x), AAL (8.41x) and IHRT (8.75x) are already above 6.0x. Stale borrowers are left out.
\*Dana's figure is inflated: after its 2026 divestiture, the old quarters still include the sold
business, which distorts year-over-year growth.

**GTX against its actual covenant:** P(leverage > 4.70x within four quarters) = **0.02%** today
and **0.41%** under 2020-style credit stress. Even then, the test applies only if the revolver
is more than 35% drawn.

## How it works

| Module | What it does |
|---|---|
| `edgar.py` | SEC EDGAR client: ticker to CIK, full-text search for EX-10 credit agreements, download, XBRL company facts. Rate-limited and cached on disk |
| `documents.py` | Filing HTML to clean text |
| `xbrl.py` | Rebuilds true 3-month figures from messy XBRL (Q4 = annual minus 9-month YTD; cash-flow items reported only YTD) and last-twelve-months sums |
| `agreements.py` | Classifies exhibits (credit agreement, restatement, amendment, DIP loan, other) and finds the agreement in force |
| `covenant.py` | `CovenantTerms` and `FacilitySnapshot` (with citations), the springing-covenant test, `quote_in_document` (code check of every quote), the covenant-EBITDA build mapped clause by clause, leverage and headroom |
| `pipeline.py` | Runs everything for each borrower in `universe.csv` and saves `data/universe_summary.csv` |
| `market.py` | FRED stress series (VIX, BAA spread) and the point-in-time stress ratio |
| `risk.py` | Growth model (AR(1), EWMA, Student-t, credit-spread scaling), MLE fit, Monte Carlo, breach probability, EBITDA-at-Risk, Kupiec / Christoffersen / Brier backtests |
| `report.py` | `covenant-desk-risk`: screening table, stress tests, GTX breach probability, backtests |
| `sections.py` | Agent tools: section and definition splitting, covenant retrieval, cross-references |
| `llm.py` | Local Ollama client with structured (JSON-schema) output, on-disk cache, usage stats |
| `agent.py` | Contract Agent: extraction schema, definition look-up loop, six verification checks, retry |
| `evaluate.py` | `covenant-desk-eval`: scores the agent against `evaluation/gold_covenants.json` with Wilson CIs |
| `cli.py` | `covenant-desk <TICKER>` report |

## Run it

```
uv sync
ollama pull qwen2.5vl:7b      # free local model for the Contract Agent
uv run covenant-desk GTX
uv run covenant-desk-risk     # breach probabilities, stress tests, backtests
uv run pytest -m "not live"   # offline unit tests
uv run pytest -m live         # checks against real SEC data
```

Set `SEC_USER_AGENT="Your Name your@email.com"`. The SEC asks automated clients to identify themselves.

## Roadmap

- [x] Phase 1: EDGAR data layer, XBRL quarterly rebuild, GTX covenant test, tests
- [x] Phase 2: agreement in force (base agreement + amendments), springing-covenant test with
      revolver usage, code verification of quotes, an 18-borrower universe
- [x] Phase 3: Contract Agent on a local LLM (Ollama): code retrieval, schema extraction,
      definition look-up, six code checks, one retry, evaluation against an answer key.
      Still to do: amendment triage, revolver usage from 10-Qs, a human approval screen,
      a larger answer key
- [x] Phase 4: breach probability (EBITDA simulation, credit-spread scaling, stress tests,
      out-of-sample backtests). Still to do: a common-factor shock, debt paths, rate shocks on
      floating debt, divestiture adjustments
- [ ] Phase 5: scheduled runs on new filings, alerts, dashboard
- [ ] Evaluation: hand-labelled agreements, extraction accuracy with confidence intervals,
      run-to-run agreement, cost per agreement

## Known limitations (so far)

- Covenant EBITDA is an XBRL proxy (see above).
- Gross debt uses carrying values (net of discount and fees), not face amounts.
- Revolver usage is entered by hand for GTX (with a verified quote). The 10-Q tables differ
  by company, so extracting it automatically is a job for the agent in Phase 3.
- Exhibit classification is rule-based. It reads only titles and document length, and can be
  wrong on unusual documents. "Conformed copy" is inferred from length (40,000+ words).
- The universe EBITDA proxy uses one generic build for every company. Sector-specific builds
  (rental fleets, airlines' aircraft rent, casino lease obligations) are not done yet.
- History before 2025 is tested against the 2025 terms. Earlier agreements had different
  covenants. 2020-2022 figures are also distorted by GTX's Chapter 11 case (reorganization
  items, debt reclassified as liabilities subject to compromise).
