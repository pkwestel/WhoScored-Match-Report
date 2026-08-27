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
       full story.) pitch_viz.plot_touch_map() (the Season Touch Map tab)
       needs scipy - make sure requirements.txt includes it.
    2. On share.streamlit.io, deploy pointed at this file.
    3. In the app's "Secrets" settings, set:
           DATABASE_URL = "postgresql://user:pass@host:port/dbname"
       (your hosted Postgres connection string - Supabase/Neon/etc. give you
       this after you create a database). Locally, DATABASE_URL can just be
       unset - it falls back to a local history.db SQLite file.

PAGE ROUTING
------------
This is a single-page Streamlit script (no multipage app), so "clicking a
match" to open its full report is done with a plain URL query parameter -
st.query_params["match_id"] - rather than a real second page. The Fixtures
tab's "Report" column is a clickable link pointing at "?match_id=<id>" on
this same app; when that param is present, the WHOLE normal tabbed
dashboard is replaced by _render_match_detail()'s single-match view instead
(see the dispatch at the bottom of this file) - there's no in-between state
where both are visible at once.
"""

import html
import io
import os
import re
from urllib.parse import quote

import matplotlib.pyplot as plt
import pandas as pd
import streamlit as st

import history_db as hdb
from pitch_viz import (plot_pass_map, plot_touch_map, PASS_CATEGORY_COLORS, TITLE_COLOR,
                       HOME_TEAM_COLOR, AWAY_TEAM_COLOR)

st.set_page_config(page_title="Match History Dashboard", layout="wide")


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


# ============================================================
# Shared helpers (used by both the normal tabbed dashboard AND the
# single-match detail view, so they're all defined up front before either
# one runs)
# ============================================================
# FotMob shot "situation" -> a friendlier display label, for the season
# Shots tab's dropdown/tables. Confirmed against a real shot map
# (fotmob_raw_5795368.json): RegularPlay, SetPiece, FromCorner, Penalty,
# FreeKick, FastBreak, and ThrowInSetPiece all actually occur. Duplicated
# here (rather than imported from fotmob_report.py) for the same reason
# pitch_viz.py duplicates PITCH_LEN_M/PITCH_WID_M instead of importing
# whoscored_report.py for them - see this module's own docstring: importing
# fotmob_report.py would pull in its top-level selenium/utils.driver
# imports, which broke a real Streamlit Cloud deploy once already.
_SITUATION_DISPLAY_MAP = {
    "RegularPlay": "Open Play",
    "SetPiece": "Set Piece",
    "FromCorner": "Corner",
    "Penalty": "Penalty",
    "FreeKick": "Free Kick",
    "FastBreak": "Fast Break",
    "ThrowInSetPiece": "Throw-In Set Piece",
    "Unknown": "Unknown",
}

# Situation values that shouldn't be offered as their own filter option in
# the season Shots tab's dropdown - "Unknown" covers shots saved with a
# missing/garbled situation (including old rows saved before upsert_shots()
# started sanitizing this - see history_db.py), and "IndividualPlay" was a
# one-off request to keep it out of the picker too. Their shots are still
# counted normally under "All situations" - this only hides them as a
# selectable category, it doesn't drop the underlying shot data.
_HIDDEN_SITUATIONS = {"Unknown", "IndividualPlay"}


def _situation_display_name(raw):
    """Friendly label for a raw FotMob shot 'situation' value - see
    _SITUATION_DISPLAY_MAP above. Falls back to spacing out an unrecognized
    CamelCase value ('SomeNewType' -> 'Some New Type') rather than showing
    it raw, in case FotMob adds a new situation type later."""
    if raw in _SITUATION_DISPLAY_MAP:
        return _SITUATION_DISPLAY_MAP[raw]
    return re.sub(r"(?<!^)(?=[A-Z])", " ", raw) if isinstance(raw, str) else str(raw)


def _no_scroll_height(df) -> int:
    """
    st.dataframe defaults to a fixed height (~400px) regardless of row
    count, which forces its OWN internal scrollbar on any table with more
    than about a dozen rows - e.g. the League Table or a season Team Stats
    table, where every row is a team and there usually aren't more than
    20-ish of them. Sizing the table to its exact content height instead
    means the table itself never scrolls - the PAGE scrolls to reach the
    bottom of a tall table instead, per the requested behavior. ~35px per
    row (Streamlit's own default row height) + ~38px for the header, with
    a few px of border padding on top.
    """
    return int(len(df) + 1) * 35 + 3


def _team_page_url(team_name):
    """
    Relative '?team=<url-encoded name>' link to a team's Team Page (see
    _render_team_page()'s own dispatch at the bottom of this module - same
    'no multipage app, swap the page via a query param' pattern as
    _render_match_detail()'s '?match_id=<id>'). Works for ANY team name,
    including one that's never been seen before (a brand new fixture's
    team) - there's no separate registry of "known" teams to keep in sync,
    the link is just built from whatever string is already in the table
    being rendered. Returns None for a missing/blank name.
    """
    if not team_name or (isinstance(team_name, float) and pd.isna(team_name)):
        return None
    return f"?team={quote(str(team_name))}"


def _linkify_team_cell(team_name):
    """
    HTML for one team-name table cell: a real '<a href="?team=...">' link
    with the clean, human-readable team name as its own display text -
    deliberately NOT built with st.dataframe's own column_config.LinkColumn
    (used elsewhere in this app for the Fixtures 'Report' column), since
    LinkColumn's display text is either one fixed string for every row or
    extracted via regex straight from the URL - the latter would show the
    url-encoded name itself (e.g. 'Manchester%20United') rather than a
    normal-looking team name. A blank/missing name renders as plain empty
    text rather than a dead link.
    """
    if not team_name or (isinstance(team_name, float) and pd.isna(team_name)):
        return ""
    url = _team_page_url(team_name)
    return f'<a href="{url}" style="color:inherit; text-decoration:underline;">{html.escape(str(team_name))}</a>'


def _render_data_table_html(df, link_columns=(), raw_html_columns=()):
    """
    Renders df as a plain HTML table via st.markdown (bordered cells, bold
    light-grey header row - a reasonably close match to st.dataframe's own
    look) instead of st.dataframe, specifically so any column named in
    link_columns can be rendered as real team-name links (see
    _linkify_team_cell()) rather than plain text. Used for the League
    Table, the 5 season Team Stats category tables, and the Fixtures table
    - all previously plain st.dataframe calls before team-name links were
    added. Numeric columns are shown right-aligned, link_columns/
    raw_html_columns left-aligned, matching st.dataframe's own default
    alignment convention.

    raw_html_columns is for a column whose cell values are ALREADY a
    ready-to-insert HTML snippet (e.g. Fixtures' 'Report' column, a
    '<a href="?match_id=...">View →</a>' link to the match detail view,
    built by the caller before this function ever sees it) - inserted as-is
    rather than html-escaped, unlike every other column.
    """
    if df.empty:
        return
    left_columns = set(link_columns) | set(raw_html_columns)
    header_cells = "".join(
        f'<th style="padding:6px 14px; border:1px solid #ddd; background:#f0f2f6; '
        f'text-align:{"left" if col in left_columns else "right"}; white-space:nowrap;">'
        f'{html.escape(str(col))}</th>'
        for col in df.columns
    )
    body_rows = []
    for _, r in df.iterrows():
        cells = []
        for col in df.columns:
            val = r[col]
            if col in link_columns:
                cell_html, align = _linkify_team_cell(val), "left"
            elif col in raw_html_columns:
                cell_html, align = ("" if pd.isna(val) else str(val)), "left"
            else:
                cell_html = "-" if pd.isna(val) else html.escape(str(val))
                align = "right"
            cells.append(
                f'<td style="padding:6px 14px; border:1px solid #ddd; text-align:{align}; '
                f'white-space:nowrap;">{cell_html}</td>'
            )
        body_rows.append(f"<tr>{''.join(cells)}</tr>")
    st.markdown(f"""
    <div style="overflow-x:auto;">
        <table style="border-collapse:collapse; font-size:0.95em;">
            <thead><tr>{header_cells}</tr></thead>
            <tbody>{"".join(body_rows)}</tbody>
        </table>
    </div>
    """, unsafe_allow_html=True)


def _render_fixtures_like_table(df):
    """
    Shared renderer for any fetch_fixtures()-shaped DataFrame (match_id,
    Date, Competition, Matchweek, Season, Home Team, Home xG, Score, Away
    xG, Away Team, Referee) - used by both the Fixtures tab (every match)
    and the Team Page's own Match Log (one team's matches only), so the two
    look identical.

    Builds the '?match_id=...' Report link (see _render_data_table_html's
    raw_html_columns), formats Home/Away xG to 2 decimal places as plain
    strings ('-' for a match with no saved xG yet, same convention as
    everywhere else), and links Home Team/Away Team to their own Team
    Pages. Drops match_id (only needed to build the Report link) and Season
    (always a filter/context elsewhere already, not its own column here).
    """
    display_df = df.copy()
    display_df["Report"] = display_df["match_id"].apply(
        lambda m: f'<a href="?match_id={m}">View →</a>'
    )
    display_df["Home xG"] = display_df["Home xG"].apply(
        lambda v: f"{v:.2f}" if pd.notna(v) else None
    )
    display_df["Away xG"] = display_df["Away xG"].apply(
        lambda v: f"{v:.2f}" if pd.notna(v) else None
    )
    display_df = display_df.drop(columns=["match_id", "Season"])
    _render_data_table_html(
        display_df,
        link_columns=("Home Team", "Away Team"),
        raw_html_columns=("Report",),
    )


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
    database instead of a freshly-scraped match. 'matches' can be every
    saved match (the normal Pass Map/Passes Received tabs) or a single-row
    DataFrame scoped to one match (the match detail view) - the match
    dropdown just has one option in that case.
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
    match_date_raw = matches.loc[matches["match_id"] == match_id, "match_date"].iloc[0]
    match_date, _ = _split_date_and_kickoff(match_date_raw)
    player_team = (player_passes["team"].iloc[0]
                    if "team" in player_passes.columns and not player_passes.empty else None)

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

    fig = plot_pass_map(player_passes, player, player_team, home_team, away_team, stat_items,
                         title_suffix=title_suffix, match_date=match_date)

    png_buf = io.BytesIO()
    fig.savefig(png_buf, format="png", dpi=220, facecolor=fig.get_facecolor())
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

    # team_filter is the team this player's data was scoped to, if the
    # picker above was used - otherwise (a player who's played for more
    # than one team this season, "All teams" selected) fall back to
    # whichever team shows up most often in their passes, rather than
    # leaving the title's '({team})' blank.
    player_team = team_filter or (
        player_passes["team"].mode().iloc[0] if "team" in player_passes.columns and not player_passes.empty
        else None
    )

    fig = plot_pass_map(player_passes, player, player_team, None, None, stat_items,
                         title_suffix=title_suffix, subtitle=f"Season - {n_matches} match(es)")

    png_buf = io.BytesIO()
    fig.savefig(png_buf, format="png", dpi=220, facecolor=fig.get_facecolor())
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


def _render_season_touchmap(db):
    """
    Touch map aggregated across every published match - every touch a
    player's had, any event type (see whoscored_report.compute_all_touches()),
    plotted on one pitch, shaded with a smoothed density where there's
    enough data to support one. Only matches saved AFTER the touches table
    existed have any rows here (see history_db.py's schema docstring), so
    older published matches won't contribute.
    """
    all_touches = hdb.fetch_touches(db)
    if all_touches.empty:
        st.info(
            "No touch data saved yet. This needs matches saved AFTER the touches table was added - "
            "re-run 'Save to Database' on your matches in the combined report app to backfill it."
        )
        return

    team_filter = _team_filter_picker(all_touches, "team", key="season_touchmap_team")
    scoped = all_touches if team_filter is None else all_touches[all_touches["team"] == team_filter]
    players = sorted(scoped["player"].dropna().unique())
    if not players:
        st.info("No players found for this filter.")
        return
    player = st.selectbox("Player", players, key="season_touchmap_player")

    player_touches = hdb.fetch_touches(db, player=player, team=team_filter)
    if player_touches.empty:
        st.info(f"No touches found for {player}.")
        return

    n_matches = player_touches["match_id"].nunique()
    st.metric("Matches", n_matches)
    stat_items = [(f"{len(player_touches)} Touches", TITLE_COLOR), (f"{n_matches} Matches", TITLE_COLOR)]

    # team_filter is the team this player's data was scoped to, if the
    # picker above was used - otherwise (a player who's played for more
    # than one team this season, "All teams" selected) fall back to
    # whichever team shows up most often in their touches.
    player_team = team_filter or (
        player_touches["team"].mode().iloc[0] if "team" in player_touches.columns and not player_touches.empty
        else None
    )

    fig = plot_touch_map(player_touches, player, player_team=player_team,
                          subtitle=f"Season - {n_matches} match(es)", stat_items=stat_items)

    png_buf = io.BytesIO()
    fig.savefig(png_buf, format="png", dpi=220, facecolor=fig.get_facecolor())
    png_buf.seek(0)
    st.image(png_buf, width=420)

    download_buf = io.BytesIO()
    fig.savefig(download_buf, format="png", dpi=300, facecolor=fig.get_facecolor())
    download_buf.seek(0)
    st.download_button(
        label="Download Season Touch Map (PNG)",
        data=download_buf,
        file_name=f"{player.replace(' ', '_')}_season_touchmap.png",
        mime="image/png",
        key="season_touchmap_download",
    )
    plt.close(fig)


def _render_match_touchmap(db, match_id, home_team, away_team, match_date=None):
    """Single-match touch map - same idea as _render_season_touchmap()
    above, scoped to one match_id instead of the whole database."""
    touches = hdb.fetch_touches(db, match_id=match_id)
    if touches.empty:
        st.info(
            "No touch data saved for this match - it was likely published before the touches table "
            "was added. Re-run 'Save to Database' for it in the combined report app to backfill it."
        )
        return
    players = sorted(touches["player"].dropna().unique())
    if not players:
        st.info("No players found in this match's touch data.")
        return
    player = st.selectbox("Player", players, key="match_detail_touchmap_player")
    player_touches = touches[touches["player"] == player]
    player_team = (player_touches["team"].iloc[0]
                    if "team" in player_touches.columns and not player_touches.empty else None)
    date_part, _ = _split_date_and_kickoff(match_date)

    fig = plot_touch_map(player_touches, player, player_team=player_team,
                          home_name=home_team, away_name=away_team, match_date=date_part)
    png_buf = io.BytesIO()
    fig.savefig(png_buf, format="png", dpi=220, facecolor=fig.get_facecolor())
    png_buf.seek(0)
    st.image(png_buf, width=420)

    download_buf = io.BytesIO()
    fig.savefig(download_buf, format="png", dpi=300, facecolor=fig.get_facecolor())
    download_buf.seek(0)
    st.download_button(
        label="Download Touch Map (PNG)",
        data=download_buf,
        file_name=f"{player.replace(' ', '_')}_touchmap.png",
        mime="image/png",
        key="match_detail_touchmap_download",
    )
    plt.close(fig)


def _render_team_summary_table(df, home_col, metric_col, away_col,
                                header_font_size="1.3em", body_font_size="1.15em",
                                cell_padding="10px 44px", header_padding="14px 44px"):
    """
    Shared renderer for every Home/Metric/Away summary table in the app
    (the Team Totals tab's match summary, and each of the Advanced Stats
    tab's 5 smaller tables) - a plain HTML table (rather than st.dataframe)
    to get styling st.dataframe can't do: a blank header cell where the
    literal word 'Metric' would show, home/away column headers colored in
    the same red/blue as the page header above, no cell borders at all (so
    it reads as a clean graphic rather than a spreadsheet grid), centered
    on the page. font_size/padding are parameterized so the Advanced Stats
    tab can reuse the exact same look at a smaller scale (per that tab's
    request to look "the same... but smaller") rather than duplicating this
    markup with different numbers baked in.
    """
    rows_html = "".join(
        f"""<tr>
            <td style="padding:{cell_padding}; text-align:center; border:none; font-size:{body_font_size};">{r[home_col]}</td>
            <td style="padding:{cell_padding}; text-align:center; border:none; font-size:{body_font_size}; color:#666;">{r[metric_col]}</td>
            <td style="padding:{cell_padding}; text-align:center; border:none; font-size:{body_font_size};">{r[away_col]}</td>
        </tr>"""
        for _, r in df.iterrows()
    )
    st.markdown(f"""
    <div style="display:flex; justify-content:center;">
        <table style="border-collapse:collapse;">
            <thead>
                <tr>
                    <th style="padding:{header_padding}; border:none; font-size:{header_font_size}; color:{HOME_TEAM_COLOR};">{home_col}</th>
                    <th style="padding:{header_padding}; border:none;"></th>
                    <th style="padding:{header_padding}; border:none; font-size:{header_font_size}; color:{AWAY_TEAM_COLOR};">{away_col}</th>
                </tr>
            </thead>
            <tbody>
                {rows_html}
            </tbody>
        </table>
    </div>
    """, unsafe_allow_html=True)


def _split_date_and_kickoff(date_str):
    """
    matches.match_date is either a full 'YYYY-MM-DD HH:MM' kickoff string
    (UK local time, when FotMob's own kickoff time was available - see
    save_report_to_db()'s docstring) or just a plain 'YYYY-MM-DD' fallback
    date. Splits whichever shape it is into (date_part, time_part), with
    time_part None when there wasn't a real kickoff time to begin with -
    used by the match report header so the kickoff time only shows up in
    parentheses when it's actually known, rather than showing a blank
    '()' for an older/date-only match.
    """
    if not date_str:
        return None, None
    s = str(date_str).strip()
    if " " in s:
        date_part, time_part = s.split(" ", 1)
        return date_part, time_part
    return s, None


def _render_match_detail(db, match_id):
    """
    The 'full uploaded match report' a Fixtures row's link opens - every
    stat already stored for ONE match (team totals, player stats, shots,
    Pass Map/Passes Received, Touch Map), built entirely from what's already
    in the database. There's no original .xlsx workbook kept anywhere (see
    this module's own docstring) - this is a from-scratch render of the
    same underlying data, not a re-download of the file that was uploaded.
    """
    fixtures = hdb.fetch_fixtures(db)
    row = fixtures[fixtures["match_id"] == match_id]
    if row.empty:
        st.error("Match not found.")
        if st.button("← Back to Fixtures"):
            st.query_params.clear()
            st.rerun()
        return
    row = row.iloc[0]

    if st.button("← Back to Fixtures"):
        st.query_params.clear()
        st.rerun()

    # Custom centered header (rather than st.title()/st.metric() columns) -
    # home/away team names bold in the same red/blue used for the pass
    # maps (pitch_viz.HOME_TEAM_COLOR/AWAY_TEAM_COLOR), score in bold
    # black between them, each side's xG in parentheses directly below its
    # own team/goal count in a smaller, non-bold font, then a blank line
    # before Competition (Matchweek)/Date (kickoff time)/Referee - all
    # center-aligned, none of that bottom block bold.
    home_team_name, away_team_name = row["Home Team"], row["Away Team"]
    score_display = row["Score"] if row["Score"] else "-"
    home_xg = f"{row['Home xG']:.2f}" if pd.notna(row["Home xG"]) else "-"
    away_xg = f"{row['Away xG']:.2f}" if pd.notna(row["Away xG"]) else "-"

    date_part, kickoff_part = _split_date_and_kickoff(row["Date"])
    competition_line = row["Competition"] or "-"
    if row["Matchweek"]:
        competition_line += f" (Matchweek {row['Matchweek']})"
    date_line = date_part or "-"
    if kickoff_part:
        date_line += f" ({kickoff_part})"
    referee_line = f"Referee: {row['Referee']}" if row["Referee"] else "Referee: -"

    st.markdown(f"""
    <div style="text-align:center;">
        <table style="margin:0 auto; border-collapse:collapse;">
            <tr>
                <td style="padding:0 18px; text-align:center;">
                    <span style="font-size:2.1em; font-weight:bold; color:{HOME_TEAM_COLOR};">{home_team_name}</span>
                </td>
                <td style="padding:0 18px; text-align:center;">
                    <span style="font-size:2.1em; font-weight:bold; color:black;">{score_display}</span>
                </td>
                <td style="padding:0 18px; text-align:center;">
                    <span style="font-size:2.1em; font-weight:bold; color:{AWAY_TEAM_COLOR};">{away_team_name}</span>
                </td>
            </tr>
            <tr>
                <td style="text-align:center;">
                    <span style="font-size:1.05em; font-weight:normal; color:#555;">({home_xg})</span>
                </td>
                <td></td>
                <td style="text-align:center;">
                    <span style="font-size:1.05em; font-weight:normal; color:#555;">({away_xg})</span>
                </td>
            </tr>
        </table>
        <div style="height:0.9em;"></div>
        <div style="font-weight:normal;">{competition_line}</div>
        <div style="font-weight:normal;">{date_line}</div>
        <div style="font-weight:normal;">{referee_line}</div>
    </div>
    """, unsafe_allow_html=True)

    mt_totals, mt_players, mt_shots, mt_advanced, mt_passmap, mt_passrecv, mt_touchmap = st.tabs(
        ["Team Totals", "Player Stats", "Shots", "Advanced Stats", "Pass Map", "Passes Received", "Touch Map"]
    )

    with mt_totals:
        match_summary = hdb.fetch_match_summary(db, match_id)
        if match_summary.empty:
            st.info("No team stats saved for this match.")
        else:
            home_col, metric_col, away_col = match_summary.columns
            _render_team_summary_table(match_summary, home_col, metric_col, away_col)

        # Full flattened stat breakdown (Possession/Passes/Tackles/Duels/
        # Physical performance/etc.) still available for anyone who wants
        # more than the summary table above - tucked away so it doesn't
        # compete with the compact Home/Metric/Away view for attention.
        with st.expander("All team stats"):
            team_stats = hdb.fetch_team_stats_for_match(db, match_id)
            if team_stats.empty:
                st.info("No team stats saved for this match.")
            else:
                st.dataframe(team_stats, use_container_width=False, hide_index=True,
                             height=_no_scroll_height(team_stats))

    with mt_players:
        home_team, away_team = row["Home Team"], row["Away Team"]

        # Each category is its own pair of home/away tables (rather than one
        # giant flattened table) - a dropdown picks which category shows,
        # matching the requested layout of one category at a time, home
        # team on top / away team below, each with a 'Team Total' row.
        _PLAYER_CATEGORY_FETCHERS = {
            "Scoring Stats": hdb.fetch_player_scoring_stats,
            "Possession": hdb.fetch_player_possession,
            "Passing": hdb.fetch_player_passing,
            "Defensive Actions": hdb.fetch_player_defensive_actions,
            "Defensive Action Locations": hdb.fetch_player_defensive_locations,
        }
        category = st.selectbox(
            "Category", list(_PLAYER_CATEGORY_FETCHERS.keys()), key="player_stats_category"
        )
        tables = _PLAYER_CATEGORY_FETCHERS[category](db, match_id, home_team, away_team)

        st.caption(home_team)
        if tables["home"].empty:
            st.info(f"No {category.lower()} saved for {home_team} in this match.")
        else:
            st.dataframe(tables["home"], use_container_width=False, hide_index=True,
                         height=_no_scroll_height(tables["home"]))

        st.caption(away_team)
        if tables["away"].empty:
            st.info(f"No {category.lower()} saved for {away_team} in this match.")
        else:
            st.dataframe(tables["away"], use_container_width=False, hide_index=True,
                         height=_no_scroll_height(tables["away"]))

        # Full flattened stat breakdown - every namespace, every player -
        # still available for anyone who wants more than the 5 category
        # tables above, same "tuck the old everything-view away" treatment
        # as the Team Totals tab above.
        with st.expander("All player stats"):
            player_stats = hdb.fetch_player_stats_for_match(db, match_id)
            if player_stats.empty:
                st.info("No player stats saved for this match.")
            else:
                st.dataframe(player_stats, use_container_width=False, hide_index=True,
                             height=_no_scroll_height(player_stats))

    with mt_shots:
        # Same output as the combined report's own "Shot Creating Actions"
        # tab (see fetch_shot_creating_actions()'s docstring) - one row per
        # shot, WhoScored's own shot list (Player/Distance/Body Part/SCA1/
        # SCA2) with FotMob's Minute/Added Time/xG/PSxG/Outcome/Situation
        # attached, split by team with a "Top 3 Shots by xG" caption under
        # each - rather than the DB's own raw shot-table shape.
        home_team, away_team = row["Home Team"], row["Away Team"]
        shots = hdb.fetch_shot_creating_actions(db, match_id)
        if shots.empty:
            st.info("No shots saved for this match.")
        else:
            for t in [home_team, away_team]:
                st.subheader(t)
                t_shots = shots[shots["Team"] == t].drop(columns=["Team"]).reset_index(drop=True)
                st.dataframe(t_shots, use_container_width=False, hide_index=True,
                             height=_no_scroll_height(t_shots))

    with mt_advanced:
        # 5 small Home/Metric/Away tables (Team Style, Shots, Expected
        # Goals, Duels, Physical - see history_db._ADVANCED_STATS_TABLES),
        # each in the same styling as the Team Totals tab's summary table
        # via the shared _render_team_summary_table() helper, just smaller.
        # Laid out up to 3 across per row (a second row picks up whatever's
        # left) rather than one long vertical list.
        advanced_tables = hdb.fetch_advanced_stats_tables(db, match_id)
        table_items = list(advanced_tables.items())
        for start in range(0, len(table_items), 3):
            row_chunk = table_items[start:start + 3]
            cols = st.columns(len(row_chunk))
            for col, (title, tdf) in zip(cols, row_chunk):
                with col:
                    st.markdown(
                        f"<div style='text-align:center; font-weight:bold; "
                        f"font-size:1.05em; margin-bottom:4px;'>{title}</div>",
                        unsafe_allow_html=True,
                    )
                    if tdf.empty:
                        st.info("No data saved for this match.")
                    else:
                        home_col, metric_col, away_col = tdf.columns
                        _render_team_summary_table(
                            tdf, home_col, metric_col, away_col,
                            header_font_size="1.0em", body_font_size="0.9em",
                            cell_padding="6px 14px", header_padding="8px 14px",
                        )

    matches_for_this_match = hdb.fetch_matches(db)
    matches_for_this_match = matches_for_this_match[matches_for_this_match["match_id"] == match_id]

    with mt_passmap:
        _render_pass_map(db, matches_for_this_match, mode="passer")

    with mt_passrecv:
        _render_pass_map(db, matches_for_this_match, mode="receiver")

    with mt_touchmap:
        _render_match_touchmap(db, match_id, row["Home Team"], row["Away Team"], row["Date"])


def _render_team_page(db, team, season=None):
    """
    The '?team=<name>' Team Page a League Table/Team Stats/Fixtures row's
    team-name link opens (see _linkify_team_cell()) - same "no multipage
    app, swap the whole page via a query param" pattern as
    _render_match_detail()'s '?match_id=<id>' (see this module's own
    docstring). There's no Team dropdown here - the link itself IS the team
    picker - just a small Season dropdown next to the title, for whenever
    more than one season's worth of data exists (today there's only one).

    season is whatever _team_page_url() baked into the link (currently
    always None, since that link never sets a season) - falls back to this
    team's most recent season if not given/not a real season.
    """
    seasons = hdb.fetch_available_seasons(db)
    if not seasons:
        st.info("No matches published yet.")
        return
    if season not in seasons:
        season = seasons[0]

    # Season dropdown rendered FIRST (even though it visually sits in the
    # right-hand column, to the title's right) so its current value is
    # known before the title text below is built - st.columns() controls
    # visual position, not code order, so this is safe.
    title_col, season_col = st.columns([6, 1])
    with season_col:
        season = st.selectbox(
            "Season", seasons, index=seasons.index(season),
            key="team_page_season", label_visibility="collapsed",
        )

    stats = hdb.fetch_team_page_stats(db, team, season=season)
    with title_col:
        title_season = stats["season"] if stats else season
        st.markdown(f'<div style="font-size:2em; font-weight:bold;">{title_season} {team}</div>',
                    unsafe_allow_html=True)

    if stats is None:
        st.info(f"No stats saved yet for {team} in {season}.")
        return

    record = f"{stats['w']}-{stats['d']}-{stats['l']}"
    home, away = stats["home"], stats["away"]
    home_record = f"{home['w']}-{home['d']}-{home['l']}"
    away_record = f"{away['w']}-{away['d']}-{away['l']}"
    rank_ordinal = hdb.ordinal(stats["league_rank"])

    st.markdown(f"""
    <div>
        <div style="margin-top:10px; font-size:1.05em;">
            <b>Record:</b> {record}, {stats['points']} points ({stats['ppg']:.2f} points per game),
            {rank_ordinal} in the {stats['competition']}
        </div>
        <div style="margin-top:4px; font-size:1.05em;">
            <b>Home Record:</b> {home_record}, {home['points']} points
            &nbsp;&nbsp;&nbsp;&nbsp;
            <b>Away Record:</b> {away_record}, {away['points']} points
        </div>
        <div style="margin-top:4px; font-size:1.05em;">
            <b>Goals:</b> {stats['goals_for']} ({stats['goals_for_pg']:.2f} per game)
            &nbsp;&nbsp;&nbsp;&nbsp;
            <b>Goals Against:</b> {stats['goals_against']} ({stats['goals_against_pg']:.2f} per game)
        </div>
    </div>
    """, unsafe_allow_html=True)

    st.markdown("<div style='height:1.4em;'></div>", unsafe_allow_html=True)

    possession_display = f"{stats['possession']:.1f}%" if stats["possession"] is not None else "-"
    headline_stats = [
        ("Avg Possession", possession_display, stats["possession_rank"]),
        ("Shots", f"{stats['shots_pg']:.2f}", stats["shots_rank"]),
        ("xG", f"{stats['xg_pg']:.2f}", stats["xg_rank"]),
        ("Shots Against", f"{stats['shots_against_pg']:.2f}", stats["shots_against_rank"]),
        ("xGA", f"{stats['xga_pg']:.2f}", stats["xga_rank"]),
    ]
    headline_cols = st.columns(len(headline_stats))
    for col, (label, value, rank) in zip(headline_cols, headline_stats):
        with col:
            rank_display = f"({hdb.ordinal(rank)})" if rank is not None else "-"
            st.markdown(f"""
            <div style="text-align:center;">
                <div style="font-size:1.3em; font-weight:bold;">{label}</div>
                <div style="font-size:1.7em; font-weight:bold; margin-top:6px;">{value}</div>
                <div style="font-size:0.85em; color:#666; margin-top:2px;">{rank_display}</div>
            </div>
            """, unsafe_allow_html=True)

    st.markdown("<div style='height:1.6em;'></div>", unsafe_allow_html=True)

    # Match-report tables, season-cumulative rather than one match's worth -
    # General Stats (né "Scoring Stats" - renamed since it now also carries
    # Age/Appearances/Starts, not just scoring numbers) is the first of
    # these; more categories (Possession, Passing, Defensive Actions, ...)
    # land here the same way later. Column labels are the exact same ones
    # the match report itself uses (see _SCORING_STATS_COLUMNS) - no
    # 'Total ...' relabeling, even though every number is now a season sum
    # rather than one match's.
    st.subheader("General Stats")
    scoring_stats = hdb.fetch_team_season_scoring_stats(db, team, season)
    if scoring_stats.empty:
        st.info(f"No scoring stats saved yet for {team} in {season}.")
    else:
        st.dataframe(scoring_stats, use_container_width=False, hide_index=True,
                     height=_no_scroll_height(scoring_stats))

    st.markdown("<div style='height:1.6em;'></div>", unsafe_allow_html=True)

    # Match log - this team's own slice of the Fixtures tab's table (same
    # columns, same renderer - see _render_fixtures_like_table()), oldest
    # match on top since fetch_team_match_log() is already ascending.
    st.subheader("Match Log")
    match_log = hdb.fetch_team_match_log(db, team, season)
    if match_log.empty:
        st.info(f"No matches saved yet for {team} in {season}.")
    else:
        _render_fixtures_like_table(match_log)


# ============================================================
# Dispatch: a "?match_id=..." query param (set by clicking a Fixtures row's
# "Report" link) swaps the WHOLE page over to the single-match detail view,
# and a "?team=..." query param (set by clicking any team-name link - see
# _linkify_team_cell()) swaps it over to that team's Team Page instead -
# both instead of the normal tabbed dashboard. See this module's own
# docstring for why a query param rather than a real second page.
# ============================================================
_match_id_param = st.query_params.get("match_id")
_team_param = st.query_params.get("team")

if _match_id_param:
    _render_match_detail(db, _match_id_param)
elif _team_param:
    _render_team_page(db, _team_param)
else:
    st.title("Match History Dashboard")

    (tab_team_totals, tab_fixtures, tab_team, tab_player, tab_shots, tab_passmap,
     tab_passrecv, tab_season_passmap, tab_season_passrecv, tab_season_touchmap) = st.tabs([
        "Team Totals", "Fixtures", "Team Trends", "Player Trends", "Shots", "Pass Map",
        "Passes Received", "Season Pass Map", "Season Passes Received", "Season Touch Map",
    ])

    with tab_team_totals:
        league_table = hdb.fetch_league_table(db)
        if league_table.empty:
            st.info(
                "No completed match results saved yet - publish at least one match with 'Save to "
                "Database' first."
            )
        else:
            st.subheader("League Table")
            # Rendered via _render_data_table_html() (rather than
            # st.dataframe) so the Team column can be a real link to that
            # team's Team Page (see _linkify_team_cell()) - st.dataframe's
            # own LinkColumn can't show a clean, human-readable team name as
            # the link text (only a fixed string or a regex pulled straight
            # from the url-encoded URL itself).
            _render_data_table_html(league_table, link_columns=("Team",))

        st.subheader("Team Stats")
        totals_category = st.selectbox(
            "Category", ["Shots", "Passing", "Touches", "Defensive Actions",
                         "Defensive Action Location"],
            key="team_totals_category"
        )
        if totals_category == "Shots":
            shot_for_df, shot_against_df = hdb.fetch_season_shot_totals(db)
            if shot_for_df.empty and shot_against_df.empty:
                st.info("No shots saved yet - publish at least one match with 'Save to Database' first.")
            else:
                def _shot_side_totals(df, suffix):
                    out_cols = ["Team", f"Shots {suffix}", f"Goals {suffix}", f"xG {suffix}"]
                    if df.empty:
                        return pd.DataFrame(columns=out_cols)
                    agg = df.groupby("Team")[["Shots", "Goals", "Total xG"]].sum().reset_index()
                    agg = agg.rename(columns={"Shots": f"Shots {suffix}", "Goals": f"Goals {suffix}",
                                               "Total xG": f"xG {suffix}"})
                    agg[f"xG {suffix}"] = agg[f"xG {suffix}"].round(2)
                    return agg

                shot_totals = _shot_side_totals(shot_for_df, "For").merge(
                    _shot_side_totals(shot_against_df, "Against"), on="Team", how="outer"
                ).fillna(0)
                for c in shot_totals.columns:
                    if c != "Team":
                        shot_totals[c] = shot_totals[c].astype(float if "xG" in c else int)
                shot_totals = shot_totals.sort_values("Shots For", ascending=False).reset_index(drop=True)
                # Team column linked to that team's Team Page - see the
                # League Table above for why this uses _render_data_table_html()
                # rather than st.dataframe.
                _render_data_table_html(shot_totals, link_columns=("Team",))
        elif totals_category == "Passing":
            passing_totals = hdb.fetch_season_passing_totals(db)
            if passing_totals.empty:
                st.info("No passing stats saved yet - publish at least one match with 'Save to Database' first.")
            else:
                _render_data_table_html(passing_totals, link_columns=("Team",))
        elif totals_category == "Touches":
            touches_totals = hdb.fetch_season_touches_totals(db)
            if touches_totals.empty:
                st.info(
                    "No touch data saved yet. This needs matches saved AFTER the touches table was "
                    "added - re-run 'Save to Database' on your matches in the combined report app to "
                    "backfill it."
                )
            else:
                _render_data_table_html(touches_totals, link_columns=("Team",))
                st.caption(
                    "Progressive Carries, Carries into Final Third/Box, and Passes Received show 0 for "
                    "matches saved before this stat was added to the database - re-save an older match "
                    "in the combined report app to backfill it. Total Touches/thirds/Attacking Box are "
                    "unaffected and cover every match with saved touch data."
                )
        elif totals_category == "Defensive Actions":
            defensive_totals = hdb.fetch_season_defensive_totals(db)
            if defensive_totals.empty:
                st.info(
                    "No defensive stats saved yet - publish at least one match with 'Save to Database' "
                    "first."
                )
            else:
                _render_data_table_html(defensive_totals, link_columns=("Team",))
        elif totals_category == "Defensive Action Location":
            defensive_location_totals = hdb.fetch_season_defensive_location_totals(db)
            if defensive_location_totals.empty:
                st.info(
                    "No defensive action location data saved yet. This namespace was added partway "
                    "through this project - re-run 'Save to Database' on your matches in the combined "
                    "report app to backfill it."
                )
            else:
                _render_data_table_html(defensive_location_totals, link_columns=("Team",))

    with tab_fixtures:
        fixtures = hdb.fetch_fixtures(db)
        if fixtures.empty:
            st.info("No matches published yet - run the combined report app and use 'Save to Database'.")
        else:
            # Three independent filters, each defaulting to "All" so the tab
            # opens showing every match exactly as before - League exists
            # mainly for whenever more than one competition gets saved here
            # (matches.competition is the free-text field on the "Save to
            # Database" form, so it already supports that; today it's
            # basically always "Premier League"). Matchweek/Season are None
            # for matches saved before those fields existed (Matchweek is a
            # genuinely new scraped field - see fotmob_report.extract_
            # matchweek() - so needs a re-save to backfill; Season is
            # derived from match_date, which every match already has, so it
            # never needs backfilling).
            filter_cols = st.columns(3)

            def _matchweek_sort_key(w):
                try:
                    return (0, int(w))
                except (ValueError, TypeError):
                    return (1, str(w))

            with filter_cols[0]:
                seasons = sorted(fixtures["Season"].dropna().unique(), reverse=True)
                season_choice = st.selectbox("Season", ["All seasons"] + seasons, key="fixtures_season")
            with filter_cols[1]:
                leagues = sorted(fixtures["Competition"].dropna().unique())
                league_choice = st.selectbox("League", ["All leagues"] + leagues, key="fixtures_league")
            with filter_cols[2]:
                weeks = sorted(fixtures["Matchweek"].dropna().unique(), key=_matchweek_sort_key)
                week_choice = st.selectbox("Matchweek", ["All matchweeks"] + weeks, key="fixtures_matchweek")

            scoped = fixtures
            if week_choice != "All matchweeks":
                scoped = scoped[scoped["Matchweek"] == week_choice]
            if league_choice != "All leagues":
                scoped = scoped[scoped["Competition"] == league_choice]
            if season_choice != "All seasons":
                scoped = scoped[scoped["Season"] == season_choice]

            if scoped.empty:
                st.info("No matches match this filter.")
            else:
                _render_fixtures_like_table(scoped)
                st.caption(f"{len(scoped)} of {len(fixtures)} match(es) shown.")

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
                st.dataframe(trends, use_container_width=True, hide_index=True)
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
                    st.dataframe(trends, use_container_width=True, hide_index=True)
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
        for_df, against_df = hdb.fetch_season_shot_totals(db)
        if for_df.empty and against_df.empty:
            st.info("No shots saved yet - publish at least one match with 'Save to Database' first.")
        else:
            situations_present = sorted(
                (set(for_df["Situation"]) | set(against_df["Situation"])) - _HIDDEN_SITUATIONS
            )
            situation_options = ["All situations"] + situations_present
            chosen = st.selectbox(
                "Situation",
                situation_options,
                format_func=lambda s: s if s == "All situations" else _situation_display_name(s),
            )

            def _team_totals_for(df):
                scoped = df if chosen == "All situations" else df[df["Situation"] == chosen]
                if scoped.empty:
                    return pd.DataFrame(columns=["Team", "Shots", "Goals", "Total xG"])
                out = (scoped.groupby("Team")[["Shots", "Goals", "Total xG"]]
                       .sum()
                       .reset_index()
                       .sort_values("Total xG", ascending=False)
                       .reset_index(drop=True))
                out["Total xG"] = out["Total xG"].round(2)
                return out

            col1, col2 = st.columns(2)
            with col1:
                st.subheader("For")
                # Team column linked to that team's Team Page - see the
                # League Table/Team Stats/Fixtures tables for the same
                # treatment (_render_data_table_html()/_linkify_team_cell()).
                _render_data_table_html(_team_totals_for(for_df), link_columns=("Team",))
            with col2:
                st.subheader("Against")
                _render_data_table_html(_team_totals_for(against_df), link_columns=("Team",))

    with tab_passmap:
        _render_pass_map(db, hdb.fetch_matches(db), mode="passer")

    with tab_passrecv:
        _render_pass_map(db, hdb.fetch_matches(db), mode="receiver")

    with tab_season_passmap:
        _render_season_pass_map(db, mode="passer")

    with tab_season_passrecv:
        _render_season_pass_map(db, mode="receiver")

    with tab_season_touchmap:
        _render_season_touchmap(db)
