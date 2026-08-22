"""Generate deterministic synthetic fixtures shaped like EODHD responses.

These are NOT real financials. They exist so the pipeline, the rejection
logic and the grounding validator can be executed and tested offline. Each
company is constructed to trip a different branch of the screen.
"""

from __future__ import annotations

import json
import math
import random
from datetime import date, timedelta
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "fixtures"
AS_OF = date(2026, 8, 15)

# name, sector, 3y revenue path (bn), fcf margin, profit margin, qtr yoy path
# (newest first), dilution, net debt/ebitda inputs, short % float, days since
# earnings, 12-1 momentum target
SPECS = [
    dict(t="ALPHA", n="Alpha Compounders",  sec="Technology",
         rev=[1.0, 1.45, 2.05, 2.85], fcf=0.22, pm=0.14,
         qtr=[0.38, 0.36, 0.33, 0.31, 0.29, 0.27], dil=0.02,
         debt=0.4, cash=1.9, short=0.03, dse=41, mom=0.52),
    dict(t="BRAVO", n="Bravo Networks",     sec="Communication Services",
         rev=[0.8, 1.1, 1.5, 1.95], fcf=0.18, pm=0.11,
         qtr=[0.32, 0.30, 0.28, 0.26, 0.24, 0.22], dil=0.03,
         debt=0.6, cash=1.2, short=0.05, dse=35, mom=0.34),
    dict(t="CHARL", n="Charlie Leverage",   sec="Technology",   # fails: leverage
         rev=[0.5, 1.2, 2.6, 5.4], fcf=-0.35, pm=-0.18,
         qtr=[1.12, 1.05, 0.98, 0.90, 0.82, 0.70], dil=0.06,
         debt=17.2, cash=1.1, short=0.09, dse=28, mom=0.61),
    dict(t="DELTA", n="Delta One-Quarter",  sec="Industrials",  # fails: no streak
         rev=[1.4, 1.5, 1.6, 2.3], fcf=0.05, pm=0.03,
         qtr=[0.44, 0.04, 0.02, -0.01, 0.03, 0.05], dil=0.04,
         debt=1.0, cash=0.8, short=0.07, dse=52, mom=0.29),
    dict(t="ECHO",  n="Echo Dilution",      sec="Healthcare",   # fails: dilution
         rev=[0.6, 0.9, 1.3, 1.8], fcf=-0.10, pm=-0.06,
         qtr=[0.40, 0.38, 0.35, 0.33, 0.30, 0.28], dil=0.19,
         debt=0.3, cash=1.4, short=0.11, dse=33, mom=0.44),
    dict(t="FOXTR", n="Foxtrot Squeeze",    sec="Consumer Cyclical",  # fails: short
         rev=[0.7, 1.0, 1.35, 1.8], fcf=0.09, pm=0.05,
         qtr=[0.33, 0.31, 0.29, 0.27, 0.26, 0.24], dil=0.02,
         debt=0.5, cash=0.9, short=0.24, dse=39, mom=0.58),
    dict(t="GOLF",  n="Golf Recent Print",  sec="Technology",   # fails: earnings gap
         rev=[0.9, 1.25, 1.7, 2.3], fcf=0.16, pm=0.10,
         qtr=[0.35, 0.33, 0.31, 0.29, 0.27, 0.25], dil=0.03,
         debt=0.7, cash=1.5, short=0.06, dse=4, mom=0.47),
    dict(t="HOTEL", n="Hotel Slow Grower",  sec="Consumer Defensive",  # fails: CAGR
         rev=[2.0, 2.1, 2.25, 2.4], fcf=0.12, pm=0.08,
         qtr=[0.07, 0.06, 0.06, 0.05, 0.05, 0.04], dil=0.01,
         debt=1.1, cash=0.6, short=0.04, dse=44, mom=0.21),
    dict(t="INDIA", n="India Flat Momentum", sec="Technology",  # fails: momentum
         rev=[0.8, 1.15, 1.6, 2.2], fcf=0.20, pm=0.13,
         qtr=[0.36, 0.34, 0.32, 0.30, 0.28, 0.26], dil=0.02,
         debt=0.4, cash=1.3, short=0.05, dse=37, mom=0.02),
]


def business_days(start: date, end: date) -> list[date]:
    out, cur = [], start
    while cur <= end:
        if cur.weekday() < 5:
            out.append(cur)
        cur += timedelta(days=1)
    return out


def price_series(seed: int, target_12_1: float, days: list[date]) -> list[dict]:
    """Random walk whose 12-1 momentum hits the target, so the screen's
    momentum branch is exercised deterministically."""
    rng = random.Random(seed)
    n = len(days)
    skip = 21
    anchor = n - skip - 1
    window = anchor - 252
    drift = math.log(1 + target_12_1) / 252 if window >= 0 else 0.0

    closes, price = [], 30.0
    for i in range(n):
        d = drift if window <= i <= anchor else drift * 0.3
        price *= math.exp(d + rng.gauss(0, 0.018))
        closes.append(price)

    # rescale so the realised 12-1 equals the target exactly
    if window >= 0:
        realised = closes[anchor] / closes[window] - 1
        adj = (1 + target_12_1) / (1 + realised)
        for i in range(window + 1, n):
            closes[i] *= adj

    return [
        {"date": d.isoformat(), "close": round(c, 4), "adjusted_close": round(c, 4)}
        for d, c in zip(days, closes)
    ]


def yearly_block(values: list[float], key: str, start_year: int = 2023) -> dict:
    return {
        f"{start_year + i}-12-31": {key: str(v * 1e9)}
        for i, v in enumerate(values)
    }


def build() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    days = business_days(date(2022, 1, 1), AS_OF)

    screener, fundamentals, prices = [], {}, {}

    for i, s in enumerate(SPECS):
        rev = s["rev"]
        latest = rev[-1] * 1e9
        mcap = latest * (8 + i)
        ebitda = latest * max(s["fcf"] + 0.08, 0.02)

        screener.append(
            {
                "code": s["t"],
                "name": s["n"],
                "exchange": "us",
                "market_capitalization": mcap,
                "avgvol_200d": 3_000_000 + i * 250_000,
            }
        )

        # quarterly revenue reconstructed from the YoY path (newest first)
        q_dates = []
        cur = AS_OF.replace(day=1) - timedelta(days=30)
        for _ in range(12):
            q_dates.append(cur.isoformat())
            cur -= timedelta(days=91)
        q_dates = sorted(q_dates)

        base = latest / 4 / (1 + s["qtr"][0])
        quarterly = {}
        for idx, qd in enumerate(q_dates):
            if idx < 4:
                quarterly[qd] = {"totalRevenue": str(base * (0.92 + 0.02 * idx) * 1e0)}
        for idx in range(4, 12):
            growth = s["qtr"][min(11 - idx, len(s["qtr"]) - 1)]
            prior = float(quarterly[q_dates[idx - 4]]["totalRevenue"])
            quarterly[q_dates[idx]] = {"totalRevenue": str(prior * (1 + growth))}

        shares_base = 1.0e9
        shares = [shares_base * (1 + s["dil"]) ** k for k in range(4)]

        fundamentals[s["t"]] = {
            "General": {"Name": s["n"], "Sector": s["sec"], "Code": s["t"]},
            "Highlights": {
                "MarketCapitalization": mcap,
                "EBITDA": ebitda,
                "ProfitMargin": s["pm"],
                # deliberately present but unused by the screen: the article
                # trusts this vendor field instead of computing from statements
                "QuarterlyRevenueGrowthYOY": s["qtr"][0],
            },
            "Valuation": {
                "PriceSalesTTM": round(mcap / latest, 2),
                "EnterpriseValueRevenue": round(
                    (mcap + s["debt"] * 1e9 - s["cash"] * 1e9) / latest, 2
                ),
            },
            "SharesStats": {"ShortPercentFloat": s["short"]},
            "Earnings": {
                "Last_Reported_Date": (AS_OF - timedelta(days=s["dse"])).isoformat()
            },
            "Financials": {
                "Income_Statement": {
                    "yearly": yearly_block(rev, "totalRevenue"),
                    "quarterly": quarterly,
                },
                "Balance_Sheet": {
                    "yearly": {
                        f"{2023 + k}-12-31": {
                            "commonStockSharesOutstanding": str(shares[k]),
                            "shortLongTermDebtTotal": str(s["debt"] * 1e9),
                            "cashAndShortTermInvestments": str(s["cash"] * 1e9),
                        }
                        for k in range(4)
                    }
                },
                "Cash_Flow": {
                    "yearly": {
                        f"{2023 + k}-12-31": {"freeCashFlow": str(rev[k] * 1e9 * s["fcf"])}
                        for k in range(4)
                    }
                },
            },
        }

        prices[s["t"]] = price_series(1000 + i, s["mom"], days)

    (OUT / "screener.json").write_text(json.dumps(screener, indent=1))
    (OUT / "fundamentals.json").write_text(json.dumps(fundamentals, indent=1))
    (OUT / "prices.json").write_text(json.dumps(prices))
    print(f"wrote fixtures for {len(SPECS)} tickers to {OUT}")


if __name__ == "__main__":
    build()
