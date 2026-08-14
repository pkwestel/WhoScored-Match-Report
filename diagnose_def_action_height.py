"""
diagnose_def_action_height.py
==============================
One-off diagnostic to reverse-engineer insight90's "Defensive Action Height"
formula more precisely, using the Man Utd vs Brentford match as ground truth
(https://www.whoscored.com/matches/1903395/live/...).

WHY THIS EXISTS
---------------
whoscored_report.py's compute_defensive_stats() already computes Defensive
Action Height as:
    median x (converted to metres) of {Tackle, Clearance, BallRecovery,
    Challenge, Aerial} events, per team
...but its own docstring admits this was TUNED against a single earlier
benchmark match (Man Utd vs Crystal Palace: 41.42m vs a published 41.44m,
31.4m vs 31.81m) rather than confirmed against insight90's actual source
code (which isn't public). For this Man Utd vs Brentford match, our report
computed 21.21m / 36.44m, but the published insight90-style graphic shows
26.55m / 38.47m - a much bigger gap than the earlier benchmark match, which
suggests the tuned formula doesn't generalize (classic overfitting-to-one-
example risk), not necessarily a bug.

WHAT THIS SCRIPT DOES
----------------------
Re-scrapes this exact match and tries a grid of plausible alternative
formulas (different combinations of event types, median vs mean, with/
without goalkeeper events) side by side against the two known target
values (26.55 for Man Utd, 38.47 for Brentford), ranked by total error
across both teams. Whichever combination comes out on top is our best
evidence-based guess at what insight90 is actually doing - the same
"tune against a real published number" approach already used elsewhere in
this project for PPDA/sequences, just done properly against BOTH numbers
from THIS match instead of guessing from one earlier match.

USAGE
-----
    python diagnose_def_action_height.py
(needs to run in the same folder as whoscored_report.py + utils/driver.py,
same as running the Streamlit apps - it drives a real, visible Chrome
window just like the report generator does)
"""

import itertools

import whoscored_report as wr

MATCH_URL = "https://www.whoscored.com/matches/1903395/live/england-premier-league-2025-2026-manchester-united-brentford"

# Targets read off the published graphic, keyed by HOME/AWAY rather than a
# hardcoded team name string - WhoScored's own match_info gives back
# whatever exact spelling it uses internally (e.g. "Man Utd", not
# "Manchester United"), and df['team'] values must match that exactly for
# any of the groupby lookups below to work. Building TARGETS from
# match_info at runtime (in main()) avoids silently mismatching on a name
# spelling guess - which is exactly what happened the first time round: the
# hardcoded "Manchester United" key never matched the real "Man Utd" team
# name in the data, so every Man Utd row above silently fell back to nan.
HOME_TARGET = 26.55  # Man Utd - listed first in the published graphic's score line
AWAY_TARGET = 38.47  # Brentford

# Candidate event-type sets to try - built from every discrete "defensive"
# event type WhoScored logs, in a few plausible combinations a public
# graphic-maker might use for this specific derived stat.
ALL_TYPES = ["Tackle", "Interception", "Clearance", "BallRecovery", "Challenge", "Aerial"]
CANDIDATE_SETS = [
    {"Tackle", "Interception", "Clearance", "BallRecovery", "Challenge", "Aerial"},  # current formula (Interception added)
    {"Tackle", "Clearance", "BallRecovery", "Challenge", "Aerial"},  # original formula, pre-Interception, for comparison
    {"Tackle", "Interception", "Clearance"},
    {"Tackle", "Interception", "Clearance", "Challenge"},
    {"Tackle", "Interception", "Clearance", "BallRecovery"},
    {"Tackle", "Interception", "Clearance", "BallRecovery", "Challenge"},
    {"Tackle", "Interception"},
    {"Tackle", "Interception", "Challenge"},
    {"Tackle", "Clearance", "BallRecovery", "Challenge"},  # current, minus Aerial
    {"Tackle", "Interception", "Clearance", "Aerial"},
    {"Tackle", "Interception", "Clearance", "BallRecovery", "Aerial"},
]


def guess_gk_names(df):
    """
    Best-effort goalkeeper detection with no explicit position field in the
    event stream: whichever player recorded the most 'Save' events on each
    team is almost certainly that team's keeper. Used only for the
    "excluding GK" variants below - if this comes back empty for a team,
    those variants just silently fall back to including everyone for that
    team (clearly labeled in the printed output).
    """
    saves = df[df["type.displayName"] == "Save"]
    gks = {}
    for team, grp in saves.groupby("team"):
        counts = grp["playerName"].value_counts()
        if not counts.empty:
            gks[team] = counts.index[0]
    return gks


def score(values_by_team, targets):
    """Total absolute error across both teams vs the known targets - lower is better."""
    return sum(abs(values_by_team.get(team, float("inf")) - target)
               for team, target in targets.items())


def main():
    df, match_info = wr.scrape_match(MATCH_URL)
    home_name, away_name = match_info.get("home_name"), match_info.get("away_name")
    print(f"Scraped {home_name} vs {away_name} ({len(df)} events)\n")

    # Built from the ACTUAL scraped team names, not a guessed spelling - see
    # the note above HOME_TARGET/AWAY_TARGET for why this matters.
    targets = {home_name: HOME_TARGET, away_name: AWAY_TARGET}

    gk_names = guess_gk_names(df)
    print(f"Guessed goalkeepers (most 'Save' events per team): {gk_names}\n")

    results = []
    for type_set in CANDIDATE_SETS:
        da = df[df["type.displayName"].isin(type_set)]
        for agg_name, agg_fn in [("median", "median"), ("mean", "mean")]:
            for exclude_gk in [False, True]:
                work = da
                label_suffix = ""
                if exclude_gk:
                    work = da[~da.apply(lambda r: gk_names.get(r["team"]) == r["playerName"], axis=1)]
                    label_suffix = ", excl. GK"
                per_team_x = getattr(work.groupby("team")["x"], agg_fn)()
                values = (per_team_x * (wr.PITCH_LEN_M / 100)).round(2).to_dict()
                label = f"{{{', '.join(sorted(type_set))}}} + {agg_name}{label_suffix}"
                results.append((score(values, targets), label, values))

    results.sort(key=lambda r: r[0])

    print(f"{'ERROR':>7}  {home_name:>8}  {away_name:>10}  FORMULA")
    print(f"{'':>7}  {HOME_TARGET:>8}  {AWAY_TARGET:>10}  (targets)")
    print("-" * 100)
    for total_err, label, values in results:
        home_val = values.get(home_name, float("nan"))
        away_val = values.get(away_name, float("nan"))
        print(f"{total_err:7.2f}  {home_val:8.2f}  {away_val:10.2f}  {label}")

    print("\nBest match (lowest combined error):")
    best = results[0]
    print(f"  {best[1]}  ->  {best[2]}")


if __name__ == "__main__":
    main()
