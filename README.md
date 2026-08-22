# gscreen

A corrected rebuild of the pipeline in *"I Asked Claude to Find the Next
Generation of High-Growth Stocks"* (Medium, 16 Aug 2026).

The article's core idea is right: screen with data, use the model for
judgment over facts it can see. The implementation doesn't hold itself to
that standard. This does, and enforces it in code.

## What changed

| # | Article | Here |
|---|---------|------|
| 1 | Published model output cites ROE, PEG, operating margin, long-term debt, trailing P/E — none of which its code passes to the model | `FACT_FIELDS` whitelist + `validate_grounding()`: cited fields must have been supplied, and every numeric literal in the prose must trace to a supplied value |
| 2 | `"filters": str(filters)` — single quotes, invalid JSON | `serialise_filters()` uses `json.dumps`; regression test pins it |
| 3 | Prose claims pass 2 excludes short squeezes; no code looks at short interest | `squeeze_risk()` on `ShortPercentFloat`, enforced in pass 3 |
| 4 | Filters on one quarter of `QuarterlyRevenueGrowthYOY` after criticising exactly that | 3y revenue CAGR + consecutive-quarter streak, computed from the income statement rather than trusting the vendor field |
| 5 | 5-day momentum, defended as "earlier detection" | 12-1 momentum with a skip-month; short windows show reversal, not continuation |
| 6 | No leverage, FCF, or dilution test | Rule of 40, net debt/EBITDA, share-count growth |
| 7 | Hardcoded API key, no retry, no rate limit, no cache | Env var, backoff, throttle, on-disk cache |
| 8 | `data["Valuation"]` bracket access next to `.get()` — KeyErrors on thin coverage | Safe traversal throughout |
| 9 | No glue between pass 1 (dicts) and pass 2 (ticker strings) | Single `run_screen()` |
| 10 | Free-text model output, unparseable | JSON schema with `insufficient_data` verdict and a `missing_for_confidence` field |
| 11 | `claude-sonnet-4-6`, a generation behind | Pinned deliberately, `temperature=0`, text blocks concatenated rather than `content[0]` |
| 12 | Eight survivors, no word on what died or why | Every rejection carries a stage and a written reason |
| 13 | No backtest of any kind | `backtest.py`, with an explicit point-in-time guard |

## The point-in-time finding

Running the backtest surfaces something the article never confronts: the
fundamentals endpoint serves *current* statements. Replay a past date and you
score the screen on figures nobody had then. The default config refuses to
run in that state and says so. `--ignore-lookahead` shows the contaminated
version, clearly labelled. A defensible backtest needs point-in-time
fundamentals with original filing dates — a different (and paid) data
product.

## Running it

```bash
pip install requests pytest
export EODHD_API_KEY=...        # live mode only
export ANTHROPIC_API_KEY=...    # for --llm

PYTHONPATH=. python -m gscreen.cli screen   --provider fixture
PYTHONPATH=. python -m gscreen.cli backtest --provider fixture
PYTHONPATH=. python -m gscreen.cli screen   --provider eodhd --as-of 2026-08-15 --llm
PYTHONPATH=. python tools/grounding_demo.py
PYTHONPATH=. python -m pytest tests -q
```

`--llm` exits non-zero if the model's output fails grounding, so it can gate
a pipeline rather than just print a warning.

## Fixtures

`fixtures/` is deterministic synthetic data, not real financials. Nine
companies, each built to trip one branch of the screen (leverage, dilution,
one-quarter wonder, squeeze, earnings-reaction, flat momentum, slow CAGR).
Regenerate with `python tools/make_fixtures.py`.

## Not included, deliberately

Sector-relative scoring, currency handling for non-US listings, ADR
adjustments, delisting/survivorship correction, and transaction costs. Each
matters before this is anything other than a research filter.

## Scope

A screen and an interpretation layer. Not advice, not a recommendation, and
not evidence that any of it works — that requires the backtest the data
doesn't currently support.
