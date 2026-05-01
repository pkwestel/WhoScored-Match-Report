"""Scrape Opta power rankings and save men's and women's CSV files.

Source:
    https://dataviz.theanalyst.com/opta-power-rankings/index.js
"""

from __future__ import annotations

import ast
import json
import os
import re
import sys

import pandas as pd
import requests

SOURCE_URL = "https://dataviz.theanalyst.com/opta-power-rankings/index.js"
OUTPUT_DIR = os.path.dirname(__file__)
MEN_OUTPUT = os.path.join(OUTPUT_DIR, "mens_power_rankings.csv")
WOMEN_OUTPUT = os.path.join(OUTPUT_DIR, "womens_power_rankings.csv")


def fetch_js_content(url: str = SOURCE_URL) -> str:
    response = requests.get(url, timeout=30)
    response.raise_for_status()
    return response.text


def parse_block(json_str: str) -> pd.DataFrame:
    """Parse one JSON block from the JS source.

    The Opta source stores the `comps` field as a JSON string that contains a
    list-like payload, so we temporarily replace it before loading JSON and
    then restore it with `ast.literal_eval`.
    """
    json_str_clean = re.sub(
        r'"comps":("(\[.*?\])")', r'"comps":"__COMPS_PLACEHOLDER__"', json_str
    )

    data = json.loads(json_str_clean)

    comps_matches = re.findall(r'"comps":("(\[.*?\])")', json_str, re.DOTALL)
    comps_strings = [match[1] for match in comps_matches]

    for row, comps_str in zip(data, comps_strings):
        try:
            row["comps"] = ast.literal_eval(comps_str)
        except Exception:
            row["comps"] = []

    return pd.DataFrame(data)


def extract_ranking_dfs(js_content: str) -> list[pd.DataFrame]:
    """Extract all ranking DataFrames from the JS content."""
    json_blocks = re.findall(r"JSON\.parse\(`(\[.*?\])`\)", js_content, re.DOTALL)

    expected_cols = {"rank", "contestantName", "comps"}
    ranking_dfs: list[pd.DataFrame] = []

    for block in json_blocks:
        try:
            df = parse_block(block)
            if expected_cols.issubset(df.columns):
                ranking_dfs.append(df)
        except Exception:
            continue

    return ranking_dfs


def main() -> int:
    print(f"Fetching Opta power rankings from {SOURCE_URL}")
    try:
        js_content = fetch_js_content()
    except Exception as exc:
        print(f"Failed to fetch JS content: {exc}")
        return 1

    ranking_dfs = extract_ranking_dfs(js_content)
    if len(ranking_dfs) < 2:
        print(f"Expected at least 2 ranking blocks, found {len(ranking_dfs)}")
        return 2

    ranking_dfs[0].to_csv(MEN_OUTPUT, index=False)
    ranking_dfs[1].to_csv(WOMEN_OUTPUT, index=False)

    print(f"Saved men's rankings to {MEN_OUTPUT}")
    print(f"Saved women's rankings to {WOMEN_OUTPUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
