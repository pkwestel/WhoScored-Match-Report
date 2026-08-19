"""
Streamlit UI for whoscored_report.py.

Local use:
    Drop this file into the same folder as whoscored_report.py (the root
    of your cloned football-data-webscraping repo), then:
        pip install streamlit matplotlib
        streamlit run streamlit_app.py
    This opens a page in your browser at http://localhost:8501 - paste in
    a WhoScored match URL, click the button, and download the workbook.
    (matplotlib is only needed for the in-app Pass Map tab below - the
    downloaded Excel workbook itself doesn't use it.)

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

import matplotlib.pyplot as plt
import streamlit as st

import whoscored_report as wr
from pitch_viz import plot_pass_map, PASS_MAP_FONT, PASS_CATEGORY_COLORS, TITLE_COLOR

st.set_page_config(page_title="WhoScored Match Report", layout="wide")
st.title("WhoScored Match Report Generator")
st.write(
    "Paste a WhoScored match-centre URL below to generate Totals, Touches, "
    "Passing, Shot Creating Actions, Progressive Passes, On/Off tables, and a "
    "per-player Pass Map."
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

            with st.spinner("Computing progressive passes..."):
                _, player_totals, team_totals, progressive_received = wr.compute_progressive_passes(df)
            with st.spinner("Computing passes received..."):
                passes_received = wr.compute_passes_received(df)
            with st.spinner("Computing passing pairs..."):
                passing_pairs = wr.compute_passing_pairs(df)
            with st.spinner("Computing carries..."):
                team_carries, player_carries = wr.compute_carries(df)
            with st.spinner("Computing shot-creating actions..."):
                sca_out = wr.compute_sca(df)
            with st.spinner("Computing shot pairs..."):
                shot_pairs = wr.compute_shot_pairs(sca_out)
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
                defensive_action_location = wr.compute_defensive_action_location(df)
            with st.spinner("Computing corners..."):
                corners = wr.compute_corners(df)
            with st.spinner("Computing totals..."):
                totals_out = wr.compute_totals(team_summary, team_totals, passing_out, sca_out,
                                                chains_df, team_sequences, field_tilt, ppda,
                                                defensive_stats, corners, home_name, away_name)
                against_totals = wr.compute_against_totals(totals_out)
            with st.spinner("Computing On/Off splits..."):
                player_windows = wr.extract_player_windows(df)
                on_off = wr.compute_on_off(df, player_windows)

            wb = wr.build_workbook(
                sca_out, team_summary, player_third, passing_out, totals_out, defensive_actions,
                defensive_action_location, passing_pairs, home_name, away_name, against_totals,
                shot_pairs, on_off,
            )
            buf = io.BytesIO()
            wb.save(buf)
            buf.seek(0)

            filename = f"{wr.sanitize_filename(home_name)}_vs_{wr.sanitize_filename(away_name)}.xlsx"

            # Stashed in session_state (rather than used directly below) so
            # the report - and the workbook download button - survive the
            # rerun that Streamlit triggers every time a widget changes,
            # e.g. picking a different player in the Pass Map tab.
            st.session_state["report"] = {
                "df": df,
                "home_name": home_name,
                "away_name": away_name,
                "totals_out": totals_out,
                "against_totals": against_totals,
                "player_third": player_third,
                "passing_out": passing_out,
                "sca_out": sca_out,
                "defensive_actions": defensive_actions,
                "defensive_action_location": defensive_action_location,
                "team_totals": team_totals,
                "player_totals": player_totals,
                "passing_pairs": passing_pairs,
                "shot_pairs": shot_pairs,
                "on_off": on_off,
                "wb_bytes": buf.getvalue(),
                "filename": filename,
                "n_events": len(df),
            }

        except Exception as e:
            st.error(f"Something went wrong: {e}")
            st.code(traceback.format_exc())

# Pass Map pitch drawing (draw_pitch/plot_pass_map) now lives in pitch_viz.py
# so dashboard_app.py can reuse the exact same drawing code - see that
# module for the full explanation and implementation.

# ---------------------------------------------------------------------------
# Render the report (from session_state, so it survives Pass Map reruns)
# ---------------------------------------------------------------------------
report = st.session_state.get("report")
if report:
    df = report["df"]
    home_name, away_name = report["home_name"], report["away_name"]
    totals_out = report["totals_out"]
    against_totals = report["against_totals"]
    player_third = report["player_third"]
    passing_out = report["passing_out"]
    sca_out = report["sca_out"]
    defensive_actions = report["defensive_actions"]
    defensive_action_location = report["defensive_action_location"]
    team_totals = report["team_totals"]
    player_totals = report["player_totals"]
    passing_pairs = report["passing_pairs"]
    shot_pairs = report["shot_pairs"]
    on_off = report["on_off"]

    st.success(f"Scraped {report['n_events']} events — {home_name} vs {away_name}")

    st.download_button(
        label=f"Download {report['filename']}",
        data=report["wb_bytes"],
        file_name=report["filename"],
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

    tab0, tabA, tab1, tab2, tab3, tab4, tab5, tab6, tab7, tabOO, tabP, tabPR = st.tabs(
        ["Totals", "Against", "Touches", "Passing", "Shot Creating Actions",
         "Defensive Actions", "Defensive Action Location", "Passing Pairs", "Shot Pairs",
         "On/Off", "Pass Map", "Passes Received"]
    )
    with tab0:
        st.dataframe(totals_out, use_container_width=True)
    with tabA:
        st.dataframe(against_totals, use_container_width=True)
    with tab1:
        st.dataframe(player_third, use_container_width=True)
    with tab2:
        st.dataframe(passing_out, use_container_width=True)
    with tab3:
        sca_teams = ([t for t in [home_name, away_name] if t is not None]
                     or sorted(sca_out["team"].unique()))
        for t in sca_teams:
            st.subheader(t)
            st.dataframe(
                sca_out[sca_out["team"] == t].drop(columns=["team"]).reset_index(drop=True),
                use_container_width=True,
            )
    with tab4:
        st.dataframe(defensive_actions, use_container_width=True)
    with tab5:
        st.dataframe(defensive_action_location, use_container_width=True)
    with tab6:
        st.write(
            "Every passer -> receiver combination (completed passes only), with a count of how many "
            "times it happened, split by team and sorted most-frequent first."
        )
        pairs_teams = ([t for t in [home_name, away_name] if t is not None]
                       or sorted(passing_pairs["team"].unique()))
        for t in pairs_teams:
            st.subheader(t)
            st.dataframe(
                passing_pairs[passing_pairs["team"] == t].drop(columns=["team"]).reset_index(drop=True),
                use_container_width=True,
            )
    with tab7:
        st.write(
            "Every passer -> shot-taker combination, with a count of how many times it happened, split "
            "by team and sorted most-frequent first. The passer is whoever played the pass immediately "
            "before the shot (SCA1) - shots preceded by a take-on, duel, rebound, or loose ball with no "
            "such pass aren't included here."
        )
        shot_pairs_teams = ([t for t in [home_name, away_name] if t is not None]
                             or sorted(shot_pairs["team"].unique()))
        for t in shot_pairs_teams:
            st.subheader(t)
            st.dataframe(
                shot_pairs[shot_pairs["team"] == t].drop(columns=["team"]).reset_index(drop=True),
                use_container_width=True,
            )
    with tabOO:
        st.write(
            "For every player, team totals - Shots, Total Touches, thirds, and Attacking Box "
            "touches - split into **For** (their own team) and **Against** (the opponent), counted "
            "only for the minutes that player was actually on the pitch. A substituted player isn't "
            "credited for the exact minute they left (that belongs to whoever replaced them). See "
            "the workbook's Notes tab for the full definition, including a known limitation around "
            "red/second-yellow cards."
        )
        on_off_teams = ([t for t in [home_name, away_name] if t is not None]
                        or sorted(on_off["Team"].unique()))
        for t in on_off_teams:
            st.subheader(t)
            st.dataframe(
                on_off[on_off["Team"] == t].drop(columns=["Team"]).reset_index(drop=True),
                use_container_width=True,
            )
    with tabP:
        st.write(
            "Every pass attempted by one player, plotted on the pitch and colored by outcome: "
            "**completed**, **incomplete**, **progressive**, or **key pass (shot assist)**. A "
            "completed pass that's both progressive and a key pass is shown as a key pass - the "
            "more specific category wins."
        )
        players = (df[["team", "playerName"]].dropna()
                   .drop_duplicates()
                   .sort_values(["team", "playerName"]))
        options = [f"{row.team} — {row.playerName}" for row in players.itertuples()]
        label_to_player = {f"{row.team} — {row.playerName}": row.playerName for row in players.itertuples()}

        if options:
            selected_label = st.selectbox("Player", options)
            selected_player = label_to_player[selected_label]
            player_passes = wr.get_player_passes(df, selected_player)

            if player_passes.empty:
                st.info(f"{selected_player} didn't attempt any passes in this match.")
            else:
                total = len(player_passes)
                completed = int(player_passes["completed"].sum())
                progressive = int(player_passes["is_progressive"].sum())
                key_passes = int(player_passes["is_key_pass"].sum())
                c1, c2, c3, c4 = st.columns(4)
                c1.metric("Passes Attempted", total)
                c2.metric("Completion %", f"{completed / total * 100:.0f}%")
                c3.metric("Progressive", progressive)
                c4.metric("Key Passes (xA-adjacent)", key_passes)

                stat_items = [
                    (f"{total} Attempted", TITLE_COLOR),
                    (f"{completed / total * 100:.0f}% Completion", TITLE_COLOR),
                    (f"{progressive} Progressive", PASS_CATEGORY_COLORS["Progressive"]),
                    (f"{key_passes} Key Passes", PASS_CATEGORY_COLORS["Key Pass"]),
                ]
                fig = plot_pass_map(player_passes, selected_player, home_name, away_name,
                                     stat_items, title_suffix="Pass Map")

                # Rendered as a fixed-width image (rather than st.pyplot's
                # default full-column-width behavior) so the pitch shows up
                # at a reasonable size on the page instead of filling the
                # whole browser window.
                png_buf = io.BytesIO()
                fig.savefig(png_buf, format="png", dpi=150, facecolor=fig.get_facecolor())
                png_buf.seek(0)
                st.image(png_buf, width=420)

                # Higher-res than the on-page preview (dpi=150 above), since
                # this copy is meant to be downloaded/kept/printed rather
                # than just previewed inline.
                download_png_buf = io.BytesIO()
                fig.savefig(download_png_buf, format="png", dpi=300, facecolor=fig.get_facecolor())
                download_png_buf.seek(0)
                st.download_button(
                    label="Download Pass Map (PNG)",
                    data=download_png_buf,
                    file_name=f"{wr.sanitize_filename(selected_player)}_pass_map.png",
                    mime="image/png",
                )
                plt.close(fig)
        else:
            st.info("No players found in this match's event data.")
    with tabPR:
        st.write(
            "Every completed pass RECEIVED by one player, plotted on the pitch at the spot they "
            "received it, colored by the same categories as the Pass Map - **completed**, "
            "**progressive**, or **key pass (shot assist)**. Incomplete passes aren't shown here "
            "since they were never actually received by anyone."
        )
        players_pr = (df[["team", "playerName"]].dropna()
                      .drop_duplicates()
                      .sort_values(["team", "playerName"]))
        options_pr = [f"{row.team} — {row.playerName}" for row in players_pr.itertuples()]
        label_to_player_pr = {f"{row.team} — {row.playerName}": row.playerName
                               for row in players_pr.itertuples()}

        if options_pr:
            selected_label_pr = st.selectbox("Player", options_pr, key="passes_received_player")
            selected_player_pr = label_to_player_pr[selected_label_pr]
            passes_received = wr.get_player_passes_received(df, selected_player_pr)

            if passes_received.empty:
                st.info(f"{selected_player_pr} didn't receive any completed passes in this match.")
            else:
                total_pr = len(passes_received)
                progressive_pr = int(passes_received["is_progressive"].sum())
                key_passes_pr = int(passes_received["is_key_pass"].sum())
                c1, c2, c3 = st.columns(3)
                c1.metric("Passes Received", total_pr)
                c2.metric("Progressive", progressive_pr)
                c3.metric("Key Passes (xA-adjacent)", key_passes_pr)

                stat_items_pr = [
                    (f"{total_pr} Received", TITLE_COLOR),
                    (f"{progressive_pr} Progressive", PASS_CATEGORY_COLORS["Progressive"]),
                    (f"{key_passes_pr} Key Passes", PASS_CATEGORY_COLORS["Key Pass"]),
                ]
                fig_pr = plot_pass_map(passes_received, selected_player_pr, home_name, away_name,
                                        stat_items_pr, title_suffix="Passes Received")

                png_buf_pr = io.BytesIO()
                fig_pr.savefig(png_buf_pr, format="png", dpi=150, facecolor=fig_pr.get_facecolor())
                png_buf_pr.seek(0)
                st.image(png_buf_pr, width=420)

                download_png_buf_pr = io.BytesIO()
                fig_pr.savefig(download_png_buf_pr, format="png", dpi=300, facecolor=fig_pr.get_facecolor())
                download_png_buf_pr.seek(0)
                st.download_button(
                    label="Download Passes Received (PNG)",
                    data=download_png_buf_pr,
                    file_name=f"{wr.sanitize_filename(selected_player_pr)}_passes_received.png",
                    mime="image/png",
                )
                plt.close(fig_pr)
        else:
            st.info("No players found in this match's event data.")

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
