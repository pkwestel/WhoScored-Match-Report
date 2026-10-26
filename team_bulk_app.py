"""
team_bulk_app.py
==================
Bulk-scrape a whole team's season of matches from a WhoScored team
fixtures page. Same scraping and database pipeline as batch_run_app.py,
but designed for loading an entire team at once rather than a single
weekend's worth of league matches.

Local use:
    pip install streamlit pandas matplotlib openpyxl
    streamlit run team_bulk_app.py

WORKFLOW
--------
1. Paste a WhoScored team fixtures URL (e.g.
   https://www.whoscored.com/teams/32/fixtures/england-manchester-united)
   The app opens Chrome, automatically navigates back through every
   month, and collects every finished match it finds.

2. Review the match list - each row shows the WhoScored URL, date (from
   WhoScored), and a FotMob URL field for you to paste in. Checkboxes
   default to unchecked so you pick which matches to run. An "Auto-match
   FotMob" button attempts to find each match's FotMob page automatically.

3. Check for already-saved matches in the database so you don't
   re-scrape what you already have.

4. Run the batch - same Chrome-based scraping as the combined report app.

5. Review results and save to the database.

Same caveats as batch_run_app.py: this uses real, non-headless Chrome,
the fixture auto-detection is built against WhoScored's current page
structure (CSS-module class prefixes), and every step has a manual
fallback.
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
import fotmob_report as fr
import batch_lib

st.set_page_config(page_title="Team Bulk Runner", layout="wide")
st.title("Team Bulk Runner")
st.caption(
    "Bulk-scrape a team's full season from their WhoScored fixtures page. "
    "Paste the team's fixtures URL, it'll scan through every month automatically, "
    "paste in FotMob URLs, and run + save them all."
)

_ROW_KEY_PREFIX = "tbulk_row_"


def _scan_id():
    """Return the current scan's unique ID, used to namespace all per-row
    widget keys so a new scan can never collide with stale widget state
    from a previous scan."""
    return st.session_state.get("tbulk_scan_id", "0")


def _row_key(name, i):
    """Build a widget key that's unique to this scan + row index."""
    return f"{_ROW_KEY_PREFIX}{_scan_id()}_{name}_{i}"


def _clear_row_widget_state():
    """Drop any leftover per-row widget state from a previous match list
    and bump the scan ID so the next set of widgets gets fresh keys."""
    for key in list(st.session_state.keys()):
        if key.startswith(_ROW_KEY_PREFIX):
            del st.session_state[key]
    # Bump the scan ID so new widgets can't collide with any lingering
    # Streamlit internal state from the old scan's widget keys
    import uuid
    st.session_state["tbulk_scan_id"] = uuid.uuid4().hex[:8]


# ---------------------------------------------------------------------------
# Step 1: build the match list from the team's fixtures page
# ---------------------------------------------------------------------------
st.header("Step 1: Load team fixtures from WhoScored")

team_fixtures_url = st.text_input(
    "WhoScored team fixtures URL",
    placeholder="https://www.whoscored.com/teams/32/fixtures/england-manchester-united",
    help="The team's own Fixtures page on WhoScored. This uses the same scraper as the "
         "batch app's league fixtures - same page structure, same caveats.",
)

only_finished = st.checkbox("Only finished matches", value=True)

manual_urls_text = st.text_area(
    "Paste additional WhoScored match URLs (one per line)",
    height=80,
    help="Optional - add any match URLs not picked up by the auto-scan.",
)

if st.button("Scan fixtures", type="primary"):
    all_matches = []

    if team_fixtures_url.strip():
        try:
            fixtures_status = st.empty()
            fixtures_status.write("Scanning team fixtures page...")
            all_matches = wr.get_team_fixture_urls(
                team_fixtures_url.strip(), only_finished=only_finished,
                status_cb=fixtures_status.write,
            )
            st.success(f"Found {len(all_matches)} match(es) total.")
        except Exception as e:
            st.warning(f"Fixture scan failed ({e}) - use the manual paste box below.")
            with st.expander("Full error"):
                st.code(traceback.format_exc())

    # Add manually pasted URLs
    seen = {m["match_url"] for m in all_matches}
    for line in manual_urls_text.splitlines():
        url = line.strip()
        if url and url not in seen:
            all_matches.append({
                "match_url": url, "home_name": None, "away_name": None,
                "status": "(manual)", "date": None,
            })
            seen.add(url)

    # Sort by date (oldest first) so the list is chronological
    def _sort_key(m):
        if m.get("date"):
            try:
                return _dateparser.parse(m["date"]).date()
            except Exception:
                pass
        return datetime.date.max
    all_matches.sort(key=_sort_key)

    _clear_row_widget_state()
    st.session_state["tbulk_matches"] = all_matches
    st.session_state.pop("tbulk_run_results", None)

    if not all_matches:
        st.error("No matches found - check the URL, or paste match URLs manually.")


# ---------------------------------------------------------------------------
# Step 2: review + FotMob matching
# ---------------------------------------------------------------------------
matches = st.session_state.get("tbulk_matches", [])
if matches:
    st.header("Step 2: Review matches and add FotMob URLs")

    default_date = st.date_input(
        "Fallback date (for manually pasted URLs with no date)",
        value=datetime.date.today(),
        help="Only used for manually pasted URLs that have no date from WhoScored.",
        key="tbulk_default_date",
    )

    if st.button("Auto-match all to FotMob"):
        matched = 0
        for i, m in enumerate(matches):
            if m.get("home_name") and m.get("away_name"):
                # Use the WhoScored date for this match if available
                match_date = default_date
                if m.get("date"):
                    try:
                        match_date = _dateparser.parse(m["date"]).date()
                    except Exception:
                        pass
                try:
                    found = fr.find_fotmob_match_url(
                        match_date.isoformat(), m["home_name"], m["away_name"]
                    )
                    matches[i]["fm_url"] = found or ""
                    if found:
                        matched += 1
                    else:
                        st.warning(f"No FotMob match for {m['home_name']} vs {m['away_name']} "
                                   f"on {match_date.isoformat()}")
                except Exception as e:
                    matches[i]["fm_url"] = ""
                    st.warning(f"FotMob auto-match failed for {m['home_name']} vs {m['away_name']}: {e}")
        st.session_state["tbulk_matches"] = matches
        st.success(f"Auto-matched {matched} of {len(matches)} match(es) to FotMob.")

    # -----------------------------------------------------------------------
    # Anti-duplicate check
    # -----------------------------------------------------------------------
    db_url_for_check = st.text_input(
        "Database URL (for duplicate check + saving)",
        value=st.session_state.get("tbulk_db_url",
                                    os.environ.get("DATABASE_URL", "sqlite:///history.db")),
        key="tbulk_db_url",
    )

    if st.button("Check for already-saved matches"):
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
                    # Pre-fill FotMob URL from database if auto-match didn't
                    # find one (or wasn't run) — saves having to hunt for it
                    # again when re-running matches after algorithm changes.
                    # Must also update the widget's session_state key so
                    # Streamlit's text_input actually shows the value (once a
                    # widget key exists in session_state, Streamlit ignores
                    # the `value=` parameter).
                    if isinstance(already, dict):
                        saved_fm = already.get("fm_url", "")
                        if saved_fm and not m.get("fm_url"):
                            matches[i]["fm_url"] = saved_fm
                            st.session_state[_row_key("fm_url", i)] = saved_fm
        st.session_state["tbulk_matches"] = matches
        st.success(f"{n_found} of {len(matches)} match(es) already in the database.")

    # -----------------------------------------------------------------------
    # Match list display
    # -----------------------------------------------------------------------
    st.write(f"**{len(matches)} match(es)** — check the ones you want to run:")

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

            # Parse the WhoScored date if available
            row_default_date = default_date
            if m.get("date"):
                try:
                    row_default_date = _dateparser.parse(m["date"]).date()
                except Exception:
                    pass

            # Default to unchecked, but also uncheck if already saved
            default_checked = False

            cols = st.columns([0.6, 2.2, 2.2, 1.4])
            cols[0].checkbox("Run", value=default_checked, key=_row_key("include", i))
            cols[1].text_input("WhoScored URL", value=m["match_url"], key=_row_key("ws_url", i))
            cols[2].text_input("FotMob URL", value=m.get("fm_url") or "", key=_row_key("fm_url", i))
            cols[3].date_input("Match date", value=row_default_date, key=_row_key("date", i))

    # ---------------------------------------------------------------------------
    # Step 3: run the batch
    # ---------------------------------------------------------------------------
    st.header("Step 3: Run the batch")
    st.caption(
        "Each match runs the same real, non-headless Chrome flow as the combined report app."
    )
    dcol1, dcol2 = st.columns(2)
    min_delay = dcol1.number_input("Min delay between matches (seconds)", min_value=0, value=7,
                                    key="tbulk_min_delay")
    max_delay = dcol2.number_input("Max delay between matches (seconds)", min_value=0, value=25,
                                    key="tbulk_max_delay")
    if max_delay < min_delay:
        st.warning("Max delay < min delay - using min for both.")
        max_delay = min_delay

    if st.button("Run batch", type="primary", key="tbulk_run"):
        to_run = []
        for i in range(len(matches)):
            if not st.session_state.get(_row_key("include", i), False):
                continue
            fm_url = st.session_state.get(_row_key("fm_url", i), "").strip()
            ws_url = st.session_state.get(_row_key("ws_url", i), matches[i]["match_url"]).strip()
            match_date = st.session_state.get(_row_key("date", i), default_date)
            to_run.append({"ws_url": ws_url, "fm_url": fm_url, "match_date": match_date,
                           "row_index": i})

        if not to_run:
            st.error("No matches checked to run.")
        else:
            results = []
            progress = st.progress(0.0)
            status_area = st.empty()
            for idx, entry in enumerate(to_run):
                n = len(to_run)
                if not entry["fm_url"]:
                    status_area.write(f"Match {idx + 1}/{n}: skipped - no FotMob URL.")
                    results.append({"ws_url": entry["ws_url"], "success": False,
                                     "error": "No FotMob URL set - skipped."})
                else:
                    try:
                        def _cb(msg, idx=idx, n=n):
                            status_area.write(f"Match {idx + 1}/{n}: {msg}")

                        fm_out_dir = tempfile.mkdtemp(prefix="fotmob_team_bulk_")
                        report = batch_lib.run_combined_report(
                            entry["ws_url"], entry["fm_url"], fm_out_dir, status_cb=_cb
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

            st.session_state["tbulk_run_results"] = results

# ---------------------------------------------------------------------------
# Step 4 + 5: review results, then save to database
# ---------------------------------------------------------------------------
results = st.session_state.get("tbulk_run_results")
if results:
    st.header("Step 4: Review results")
    n_success = sum(r["success"] for r in results)
    st.write(f"{n_success}/{len(results)} match(es) scraped successfully.")

    for i, r in enumerate(results):
        if r["success"]:
            report = r["report"]
            ws_date_str = report.get("ws_match_date") or "(no date)"
            with st.expander(
                f"✅ {report['ws_home_name']} vs {report['ws_away_name']}  —  {ws_date_str}"
            ):
                st.write(f"**Requested URL:** `{r['ws_url']}`")
                st.write(f"**WhoScored returned:** {report['ws_home_name']} vs "
                         f"{report['ws_away_name']} ({ws_date_str})")
                st.write(f"WhoScored: {report['n_ws_events']} events  |  "
                         f"FotMob: {report['n_fm_shots']} shots")
                # Date mismatch warning
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
                        "Team names from WhoScored and FotMob don't match - "
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
                    key=f"tbulk_dl_{i}",
                )
        else:
            with st.expander(f"❌ {r['ws_url']}", expanded=True):
                st.error(r["error"])
                if r.get("traceback"):
                    st.code(r["traceback"])

    st.header("Step 5: Save to database")
    db_url = st.text_input(
        "Database URL",
        value=st.session_state.get("tbulk_db_url",
                                    os.environ.get("DATABASE_URL", "sqlite:///history.db")),
        help="Reuses whatever you entered in Step 2.",
        key="tbulk_db_url_save",
    )
    competition = st.selectbox("Competition",
                                ["Premier League", "UEFA Champions League"],
                                index=0, key="tbulk_competition")

    if st.button("Save all successful matches to the database", type="primary",
                  key="tbulk_save"):
        any_saved = False
        for r in results:
            if not r["success"]:
                continue
            report = r["report"]
            # Safety check: skip matches with empty FotMob data
            fm_totals_df = report.get("fm_totals_df")
            if fm_totals_df is None or fm_totals_df.empty or "Goals" not in fm_totals_df.columns:
                st.warning(
                    f"Skipped {report['ws_home_name']} vs {report['ws_away_name']}: "
                    "FotMob data came back empty - saving would overwrite existing data "
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
            st.info("Nothing saved - no successful matches, or all saves failed.")
