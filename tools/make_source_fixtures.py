"""Fixtures shaped like real SEC companyfacts and Yahoo chart responses.

The EDGAR fixture deliberately contains three awkward cases:
  1. a FY2025 revenue filed 2026-02-20 - invisible to an as_of of 2026-01-31
  2. a restated FY2023 revenue, filed later than the original
  3. a quarterly duration alongside annual ones, to test duration filtering
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "fixtures"


def usd(start, end, val, filed, form="10-K", fy=None, fp="FY"):
    return {
        "start": start,
        "end": end,
        "val": val,
        "accn": f"acc-{filed}",
        "fy": fy or int(end[:4]),
        "fp": fp,
        "form": form,
        "filed": filed,
    }


COMPANYFACTS = {
    "cik": 1234567,
    "entityName": "Testco Incorporated",
    "facts": {
        "us-gaap": {
            "RevenueFromContractWithCustomerExcludingAssessedTax": {
                "units": {
                    "USD": [
                        # annual
                        usd("2022-01-01", "2022-12-31", 1_000_000_000, "2023-02-15"),
                        usd("2023-01-01", "2023-12-31", 1_400_000_000, "2024-02-15"),
                        # restatement of FY2023, filed a year later
                        usd("2023-01-01", "2023-12-31", 1_380_000_000, "2025-02-14"),
                        usd("2024-01-01", "2024-12-31", 2_000_000_000, "2025-02-14"),
                        # filed AFTER the as_of used in the test
                        usd("2025-01-01", "2025-12-31", 2_900_000_000, "2026-02-20"),
                        # quarterly
                        usd("2024-01-01", "2024-03-31", 440_000_000, "2024-05-01",
                            "10-Q", fp="Q1"),
                        usd("2024-04-01", "2024-06-30", 480_000_000, "2024-08-01",
                            "10-Q", fp="Q2"),
                        usd("2025-01-01", "2025-03-31", 580_000_000, "2025-05-01",
                            "10-Q", fp="Q1"),
                        usd("2025-04-01", "2025-06-30", 640_000_000, "2025-08-01",
                            "10-Q", fp="Q2"),
                    ]
                }
            },
            "NetCashProvidedByUsedInOperatingActivities": {
                "units": {
                    "USD": [
                        usd("2023-01-01", "2023-12-31", 300_000_000, "2024-02-15"),
                        usd("2024-01-01", "2024-12-31", 500_000_000, "2025-02-14"),
                    ]
                }
            },
            "PaymentsToAcquirePropertyPlantAndEquipment": {
                "units": {
                    "USD": [
                        usd("2023-01-01", "2023-12-31", 80_000_000, "2024-02-15"),
                        usd("2024-01-01", "2024-12-31", 100_000_000, "2025-02-14"),
                    ]
                }
            },
            "NetIncomeLoss": {
                "units": {
                    "USD": [
                        usd("2024-01-01", "2024-12-31", 240_000_000, "2025-02-14"),
                    ]
                }
            },
            "CommonStockSharesOutstanding": {
                "units": {
                    "shares": [
                        {"end": "2023-12-31", "val": 1_000_000_000,
                         "filed": "2024-02-15", "form": "10-K", "fy": 2023, "fp": "FY"},
                        {"end": "2024-12-31", "val": 1_020_000_000,
                         "filed": "2025-02-14", "form": "10-K", "fy": 2024, "fp": "FY"},
                    ]
                }
            },
            "LongTermDebtNoncurrent": {
                "units": {
                    "USD": [
                        {"end": "2024-12-31", "val": 600_000_000,
                         "filed": "2025-02-14", "form": "10-K", "fy": 2024, "fp": "FY"},
                    ]
                }
            },
            "CashAndCashEquivalentsAtCarryingValue": {
                "units": {
                    "USD": [
                        {"end": "2024-12-31", "val": 1_500_000_000,
                         "filed": "2025-02-14", "form": "10-K", "fy": 2024, "fp": "FY"},
                    ]
                }
            },
        }
    },
}


def yahoo_chart() -> dict:
    days, closes = [], []
    price = 100.0
    start = datetime(2025, 1, 2, tzinfo=timezone.utc)
    for i in range(5):
        ts = int((start.timestamp())) + i * 86_400
        days.append(ts)
        closes.append(price)
        price *= 1.01
    return {
        "chart": {
            "result": [
                {
                    "meta": {"symbol": "TEST", "currency": "USD"},
                    "timestamp": days,
                    "indicators": {
                        "adjclose": [{"adjclose": closes[:3] + [None] + closes[4:]}]
                    },
                }
            ],
            "error": None,
        }
    }


def build() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "edgar_companyfacts.json").write_text(json.dumps(COMPANYFACTS, indent=1))
    (OUT / "yahoo_chart.json").write_text(json.dumps(yahoo_chart(), indent=1))
    print(f"wrote EDGAR and Yahoo fixtures to {OUT}")


if __name__ == "__main__":
    build()
