"""
batch_lib.py
=============
Shared, non-UI orchestration logic for running a full combined WhoScored +
FotMob report for one match, and for publishing it to the history database
- factored out so both combined_streamlit_app.py (one match at a time) and
batch_run_app.py (a whole weekend's matches in one sitting) call the exact
same underlying report-building and database-saving code, rather than two
copies that could quietly drift apart.

This module has NO Streamlit dependency at all (same reasoning as
pitch_viz.py not having top-level UI calls - see that file's own docstring)
- progress updates go through an optional status_cb callback instead of
calling st.write()/st.spinner() directly, so this same code is usable from
a plain script, a future scheduled job, or any UI, not just Streamlit.
"""

import io

import whoscored_report as wr
import fotmob_report as fr
import combined_report as cr
import history_db as hdb


def find_existing_match(db_url, home_name, away_name, match_date_iso):
    """
    Anti-duplicate check for the batch runner: does a match already exist in
    the history database for this exact (home team, away team, date)
    combination? Used to default an already-saved match's "Run" checkbox to
    UNCHECKED, so re-running the batch runner over a weekend you already
    processed doesn't silently re-scrape (and re-hit WhoScored/FotMob for)
    matches that are already saved - the whole point of a random delay
    between matches is to avoid unnecessary bursty traffic, and re-scraping
    something already in the database is the most avoidable traffic there
    is.

    Matches on (home, away, date) rather than home/away alone, since the
    same two teams can legitimately meet twice in a season (the reverse
    league fixture, or a cup tie) - date is what disambiguates those rather
    than treating every past meeting between two teams as the same match.
    Team names are compared via combined_report.canonical_team_name(), so a
    spelling difference between what the fixtures page calls a team and
    what's already stored ("Man Utd" vs "Manchester United") doesn't cause
    a false "not found" and a needless re-scrape.

    This intentionally does NOT block a deliberate re-run - it only affects
    the checkbox's default state, never whether a row even appears. Rerun
    something on purpose (e.g. to pick up a stat-calculation fix, like
    Defensive Action Height's formula update earlier in this project) by
    just checking the box yourself.

    Returns False (never found) on any database error - a connection
    hiccup here should never block you from running matches, just leave
    the duplicate check inconclusive rather than crashing the batch runner
    over it.
    """
    try:
        db = hdb.get_db(db_url)
        hdb.init_schema(db)
        existing = hdb.fetch_matches(db)
        db.close()
    except Exception:
        return False

    if existing.empty:
        return False

    target_home = cr.canonical_team_name(home_name)
    target_away = cr.canonical_team_name(away_name)
    for _, row in existing.iterrows():
        if (cr.canonical_team_name(row.get("home_team", "")) == target_home
                and cr.canonical_team_name(row.get("away_team", "")) == target_away
                and str(row.get("match_date", "")) == str(match_date_iso)):
            return True
    return False


def run_combined_report(ws_url, fm_url, fm_out_dir, status_cb=None):
    """
    Runs the exact same scrape-and-compute sequence combined_streamlit_app.py's
    single-match flow already uses, for one (WhoScored URL, FotMob URL) pair,
    returning a dict in the same shape as that app's own
    st.session_state["combined_report"] (including the built workbook's raw
    bytes, so a batch run can still offer a per-match download).

    status_cb: optional callable(str) invoked with short progress messages
    ("Scraping WhoScored...", "Opening the FotMob match page...", etc.) -
    lets a caller show live progress without this function needing to know
    anything about Streamlit widgets (st.spinner, st.write, ...) itself.
    Pass None to run silently (e.g. from a plain script).

    Raises on any failure - the caller (batch_run_app.py) is responsible for
    catching this per-match so one bad match in a batch doesn't stop the
    rest from running.
    """
    def _status(msg):
        if status_cb:
            status_cb(msg)

    _status("Scraping WhoScored...")
    df, match_info = wr.scrape_match(ws_url.strip())
    ws_home_name = match_info.get("home_name")
    ws_away_name = match_info.get("away_name")

    _status("Computing WhoScored tables...")
    _, player_totals, team_totals, progressive_received = wr.compute_progressive_passes(df)
    passes_received = wr.compute_passes_received(df)
    passing_pairs = wr.compute_passing_pairs(df)
    team_carries, player_carries, carries_df = wr.compute_carries(df)
    sca_out = wr.compute_sca(df)
    shot_pairs = wr.compute_shot_pairs(sca_out)
    team_summary, player_third = wr.compute_touches(df, team_carries, player_carries,
                                                      passes_received, progressive_received)
    passing_out = wr.compute_passing(df, player_totals, sca_out)
    chains_df, team_sequences = wr.compute_sequences(df)
    field_tilt = wr.compute_field_tilt(team_summary)
    ppda = wr.compute_ppda(df)
    defensive_stats = wr.compute_defensive_stats(df)
    defensive_actions = wr.compute_defensive_actions(df)
    defensive_action_location = wr.compute_defensive_action_location(df)
    corners = wr.compute_corners(df)
    totals_out = wr.compute_totals(team_summary, team_totals, passing_out, sca_out,
                                    chains_df, team_sequences, field_tilt, ppda,
                                    defensive_stats, corners, ws_home_name, ws_away_name)
    against_totals = wr.compute_against_totals(totals_out)
    all_passes = wr.compute_all_passes(df)
    player_windows = wr.extract_player_windows(df)
    on_off = wr.compute_on_off(df, player_windows, carries_df)

    wb_ws = wr.build_workbook(sca_out, team_summary, player_third, passing_out, totals_out,
                               defensive_actions, defensive_action_location, passing_pairs,
                               ws_home_name, ws_away_name, against_totals, shot_pairs, on_off)

    _status("Opening the FotMob match page (a real Chrome window will pop up)...")
    fm_match_json, fm_match_id = fr.scrape_match(fm_url.strip(), out_dir=fm_out_dir)
    fm_home_name, fm_away_name = fr.extract_team_names(fm_match_json)

    _status("Computing FotMob tables...")
    shots_df = fr.compute_shots(fm_match_json)
    fm_totals_df = fr.compute_totals(fm_match_json, shots_df, fm_home_name, fm_away_name)
    player_xa = fr.extract_player_xa(fm_match_json)
    player_minutes = fr.extract_player_minutes(fm_match_json)
    shot_breakdowns = fr.compute_shot_breakdowns(shots_df, player_xa, player_minutes)
    xg_breakdown = fr.compute_xg_breakdown(shots_df, fm_home_name, fm_away_name)
    player_windows = fr.extract_player_windows(fm_match_json, player_minutes, shots_df)
    plus_minus = fr.compute_plus_minus(shots_df, player_windows, fm_home_name, fm_away_name)
    plus_minus_warning = (
        "Plus Minus is empty - FotMob didn't return any lineup/substitution data for this "
        "match yet."
    ) if player_windows.empty else None

    wb_fm = fr.build_workbook(shots_df, fm_totals_df, fm_home_name, fm_away_name, fm_match_id,
                               shot_breakdowns, xg_breakdown, plus_minus)

    _status("Merging Shot Creating Actions + Shots...")
    combined_shots = cr.compute_combined_shots(sca_out, shots_df)

    wb = cr.build_combined_workbook(wb_ws, wb_fm, combined_shots, ws_home_name, ws_away_name)
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)

    filename = f"{wr.sanitize_filename(ws_home_name)}_vs_{wr.sanitize_filename(ws_away_name)}_combined.xlsx"

    team_name_mismatch = (
        cr.canonical_team_name(ws_home_name) != cr.canonical_team_name(fm_home_name)
        or cr.canonical_team_name(ws_away_name) != cr.canonical_team_name(fm_away_name)
    )

    return {
        "ws_url": ws_url,
        "fm_url": fm_url,
        "ws_home_name": ws_home_name,
        "ws_away_name": ws_away_name,
        "fm_home_name": fm_home_name,
        "fm_away_name": fm_away_name,
        "team_name_mismatch": team_name_mismatch,
        "totals_out": totals_out,
        "against_totals": against_totals,
        "player_third": player_third,
        "passing_out": passing_out,
        "defensive_actions": defensive_actions,
        "defensive_action_location": defensive_action_location,
        "passing_pairs": passing_pairs,
        "shot_pairs": shot_pairs,
        "combined_shots": combined_shots,
        "fm_match_id": fm_match_id,
        "fm_totals_df": fm_totals_df,
        "xg_breakdown": xg_breakdown,
        "shot_breakdowns": shot_breakdowns,
        "plus_minus": plus_minus,
        "plus_minus_warning": plus_minus_warning,
        "all_passes": all_passes,
        "wb_bytes": buf.getvalue(),
        "filename": filename,
        "n_ws_events": len(df),
        "n_fm_shots": len(shots_df),
    }


def build_db_stats(report):
    """
    Extracts the team_stats/player_stats dicts publish_report() expects out
    of a report dict built by run_combined_report() (or the equivalent
    inline code in combined_streamlit_app.py's single-match flow - both
    produce the same shape). Factored out so both the single-match "Save to
    Database" button and the batch runner's "Save all" button build these
    the exact same way.
    """
    team_stats = {}
    for _, row in report["totals_out"].iterrows():
        t = row["team"]
        team_stats.setdefault(t, {})["ws_totals"] = row.drop("team").to_dict()
    for _, row in report["fm_totals_df"].iterrows():
        t = row["team"]
        team_stats.setdefault(t, {})["fm_totals"] = row.drop("team").to_dict()

    player_stats = {}
    for _, row in report["passing_out"].iterrows():
        key = (row["team"], row["player"])
        player_stats.setdefault(key, {})["ws_passing"] = row.drop(["team", "player"]).to_dict()
    for _, row in report["defensive_actions"].iterrows():
        key = (row["team"], row["player"])
        player_stats.setdefault(key, {})["ws_defensive"] = row.drop(["team", "player"]).to_dict()
    if not report["plus_minus"].empty:
        for _, row in report["plus_minus"].iterrows():
            key = (row["Team"], row["Player"])
            player_stats.setdefault(key, {})["fm_plus_minus"] = row.drop(["Team", "Player"]).to_dict()

    return team_stats, player_stats


def save_report_to_db(db_url, report, competition, match_date_iso):
    """
    One-call save for a single already-scraped report dict - opens the DB,
    builds team/player stats via build_db_stats(), and publishes everything
    (team stats, player stats, shots, passes) in one transaction via
    history_db.publish_report(). Returns the match_id used (FotMob's own
    numeric id, same convention as combined_streamlit_app.py's existing
    single-match save). Raises on failure - caller decides how to surface
    that (a single st.error for one match, or a row in a batch results
    table for many).
    """
    match_id = report["fm_match_id"]
    team_stats, player_stats = build_db_stats(report)

    db = hdb.get_db(db_url)
    try:
        hdb.init_schema(db)
        hdb.publish_report(
            db, match_id=match_id,
            home_team=report["ws_home_name"], away_team=report["ws_away_name"],
            team_stats=team_stats, player_stats=player_stats,
            shots_df=report["combined_shots"],
            passes_df=report["all_passes"],
            competition=competition, match_date=match_date_iso,
            ws_events=report["n_ws_events"], fm_shots=report["n_fm_shots"],
        )
    finally:
        db.close()
    return match_id
