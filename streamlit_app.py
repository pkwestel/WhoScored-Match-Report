"""
Streamlit UI for whoscored_report.py.

Local use:
    Drop this file into the same folder as whoscored_report.py (the root
    of your cloned football-data-webscraping repo), then:
        pip install streamlit
        streamlit run streamlit_app.py
    This opens a page in your browser at http://localhost:8501 - paste in
    a WhoScored match URL, click the button, and download the workbook.

    NOTE: if you edit whoscored_report.py while Streamlit is already
    running, clicking "Rerun" in the browser is NOT enough to pick up the
    change - Python keeps the already-imported whoscored_report module
    cached in memory. Stop the server (Ctrl+C in the terminal) and run
    `streamlit run streamlit_app.py` again to force a fresh import.

Hosting it so it's reachable from any device (Streamlit Community Cloud):
    1. Push this repo to GitHub (needs to include whoscored_report.py,
       streamlit_app.py, the whoscored/ and utils/ folders, requirements.txt).
    2. Add a `packages.txt` file (see note at the bottom of this file) so
       the cloud container installs a real Chromium browser - it doesn't
       have one by default.
    3. Go to share.streamlit.io, sign in with GitHub, and deploy this repo.

    IMPORTANT CAVEAT: WhoScored (like many sites) may block requests coming
    from known data-center IP ranges (which is what cloud hosts use), even
    though scraping works fine from your home internet connection. If the
    hosted version can't scrape at all, that's most likely why - there's no
    code fix for that, only workarounds like a paid proxy/residential IP
    service, which is a bigger step up in cost and complexity.
"""

import io
import traceback

import streamlit as st

import whoscored_report as wr

st.set_page_config(page_title="WhoScored Match Report", layout="wide")
st.title("WhoScored Match Report Generator")
st.write(
    "Paste a WhoScored match-centre URL below to generate Totals, Touches, "
    "Passing, Shot Creating Actions, and Progressive Passes tables."
)

url = st.text_input(
    "WhoScored match URL",
    placeholder="https://www.whoscored.com/matches/1903410/live/...",
)

if st.button("Generate Report", type="primary"):
    if not url.strip():
        st.error("Please paste a match URL first.")
    else:
        try:
            with st.spinner("Opening the match page (this drives a real headless browser, ~10-20s)..."):
                df, match_info = wr.scrape_match(url.strip())
            home_name = match_info.get("home_name")
            away_name = match_info.get("away_name")
            st.success(f"Scraped {len(df)} events — {home_name} vs {away_name}")

            with st.spinner("Computing progressive passes..."):
                pp_out, player_totals, team_totals, progressive_received = wr.compute_progressive_passes(df)
            with st.spinner("Computing passes received..."):
                passes_received = wr.compute_passes_received(df)
            with st.spinner("Computing carries..."):
                team_carries, player_carries = wr.compute_carries(df)
            with st.spinner("Computing shot-creating actions..."):
                sca_out = wr.compute_sca(df)
            with st.spinner("Computing touches..."):
                team_summary, player_third = wr.compute_touches(df, team_carries, player_carries,
                                                                  passes_received, progressive_received)
            with st.spinner("Computing passing..."):
                passing_out = wr.compute_passing(df, player_totals, sca_out)
            with st.spinner("Computing possession sequences..."):
                chains_df, team_sequences = wr.compute_sequences(df)
            with st.spinner("Computing field tilt and PPDA..."):
                field_tilt = wr.compute_field_tilt(team_summary)
                ppda = wr.compute_ppda(df)
            with st.spinner("Computing defensive stats..."):
                defensive_stats = wr.compute_defensive_stats(df)
                defensive_actions = wr.compute_defensive_actions(df)
            with st.spinner("Computing corners..."):
                corners = wr.compute_corners(df)
            with st.spinner("Computing totals..."):
                totals_out = wr.compute_totals(team_summary, team_totals, passing_out, sca_out,
                                                chains_df, team_sequences, field_tilt, ppda,
                                                defensive_stats, corners, home_name, away_name)

            wb = wr.build_workbook(
                pp_out, player_totals, team_totals, sca_out,
                team_summary, player_third, passing_out, totals_out, defensive_actions,
                home_name, away_name,
            )
            buf = io.BytesIO()
            wb.save(buf)
            buf.seek(0)

            filename = f"{wr.sanitize_filename(home_name)}_vs_{wr.sanitize_filename(away_name)}.xlsx"
            st.download_button(
                label=f"Download {filename}",
                data=buf,
                file_name=filename,
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )

            tab0, tab1, tab2, tab3, tab4, tab5 = st.tabs(
                ["Totals", "Touches", "Passing", "Shot Creating Actions",
                 "Defensive Actions", "Progressive Passes"]
            )
            with tab0:
                st.dataframe(totals_out, use_container_width=True)
            with tab1:
                st.dataframe(player_third, use_container_width=True)
            with tab2:
                st.dataframe(passing_out, use_container_width=True)
            with tab3:
                st.dataframe(sca_out, use_container_width=True)
            with tab4:
                st.dataframe(defensive_actions, use_container_width=True)
            with tab5:
                st.subheader("Progressive Passes by Team")
                st.dataframe(team_totals, use_container_width=True)
                st.subheader("Progressive Passes by Player")
                st.dataframe(player_totals, use_container_width=True)
                st.subheader("All Progressive Passes")
                st.dataframe(pp_out, use_container_width=True)

        except Exception as e:
            st.error(f"Something went wrong: {e}")
            st.code(traceback.format_exc())

# ---------------------------------------------------------------------------
# packages.txt (create this as a SEPARATE file, same folder, if deploying to
# Streamlit Community Cloud - it is NOT Python code, just plain text):
#
#   chromium
#   chromium-driver
#
# You'll likely also need to point Selenium at the system chromium binary
# instead of letting webdriver-manager download its own, since the cloud
# container's Chromium version may not match what webdriver-manager fetches.
# In utils/driver.py, that means adding something like:
#   options.binary_location = "/usr/bin/chromium"
#   service = Service("/usr/bin/chromedriver")
# in place of the ChromeDriverManager().install() call. This is the part
# most likely to need troubleshooting once you actually attempt a deploy,
# since it depends on exact versions Streamlit Cloud's container ships with.
# ---------------------------------------------------------------------------
