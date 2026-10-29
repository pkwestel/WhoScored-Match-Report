"""
history_bulk_app.py
====================
Bulk-scrape historical league matches using WhoScored + SofaScore (instead
of FotMob), for seasons predating FotMob's data availability (pre-2020-21).

Same architecture as league_bulk_app.py (same WhoScored fixtures scanning,
same database pipeline), but swaps SofaScore in for FotMob as the source of
shot map, player stats, team statistics, and match metadata.

Local use:
    pip install streamlit pandas matplotlib openpyxl
    streamlit run history_bulk_app.py

WORKFLOW
--------
1. Paste a WhoScored league fixtures URL (same as league_bulk_app.py).
   The app scans every month on the fixtures page for finished matches.

2. Review the match list — each row shows the WhoScored URL, date, and a
   SofaScore URL field. You'll need to paste SofaScore URLs manually (or
   use the auto-search feature to try to find them).

3. Check for already-saved matches in the database.

4. Run the batch — same Chrome-based WhoScored + SofaScore scraping.

5. Review results and save to the database.

NOTE: SofaScore doesn't provide sprints or line-breaking passes (FotMob-
specific stats). Those columns will be zero-filled in the database.
"""

import os
import random
import tempfile
import time
import traceback
import datetime

from dateutil import parser as _dateparser
import streamlit as st

import whoscored_report as wr
import batch_lib

st.set_page_config(page_title="Historical Bulk Runner (SofaScore)", layout="wide")
st.title("Historical Bulk Runner")
st.caption(
    "Bulk-scrape historical league matches using WhoScored + SofaScore. "
    "For pre-2020-21 seasons where FotMob data isn't available. "
    "Paste the league's WhoScored fixtures URL, scan for matches, add SofaScore URLs, "
    "and run + save them all."
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


# ---------------------------------------------------------------------------
# Step 2: review + SofaScore URL entry
# ---------------------------------------------------------------------------
matches = st.session_state.get("hbulk_matches", [])
if matches:
    st.header("Step 2: Review matches and add SofaScore URLs")
    st.write(f"**{len(matches)} match(es)** found — check the ones you want to run.")
    st.info(
        "Paste SofaScore match URLs for each match. You can find these by searching "
        "sofascore.com for the match date and team names. The URL should contain "
        "`#id:` followed by the numeric match ID."
    )

    default_date = st.date_input(
        "Fallback date (for manually pasted URLs with no date)",
        value=datetime.date.today(),
        help="Only used for manually pasted URLs that have no date from WhoScored.",
        key="hbulk_default_date",
    )

    # Bulk SofaScore URL paste
    with st.expander("Bulk paste SofaScore URLs"):
        st.caption(
            "Paste SofaScore URLs one per line, in the same order as the match list above. "
            "Lines that don't contain 'sofascore' are skipped."
        )
        bulk_ss_urls = st.text_area("SofaScore URLs (one per line)", height=120,
                                     key="hbulk_bulk_ss_urls")
        if st.button("Apply bulk SofaScore URLs", key="hbulk_apply_bulk_ss"):
            ss_urls = [u.strip() for u in bulk_ss_urls.splitlines()
                       if u.strip() and 'sofascore' in u.lower()]
            applied = 0
            for i, ss_url in enumerate(ss_urls):
                if i < len(matches):
                    matches[i]["ss_url"] = ss_url
                    st.session_state[_row_key("ss_url", i)] = ss_url
                    applied += 1
            st.session_state["hbulk_matches"] = matches
            st.success(f"Applied {applied} SofaScore URL(s).")

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

            cols = st.columns([0.6, 2.2, 2.2, 1.4])
            cols[0].checkbox("Run", value=default_checked, key=_row_key("include", i))
            cols[1].text_input("WhoScored URL", value=m["match_url"], key=_row_key("ws_url", i))
            cols[2].text_input(
                "SofaScore URL", value=m.get("ss_url") or "",
                key=_row_key("ss_url", i),
                help="e.g. https://www.sofascore.com/football/match/team1-team2/abc#id:12345678",
            )
            cols[3].date_input("Match date", value=row_default_date, key=_row_key("date", i))

    # ---------------------------------------------------------------------------
    # Step 3: run the batch
    # ---------------------------------------------------------------------------
    st.header("Step 3: Run the batch")
    st.caption(
        "Each match runs real, non-headless Chrome for both WhoScored and SofaScore scraping. "
        "SofaScore data (shots, stats, lineups) replaces what FotMob normally provides."
    )
    dcol1, dcol2 = st.columns(2)
    min_delay = dcol1.number_input("Min delay between matches (seconds)", min_value=0, value=7,
                                    key="hbulk_min_delay")
    max_delay = dcol2.number_input("Max delay between matches (seconds)", min_value=0, value=25,
                                    key="hbulk_max_delay")
    if max_delay < min_delay:
        st.warning("Max delay < min delay — using min for both.")
        max_delay = min_delay

    if st.button("Run batch", type="primary", key="hbulk_run"):
        to_run = []
        for i in range(len(matches)):
            if not st.session_state.get(_row_key("include", i), False):
                continue
            ss_url = st.session_state.get(_row_key("ss_url", i), "").strip()
            ws_url = st.session_state.get(_row_key("ws_url", i), matches[i]["match_url"]).strip()
            match_date = st.session_state.get(_row_key("date", i), default_date)
            to_run.append({"ws_url": ws_url, "ss_url": ss_url, "match_date": match_date,
                           "row_index": i})

        if not to_run:
            st.error("No matches checked to run.")
        else:
            results = []
            progress = st.progress(0.0)
            status_area = st.empty()
            for idx, entry in enumerate(to_run):
                n = len(to_run)
                if not entry["ss_url"]:
                    status_area.write(f"Match {idx + 1}/{n}: skipped — no SofaScore URL.")
                    results.append({"ws_url": entry["ws_url"], "success": False,
                                     "error": "No SofaScore URL set — skipped."})
                else:
                    try:
                        def _cb(msg, idx=idx, n=n):
                            status_area.write(f"Match {idx + 1}/{n}: {msg}")

                        report = batch_lib.run_combined_report_sofascore(
                            entry["ws_url"], entry["ss_url"], status_cb=_cb
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
                st.write(f"WhoScored: {report['n_ws_events']} events  |  "
                         f"SofaScore: {report['n_fm_shots']} shots")
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
                if report["team_name_mismatch"]:
                    st.warning(
                        "Team names from WhoScored and SofaScore don't match — "
                        "double check URLs before saving."
                    )
                if report.get("plus_minus_warning"):
                    st.info(report["plus_minus_warning"])
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
            fm_totals_df = report.get("fm_totals_df")
            if fm_totals_df is None or fm_totals_df.empty or "Goals" not in fm_totals_df.columns:
                st.warning(
                    f"Skipped {report['ws_home_name']} vs {report['ws_away_name']}: "
                    "SofaScore data came back empty — saving would overwrite existing data "
                    "with blanks. Re-scrape this match individually."
                )
                continue
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
