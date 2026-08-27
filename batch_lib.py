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
    all_touches = wr.compute_all_touches(df)
    player_windows = wr.extract_player_windows(df)
    on_off = wr.compute_on_off(df, player_windows, carries_df)

    wb_ws = wr.build_workbook(sca_out, team_summary, player_third, passing_out, totals_out,
                               defensive_actions, defensive_action_location, passing_pairs,
                               ws_home_name, ws_away_name, against_totals, shot_pairs, on_off)

    _status("Opening the FotMob match page (a real Chrome window will pop up)...")
    fm_match_json, fm_match_id = fr.scrape_match(fm_url.strip(), out_dir=fm_out_dir)
    fm_home_name, fm_away_name = fr.extract_team_names(fm_match_json)
    referee = fr.extract_referee(fm_match_json)
    # Real kickoff date+time (UK local), not just a date - see save_report_to_db()
    # for why this matters (same-day matches sorting correctly on the Fixtures tab).
    kickoff = fr.extract_kickoff_local_str(fm_match_json)
    # Matchweek/round number - powers the Fixtures tab's matchweek filter.
    matchweek = fr.extract_matchweek(fm_match_json)

    _status("Computing FotMob tables...")
    shots_df = fr.compute_shots(fm_match_json)
    fm_totals_df = fr.compute_totals(fm_match_json, shots_df, fm_home_name, fm_away_name)
    player_xa = fr.extract_player_xa(fm_match_json)
    player_minutes = fr.extract_player_minutes(fm_match_json)
    player_sprints = fr.extract_player_sprints(fm_match_json)
    player_line_breaking_passes = fr.extract_player_line_breaking_passes(fm_match_json)
    player_lineup = fr.extract_player_age_and_start(fm_match_json)
    player_cards = fr.extract_player_cards(fm_match_json)
    shot_breakdowns = fr.compute_shot_breakdowns(
        shots_df, player_xa, player_minutes, player_sprints, player_line_breaking_passes)
    player_scoring = fr.compute_player_scoring_stats(
        fm_match_json, shots_df, player_xa, player_minutes, player_sprints)
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
        "referee": referee,
        "kickoff": kickoff,
        "matchweek": matchweek,
        "team_name_mismatch": team_name_mismatch,
        "totals_out": totals_out,
        "against_totals": against_totals,
        "team_summary": team_summary,
        "player_third": player_third,
        "passing_out": passing_out,
        "defensive_actions": defensive_actions,
        "defensive_action_location": defensive_action_location,
        "passing_pairs": passing_pairs,
        "shot_pairs": shot_pairs,
        "combined_shots": combined_shots,
        "fm_match_id": fm_match_id,
        "fm_totals_df": fm_totals_df,
        "player_scoring": player_scoring,
        "player_lineup": player_lineup,
        "player_cards": player_cards,
        "xg_breakdown": xg_breakdown,
        "shot_breakdowns": shot_breakdowns,
        "plus_minus": plus_minus,
        "plus_minus_warning": plus_minus_warning,
        "all_passes": all_passes,
        "all_touches": all_touches,
        "wb_bytes": buf.getvalue(),
        "filename": filename,
        "n_ws_events": len(df),
        "n_fm_shots": len(shots_df),
    }


def build_db_stats(report):
    """
    Extracts the team_stats/player_stats dicts publish_report() expects out
    of a report dict built by run_combined_report() (or the equivalent
    single-match flow in combined_streamlit_app.py, which now calls this
    same function rather than duplicating it - see that file's "Save to
    Database" handler). Factored out so both the single-match "Save to
    Database" button and the batch runner's "Save all" button build these
    the exact same way.

    TEAM NAME RECONCILIATION (important): FotMob and WhoScored often use a
    different display name for the same club - Ipswich vs Ipswich Town, Man
    Utd vs Manchester United, Spurs vs Tottenham Hotspur, Hull vs Hull City,
    Leeds vs Leeds United, Coventry vs Coventry City, and more (see
    combined_report.TEAM_NAME_ALIASES for the full list). totals_out/
    passing_out/defensive_actions all use WhoScored's own team name (they're
    built from ws_home_name/ws_away_name), but fm_totals_df/plus_minus use
    FotMob's own team name - if that FotMob name is saved as-is, a team's
    FotMob-sourced stats (fm_totals, fm_plus_minus) land under a DIFFERENT
    team_match_stats/player_match_stats row than that same team's
    WhoScored-sourced stats, keyed by whichever name that source happened to
    use. That's not just cosmetic: matches.home_team/away_team (see
    upsert_match()) are always WhoScored's own name, and fetch_fixtures()
    looks up each team's Score/xG in team_match_stats by that exact name -
    so a mismatched name means fm_totals is saved under a row
    fetch_fixtures() never looks at, and the Fixtures tab shows blank Score/
    xG for that match. _to_ws_name() below fixes this at the source: every
    FotMob team name is remapped to whichever of this match's two
    WhoScored team names it canonically matches (via combined_report.
    canonical_team_name() - the exact same alias table already used for the
    Shot Creating Actions merge), so every stat for a team ends up keyed by
    ONE consistent (WhoScored's) name regardless of which source it came
    from. A FotMob name that doesn't canonically match either WhoScored
    name (a genuine, not-yet-aliased mismatch) is kept as its own row rather
    than dropped - better to have it show up on its own than silently lose
    it, and it's an easy fix (add the alias to TEAM_NAME_ALIASES) once
    spotted.
    """
    ws_names = [report.get("ws_home_name"), report.get("ws_away_name")]

    def _to_ws_name(fm_name):
        if not fm_name:
            return fm_name
        for w in ws_names:
            if w and cr.canonical_team_name(w) == cr.canonical_team_name(fm_name):
                return w
        return fm_name

    team_stats = {}
    for _, row in report["totals_out"].iterrows():
        t = row["team"]
        team_stats.setdefault(t, {})["ws_totals"] = row.drop("team").to_dict()
    for _, row in report["fm_totals_df"].iterrows():
        t = _to_ws_name(row["team"])
        team_stats.setdefault(t, {})["fm_totals"] = row.drop("team").to_dict()
    # team_summary (compute_touches()'s team-level table: Total touches,
    # thirds, Attacking Box, Progressive Carries, Carries into Final Third/
    # Box, and their %s) - saved under 'ws_touches' so dashboard_app.py's
    # season Touches tab can read Progressive Carries/Carries into Final
    # Third/Box directly instead of only approximating thirds/box from raw
    # touch (x,y) coordinates (see history_db.fetch_season_touches_totals()'s
    # own docstring for the full story on why this was missing before).
    # .get() with an empty default rather than report["team_summary"], since
    # any OLDER report dict built before this field existed (unlikely at this
    # point, but cheap to guard) shouldn't hard-crash a save over it.
    team_summary = report.get("team_summary")
    if team_summary is not None and not team_summary.empty:
        for _, row in team_summary.iterrows():
            t = row["team"]
            team_stats.setdefault(t, {})["ws_touches"] = row.drop("team").to_dict()

    player_stats = {}
    for _, row in report["passing_out"].iterrows():
        key = (row["team"], row["player"])
        player_stats.setdefault(key, {})["ws_passing"] = row.drop(["team", "player"]).to_dict()
    for _, row in report["defensive_actions"].iterrows():
        key = (row["team"], row["player"])
        player_stats.setdefault(key, {})["ws_defensive"] = row.drop(["team", "player"]).to_dict()
    # defensive_action_location (compute_defensive_action_location()'s per-
    # player, per-pitch-third breakdown) - previously computed and put in
    # the workbook but never actually saved to the database at all, so the
    # match detail view's Defensive Action Locations category (see
    # history_db.fetch_player_defensive_locations()) had nothing to read
    # until this was added.
    defensive_action_location = report.get("defensive_action_location")
    if defensive_action_location is not None and not defensive_action_location.empty:
        for _, row in defensive_action_location.iterrows():
            key = (row["team"], row["player"])
            player_stats.setdefault(key, {})["ws_defensive_locations"] = (
                row.drop(["team", "player"]).to_dict()
            )
    # player_third (compute_touches()'s per-player table) - same 'ws_touches'
    # key as team_summary above (different table, same namespace name - one
    # is per-team, the other per-player, so there's no key collision). This
    # is the ONLY place Passes Received/Progressive Passes Received are
    # tracked at all (team_summary doesn't carry them) - see
    # fetch_season_touches_totals()'s docstring for how these get summed
    # into a team-level season total from here.
    player_third = report.get("player_third")
    if player_third is not None and not player_third.empty:
        for _, row in player_third.iterrows():
            key = (row["team"], row["player"])
            player_stats.setdefault(key, {})["ws_touches"] = row.drop(["team", "player"]).to_dict()
    if not report["plus_minus"].empty:
        for _, row in report["plus_minus"].iterrows():
            team = _to_ws_name(row["Team"])
            key = (team, row["Player"])
            player_stats.setdefault(key, {})["fm_plus_minus"] = row.drop(["Team", "Player"]).to_dict()

    # compute_player_scoring_stats()'s full per-player table (Minutes
    # Played, Goals, Assists, Shots, NPxG, PS-xG, xA, PK, PK Attempted,
    # Sprints) - saved whole under one 'fm_scoring' namespace so the match
    # detail view's Scoring Stats category (see
    # history_db.fetch_player_scoring_stats()) can read it directly rather
    # than reassembling it from several small namespaces. Team names here
    # are FotMob's own, same as fm_totals_df/plus_minus above, so the same
    # _to_ws_name() reconciliation applies.
    player_scoring = report.get("player_scoring")
    if player_scoring is not None and not player_scoring.empty:
        for _, row in player_scoring.iterrows():
            team = _to_ws_name(row["Team"])
            key = (team, row["Player"])
            player_stats.setdefault(key, {})["fm_scoring"] = row.drop(["Team", "Player"]).to_dict()

    # FotMob's per-player Line Breaking Passes total (from the Shot
    # Breakdown tab's 'By Player' table) - its own namespace (rather than
    # folded into fm_scoring above) since it belongs on the Passing
    # category table, not Scoring Stats.
    by_player = (report.get("shot_breakdowns") or {}).get("By Player")
    if by_player is not None and not by_player.empty and "Line Breaking Passes" in by_player.columns:
        for _, row in by_player.dropna(subset=["Line Breaking Passes"]).iterrows():
            team = _to_ws_name(row["Team"])
            key = (team, row["Player"])
            player_stats.setdefault(key, {})["fm_line_breaking_passes"] = {
                "Line Breaking Passes": int(row["Line Breaking Passes"])
            }

    # extract_player_age_and_start()'s per-player Age/Started (starting XI
    # vs substitute) for THIS match - its own namespace, 'fm_lineup', read
    # from a different part of FotMob's payload than every other fm_*
    # namespace here (content.lineup rather than content.playerStats - see
    # that function's own docstring). Backs the Team Page's season Scoring
    # Stats table (history_db.fetch_team_season_scoring_stats()) - Age
    # specifically is "age as of this match", not a birthdate, which is why
    # that function picks one match's reading rather than summing/averaging
    # across matches the way every other column there does. Team names here
    # are FotMob's own, same _to_ws_name() reconciliation as fm_scoring.
    player_lineup = report.get("player_lineup")
    if player_lineup is not None and not player_lineup.empty:
        for _, row in player_lineup.iterrows():
            team = _to_ws_name(row["Team"])
            key = (team, row["Player"])
            player_stats.setdefault(key, {})["fm_lineup"] = {
                "Age": int(row["Age"]), "Started": bool(row["Started"])
            }

    # extract_player_cards()'s per-player Yellow/Red card counts for THIS
    # match - its own 'fm_cards' namespace, read from yet another different
    # part of FotMob's payload (content.matchFacts.events rather than
    # playerStats or lineup - see that function's own docstring). Backs the
    # Team Page's season General Stats table (history_db.fetch_team_season_
    # scoring_stats()'s Totals block). Only players carded at least once
    # appear in player_cards at all - everyone else simply never gets a
    # 'fm_cards' entry here, which that fetch function's summing loop
    # already treats as 0 (see its own docstring). Team names here are
    # FotMob's own, same _to_ws_name() reconciliation as fm_scoring/
    # fm_lineup above.
    player_cards = report.get("player_cards")
    if player_cards is not None and not player_cards.empty:
        for _, row in player_cards.iterrows():
            team = _to_ws_name(row["Team"])
            key = (team, row["Player"])
            player_stats.setdefault(key, {})["fm_cards"] = {
                "Yellow Cards": int(row["Yellow Cards"]), "Red Cards": int(row["Red Cards"])
            }

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

    match_date_iso: caller-supplied fallback date (a date picker's value,
    or the batch runner's target day) - a plain date with no time. This is
    used ONLY if report['kickoff'] (see run_combined_report(), which reads
    it straight off FotMob's own kickoff timestamp) couldn't be extracted.
    When kickoff IS available, it's used instead - it's a full 'YYYY-MM-DD
    HH:MM' string (UK local time), not just a date. That distinction
    matters for the Fixtures tab: several matches saved with the same
    caller-supplied date and no time at all are indistinguishable when
    sorting by Date - kickoff time is what lets same-day matches (a full
    Saturday 3pm slate, say) actually come out in kickoff order.
    """
    match_id = report["fm_match_id"]
    team_stats, player_stats = build_db_stats(report)
    match_date = report.get("kickoff") or match_date_iso

    db = hdb.get_db(db_url)
    try:
        hdb.init_schema(db)
        hdb.publish_report(
            db, match_id=match_id,
            home_team=report["ws_home_name"], away_team=report["ws_away_name"],
            team_stats=team_stats, player_stats=player_stats,
            shots_df=report["combined_shots"],
            passes_df=report["all_passes"],
            touches_df=report["all_touches"],
            competition=competition, match_date=match_date,
            ws_events=report["n_ws_events"], fm_shots=report["n_fm_shots"],
            referee=report.get("referee"),
            matchweek=report.get("matchweek"),
        )
    finally:
        db.close()
    return match_id
