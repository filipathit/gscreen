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
| 14 | Single vendor, hardcoded | Universe, prices and fundamentals chosen independently at run time; free stack available |
| 15 | One failed request kills the run | Per-ticker failures logged as `[data]` rejections |

## Choosing your data at run time

Three capabilities, each swappable independently:

| | eodhd | yahoo | edgar | static | fixture |
|---|---|---|---|---|---|
| universe | screener | – | – | `universe.txt` | offline |
| prices | paid | free, decades of history | – | – | offline |
| fundamentals | paid snapshot | – | **free, point-in-time** | – | offline |

Presets are shorthand; any flag overrides the preset:

```bash
python -m gscreen.cli sources                     # list everything
python -m gscreen.cli screen --preset offline     # fixtures, no network
python -m gscreen.cli screen --preset free        # static + Yahoo + EDGAR, $0
python -m gscreen.cli screen --preset paid        # EODHD throughout
python -m gscreen.cli screen --preset hybrid      # EODHD discovery, EDGAR facts
python -m gscreen.cli screen --preset free --prices eodhd   # mix freely
```

**free** costs nothing but discovers nothing — the universe is whatever is in
`universe.txt`. **hybrid** is the interesting one: EODHD's screener finds
candidates, EDGAR supplies fundamentals that are actually point-in-time.

Trade-offs worth knowing before you pick:

- **Yahoo** has had no official API since 2017. These are the endpoints the
  website calls; they throttle hard, and shared cloud IPs (CI runners) get
  throttled first. A failed fetch is logged as a `[data]` rejection and the
  run continues rather than dying.
- **EDGAR** is official, free, and stamps every fact with its filing date —
  but covers US filers only, has no market cap, no EBITDA, and no GICS
  sector. Those fields stay null and the model is told they're missing rather
  than being handed a guess.

## The point-in-time finding

Running the backtest surfaces something the article never confronts: a vendor
fundamentals endpoint serves *current* statements. Replay a past date and you
score the screen on figures nobody had then. The default config refuses to run
in that state and says so; `--ignore-lookahead` shows the contaminated version,
clearly labelled.

**EDGAR fixes this.** Every XBRL fact carries a `filed` date, so
`from_edgar(..., as_of=...)` returns only what was public then — including
using the restatement that was current at the time rather than the latest one.
With `--preset free` or `--preset hybrid`, the guard lifts automatically
because the source is genuinely point-in-time, and the backtest becomes
evidence instead of scaffolding.

## Running it

```bash
pip install requests pytest
export EODHD_API_KEY=...        # only for the paid/hybrid presets
export ANTHROPIC_API_KEY=...    # only for --llm
export SEC_USER_AGENT="you@example.com"   # EDGAR asks for a contact address

PYTHONPATH=. python -m gscreen.cli screen   --preset offline
PYTHONPATH=. python -m gscreen.cli backtest --preset free
PYTHONPATH=. python -m gscreen.cli screen   --preset free --as-of 2026-08-15 --llm
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
