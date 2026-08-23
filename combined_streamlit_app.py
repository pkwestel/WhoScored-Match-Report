"""
Streamlit UI combining whoscored_report.py + fotmob_report.py into ONE
workbook for the same match, pulled from both sources.

Local use:
    Drop this file into the same folder as whoscored_report.py,
    fotmob_report.py, and combined_report.py (the root of your cloned
    football-data-webscraping repo), then:
        pip install streamlit matplotlib
        streamlit run combined_streamlit_app.py
    This opens a page in your browser at http://localhost:8501 - paste in
    BOTH a WhoScored match-centre URL and a FotMob match URL for the SAME
    match, click the button, and download the combined workbook.

    This is a THIRD, separate app - streamlit_app.py (WhoScored only) and
    fotmob_streamlit_app.py (FotMob only) are unchanged and still work on
    their own. Use this one specifically when you want both sources merged
    into a single file. There's no live Pass Map preview in this app itself
    (that's still streamlit_app.py's tab) - but "Save to Database" below
    does publish every pass in the match, so the Pass Map/Passes Received
    visuals show up on the read-only dashboard_app.py site once saved.

    NOTE: if you edit whoscored_report.py, fotmob_report.py, or
    combined_report.py while Streamlit is already running, clicking
    "Rerun" in the browser is NOT enough to pick up the change - Python
    keeps the already-imported modules cached in memory. Stop the server
    (Ctrl+C) and run `streamlit run combined_streamlit_app.py` again.

WHY TWO URLS
------------
WhoScored and FotMob don't share any common match ID, so there's no way to
auto-derive one site's link from the other - you have to find and paste in
both links for the same match yourself. This app does a light sanity check
(comparing each source's own home/away team names) and warns you if they
don't look like the same match, but it can't catch every mismatch (e.g. two
different meetings between the same two teams).

WHY THE FOTMOB HALF POPS UP A VISIBLE BROWSER WINDOW
------------------------------------------------------
fotmob_report.py's scraper specifically requires a real, non-headless
Chrome session with network logging enabled to capture FotMob's own
matchDetails response - see that file's module docstring for the full
explanation. The WhoScored half runs headless as usual; only the FotMob
half will visibly pop up a Chrome window for a few seconds.
"""

import contextlib
import datetime
import io
import os
import tempfile
import traceback

import streamlit as st

import whoscored_report as wr
import fotmob_report as fr
import combined_report as cr
import history_db as hdb
import batch_lib as bl

st.set_page_config(page_title="Combined Match Report", layout="wide")
st.title("Combined WhoScored + FotMob Match Report Generator")
st.write(
    "Paste BOTH a WhoScored match-centre URL and a FotMob match URL for the SAME match. This "
    "scrapes both sites and builds one combined workbook - each source's own tabs kept separate "
    "(only Totals is prefixed WS -/FM -, since that's the one name both sources share), plus a new "
    "merged Shot Creating Actions tab blending WhoScored's shot detail with FotMob's xG/outcome data."
)
st.caption(
    "The FotMob half needs a real, visible Chrome window (not headless) to work - expect a "
    "browser window to pop up briefly partway through."
)

ws_url = st.text_input(
    "WhoScored match URL",
    placeholder="https://www.whoscored.com/matches/1903410/live/...",
)
fm_url = st.text_input(
    "FotMob match URL",
    placeholder="https://www.fotmob.com/matches/<slug>/<code>#<matchId>",
)

if st.button("Generate Combined Report", type="primary"):
    if not ws_url.strip() or not fm_url.strip():
        st.error("Please paste both a WhoScored URL and a FotMob URL.")
    else:
        try:
            with st.spinner("Scraping WhoScored (headless, ~10-20s)..."):
                df, match_info = wr.scrape_match(ws_url.strip())
            ws_home_name = match_info.get("home_name")
            ws_away_name = match_info.get("away_name")

            with st.spinner("Computing WhoScored tables..."):
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
                # Every pass in the match (both ends, all outcomes) - computed
                # once here rather than per-player, purely so "Save to
                # Database" below can publish the Pass Map / Passes Received
                # data for every player in one shot (see compute_all_passes()'s
                # own docstring in whoscored_report.py).
                all_passes = wr.compute_all_passes(df)
                # Same reasoning as all_passes above, but for every touch on
                # the ball rather than just passes - backs the dashboard's
                # (single-match and season-long) touch heat map.
                all_touches = wr.compute_all_touches(df)
                player_windows = wr.extract_player_windows(df)
                on_off = wr.compute_on_off(df, player_windows, carries_df)

            wb_ws = wr.build_workbook(sca_out, team_summary, player_third, passing_out, totals_out,
                                       defensive_actions, defensive_action_location, passing_pairs,
                                       ws_home_name, ws_away_name, against_totals, shot_pairs, on_off)

            fm_debug_buf = io.StringIO()
            with st.spinner("Opening the FotMob match page (a real Chrome window will pop up, ~10-25s)..."):
                fm_out_dir = tempfile.mkdtemp(prefix="fotmob_")
                with contextlib.redirect_stdout(fm_debug_buf):
                    fm_match_json, fm_match_id = fr.scrape_match(fm_url.strip(), out_dir=fm_out_dir)

            fm_home_name, fm_away_name = fr.extract_team_names(fm_match_json)
            referee = fr.extract_referee(fm_match_json)

            with st.spinner("Computing FotMob tables..."):
                shots_df = fr.compute_shots(fm_match_json)
                fm_totals_df = fr.compute_totals(fm_match_json, shots_df, fm_home_name, fm_away_name)
                player_xa = fr.extract_player_xa(fm_match_json)
                player_minutes = fr.extract_player_minutes(fm_match_json)
                shot_breakdowns = fr.compute_shot_breakdowns(shots_df, player_xa, player_minutes)
                xg_breakdown = fr.compute_xg_breakdown(shots_df, fm_home_name, fm_away_name)
                player_windows = fr.extract_player_windows(fm_match_json, player_minutes, shots_df)
                plus_minus = fr.compute_plus_minus(shots_df, player_windows, fm_home_name, fm_away_name)
                plus_minus_warning = (
                    "Plus Minus is empty - FotMob didn't return any lineup/substitution data for "
                    "this match (no starters/subs list at all), which happens most often for a "
                    "match FotMob hasn't fully finished processing yet. Try again later, or check "
                    "the 'lineup' section of the saved fotmob_raw_*.json debug file."
                ) if player_windows.empty else None

            wb_fm = fr.build_workbook(shots_df, fm_totals_df, fm_home_name, fm_away_name, fm_match_id,
                                       shot_breakdowns, xg_breakdown, plus_minus)

            with st.spinner("Merging Shot Creating Actions + Shots..."):
                combined_shots = cr.compute_combined_shots(sca_out, shots_df)

            wb = cr.build_combined_workbook(wb_ws, wb_fm, combined_shots, ws_home_name, ws_away_name)
            buf = io.BytesIO()
            wb.save(buf)
            buf.seek(0)

            filename = f"{wr.sanitize_filename(ws_home_name)}_vs_{wr.sanitize_filename(ws_away_name)}_combined.xlsx"

            # Stashed in session_state so the report - and the download
            # button - survive Streamlit's rerun on any later widget change.
            st.session_state["combined_report"] = {
                "ws_home_name": ws_home_name,
                "ws_away_name": ws_away_name,
                "fm_home_name": fm_home_name,
                "fm_away_name": fm_away_name,
                "referee": referee,
                "totals_out": totals_out,
                "against_totals": against_totals,
                "player_third": player_third,
                "passing_out": passing_out,
                "defensive_actions": defensive_actions,
                "defensive_action_location": defensive_action_location,
                "passing_pairs": passing_pairs,
                "shot_pairs": shot_pairs,
                "on_off": on_off,
                "combined_shots": combined_shots,
                "fm_match_id": fm_match_id,
                "fm_totals_df": fm_totals_df,
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
                "fm_debug_log": fm_debug_buf.getvalue(),
            }

        except Exception as e:
            st.error(f"Something went wrong: {e}")
            if "fm_debug_buf" in locals():
                with st.expander("FotMob debug log (scrape_match console output)"):
                    st.code(fm_debug_buf.getvalue() or "(nothing captured)")
            st.code(traceback.format_exc())

# ---------------------------------------------------------------------------
# Render the report (from session_state, so it survives later reruns)
# ---------------------------------------------------------------------------
report = st.session_state.get("combined_report")
if report:
    st.success(
        f"WhoScored: {report['ws_home_name']} vs {report['ws_away_name']} "
        f"({report['n_ws_events']} events)  |  FotMob: {report['fm_home_name']} vs "
        f"{report['fm_away_name']} ({report['n_fm_shots']} shots)"
    )
    # Compared via canonical_team_name() rather than a plain string match,
    # so known spelling differences ("Man Utd" vs "Manchester United",
    # "Spurs" vs "Tottenham Hotspur" - see combined_report.TEAM_NAME_ALIASES)
    # don't trigger a false-positive warning here.
    if (cr.canonical_team_name(report["ws_home_name"]) != cr.canonical_team_name(report["fm_home_name"])
            or cr.canonical_team_name(report["ws_away_name"]) != cr.canonical_team_name(report["fm_away_name"])):
        st.warning(
            "The team names from WhoScored and FotMob don't look like the same match - double "
            "check both URLs are really for the SAME match before trusting the combined Shot "
            "Creating Actions tab."
        )

    st.download_button(
        label=f"Download {report['filename']}",
        data=report["wb_bytes"],
        file_name=report["filename"],
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

    # -----------------------------------------------------------------------
    # Save to Database - pushes this match into the hosted history DB (see
    # history_db.py) so it accumulates across matches for the read-only
    # dashboard_app.py site, instead of only existing in this one-off
    # downloaded workbook. Building a season-long database was the whole
    # point of this - see history_db.py's own docstring for the schema.
    # -----------------------------------------------------------------------
    with st.expander("Save to Database", expanded=False):
        st.write(
            "Pushes this match's team/player/shot data into the history database (SQLite locally, "
            "or your hosted Postgres once you've set one up) so it accumulates across matches for "
            "the season-long dashboard."
        )
        default_db_url = os.environ.get("DATABASE_URL", "sqlite:///history.db")
        db_url = st.text_input(
            "Database URL",
            value=default_db_url,
            help="sqlite:///history.db for local testing, or postgresql://user:pass@host:port/dbname "
                 "for your hosted database. Defaults to the DATABASE_URL environment variable if set.",
        )
        competition = st.text_input("Competition", value="Premier League")
        match_date = st.date_input("Match date", value=datetime.date.today())

        if st.button("Save this match to the database"):
            try:
                match_id = report["fm_match_id"]

                # Team stats: WhoScored's Totals + FotMob's Totals, namespaced
                # separately per team rather than merged, since the two
                # sources don't share a column schema (see history_db.py's
                # own docstring on this). Player stats: WhoScored Passing +
                # Defensive Actions, and FotMob Plus Minus, each nested under
                # their own key. Built via batch_lib.build_db_stats() (rather
                # than duplicating this logic here, which is what used to
                # happen - batch_lib.py's own docstring specifically warns
                # about the two copies drifting apart) so this single-match
                # flow and the batch runner's "Save all" button always
                # reconcile FotMob's vs WhoScored's team names for the same
                # club (Ipswich/Ipswich Town, Man Utd/Manchester United, etc.
                # - see build_db_stats()'s docstring) the exact same way.
                team_stats, player_stats = bl.build_db_stats(report)

                db = hdb.get_db(db_url)
                hdb.init_schema(db)
                hdb.publish_report(
                    db, match_id=match_id,
                    home_team=report["ws_home_name"], away_team=report["ws_away_name"],
                    team_stats=team_stats, player_stats=player_stats,
                    shots_df=report["combined_shots"],
                    passes_df=report["all_passes"],
                    touches_df=report["all_touches"],
                    competition=competition, match_date=match_date.isoformat(),
                    ws_events=report["n_ws_events"], fm_shots=report["n_fm_shots"],
                    referee=report.get("referee"),
                )
                db.close()
                st.success(
                    f"Saved match {match_id} to the database, including "
                    f"{len(report['all_passes'])} passes and {len(report['all_touches'])} touches "
                    "for the dashboard's Pass Map/Passes Received/Heat Map tabs."
                )
            except Exception as e:
                st.error(f"Couldn't save to the database: {e}")
                st.code(traceback.format_exc())

    ws_home_name, ws_away_name = report["ws_home_name"], report["ws_away_name"]

    (tabWSTotals, tabWSAgainst, tabWSTouches, tabWSPassing, tabSCA, tabWSDef, tabWSDefLoc,
     tabWSPairs, tabWSShotPairs, tabWSOnOff, tabFMTotals, tabFMBreakdown, tabFMPlusMinus) = st.tabs([
        "WS - Totals", "Against", "Touches", "Passing",
        "Shot Creating Actions", "Defensive Actions",
        "Defensive Action Location", "Passing Pairs", "Shot Pairs", "On/Off",
        "FM - Totals", "Shot Breakdown", "Plus Minus",
    ])

    with tabWSTotals:
        st.dataframe(report["totals_out"], use_container_width=True)
    with tabWSAgainst:
        st.dataframe(report["against_totals"], use_container_width=True)
    with tabWSTouches:
        player_third = report["player_third"]
        for t in [ws_home_name, ws_away_name]:
            st.subheader(t)
            st.dataframe(
                player_third[player_third["team"] == t].drop(columns=["team"]).reset_index(drop=True),
                use_container_width=True,
            )
    with tabWSPassing:
        passing_out = report["passing_out"]
        for t in [ws_home_name, ws_away_name]:
            st.subheader(t)
            st.dataframe(
                passing_out[passing_out["team"] == t].drop(columns=["team"]).reset_index(drop=True),
                use_container_width=True,
            )
    with tabSCA:
        st.write(
            "One row per shot - WhoScored's own shot list (Player/Distance/Body Part/SCA1/SCA2), "
            "with FotMob's own Minute/Added Time and xG/PSxG/Outcome/Situation attached to each row by "
            "matching team and chronological order (shots matched by team + chronological order; a "
            "shot-count mismatch between the two sources leaves the extra shot(s) with blank FotMob "
            "fields)."
        )
        combined_shots = report["combined_shots"]
        for t in [ws_home_name, ws_away_name]:
            st.subheader(t)
            t_shots = combined_shots[combined_shots["Team"] == t].drop(columns=["Team"]).reset_index(drop=True)
            st.dataframe(t_shots, use_container_width=True)

            top3 = (t_shots[t_shots["Situation"] != "Penalty"][["Minute", "Player", "xG"]]
                    .dropna(subset=["xG"])
                    .sort_values("xG", ascending=False)
                    .head(3)
                    .reset_index(drop=True))
            st.caption("Top 3 Shots by xG")
            st.dataframe(top3, use_container_width=True)
    with tabWSDef:
        defensive_actions = report["defensive_actions"]
        for t in [ws_home_name, ws_away_name]:
            st.subheader(t)
            st.dataframe(
                defensive_actions[defensive_actions["team"] == t].drop(columns=["team"]).reset_index(drop=True),
                use_container_width=True,
            )
    with tabWSDefLoc:
        defensive_action_location = report["defensive_action_location"]
        for t in [ws_home_name, ws_away_name]:
            st.subheader(t)
            st.dataframe(
                defensive_action_location[defensive_action_location["team"] == t]
                .drop(columns=["team"]).reset_index(drop=True),
                use_container_width=True,
            )
    with tabWSPairs:
        passing_pairs = report["passing_pairs"]
        for t in [ws_home_name, ws_away_name]:
            st.subheader(t)
            st.dataframe(
                passing_pairs[passing_pairs["team"] == t].drop(columns=["team"]).reset_index(drop=True),
                use_container_width=True,
            )
    with tabWSShotPairs:
        shot_pairs = report["shot_pairs"]
        for t in [ws_home_name, ws_away_name]:
            st.subheader(t)
            st.dataframe(
                shot_pairs[shot_pairs["team"] == t].drop(columns=["team"]).reset_index(drop=True),
                use_container_width=True,
            )
    with tabWSOnOff:
        st.write(
            "For every player, team totals - Shots, Total Touches, thirds, Attacking Box, carries/"
            "passes into the final third and box, and open-play crosses - split into **For** (their "
            "own team) and **Against** (the opponent), counted only for the minutes that player was "
            "actually on the pitch. Interceptions, Blocked Passes, and Blocked Shots are also shown, "
            "For only. A substituted player isn't credited for the exact minute they left."
        )
        on_off = report["on_off"]
        for t in [ws_home_name, ws_away_name]:
            st.subheader(t)
            st.dataframe(
                wr.on_off_display_columns(
                    on_off[on_off["Team"] == t].drop(columns=["Team"]).reset_index(drop=True)
                ),
                use_container_width=True,
            )
    with tabFMTotals:
        st.dataframe(report["fm_totals_df"], use_container_width=True)
        st.subheader("xG Breakdown (by half, by phase)")
        st.dataframe(report["xg_breakdown"], use_container_width=True)
    with tabFMBreakdown:
        for label, bdf in report["shot_breakdowns"].items():
            st.subheader(label)
            breakdown_teams = ([t for t in [report["fm_home_name"], report["fm_away_name"]] if t is not None]
                                or sorted(bdf["Team"].dropna().unique()))
            for t in breakdown_teams:
                st.caption(t)
                st.dataframe(
                    bdf[bdf["Team"] == t].drop(columns=["Team"]).reset_index(drop=True),
                    use_container_width=True,
                )
    with tabFMPlusMinus:
        st.write(
            "Goals/Shots/xG For and Against, totaled for exactly the minutes each player was on "
            "the pitch. Penalties are excluded, and xG combines same-minute shots by the same "
            "team into one probability rather than summing them, since two shots in the same "
            "minute are almost certainly a rebound/scramble in the same phase of play."
        )
        if report.get("plus_minus_warning"):
            st.warning(report["plus_minus_warning"])
        plus_minus = report["plus_minus"]
        pm_teams = ([] if plus_minus.empty else
                    ([t for t in [report["fm_home_name"], report["fm_away_name"]] if t is not None]
                     or sorted(plus_minus["Team"].dropna().unique())))
        for t in pm_teams:
            st.subheader(t)
            st.dataframe(
                plus_minus[plus_minus["Team"] == t].drop(columns=["Team"]).reset_index(drop=True),
                use_container_width=True,
            )

    with st.expander("FotMob debug log (scrape_match console output)"):
        st.code(report.get("fm_debug_log") or "(nothing captured)")
