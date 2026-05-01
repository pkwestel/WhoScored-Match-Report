"""Fetch Opta league metadata and save men's and women's CSV files.

Source endpoints:
    https://dataviz.theanalyst.com/opta-power-rankings/league-meta.json
    https://dataviz.theanalyst.com/opta-power-rankings/women-league-meta.json
"""

from __future__ import annotations

import os
import sys

import pandas as pd
import requests

MEN_URL = "https://dataviz.theanalyst.com/opta-power-rankings/league-meta.json"
WOMEN_URL = "https://dataviz.theanalyst.com/opta-power-rankings/women-league-meta.json"

OUTPUT_DIR = os.path.dirname(__file__)
MEN_OUTPUT = os.path.join(OUTPUT_DIR, "mens_league_rankings.csv")
WOMEN_OUTPUT = os.path.join(OUTPUT_DIR, "womens_league_rankings.csv")


def fetch_json(url: str) -> list[dict]:
    response = requests.get(url, timeout=30)
    response.raise_for_status()
    return response.json()


def save_csv(data: list[dict], path: str) -> None:
    df = pd.DataFrame(data)
    df.to_csv(path, index=False)


def main() -> int:
    print(f"Fetching men's league rankings from {MEN_URL}")
    print(f"Fetching women's league rankings from {WOMEN_URL}")

    try:
        men_data = fetch_json(MEN_URL)
        women_data = fetch_json(WOMEN_URL)
    except Exception as exc:
        print(f"Failed to fetch league metadata: {exc}")
        return 1

    save_csv(men_data, MEN_OUTPUT)
    save_csv(women_data, WOMEN_OUTPUT)

    print(f"Saved men's league rankings to {MEN_OUTPUT}")
    print(f"Saved women's league rankings to {WOMEN_OUTPUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
