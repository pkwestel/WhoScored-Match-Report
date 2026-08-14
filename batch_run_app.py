"""
batch_run_app.py
==================
Run a whole weekend's worth of Premier League matches in one sitting,
instead of pasting each match's WhoScored + FotMob URLs into
combined_streamlit_app.py one at a time.

Local use:
    pip install streamlit pandas matplotlib openpyxl
    streamlit run batch_run_app.py

HOW IT WORKS (five steps, top to bottom on the page)
-----------------------------------------------------
1. Build a match list - either auto-detected from a WhoScored fixtures page
   (wr.get_fixture_urls()) or pasted in by hand (or both, combined).
2. Review each match, let it try to auto-find the matching FotMob URL
   (fr.find_fotmob_match_url()), and fix up anything wrong by hand -
   nothing here is locked to the auto-detected guess.
3. Set a random min/max delay range and click "Run batch" - it scrapes each
   match one after another (same real, non-headless Chrome requirement as
   the single-match app), sleeping a random number of seconds between
   matches so requests aren't sent in one tight burst.
4. Review every match's results (scores, warnings, a per-match workbook
   download) before anything touches the database.
5. Click "Save all successful matches to the database" once you're happy
   with what came back.

WHY THIS IS DIFFERENT FROM "FULL AUTOMATION" (see project chat history for
the earlier feasibility discussion)
-----------------------------------------------------------------------------
This still requires you to be at your computer, click "Run batch" yourself,
and watch real (non-headless) Chrome windows open and close - nothing runs
on a schedule while you're away. That keeps the traffic pattern close to
what manual, one-match-at-a-time scraping already looks like, just faster
to kick off - the random delay between matches is meant to avoid a single
tight burst, not to disguise this as organic browsing across days the way
spreading matches out over a whole weekend naturally would. Running all
9-10 matches back-to-back in one sitting is still a real behavior change
from that - there's no confirmed evidence this crosses any actual rate
threshold, same uncertainty as the earlier discussion, just worth knowing.

HONESTY NOTE ON THE TWO NEW SCRAPERS THIS APP RELIES ON
---------------------------------------------------------
whoscored_report.get_fixture_urls() and fotmob_report.find_fotmob_match_url()
were both written without the ability to open a real browser and inspect
the live pages they target (unlike every other scraper in this project,
which was built and debugged against real captured data). They follow the
same resilient patterns already proven elsewhere (search embedded JSON by
key name, not by one hard-coded path), but treat their first few real runs
as a debugging session, not a guarantee - see each function's own docstring
for exactly what's guessed vs confirmed. Every step in this app has a
manual fallback specifically because of this.
"""

import os
import random
import tempfile
import time
import traceback
import datetime

import streamlit as st

import whoscored_report as wr
import fotmob_report as fr
import batch_lib

st.set_page_config(page_title="Batch Match Runner", layout="wide")
st.title("Batch Match Runner")
st.caption(
    "Run several matches in one sitting instead of pasting URLs into the combined report app "
    "one at a time. Read the module docstring in batch_run_app.py for the full explanation - "
    "in short: the fixture auto-detection below is unverified against WhoScored/FotMob's real "
    "pages, so every step has a manual override if auto-detect gets something wrong."
)

_ROW_KEY_PREFIXES = ("batch_include_", "batch_ws_url_", "batch_fm_url_", "batch_date_")


def _clear_row_widget_state():
    """Drop any leftover per-row widget state from a previous match list, so
    rebuilding the list doesn't leave stale keys from a longer old list."""
    for key in list(st.session_state.keys()):
        if key.startswith(_ROW_KEY_PREFIXES):
            del st.session_state[key]


# ---------------------------------------------------------------------------
# Step 1: build the match list
# ---------------------------------------------------------------------------
st.header("Step 1: Build this weekend's match list")

fixtures_url = st.text_input(
    "WhoScored fixtures/results page URL",
    placeholder="https://www.whoscored.com/Regions/252/Tournaments/2/Seasons/.../Fixtures",
    help="The Premier League's own Fixtures/Results page on WhoScored. Auto-detection here is "
         "unverified (see this app's module docstring) - if it fails, just use the manual paste "
         "box below instead.",
)
only_finished = st.checkbox("Only keep matches that look finished", value=True)
manual_urls_text = st.text_area(
    "Paste additional (or alternate) WhoScored match URLs, one per line",
    height=100,
    help="Works with or without the fixtures URL above - use this alone if auto-detect doesn't "
         "work for you.",
)

if st.button("Build match list", type="primary"):
    matches = []
    if fixtures_url.strip():
        try:
            matches = wr.get_fixture_urls(fixtures_url.strip(), only_finished=only_finished)
            st.success(f"Auto-detected {len(matches)} match(es) from the fixtures page.")
        except Exception as e:
            st.warning(f"Auto-detect didn't work ({e}) - continuing with manually pasted URLs only.")
            with st.expander("Full error (share this if you want it fixed)"):
                st.code(traceback.format_exc())

    seen = {m["match_url"] for m in matches}
    for line in manual_urls_text.splitlines():
        url = line.strip()
        if url and url not in seen:
            matches.append({"match_url": url, "home_name": None, "away_name": None, "status": "(manual)"})
            seen.add(url)

    _clear_row_widget_state()
    st.session_state["batch_matches"] = matches
    st.session_state.pop("batch_run_results", None)

    if not matches:
        st.error("No matches to work with - check the fixtures URL, or paste at least one match URL.")

# ---------------------------------------------------------------------------
# Step 2: review + FotMob matching
# ---------------------------------------------------------------------------
matches = st.session_state.get("batch_matches", [])
if matches:
    st.header("Step 2: Review matches and match each to FotMob")
    default_date = st.date_input(
        "Default match date",
        value=datetime.date.today(),
        help="Used to look up each match's FotMob page, and as the default 'Match date' saved "
             "to the database. Override per row below for matches played on a different day "
             "(e.g. Saturday vs Sunday fixtures in the same batch).",
    )

    if st.button("Auto-match each to FotMob"):
        for i, m in enumerate(matches):
            if m.get("home_name") and m.get("away_name"):
                try:
                    found = fr.find_fotmob_match_url(default_date.isoformat(), m["home_name"], m["away_name"])
                    matches[i]["fm_url"] = found or ""
                    if not found:
                        st.warning(f"No FotMob match found for {m['home_name']} vs {m['away_name']} on "
                                   f"{default_date.isoformat()} - paste its FotMob URL manually below.")
                except Exception as e:
                    matches[i]["fm_url"] = ""
                    st.warning(f"FotMob auto-match failed for {m['home_name']} vs {m['away_name']}: {e}")
            else:
                st.info(f"Skipping auto-match for {m['match_url']} (manually pasted, no team names known "
                        "yet) - paste its FotMob URL directly below instead.")
        st.session_state["batch_matches"] = matches

    st.write(f"{len(matches)} match(es) in this batch - uncheck any you don't want to run, and fix up "
             "URLs/dates as needed:")

    for i, m in enumerate(matches):
        with st.container(border=True):
            label = f"{m.get('home_name') or '?'} vs {m.get('away_name') or '?'}"
            if m.get("status"):
                label += f"  ({m['status']})"
            st.write(f"**{label}**")
            cols = st.columns([0.6, 2.2, 2.2, 1.4])
            cols[0].checkbox("Run", value=True, key=f"batch_include_{i}")
            cols[1].text_input("WhoScored URL", value=m["match_url"], key=f"batch_ws_url_{i}")
            cols[2].text_input("FotMob URL", value=m.get("fm_url") or "", key=f"batch_fm_url_{i}")
            cols[3].date_input("Match date", value=default_date, key=f"batch_date_{i}")

    # ---------------------------------------------------------------------------
    # Step 3: run the batch
    # ---------------------------------------------------------------------------
    st.header("Step 3: Run the batch")
    st.caption(
        "Each match runs the same real, non-headless Chrome flow as the combined report app - "
        "expect a browser window to pop up for every match's FotMob half."
    )
    dcol1, dcol2 = st.columns(2)
    min_delay = dcol1.number_input("Minimum delay between matches (seconds)", min_value=0, value=15)
    max_delay = dcol2.number_input("Maximum delay between matches (seconds)", min_value=0, value=45)
    if max_delay < min_delay:
        st.warning("Maximum delay is less than minimum delay - using minimum delay for both.")
        max_delay = min_delay

    if st.button("Run batch", type="primary"):
        to_run = []
        for i in range(len(matches)):
            if not st.session_state.get(f"batch_include_{i}", True):
                continue
            fm_url = st.session_state.get(f"batch_fm_url_{i}", "").strip()
            ws_url = st.session_state.get(f"batch_ws_url_{i}", matches[i]["match_url"]).strip()
            match_date = st.session_state.get(f"batch_date_{i}", default_date)
            to_run.append({"ws_url": ws_url, "fm_url": fm_url, "match_date": match_date})

        if not to_run:
            st.error("Nothing checked to run.")
        else:
            results = []
            progress = st.progress(0.0)
            status_area = st.empty()
            for idx, entry in enumerate(to_run):
                n = len(to_run)
                if not entry["fm_url"]:
                    status_area.write(f"Match {idx + 1}/{n}: skipped - no FotMob URL set.")
                    results.append({"ws_url": entry["ws_url"], "success": False,
                                     "error": "No FotMob URL set - skipped."})
                else:
                    try:
                        def _cb(msg, idx=idx, n=n):
                            status_area.write(f"Match {idx + 1}/{n}: {msg}")

                        fm_out_dir = tempfile.mkdtemp(prefix="fotmob_batch_")
                        report = batch_lib.run_combined_report(
                            entry["ws_url"], entry["fm_url"], fm_out_dir, status_cb=_cb
                        )
                        report["match_date"] = entry["match_date"]
                        results.append({"ws_url": entry["ws_url"], "success": True, "report": report})
                    except Exception as e:
                        status_area.write(f"Match {idx + 1}/{n}: FAILED ({e})")
                        results.append({"ws_url": entry["ws_url"], "success": False,
                                         "error": str(e), "traceback": traceback.format_exc()})

                progress.progress((idx + 1) / n)
                if idx < n - 1:
                    delay = random.uniform(min_delay, max_delay)
                    status_area.write(f"Waiting {delay:.0f}s before the next match...")
                    time.sleep(delay)

            st.session_state["batch_run_results"] = results

# ---------------------------------------------------------------------------
# Step 4 + 5: review results, then save to database
# ---------------------------------------------------------------------------
results = st.session_state.get("batch_run_results")
if results:
    st.header("Step 4: Review results")
    n_success = sum(r["success"] for r in results)
    st.write(f"{n_success}/{len(results)} match(es) scraped successfully.")

    for i, r in enumerate(results):
        if r["success"]:
            report = r["report"]
            with st.expander(f"✅ {report['ws_home_name']} vs {report['ws_away_name']}"):
                st.write(f"WhoScored: {report['n_ws_events']} events  |  FotMob: {report['n_fm_shots']} shots")
                if report["team_name_mismatch"]:
                    st.warning(
                        "The team names from WhoScored and FotMob don't look like the same match - "
                        "double check this row's URLs before trusting/saving it."
                    )
                if report.get("plus_minus_warning"):
                    st.info(report["plus_minus_warning"])
                st.dataframe(report["totals_out"], use_container_width=True)
                st.download_button(
                    label=f"Download {report['filename']}",
                    data=report["wb_bytes"],
                    file_name=report["filename"],
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    key=f"batch_dl_{i}",
                )
        else:
            with st.expander(f"❌ {r['ws_url']}", expanded=True):
                st.error(r["error"])
                if r.get("traceback"):
                    st.code(r["traceback"])

    st.header("Step 5: Save to database")
    default_db_url = os.environ.get("DATABASE_URL", "sqlite:///history.db")
    db_url = st.text_input(
        "Database URL", value=default_db_url,
        help="Same DATABASE_URL convention as combined_streamlit_app.py's own 'Save to Database'.",
    )
    competition = st.text_input("Competition", value="Premier League")

    if st.button("Save all successful matches to the database", type="primary"):
        any_saved = False
        for r in results:
            if not r["success"]:
                continue
            report = r["report"]
            match_date = report["match_date"]
            match_date_iso = match_date.isoformat() if hasattr(match_date, "isoformat") else str(match_date)
            try:
                match_id = batch_lib.save_report_to_db(db_url, report, competition, match_date_iso)
                st.success(
                    f"Saved {report['ws_home_name']} vs {report['ws_away_name']} "
                    f"(match_id={match_id}, {len(report['all_passes'])} passes)."
                )
                any_saved = True
            except Exception as e:
                st.error(f"Couldn't save {report['ws_home_name']} vs {report['ws_away_name']}: {e}")
                st.code(traceback.format_exc())
        if not any_saved:
            st.info("Nothing was saved - no successful matches in this run, or all saves failed.")
