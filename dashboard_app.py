"""
dashboard_app.py
=================
Read-only season dashboard over the history database (see history_db.py) -
this is the "website" half of the setup. It has NO scraping code at all and
never touches WhoScored/FotMob directly, which is deliberate: it's the piece
meant to actually be deployed somewhere public (Streamlit Community Cloud),
so it can't carry the real-browser/Chrome requirement that makes the
scraping side hard to host in the cloud. All it does is read whatever's
already been published into the database by combined_streamlit_app.py's
"Save to Database" step, which stays local/manual (see project chat history
for why).

Local use:
    pip install streamlit pandas
    streamlit run dashboard_app.py

Deploying to Streamlit Community Cloud:
    1. Push this file, history_db.py, pitch_viz.py, and
       kwest_thoughts_logo_v2.png to the GitHub repo - that's it, it does
       NOT need whoscored_report.py, fotmob_report.py, selenium, or any of
       the scraping dependencies. (pitch_viz.py used to import
       whoscored_report.py just to read two float constants, which
       transitively required the entire selenium/fake_useragent scraper
       stack just to draw a pitch - that constant is now duplicated
       directly inside pitch_viz.py instead, so this app's dependency list
       stays genuinely minimal. See pitch_viz.py's own docstring for the
       full story.) pitch_viz.plot_heatmap() (the Season Heat Map tab) needs
       scipy - make sure requirements.txt includes it.
    2. On share.streamlit.io, deploy pointed at this file.
    3. In the app's "Secrets" settings, set:
           DATABASE_URL = "postgresql://user:pass@host:port/dbname"
       (your hosted Postgres connection string - Supabase/Neon/etc. give you
       this after you create a database). Locally, DATABASE_URL can just be
       unset - it falls back to a local history.db SQLite file.
"""

import io
import os

import matplotlib.pyplot as plt
import pandas as pd
import streamlit as st

import history_db as hdb
from pitch_viz import plot_pass_map, plot_heatmap, PASS_CATEGORY_COLORS, TITLE_COLOR

st.set_page_config(page_title="Match History Dashboard", layout="wide")
st.title("Match History Dashboard")


def _database_url():
    # st.secrets works once deployed on Streamlit Community Cloud; the
    # try/except is just because st.secrets raises if no secrets.toml
    # exists at all (e.g. running this locally without one).
    try:
        if "DATABASE_URL" in st.secrets:
            return st.secrets["DATABASE_URL"]
    except Exception:
        pass
    return os.environ.get("DATABASE_URL", "sqlite:///history.db")


@st.cache_resource
def get_db():
    db = hdb.get_db(_database_url())
    hdb.init_schema(db)
    return db


db = get_db()

(tab_matches, tab_team, tab_player, tab_shots, tab_passmap, tab_passrecv,
 tab_season_passmap, tab_season_passrecv, tab_season_heatmap) = st.tabs([
    "Matches", "Team Trends", "Player Trends", "Shots", "Pass Map", "Passes Received",
    "Season Pass Map", "Season Passes Received", "Season Heat Map",
])

with tab_matches:
    matches = hdb.fetch_matches(db)
    if matches.empty:
        st.info("No matches published yet - run the combined report app and use 'Save to Database'.")
    else:
        st.dataframe(matches, use_container_width=True)
        st.caption(f"{len(matches)} match(es) in the database.")

with tab_team:
    matches = hdb.fetch_matches(db)
    teams = sorted(set(matches["home_team"].dropna()) | set(matches["away_team"].dropna()))
    if not teams:
        st.info("No matches published yet.")
    else:
        team = st.selectbox("Team", teams)
        trends = hdb.fetch_team_trends(db, team)
        if trends.empty:
            st.info(f"No stats saved yet for {team}.")
        else:
            st.dataframe(trends, use_container_width=True)
            numeric_cols = [c for c in trends.columns
                             if c not in ("match_date", "match_id", "team", "is_home")
                             and pd.api.types.is_numeric_dtype(trends[c])]
            if numeric_cols:
                chosen = st.multiselect("Chart these stats over time", numeric_cols,
                                         default=numeric_cols[:3])
                if chosen:
                    chart_df = trends.set_index("match_date")[chosen]
                    st.line_chart(chart_df)

with tab_player:
    matches = hdb.fetch_matches(db)
    if matches.empty:
        st.info("No matches published yet.")
    else:
        # A dropdown of the players actually saved in the database, rather
        # than a free-text "type the exact name" box - fetch_player_trends()
        # does an exact, case-sensitive match with no fuzzy fallback, so a
        # typed name that's off by a space or capitalization used to just
        # silently return nothing. Picking from a list removes that failure
        # mode entirely.
        players = hdb.fetch_distinct_players(db)
        if not players:
            st.info("No player stats saved yet.")
        else:
            player = st.selectbox("Player", players)
            trends = hdb.fetch_player_trends(db, player)
            if trends.empty:
                st.info(f"No stats saved yet for '{player}'.")
            else:
                st.dataframe(trends, use_container_width=True)
                numeric_cols = [c for c in trends.columns
                                 if c not in ("match_date", "match_id", "team", "player")
                                 and pd.api.types.is_numeric_dtype(trends[c])]
                if numeric_cols:
                    chosen = st.multiselect("Chart these stats over time", numeric_cols,
                                             default=numeric_cols[:3], key="player_chart_cols")
                    if chosen:
                        chart_df = trends.set_index("match_date")[chosen]
                        st.line_chart(chart_df)

with tab_shots:
    matches = hdb.fetch_matches(db)
    if matches.empty:
        st.info("No matches published yet.")
    else:
        col1, col2 = st.columns(2)
        with col1:
            match_options = {"All matches": None}
            match_options.update({
                f"{r.home_team} vs {r.away_team} ({r.match_date})": r.match_id
                for r in matches.itertuples()
            })
            match_label = st.selectbox("Match", list(match_options.keys()))
            match_filter = match_options[match_label]
        with col2:
            player_filter = st.text_input("Filter by player (optional)", key="shots_player_filter")

        shots = hdb.fetch_shots(db, match_id=match_filter, player=player_filter or None)
        if shots.empty:
            st.info("No shots match this filter.")
        else:
            st.dataframe(shots.drop(columns=["extra_json"], errors="ignore"), use_container_width=True)
            if "xg" in shots.columns:
                c1, c2, c3 = st.columns(3)
                c1.metric("Shots", len(shots))
                c2.metric("Total xG", f"{shots['xg'].fillna(0).sum():.2f}")
                goals = (shots["outcome"] == "Goal").sum() if "outcome" in shots.columns else 0
                c3.metric("Goals", int(goals))


def _match_picker(matches, key):
    """Shared match dropdown for the Pass Map/Passes Received tabs below."""
    options = {
        f"{r.home_team} vs {r.away_team} ({r.match_date})": r.match_id
        for r in matches.itertuples()
    }
    label = st.selectbox("Match", list(options.keys()), key=key)
    return options[label], label


def _render_pass_map(db, matches, mode):
    """
    mode='passer' draws the outgoing Pass Map (every pass attempted by the
    selected player); mode='receiver' draws Passes Received (every completed
    pass received by them). Both read from the same 'passes' table (see
    history_db.py/whoscored_report.compute_all_passes()) and share
    pitch_viz.plot_pass_map() - the exact same drawing code streamlit_app.py
    uses for its own live versions of these two charts, just fed from the
    database instead of a freshly-scraped match.
    """
    if matches.empty:
        st.info("No matches published yet.")
        return
    match_id, match_label = _match_picker(matches, key=f"passmap_match_{mode}")

    # Passes for the whole match, unfiltered - used just to populate the
    # player dropdown with only players who actually have pass data saved
    # (older matches saved before compute_all_passes() existed won't have
    # any rows here at all).
    all_match_passes = hdb.fetch_passes(db, match_id)
    if all_match_passes.empty:
        st.info(
            f"No pass data saved for {match_label} - this match was likely published before the "
            "Pass Map/Passes Received feature was added. Re-run 'Save to Database' for it in the "
            "combined report app to backfill it."
        )
        return

    if mode == "passer":
        player_col = "passer"
    else:
        player_col = "receiver"
    players = sorted(all_match_passes[player_col].dropna().unique())
    if not players:
        st.info(f"No {'passes' if mode == 'passer' else 'received passes'} recorded for {match_label}.")
        return
    player = st.selectbox("Player", players, key=f"passmap_player_{mode}")

    if mode == "passer":
        player_passes = hdb.fetch_passes(db, match_id, passer=player)
    else:
        # Mirrors get_player_passes_received()'s own restriction to
        # completed passes only - an incomplete pass has no real receiver.
        player_passes = hdb.fetch_passes(db, match_id, receiver=player, completed_only=True)

    if player_passes.empty:
        st.info(f"No {'passes' if mode == 'passer' else 'received passes'} found for {player}.")
        return

    # plot_pass_map()/pitch_viz.py expect whoscored_report.py's own dataframe
    # convention (endX/endY), but the passes table stores those as end_x/
    # end_y (SQL-friendlier column names) - rename before handing off.
    player_passes = player_passes.rename(columns={"end_x": "endX", "end_y": "endY"})

    home_team = matches.loc[matches["match_id"] == match_id, "home_team"].iloc[0]
    away_team = matches.loc[matches["match_id"] == match_id, "away_team"].iloc[0]

    total = len(player_passes)
    progressive = int(player_passes["is_progressive"].sum())
    key_passes = int(player_passes["is_key_pass"].sum())

    if mode == "passer":
        completed = int(player_passes["completed"].sum())
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
        title_suffix = "Pass Map"
    else:
        c1, c2, c3 = st.columns(3)
        c1.metric("Passes Received", total)
        c2.metric("Progressive", progressive)
        c3.metric("Key Passes (xA-adjacent)", key_passes)
        stat_items = [
            (f"{total} Received", TITLE_COLOR),
            (f"{progressive} Progressive", PASS_CATEGORY_COLORS["Progressive"]),
            (f"{key_passes} Key Passes", PASS_CATEGORY_COLORS["Key Pass"]),
        ]
        title_suffix = "Passes Received"

    fig = plot_pass_map(player_passes, player, home_team, away_team, stat_items,
                         title_suffix=title_suffix)

    png_buf = io.BytesIO()
    fig.savefig(png_buf, format="png", dpi=150, facecolor=fig.get_facecolor())
    png_buf.seek(0)
    st.image(png_buf, width=420)

    download_buf = io.BytesIO()
    fig.savefig(download_buf, format="png", dpi=300, facecolor=fig.get_facecolor())
    download_buf.seek(0)
    st.download_button(
        label=f"Download {title_suffix} (PNG)",
        data=download_buf,
        file_name=f"{player.replace(' ', '_')}_{title_suffix.lower().replace(' ', '_')}.png",
        mime="image/png",
        key=f"passmap_download_{mode}",
    )
    plt.close(fig)


with tab_passmap:
    st.write(
        "Every pass attempted by one player in a saved match, colored by outcome: **completed**, "
        "**incomplete**, **progressive**, or **key pass (shot assist)**. Same chart as "
        "streamlit_app.py's live Pass Map tab, read from whatever's already been published here."
    )
    _render_pass_map(db, hdb.fetch_matches(db), mode="passer")

with tab_passrecv:
    st.write(
        "Every COMPLETED pass received by one player in a saved match, plotted at the spot they "
        "received it. Incomplete passes aren't shown - they were never actually received by anyone."
    )
    _render_pass_map(db, hdb.fetch_matches(db), mode="receiver")


def _team_filter_picker(df, team_col, key):
    """
    Shared 'All teams' + team dropdown for the season tabs below. A team
    filter matters here specifically BECAUSE these tabs aggregate across
    every published match - a player who's transferred mid-season would
    otherwise have passes/touches from two different teams mixed into one
    map with nothing to tell them apart.
    """
    teams = sorted(df[team_col].dropna().unique())
    choice = st.selectbox("Team (optional filter)", ["All teams"] + teams, key=key)
    return None if choice == "All teams" else choice


def _render_season_pass_map(db, mode):
    """
    Same chart as _render_pass_map() above, but aggregated across EVERY
    published match instead of one - every pass a player has ever attempted
    (mode='passer') or received (mode='receiver') in the whole database,
    plotted on one pitch, so the shape reflects a season's worth of games
    rather than a single one. Reads from the exact same 'passes' table -
    the only difference is fetch_passes() is called with match_id=None.
    """
    all_passes = hdb.fetch_passes(db)
    if all_passes.empty:
        st.info("No pass data saved yet - publish at least one match with 'Save to Database' first.")
        return

    player_col = "passer" if mode == "passer" else "receiver"
    col1, col2 = st.columns(2)
    with col1:
        team_filter = _team_filter_picker(all_passes, "team", key=f"season_passmap_team_{mode}")

    scoped = all_passes if team_filter is None else all_passes[all_passes["team"] == team_filter]
    players = sorted(scoped[player_col].dropna().unique())
    if not players:
        st.info("No players found for this filter.")
        return
    with col2:
        player = st.selectbox("Player", players, key=f"season_passmap_player_{mode}")

    if mode == "passer":
        player_passes = hdb.fetch_passes(db, passer=player, team=team_filter)
    else:
        player_passes = hdb.fetch_passes(db, receiver=player, team=team_filter, completed_only=True)

    if player_passes.empty:
        st.info(f"No {'passes' if mode == 'passer' else 'received passes'} found for {player}.")
        return

    player_passes = player_passes.rename(columns={"end_x": "endX", "end_y": "endY"})
    n_matches = player_passes["match_id"].nunique()

    total = len(player_passes)
    progressive = int(player_passes["is_progressive"].sum())
    key_passes = int(player_passes["is_key_pass"].sum())

    if mode == "passer":
        completed = int(player_passes["completed"].sum())
        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("Matches", n_matches)
        c2.metric("Passes Attempted", total)
        c3.metric("Completion %", f"{completed / total * 100:.0f}%")
        c4.metric("Progressive", progressive)
        c5.metric("Key Passes (xA-adjacent)", key_passes)
        stat_items = [
            (f"{total} Attempted", TITLE_COLOR),
            (f"{completed / total * 100:.0f}% Completion", TITLE_COLOR),
            (f"{progressive} Progressive", PASS_CATEGORY_COLORS["Progressive"]),
            (f"{key_passes} Key Passes", PASS_CATEGORY_COLORS["Key Pass"]),
        ]
        title_suffix = "Season Pass Map"
    else:
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Matches", n_matches)
        c2.metric("Passes Received", total)
        c3.metric("Progressive", progressive)
        c4.metric("Key Passes (xA-adjacent)", key_passes)
        stat_items = [
            (f"{total} Received", TITLE_COLOR),
            (f"{progressive} Progressive", PASS_CATEGORY_COLORS["Progressive"]),
            (f"{key_passes} Key Passes", PASS_CATEGORY_COLORS["Key Pass"]),
        ]
        title_suffix = "Season Passes Received"

    fig = plot_pass_map(player_passes, player, None, None, stat_items, title_suffix=title_suffix,
                         subtitle=f"Season - {n_matches} match(es)")

    png_buf = io.BytesIO()
    fig.savefig(png_buf, format="png", dpi=150, facecolor=fig.get_facecolor())
    png_buf.seek(0)
    st.image(png_buf, width=420)

    download_buf = io.BytesIO()
    fig.savefig(download_buf, format="png", dpi=300, facecolor=fig.get_facecolor())
    download_buf.seek(0)
    st.download_button(
        label=f"Download {title_suffix} (PNG)",
        data=download_buf,
        file_name=f"{player.replace(' ', '_')}_season_{title_suffix.lower().replace(' ', '_')}.png",
        mime="image/png",
        key=f"season_passmap_download_{mode}",
    )
    plt.close(fig)


def _render_season_heatmap(db):
    """
    Touch heat map aggregated across every published match - every touch a
    player's had, any event type (see whoscored_report.compute_all_touches()),
    plotted as a smoothed density over one pitch. Only matches saved AFTER
    the touches table existed have any rows here (see history_db.py's
    schema docstring), so older published matches won't contribute.
    """
    all_touches = hdb.fetch_touches(db)
    if all_touches.empty:
        st.info(
            "No touch data saved yet. This needs matches saved AFTER the touches table was added - "
            "re-run 'Save to Database' on your matches in the combined report app to backfill it."
        )
        return

    team_filter = _team_filter_picker(all_touches, "team", key="season_heatmap_team")
    scoped = all_touches if team_filter is None else all_touches[all_touches["team"] == team_filter]
    players = sorted(scoped["player"].dropna().unique())
    if not players:
        st.info("No players found for this filter.")
        return
    player = st.selectbox("Player", players, key="season_heatmap_player")

    player_touches = hdb.fetch_touches(db, player=player, team=team_filter)
    if player_touches.empty:
        st.info(f"No touches found for {player}.")
        return

    n_matches = player_touches["match_id"].nunique()
    st.metric("Matches", n_matches)
    stat_items = [(f"{len(player_touches)} Touches", TITLE_COLOR), (f"{n_matches} Matches", TITLE_COLOR)]

    fig = plot_heatmap(player_touches, player, subtitle=f"Season - {n_matches} match(es)",
                        stat_items=stat_items)

    png_buf = io.BytesIO()
    fig.savefig(png_buf, format="png", dpi=150, facecolor=fig.get_facecolor())
    png_buf.seek(0)
    st.image(png_buf, width=420)

    download_buf = io.BytesIO()
    fig.savefig(download_buf, format="png", dpi=300, facecolor=fig.get_facecolor())
    download_buf.seek(0)
    st.download_button(
        label="Download Season Heat Map (PNG)",
        data=download_buf,
        file_name=f"{player.replace(' ', '_')}_season_heatmap.png",
        mime="image/png",
        key="season_heatmap_download",
    )
    plt.close(fig)


with tab_season_passmap:
    st.write(
        "Every pass a player has attempted across EVERY match saved to the database so far, "
        "plotted on one pitch - the same coloring as the single-match Pass Map, aggregated into a "
        "season-long shape. Grows automatically as you save more matches."
    )
    _render_season_pass_map(db, mode="passer")

with tab_season_passrecv:
    st.write(
        "Every COMPLETED pass a player has received across every saved match, aggregated the same "
        "way as the Season Pass Map."
    )
    _render_season_pass_map(db, mode="receiver")

with tab_season_heatmap:
    st.write(
        "A smoothed density of every touch a player's had on the ball - any event, not just passes "
        "- across every saved match, showing where on the pitch they're most involved over a "
        "season. Only matches saved after this feature was added contribute (see the info message "
        "below if it looks empty)."
    )
    _render_season_heatmap(db)
