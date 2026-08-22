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

| | eodhd | tiingo | stooq | yahoo | edgar | static | fixture |
|---|---|---|---|---|---|---|---|
| universe | screener | – | – | – | – | `universe.txt` | offline |
| prices | paid | **free key, works from CI** | free, IP-blocked from CI | free, IP-blocked from CI | – | – | offline |
| fundamentals | paid snapshot | – | – | – | **free, point-in-time** | – | offline |

### Discovering a universe instead of guessing one

`universe.txt` is a hand-written list, which means the screen can only ever
confirm names someone already thought of — the same hindsight bias the source
article's listicles have, relocated into a file.

`--universe frames` replaces it with SEC's XBRL frames API, where one request
returns a single fact for *every* US filer in a period:

    data.sec.gov/api/xbrl/frames/us-gaap/Revenues/USD/CY2024.json

A dozen calls cover annual revenue across four years for roughly eight
thousand filers. Growth is computed cross-sectionally, the list is cut to a
couple of hundred candidates, and only then does the pipeline spend a price
call per name — which is what keeps a free Tiingo key viable.

```bash
python -m gscreen.cli universe --preset free-wide   # preview + funnel counts
python -m gscreen.cli screen   --preset free-wide --limit 50
```

The funnel prints why each filer dropped out: incomplete history, below the
revenue floor, below the growth floor, or no ticker (private filers, trusts,
and funds all file XBRL too).

**Frames are not point-in-time.** Unlike companyfacts, a frame carries no
`filed` date, and restatements silently replace originals. That is fine for
screening today and is a real look-ahead leak in a backtest, so
`FramesUniverse.point_in_time` is False and `backtest` prints a warning rather
than presenting contaminated numbers as clean. For a defensible backtest, fix
the universe in advance and use `--universe static`.

### Caching

Responses are cached under `_cache/<YYYY-MM-DD>/`, so a rerun on the same day
costs zero requests — which is what makes iterating viable against a free
price key of roughly 50 symbols an hour.

The scoping is by day rather than forever, and that matters: Tiingo URLs embed
`endDate` and self-expire, but EDGAR `companyfacts` and frames URLs carry no
date at all. Cached indefinitely they would serve pre-filing figures forever
and the screen would silently stop seeing new results. Directories older than
three days are pruned on startup so the cache cannot grow without bound.

In CI the cache key includes the UTC date, so it rolls over at midnight and
seeds from the previous day via `restore-keys`.

### Why the free price source needs a key

Yahoo and Stooq are both free and keyless, and both refused a GitHub Actions
runner: Yahoo with `HTTP 429`, Stooq with a JavaScript bot-challenge page
served in place of the CSV. They identify callers by IP, and a CI runner is a
shared datacenter IP. Tiingo authenticates by token, so the runner is
irrelevant. That is the whole reason `free` needs a (free, no-card) key.

Both keyless sources remain selectable — `--preset free-stooq`,
`--preset free-yahoo` — and work fine from a laptop.

Presets are shorthand; any flag overrides the preset:

```bash
python -m gscreen.cli sources                     # list everything
python -m gscreen.cli screen --preset offline     # fixtures, no network
python -m gscreen.cli screen --preset free        # static + Stooq + EDGAR, $0
python -m gscreen.cli doctor                      # probe each source, report status
python -m gscreen.cli screen --preset paid        # EODHD throughout
python -m gscreen.cli screen --preset hybrid      # EODHD discovery, EDGAR facts
python -m gscreen.cli screen --preset free --prices eodhd   # mix freely
```

**free** costs nothing but discovers nothing — the universe is whatever is in
`universe.txt`. **hybrid** is the interesting one: EODHD's screener finds
candidates, EDGAR supplies fundamentals that are actually point-in-time.

Trade-offs worth knowing before you pick:

- **Stooq** is the default free price source: no key, plain CSV, and it does
  not single out datacenter IPs. Its closes are split-adjusted but not
  dividend-adjusted, so momentum is understated for dividend payers.
- **Yahoo** has had no official API since 2017 and throttles shared cloud IPs
  first — the initial CI run rejected every ticker. Available via
  `--prices yahoo` or `--preset free-yahoo`; better from a laptop than from a
  runner. A failed fetch is logged as a `[data]` rejection naming the status
  code, and the run continues.
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
export TIINGO_API_KEY=...       # free key, needed by the `free` preset

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
