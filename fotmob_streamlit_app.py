"""
Streamlit UI for fotmob_report.py.

Local use:
    Drop this file into the same folder as fotmob_report.py (and
    utils/driver.py - the same one whoscored_report.py already uses),
    then:
        pip install streamlit
        streamlit run fotmob_streamlit_app.py
    This opens a page in your browser at http://localhost:8501 - paste in
    a FotMob match URL, click the button, and download the workbook.

    IMPORTANT: fotmob_report.py calls get_driver(track_network=True),
    which runs a REAL, VISIBLE (non-headless) Chrome window - not
    headless - because that's required to capture the network response
    FotMob's own page JS receives (see fotmob_report.py's module
    docstring for why). So while this Streamlit page is "loading", expect
    an actual separate Chrome window to pop up on your screen for a few
    seconds - that's normal, not a bug.

    NOTE: if you edit fotmob_report.py while Streamlit is already
    running, clicking "Rerun" in the browser is NOT enough to pick up the
    change - Python keeps the already-imported module cached in memory.
    Stop the server (Ctrl+C in the terminal) and run `streamlit run
    fotmob_streamlit_app.py` again to force a fresh import.

HOSTING CAVEAT (stronger than the WhoScored app's caveat)
------------------------------------------------------------
Streamlit Community Cloud (and most cloud hosts) run headless containers
with no real display attached at all. Since this scraper specifically
requires a non-headless browser session (not just Chrome-without-a-GUI,
which headless mode already provides), it very likely CANNOT run on a
typical cloud host without extra work (e.g. an Xvfb virtual display) -
this is meaningfully harder than the WhoScored app's hosting story, not
just the same "install a real Chromium" caveat. Local use is the
straightforward path.
"""

import io
import os
import tempfile
import traceback
import contextlib

import streamlit as st

import fotmob_report as fr
from app_logo import render_logo_top_left

st.set_page_config(page_title="FotMob Match Report", layout="wide")
render_logo_top_left()
st.title("FotMob Match Report Generator")
st.write(
    "Paste a FotMob match URL below to generate a Shots/xG report - Totals, Shots, and a "
    "Shot Breakdown (by player, situation, and body part)."
)
st.caption(
    "A real Chrome window will briefly pop up while this runs (not headless) - that's "
    "required to capture the match data correctly, see this file's module docstring."
)

url = st.text_input(
    "FotMob match URL",
    placeholder="https://www.fotmob.com/matches/<slug>/<code>#<matchId>",
)

if st.button("Generate Report", type="primary"):
    if not url.strip():
        st.error("Please paste a match URL first.")
    else:
        debug_buf = io.StringIO()
        try:
            with st.spinner("Opening the match page (a real Chrome window will pop up, ~10-25s)..."):
                out_dir = tempfile.mkdtemp(prefix="fotmob_")
                with contextlib.redirect_stdout(debug_buf):
                    match_json, match_id = fr.scrape_match(url.strip(), out_dir=out_dir)

            home_name, away_name = fr.extract_team_names(match_json)
            st.success(f"Match {match_id}: {home_name} vs {away_name}")

            with st.spinner("Computing shots..."):
                shots_df = fr.compute_shots(match_json)
            if shots_df.empty:
                hint = fr.extract_match_status_hint(match_json)
                st.warning(
                    "No shots found"
                    + (f" - likely reason: {hint}" if hint else " - check the debug log below.")
                )
            else:
                st.info(f"Found {len(shots_df)} shots.")

            with st.spinner("Computing totals..."):
                totals_df = fr.compute_totals(match_json, shots_df, home_name, away_name)

            with st.spinner("Computing shot breakdowns..."):
                player_xa = fr.extract_player_xa(match_json)
                player_minutes = fr.extract_player_minutes(match_json)
                player_sprints = fr.extract_player_sprints(match_json)
                player_line_breaking_passes = fr.extract_player_line_breaking_passes(match_json)
                shot_breakdowns = fr.compute_shot_breakdowns(
                    shots_df, player_xa, player_minutes, player_sprints, player_line_breaking_passes)

            with st.spinner("Computing xG breakdown..."):
                xg_breakdown = fr.compute_xg_breakdown(shots_df, home_name, away_name)

            with st.spinner("Computing plus/minus..."):
                player_windows = fr.extract_player_windows(match_json, player_minutes, shots_df)
                plus_minus = fr.compute_plus_minus(shots_df, player_windows, home_name, away_name)
                if player_windows.empty:
                    st.warning(
                        "Plus Minus is empty - FotMob didn't return any lineup/substitution data for "
                        "this match (no starters/subs list at all), which happens most often for a "
                        "match FotMob hasn't fully finished processing yet. Try again later, or check "
                        "the 'lineup' section of the saved fotmob_raw_*.json debug file."
                    )

            wb = fr.build_workbook(shots_df, totals_df, home_name, away_name, match_id,
                                    shot_breakdowns, xg_breakdown, plus_minus)
            buf = io.BytesIO()
            wb.save(buf)
            buf.seek(0)

            filename = f"{fr.sanitize_filename(home_name)}_vs_{fr.sanitize_filename(away_name)}_fotmob.xlsx"
            st.download_button(
                label=f"Download {filename}",
                data=buf,
                file_name=filename,
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )

            tab0, tab1, tab2, tab3 = st.tabs(["Totals", "Shots", "Shot Breakdown", "Plus Minus"])
            with tab0:
                st.dataframe(totals_df, use_container_width=True)
                st.subheader("xG Breakdown (by half, by phase)")
                st.dataframe(xg_breakdown, use_container_width=True)
            with tab1:
                st.dataframe(shots_df, use_container_width=True)
            with tab2:
                for label, df in shot_breakdowns.items():
                    st.subheader(label)
                    breakdown_teams = ([t for t in [home_name, away_name] if t is not None]
                                        or sorted(df["Team"].dropna().unique()))
                    for t in breakdown_teams:
                        st.caption(t)
                        st.dataframe(
                            df[df["Team"] == t].drop(columns=["Team"]).reset_index(drop=True),
                            use_container_width=True,
                        )
            with tab3:
                st.write(
                    "Goals/Shots/xG For and Against, totaled for exactly the minutes each player was "
                    "on the pitch. Penalties are excluded, and xG combines same-minute shots by the "
                    "same team into one probability rather than summing them - see the Notes tab in "
                    "the downloaded workbook for the full methodology."
                )
                pm_teams = ([] if plus_minus.empty else
                            ([t for t in [home_name, away_name] if t is not None]
                             or sorted(plus_minus["Team"].dropna().unique())))
                for t in pm_teams:
                    st.subheader(t)
                    st.dataframe(
                        plus_minus[plus_minus["Team"] == t].drop(columns=["Team"]).reset_index(drop=True),
                        use_container_width=True,
                    )

            raw_json_path = os.path.join(out_dir, f"fotmob_raw_{match_id}.json")
            if os.path.exists(raw_json_path):
                with open(raw_json_path, "rb") as f:
                    st.download_button(
                        label="Download raw matchDetails JSON (debugging)",
                        data=f.read(),
                        file_name=f"fotmob_raw_{match_id}.json",
                        mime="application/json",
                    )

            with st.expander("Debug log (scrape_match console output)"):
                st.code(debug_buf.getvalue() or "(nothing captured)")

        except Exception as e:
            st.error(f"Something went wrong: {e}")
            with st.expander("Debug log (scrape_match console output)"):
                st.code(debug_buf.getvalue() or "(nothing captured)")
            st.code(traceback.format_exc())

# ---------------------------------------------------------------------------
# packages.txt (create this as a SEPARATE file, same folder, if attempting a
# cloud deploy despite the HOSTING CAVEAT above):
#
#   chromium
#   chromium-driver
#   xvfb
#
# You would ALSO need to wrap the driver launch in a virtual display (e.g.
# the `pyvirtualdisplay` package's Display() context manager) since
# track_network=True explicitly avoids `--headless`. This is meaningfully
# more setup than the WhoScored app needed - budget real time for it, or
# just run this one locally.
# ---------------------------------------------------------------------------
