"""
history_bulk_app.py
====================
Bulk-scrape historical league matches using WhoScored only (no FotMob or
SofaScore data needed), for seasons predating FotMob's data availability.

Same architecture as league_bulk_app.py (same WhoScored fixtures scanning,
same database pipeline), but runs only WhoScored — FotMob xG fields are
left blank in the database for these old seasons.

Local use:
    pip install streamlit pandas matplotlib openpyxl
    streamlit run history_bulk_app.py

WORKFLOW
--------
1. Paste a WhoScored league fixtures URL (same as league_bulk_app.py).
   The app scans every month on the fixtures page for finished matches.

2. Review the match list.

3. Check for already-saved matches in the database.

4. Run the batch — Chrome-based WhoScored scraping only.

5. Review results and save to the database.

NOTE: FotMob-sourced stats (xG, NPxG, xA, Minutes Played, Sprints, Line
Breaking Passes, etc.) will be blank in the database for these matches.
WhoScored-sourced stats (Totals, Passing, Touches, Defensive, SCA,
Progressive Passes, Goals) are fully populated.
"""

import io
import os
import random
import re
import tempfile
import time
import traceback
import datetime

from dateutil import parser as _dateparser
import pandas as pd
import streamlit as st

import whoscored_report as wr
import batch_lib
import history_db as hdb

st.set_page_config(page_title="Historical Bulk Runner (WhoScored)", layout="wide")
st.title("Historical Bulk Runner")
st.caption(
    "Bulk-scrape historical league matches using WhoScored data only. "
    "For old seasons where FotMob data isn't available. "
    "Paste the league's WhoScored fixtures URL, scan for matches, "
    "and run + save them all. FotMob xG fields will be left blank."
)

_ROW_KEY_PREFIX = "hbulk_row_"


def _scan_id():
    return st.session_state.get("hbulk_scan_id", "0")


def _row_key(name, i):
    return f"{_ROW_KEY_PREFIX}{_scan_id()}_{name}_{i}"


def _clear_row_widget_state():
    for key in list(st.session_state.keys()):
        if key.startswith(_ROW_KEY_PREFIX):
            del st.session_state[key]
    import uuid
    st.session_state["hbulk_scan_id"] = uuid.uuid4().hex[:8]


# ---------------------------------------------------------------------------
# WhoScored-only report runner
# ---------------------------------------------------------------------------

def _extract_ws_match_id(ws_url):
    """Extract the numeric match ID from a WhoScored URL.

    URLs look like https://www.whoscored.com/Matches/1903410/Live/...
    Returns e.g. 1903410 as an int, or None if not found.
    """
    m = re.search(r"/[Mm]atches/(\d+)/", ws_url)
    return int(m.group(1)) if m else None


def run_whoscored_only_report(ws_url, status_cb=None):
    """
    Scrape a single match from WhoScored only — no FotMob or SofaScore.
    Returns a report dict compatible with batch_lib.build_db_stats() and
    batch_lib.save_report_to_db(), with empty DataFrames for all FotMob
    fields so the save pipeline works unchanged.
    """
    def _status(msg):
        if status_cb:
            status_cb(msg)

    # ---- WhoScored scraping ----
    _status(f"Scraping WhoScored: {ws_url.strip()}")
    df, match_info = wr.scrape_match(ws_url.strip())
    ws_home_name = match_info.get("home_name")
    ws_away_name = match_info.get("away_name")
    ws_match_json = match_info.get("match_json")
    ws_match_date = match_info.get("match_date")

    _status(f"WhoScored returned: {ws_home_name} vs {ws_away_name} ({ws_match_date or 'no date'})")

    # ---- WhoScored computations (same as batch_lib.run_combined_report) ----
    _status("Computing WhoScored tables...")
    _, player_totals, team_totals, progressive_received = wr.compute_progressive_passes(df)
    passes_received = wr.compute_passes_received(df)
    passing_pairs = wr.compute_passing_pairs(df)
    team_carries, player_carries, carries_df = wr.compute_carries(df)
    sca_out = wr.compute_sca(df)
    shot_pairs = wr.compute_shot_pairs(sca_out)
    goals_out = wr.compute_goals(df, ws_home_name, ws_away_name)
    player_windows = wr.extract_player_windows(df)
    player_stats_out = wr.compute_player_stats(df, player_windows=player_windows)
    xt_received = wr.compute_xt_received(df)
    team_summary, player_third = wr.compute_touches(df, team_carries, player_carries,
                                                      passes_received, progressive_received,
                                                      xt_received=xt_received)
    passing_out = wr.compute_passing(df, player_totals, sca_out)
    chains_df, team_sequences = wr.compute_sequences(df)
    field_tilt = wr.compute_field_tilt(team_summary)
    ppda = wr.compute_ppda(df)
    defensive_stats = wr.compute_defensive_stats(df, match_json=ws_match_json)
    defensive_actions = wr.compute_defensive_actions(df)
    defensive_action_location = wr.compute_defensive_action_location(df)
    corners = wr.compute_corners(df)
    totals_out = wr.compute_totals(team_summary, team_totals, passing_out, sca_out,
                                    chains_df, team_sequences, field_tilt, ppda,
                                    defensive_stats, corners, ws_home_name, ws_away_name, df=df)
    against_totals = wr.compute_against_totals(totals_out)
    all_passes = wr.compute_all_passes(df)
    all_touches = wr.compute_all_touches(df)
    all_carries = wr.compute_all_carries(df)
    on_off = wr.compute_on_off(df, player_windows, carries_df)

    # ---- Build the Excel workbook (WhoScored-only) ----
    _status("Building workbook...")
    wb = wr.build_workbook(
        sca_out, team_summary, player_third, passing_out, totals_out,
        defensive_actions, defensive_action_location, passing_pairs,
        ws_home_name, ws_away_name, against_totals,
        shot_pairs, on_off, goals_out=goals_out,
        player_stats_out=player_stats_out,
    )
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)

    filename = f"{wr.sanitize_filename(ws_home_name)}_vs_{wr.sanitize_filename(ws_away_name)}.xlsx"

    # ---- Extract match ID from WhoScored URL ----
    ws_match_id = _extract_ws_match_id(ws_url)
    if ws_match_id is None:
        # Fallback: use a hash of the URL
        import hashlib
        ws_match_id = int(hashlib.md5(ws_url.encode()).hexdigest()[:12], 16)

    # ---- Build report dict (same shape as batch_lib's) ----
    # FotMob fields are set to empty DataFrames / None so build_db_stats()
    # and save_report_to_db() work unchanged.
    return {
        "df": df,
        "ws_home_name": ws_home_name,
        "ws_away_name": ws_away_name,
        "ws_match_date": ws_match_date,
        "ws_url": ws_url.strip(),
        "fm_url": None,
        "referee": None,
        "kickoff": None,
        "matchweek": None,
        "team_name_mismatch": False,
        "totals_out": totals_out,
        "against_totals": against_totals,
        "team_summary": team_summary,
        "player_third": player_third,
        "passing_out": passing_out,
        "defensive_actions": defensive_actions,
        "defensive_action_location": defensive_action_location,
        "passing_pairs": passing_pairs,
        "shot_pairs": shot_pairs,
        "sca_out": sca_out,
        "goals_out": goals_out,
        "on_off": on_off,
        # FotMob-compatible fields — empty so build_db_stats() doesn't crash
        "fm_match_id": ws_match_id,
        "combined_shots": pd.DataFrame(),
        "fm_totals_df": pd.DataFrame(),
        "plus_minus": pd.DataFrame(),
        "player_scoring": None,
        "player_lineup": None,
        "player_cards": None,
        "player_positions": None,
        "shot_breakdowns": None,
        # Raw data for DB
        "all_passes": all_passes,
        "all_touches": all_touches,
        "all_carries": all_carries,
        # Workbook
        "wb_bytes": buf.getvalue(),
        "filename": filename,
        "n_ws_events": len(df),
        "n_fm_shots": 0,
    }


# ---------------------------------------------------------------------------
# Step 1: build the match list from the league fixtures page
# ---------------------------------------------------------------------------
st.header("Step 1: Load fixtures from WhoScored league page")

league_fixtures_url = st.text_input(
    "WhoScored league fixtures URL",
    placeholder="https://www.whoscored.com/Regions/252/Tournaments/2/Seasons/.../Fixtures/...",
    help="The league's own Fixtures/Results page on WhoScored for a historical season.",
    key="hbulk_fixtures_url",
)

only_finished = st.checkbox("Only finished matches", value=True, key="hbulk_only_finished")

# Month range selector
_SEASON_MONTHS = [
    "August", "September", "October", "November", "December",
    "January", "February", "March", "April", "May", "June", "July",
]
_MONTH_NUM = {
    "August": 8, "September": 9, "October": 10, "November": 11, "December": 12,
    "January": 1, "February": 2, "March": 3, "April": 4, "May": 5, "June": 6, "July": 7,
}

mcol1, mcol2 = st.columns(2)
from_month = mcol1.selectbox("From month", _SEASON_MONTHS, index=0, key="hbulk_from_month")
to_month = mcol2.selectbox("To month", _SEASON_MONTHS, index=9, key="hbulk_to_month")


def _month_range_set(from_name, to_name):
    start_idx = _SEASON_MONTHS.index(from_name)
    end_idx = _SEASON_MONTHS.index(to_name)
    if end_idx >= start_idx:
        return {_MONTH_NUM[_SEASON_MONTHS[i]] for i in range(start_idx, end_idx + 1)}
    return ({_MONTH_NUM[_SEASON_MONTHS[i]] for i in range(start_idx, len(_SEASON_MONTHS))} |
            {_MONTH_NUM[_SEASON_MONTHS[i]] for i in range(0, end_idx + 1)})


manual_urls_text = st.text_area(
    "Paste additional WhoScored match URLs (one per line)",
    height=80,
    help="Optional — add any match URLs not picked up by the auto-scan.",
    key="hbulk_manual_urls",
)

if st.button("Scan all months", type="primary", key="hbulk_scan"):
    all_matches = []

    if league_fixtures_url.strip():
        try:
            fixtures_status = st.empty()
            allowed_months = _month_range_set(from_month, to_month)
            all_matches = wr.get_all_fixture_urls(
                league_fixtures_url.strip(),
                only_finished=only_finished,
                month_range=allowed_months,
                status_cb=fixtures_status.write,
            )
            st.success(f"Found {len(all_matches)} match(es) across all months.")
        except Exception as e:
            st.warning(f"Fixture scan failed ({e}) — use the manual paste box below.")
            with st.expander("Full error"):
                st.code(traceback.format_exc())

    seen = {m["match_url"] for m in all_matches}
    for line in manual_urls_text.splitlines():
        url = line.strip()
        if url and url not in seen:
            all_matches.append({
                "match_url": url, "home_name": None, "away_name": None,
                "status": "(manual)", "date": None,
            })
            seen.add(url)

    _clear_row_widget_state()
    st.session_state["hbulk_matches"] = all_matches
    st.session_state.pop("hbulk_run_results", None)

    if not all_matches:
        st.error("No matches found — check the URL, or paste match URLs manually.")
    else:
        # --- Auto-run: check for already-saved matches in DB ---
        _auto_db = os.environ.get("DATABASE_URL", "sqlite:///history.db")
        _auto_db = st.session_state.get("hbulk_db_url", _auto_db)
        n_dup = 0
        for i, m in enumerate(all_matches):
            row_date = datetime.date.today()
            if m.get("date"):
                try:
                    row_date = _dateparser.parse(m["date"]).date()
                except Exception:
                    pass
            row_date_iso = row_date.isoformat()
            if m.get("home_name") and m.get("away_name"):
                already = batch_lib.find_existing_match(
                    _auto_db, m["home_name"], m["away_name"], row_date_iso)
                all_matches[i]["already_saved"] = already
                all_matches[i]["already_saved_date"] = row_date_iso
                if already:
                    n_dup += 1
        if n_dup:
            st.info(f"{n_dup} of {len(all_matches)} match(es) already in the database.")
        st.session_state["hbulk_matches"] = all_matches


# ---------------------------------------------------------------------------
# Step 2: review match list
# ---------------------------------------------------------------------------
matches = st.session_state.get("hbulk_matches", [])
if matches:
    st.header("Step 2: Review matches")
    st.write(f"**{len(matches)} match(es)** found — check the ones you want to run.")

    default_date = st.date_input(
        "Fallback date (for manually pasted URLs with no date)",
        value=datetime.date.today(),
        help="Only used for manually pasted URLs that have no date from WhoScored.",
        key="hbulk_default_date",
    )

    # -----------------------------------------------------------------------
    # Anti-duplicate check
    # -----------------------------------------------------------------------
    db_url_for_check = st.text_input(
        "Database URL (for duplicate check + saving)",
        value=st.session_state.get("hbulk_db_url",
                                    os.environ.get("DATABASE_URL", "sqlite:///history.db")),
        key="hbulk_db_url",
    )

    if st.button("Check for already-saved matches", key="hbulk_dup_check"):
        n_found = 0
        for i, m in enumerate(matches):
            row_date = st.session_state.get(_row_key("date", i), default_date)
            if m.get("date"):
                try:
                    row_date = _dateparser.parse(m["date"]).date()
                except Exception:
                    pass
            row_date_iso = row_date.isoformat() if hasattr(row_date, "isoformat") else str(row_date)
            if m.get("home_name") and m.get("away_name"):
                already = batch_lib.find_existing_match(
                    db_url_for_check, m["home_name"], m["away_name"], row_date_iso
                )
                matches[i]["already_saved"] = already
                matches[i]["already_saved_date"] = row_date_iso
                if already:
                    n_found += 1
        st.session_state["hbulk_matches"] = matches
        st.success(f"{n_found} of {len(matches)} match(es) already in the database.")

    # -----------------------------------------------------------------------
    # Match list display
    # -----------------------------------------------------------------------
    st.write("Check the matches you want to run:")

    def _hbulk_select_all():
        val = st.session_state.get("hbulk_select_all", False)
        for _j in range(len(matches)):
            st.session_state[_row_key("include", _j)] = val

    st.checkbox("Select all", value=False, key="hbulk_select_all", on_change=_hbulk_select_all)

    for i, m in enumerate(matches):
        with st.container(border=True):
            label = f"{m.get('home_name') or '?'} vs {m.get('away_name') or '?'}"
            if m.get("date"):
                label += f"  —  {m['date']}"
            if m.get("status"):
                label += f"  ({m['status']})"
            st.write(f"**{label}**")

            if m.get("already_saved"):
                st.caption(
                    f"Already saved to the database for {m.get('already_saved_date')}. "
                    "Check the box only to re-scrape and overwrite."
                )

            row_default_date = default_date
            if m.get("date"):
                try:
                    row_default_date = _dateparser.parse(m["date"]).date()
                except Exception:
                    pass

            default_checked = False

            cols = st.columns([0.6, 3.0, 1.4])
            cols[0].checkbox("Run", value=default_checked, key=_row_key("include", i))
            cols[1].text_input("WhoScored URL", value=m["match_url"], key=_row_key("ws_url", i))
            cols[2].date_input("Match date", value=row_default_date, key=_row_key("date", i))

    # ---------------------------------------------------------------------------
    # Step 3: run the batch
    # ---------------------------------------------------------------------------
    st.header("Step 3: Run the batch")
    st.caption(
        "Each match runs real, non-headless Chrome for WhoScored scraping. "
        "Only WhoScored data is collected — FotMob xG fields will be blank."
    )
    dcol1, dcol2 = st.columns(2)
    min_delay = dcol1.number_input("Min delay between matches (seconds)", min_value=0, value=7,
                                    key="hbulk_min_delay")
    max_delay = dcol2.number_input("Max delay between matches (seconds)", min_value=0, value=25,
                                    key="hbulk_max_delay")
    if max_delay < min_delay:
        st.warning("Max delay < min delay — using min for both.")
        max_delay = min_delay

    _as_col1, _as_col2 = st.columns([1, 1])
    auto_save = _as_col1.checkbox(
        "Automatically save to database",
        help="After all matches are scraped, every successful match is saved to the "
             "database automatically — one click does everything.",
        key="hbulk_auto_save",
    )
    auto_save_comp = _as_col2.selectbox(
        "Competition (for auto-save)",
        ["Premier League", "UEFA Champions League"],
        index=0, key="hbulk_auto_save_comp",
    )

    if st.button("Run batch", type="primary", key="hbulk_run"):
        to_run = []
        for i in range(len(matches)):
            if not st.session_state.get(_row_key("include", i), False):
                continue
            ws_url = st.session_state.get(_row_key("ws_url", i), matches[i]["match_url"]).strip()
            match_date = st.session_state.get(_row_key("date", i), default_date)
            to_run.append({"ws_url": ws_url, "match_date": match_date, "row_index": i})

        if not to_run:
            st.error("No matches checked to run.")
        else:
            results = []
            progress = st.progress(0.0)
            status_area = st.empty()
            for idx, entry in enumerate(to_run):
                n = len(to_run)
                try:
                    def _cb(msg, idx=idx, n=n):
                        status_area.write(f"Match {idx + 1}/{n}: {msg}")

                    report = run_whoscored_only_report(
                        entry["ws_url"], status_cb=_cb
                    )
                    report["match_date"] = entry["match_date"]
                    results.append({"ws_url": entry["ws_url"], "success": True,
                                     "report": report})
                except Exception as e:
                    status_area.write(f"Match {idx + 1}/{n}: FAILED ({e})")
                    results.append({"ws_url": entry["ws_url"], "success": False,
                                     "error": str(e), "traceback": traceback.format_exc()})

                progress.progress((idx + 1) / n)
                if idx < n - 1:
                    delay = random.uniform(min_delay, max_delay)
                    status_area.write(f"Waiting {delay:.0f}s before next match...")
                    time.sleep(delay)

            st.session_state["hbulk_run_results"] = results

            # --- Auto-save: save ALL successful matches after scraping completes ---
            if auto_save:
                _auto_db_url = st.session_state.get(
                    "hbulk_db_url",
                    os.environ.get("DATABASE_URL", "sqlite:///history.db"))
                _auto_comp = st.session_state.get("hbulk_auto_save_comp", "Premier League")
                successful = [r for r in results if r["success"]]
                if successful:
                    save_progress = st.progress(0.0)
                    save_status = st.empty()
                    n_saved = 0
                    for si, r in enumerate(successful):
                        report = r["report"]
                        try:
                            match_date_iso = (
                                report["match_date"].isoformat()
                                if hasattr(report["match_date"], "isoformat")
                                else str(report["match_date"]))
                            match_id = batch_lib.save_report_to_db(
                                _auto_db_url, report, _auto_comp, match_date_iso)
                            n_saved += 1
                            save_status.write(
                                f"Saving {si + 1}/{len(successful)}: "
                                f"{report['ws_home_name']} vs {report['ws_away_name']} ✓")
                        except Exception as save_err:
                            save_status.error(
                                f"Failed to save {report['ws_home_name']} vs "
                                f"{report['ws_away_name']}: {save_err}")
                        save_progress.progress((si + 1) / len(successful))
                    st.success(f"Auto-save complete: {n_saved}/{len(successful)} matches saved.")

# ---------------------------------------------------------------------------
# Step 4 + 5: review results, then save to database
# ---------------------------------------------------------------------------
results = st.session_state.get("hbulk_run_results")
if results:
    st.header("Step 4: Review results")
    n_success = sum(r["success"] for r in results)
    st.write(f"{n_success}/{len(results)} match(es) scraped successfully.")

    for i, r in enumerate(results):
        if r["success"]:
            report = r["report"]
            ws_date_str = report.get("ws_match_date") or "(no date)"
            with st.expander(
                f"OK {report['ws_home_name']} vs {report['ws_away_name']}  —  {ws_date_str}"
            ):
                st.write(f"**Requested URL:** `{r['ws_url']}`")
                st.write(f"**WhoScored returned:** {report['ws_home_name']} vs "
                         f"{report['ws_away_name']} ({ws_date_str})")
                st.write(f"WhoScored: {report['n_ws_events']} events")
                batch_date = report.get("match_date")
                if batch_date and report.get("ws_match_date"):
                    try:
                        import datetime as _dt
                        scraped_dt = _dateparser.parse(report["ws_match_date"]).date()
                        batch_dt = (batch_date if isinstance(batch_date, _dt.date)
                                    else _dateparser.parse(str(batch_date)).date())
                        if scraped_dt != batch_dt:
                            st.error(
                                f"**DATE MISMATCH**: You entered {batch_dt.isoformat()} but "
                                f"WhoScored's data is for **{report['ws_match_date']}**. "
                                f"Do NOT save this row."
                            )
                    except Exception:
                        pass
                st.dataframe(report["totals_out"], use_container_width=True)
                st.download_button(
                    label=f"Download {report['filename']}",
                    data=report["wb_bytes"],
                    file_name=report["filename"],
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    key=f"hbulk_dl_{i}",
                )
        else:
            with st.expander(f"FAILED {r['ws_url']}", expanded=True):
                st.error(r["error"])
                if r.get("traceback"):
                    st.code(r["traceback"])

    st.header("Step 5: Save to database")
    db_url = st.text_input(
        "Database URL",
        value=st.session_state.get("hbulk_db_url",
                                    os.environ.get("DATABASE_URL", "sqlite:///history.db")),
        help="Reuses whatever you entered in Step 2.",
        key="hbulk_db_url_save",
    )
    competition = st.selectbox("Competition",
                                ["Premier League", "UEFA Champions League"],
                                index=0, key="hbulk_competition")

    if st.button("Save all successful matches to the database", type="primary",
                  key="hbulk_save"):
        any_saved = False
        for r in results:
            if not r["success"]:
                continue
            report = r["report"]
            match_date = report["match_date"]
            match_date_iso = (match_date.isoformat() if hasattr(match_date, "isoformat")
                              else str(match_date))
            try:
                match_id = batch_lib.save_report_to_db(db_url, report, competition,
                                                        match_date_iso)
                st.success(
                    f"Saved {report['ws_home_name']} vs {report['ws_away_name']} "
                    f"(match_id={match_id}, {len(report['all_passes'])} passes)."
                )
                any_saved = True
            except Exception as e:
                st.error(f"Couldn't save {report['ws_home_name']} vs "
                         f"{report['ws_away_name']}: {e}")
                st.code(traceback.format_exc())
        if not any_saved:
            st.info("Nothing saved — no successful matches, or all saves failed.")
