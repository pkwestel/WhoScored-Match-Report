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
import os
import traceback

import matplotlib.pyplot as plt
import matplotlib.image as mpimg
import matplotlib.patheffects as path_effects
from matplotlib.patches import Arc, Rectangle
import streamlit as st

import whoscored_report as wr

st.set_page_config(page_title="WhoScored Match Report", layout="wide")
st.title("WhoScored Match Report Generator")
st.write(
    "Paste a WhoScored match-centre URL below to generate Totals, Touches, "
    "Passing, Shot Creating Actions, Progressive Passes tables, and a "
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

            wb = wr.build_workbook(
                sca_out, team_summary, player_third, passing_out, totals_out, defensive_actions,
                defensive_action_location, passing_pairs, home_name, away_name, against_totals,
                shot_pairs,
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
                "wb_bytes": buf.getvalue(),
                "filename": filename,
                "n_events": len(df),
            }

        except Exception as e:
            st.error(f"Something went wrong: {e}")
            st.code(traceback.format_exc())

# ---------------------------------------------------------------------------
# Pass Map pitch drawing (matplotlib, real 105m x 68m pitch dimensions -
# same PITCH_LEN_M/PITCH_WID_M convention whoscored_report.py uses elsewhere
# - Opta's 0-100 normalized x/y are converted to metres before plotting so
# the pitch/center circle/boxes render with correct real-world proportions).
# Orientation: vertical, attacking goal at the TOP of the image - the
# horizontal axis is pitch WIDTH (from normalized y), the vertical axis is
# pitch LENGTH (from normalized x, since higher x is always a team's own
# attacking direction per WhoScored/Opta convention already used elsewhere
# in whoscored_report.py).
# ---------------------------------------------------------------------------
PITCH_LIGHT = "#eeeeee"
PITCH_DARK = "#dcdcdc"
# The area outside the pitch's own black touchlines - matches PITCH_DARK
# exactly (rather than a separate near-white shade) so the striping reads as
# fully contained inside the black lines, with one clean solid color outside
# them, instead of a third tone that made the pitch boundary look blurry.
MAIN_BG = PITCH_DARK
N_PITCH_STRIPES = 12  # alternating bands, stacked along the pitch LENGTH (horizontal rows)
PITCH_LINE_COLOR = "#1a1a1a"  # near-black
PITCH_LINE_WIDTH = 2.4
TITLE_COLOR = "#1a1a1a"  # near-black, matches the pitch lines
PITCH_PAD_M = 1.3  # metres of padding around the pitch boundary in the plot

PASS_CATEGORY_COLORS = {
    "Completed": "#4a4a4a",       # dark charcoal - the primary category, meant to read as the boldest line
    "Incomplete": "#d9754a",      # darker dusty orange - a bit more visible, still lighter than Completed
    "Progressive": "#2f9bf0",     # sky blue
    "Key Pass": "#7b1fa2",        # bold purple - gold didn't read against a light pitch, this does
}
# Per-category line weight/opacity - Completed is drawn thicker and fully
# opaque so it dominates (it's the primary category); Incomplete is thinner
# and more transparent so it recedes rather than competing with it; Key Pass
# stays the boldest since it's the rarest/most important category.
PASS_CATEGORY_STYLE = {
    "Completed": {"lw": 2.0, "alpha": 1.0},
    "Incomplete": {"lw": 1.4, "alpha": 0.7},
    "Progressive": {"lw": 1.8, "alpha": 0.9},
    "Key Pass": {"lw": 2.6, "alpha": 1.0},
}
# Draw order matters - later categories are drawn on top, so the rarer/more
# important ones (Progressive, then Key Pass) stay visible even when a lot
# of ordinary completed passes overlap them.
PASS_CATEGORY_DRAW_ORDER = ["Incomplete", "Completed", "Progressive", "Key Pass"]

# Title/subtitle/legend font (per request - Arial specifically, not
# matplotlib's default DejaVu Sans). Assumes Arial is actually installed on
# whatever machine renders this (true by default on Windows/macOS; on Linux
# it may not be, in which case matplotlib silently falls back to its default
# rather than erroring).
PASS_MAP_FONT = "Arial"

# Small watermark-style logo drawn in the pass map's top-left corner. Kept
# next to this file (same folder) so a relative path works regardless of
# the machine's working directory - if it's ever missing, the logo is just
# skipped rather than crashing the whole Pass Map render.
LOGO_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "kwest_thoughts_logo_v2.png")


def _load_logo():
    """
    Read the logo image fresh each call - NOT cached across reruns. (An
    earlier version cached the result in a module-level dict; if the very
    first load failed - e.g. before the file existed locally - that failure
    was cached forever for the life of the Streamlit process, so the logo
    would stay missing even after the file was added, until a full server
    restart. Re-reading a small PNG each render is cheap, so there's no real
    cost to just always trying again.)

    Returns None (and shows a one-time warning with the exact path tried) if
    the file is missing/unreadable, so a bad path is visible instead of
    silently doing nothing.
    """
    try:
        return mpimg.imread(LOGO_PATH)
    except Exception as e:
        st.warning(f"Pass Map logo not found/readable at {LOGO_PATH} ({e}) - skipping it.")
        return None


def _to_m_x(x):
    """Normalized (0-100) x -> metres along the pitch LENGTH (vertical axis)."""
    return x / 100.0 * wr.PITCH_LEN_M


def _to_m_y(y):
    """Normalized (0-100) y -> metres along the pitch WIDTH (horizontal axis)."""
    return y / 100.0 * wr.PITCH_WID_M


def draw_pitch(ax):
    length, width = wr.PITCH_LEN_M, wr.PITCH_WID_M
    lc, lw = PITCH_LINE_COLOR, PITCH_LINE_WIDTH
    pad = PITCH_PAD_M

    # The area outside the pitch boundary (the padding margin) gets its own
    # flat background color - the striping below is confined exactly to the
    # [0, width] x [0, length] pitch rectangle, not the padded area around it.
    ax.set_facecolor(MAIN_BG)

    # Alternating horizontal bands (light/dark gray), stacked along the
    # pitch LENGTH so each band runs the full WIDTH of the field - sized to
    # exactly tile [0, length], so they stay confined within the pitch's own
    # boundary lines rather than bleeding into the padding margin.
    stripe_h = length / N_PITCH_STRIPES
    for i in range(N_PITCH_STRIPES):
        color = PITCH_LIGHT if i % 2 == 0 else PITCH_DARK
        ax.add_patch(Rectangle((0, i * stripe_h), width, stripe_h,
                                color=color, lw=0, zorder=0))

    # Outer boundary: horizontal extent = width, vertical extent = length.
    ax.add_patch(Rectangle((0, 0), width, length, fill=False, color=lc, lw=lw, zorder=1))
    # Halfway line
    ax.plot([0, width], [length / 2, length / 2], color=lc, lw=lw, zorder=1)

    center_x = width / 2
    ax.add_patch(plt.Circle((center_x, length / 2), 9.15, fill=False, color=lc, lw=lw, zorder=1))
    ax.add_patch(plt.Circle((center_x, length / 2), 0.35, color=lc, zorder=1))

    box_half = 20.16
    six_half = 9.16
    # direction=1: own goal at the bottom (y=0), box opens upward.
    # direction=-1: attacking goal at the top (y=length), box opens downward.
    for y0, direction in [(0, 1), (length, -1)]:
        py = y0 if direction == 1 else y0 - 16.5
        ax.add_patch(Rectangle((center_x - box_half, py), box_half * 2, 16.5,
                                fill=False, color=lc, lw=lw, zorder=1))
        sy = y0 if direction == 1 else y0 - 5.5
        ax.add_patch(Rectangle((center_x - six_half, sy), six_half * 2, 5.5,
                                fill=False, color=lc, lw=lw, zorder=1))
        spot_y = y0 + direction * 11
        ax.add_patch(plt.Circle((center_x, spot_y), 0.35, color=lc, zorder=1))
        # Penalty arc ("D") - only the portion outside the box, bulging away
        # from the goal line (upward for the bottom box, downward for the top).
        theta1, theta2 = (37, 143) if direction == 1 else (217, 323)
        ax.add_patch(Arc((center_x, spot_y), 18.3, 18.3, angle=0, theta1=theta1, theta2=theta2,
                          color=lc, lw=lw, zorder=1))
        # Goal (drawn just outside the pitch boundary)
        goal_y = -2 if direction == 1 else length
        ax.add_patch(Rectangle((center_x - 3.66, goal_y), 7.32, 2,
                                fill=False, color=lc, lw=lw, zorder=1))

    ax.set_xlim(-pad, width + pad)
    ax.set_ylim(-pad, length + pad)
    ax.set_aspect("equal")
    ax.axis("off")

    # Watermark - positioned in pitch DATA coordinates (not axes-fraction),
    # centered on the pitch's horizontal midline (which is also the
    # horizontal center of the whole figure, since this axes sits
    # symmetrically within it). Placed in the MIDDLE of one of the LIGHTER
    # stripe bands (stripe index 4, 0-indexed from the bottom - always light
    # since PITCH_LIGHT is used at even indices), using PITCH_DARK's own
    # color for the text - so it reads as a subtle tonal shift in the turf
    # itself (dark-on-light here, the inverse of light-on-dark) rather than
    # a bold overlay competing with the passes.
    watermark_x = center_x
    watermark_y = 4.5 * stripe_h
    # zorder=1.5 - above the striping (0) and pitch markings (1), but below
    # the pass lines (2) and their endpoint markers (3), so passes drawn on
    # top of the pitch visually cover the watermark where they cross it,
    # same as they'd cover any other part of the pitch background.
    watermark_txt = ax.text(watermark_x, watermark_y, "@pkwestel", ha="center", va="center",
                             color=PITCH_DARK, fontsize=20, fontweight="bold", alpha=1.0, zorder=1.5)
    # A same-color stroke around each glyph, rather than relying on the fill
    # alone - at this contrast level (PITCH_DARK vs PITCH_LIGHT is a fairly
    # subtle 18-unit gray-on-gray difference) the anti-aliased edge pixels of
    # thin lettering otherwise make it read as softer/more "see-through"
    # than a true opaque cutout, even at alpha=1.0. Thickening the shape this
    # way (still the exact same PITCH_DARK color, no new tone introduced)
    # makes it read as solid.
    watermark_txt.set_path_effects([path_effects.withStroke(linewidth=1.8, foreground=PITCH_DARK)])


def plot_pass_map(passes_df, player_name, home_name, away_name, stat_items, title_suffix="Pass Map"):
    """
    Shared drawing code for both the outgoing Pass Map and the Passes
    Received map - same pitch/logo/watermark, same per-category coloring
    (passes_df just needs 'category', 'x', 'y', 'endX', 'endY' columns).
    title_suffix distinguishes the two ("Pass Map" vs "Passes Received") in
    the figure's title. stat_items is a caller-built list of (text, color)
    tuples for the stat line below the pitch, since the two charts don't
    track the same stats (e.g. "Completion %" doesn't apply to a received-
    passes view, where everything shown is already complete by definition).
    """
    length, width = wr.PITCH_LEN_M, wr.PITCH_WID_M
    pad = PITCH_PAD_M
    # Figure size matches the pitch's real aspect ratio exactly, so there's
    # no leftover whitespace inside the axes from a mismatched box shape -
    # this is what actually "zooms in": the pitch fills the whole frame.
    # Title/subtitle/stat-line and the color key get small, fixed-inch
    # margins (not a fraction of the whole figure) so there's no dead space
    # between the pitch and any of them.
    pitch_h = 10.0
    top_pad_in = 0.8    # suptitle + subtitle only (no key, no stat line up here anymore)
    bottom_pad_in = 0.5  # one row of stat-line text, below the pitch (no key anymore)
    fig_h = pitch_h + top_pad_in + bottom_pad_in
    fig_w = pitch_h * (width + 2 * pad) / (length + 2 * pad)
    fig = plt.figure(figsize=(fig_w, fig_h))
    fig.patch.set_facecolor(MAIN_BG)

    bottom_frac = bottom_pad_in / fig_h
    axes_h_frac = pitch_h / fig_h
    ax = fig.add_axes([0.03, bottom_frac, 0.94, axes_h_frac])
    draw_pitch(ax)

    # Small logo, top-left corner of the whole figure (fixed inches, not a
    # fraction of the figure, so its size stays "small" regardless of the
    # pitch's own aspect ratio).
    logo_img = _load_logo()
    if logo_img is not None:
        logo_h_in = 0.65
        margin_in = 0.12
        aspect = logo_img.shape[1] / logo_img.shape[0]  # width / height, in pixels
        logo_w_in = logo_h_in * aspect
        logo_ax = fig.add_axes([
            margin_in / fig_w,
            1 - (margin_in + logo_h_in) / fig_h,
            logo_w_in / fig_w,
            logo_h_in / fig_h,
        ])
        logo_ax.imshow(logo_img)
        logo_ax.axis("off")

    for category in PASS_CATEGORY_DRAW_ORDER:
        cat_passes = passes_df[passes_df["category"] == category]
        color = PASS_CATEGORY_COLORS[category]
        style = PASS_CATEGORY_STYLE[category]
        lw, alpha = style["lw"], style["alpha"]
        for _, p in cat_passes.iterrows():
            x0, y0 = _to_m_y(p["y"]), _to_m_x(p["x"])
            x1, y1 = _to_m_y(p["endY"]), _to_m_x(p["endX"])
            # Circle-with-tail style: a plain line (no arrowhead) traces the
            # pass, with an open circle marking where it ENDED.
            ax.plot([x0, x1], [y0, y1], color=color, lw=lw, alpha=alpha,
                    solid_capstyle="round", zorder=2)
            ax.scatter([x1], [y1], s=38, facecolors="white", edgecolors=color,
                       linewidths=1.3, alpha=alpha, zorder=3)

    fig.suptitle(f"{player_name} - {title_suffix}", color=TITLE_COLOR, fontsize=17,
                 fontweight="bold", fontname=PASS_MAP_FONT, y=1 - 0.22 / fig_h)
    subtitle_y = 1 - 0.65 / fig_h
    fig.text(0.5, subtitle_y, f"{home_name} vs {away_name}", color=TITLE_COLOR, fontsize=12,
              fontname=PASS_MAP_FONT, ha="center")

    # Stat line: moved below the pitch (per request - the color key that
    # used to live down here is gone entirely). Caller decides exactly which
    # stats/colors go here (see docstring above).
    stats_y = 0.22 / fig_h
    n = len(stat_items)
    for i, (text, color) in enumerate(stat_items):
        x = (i + 0.5) / n
        fig.text(x, stats_y, text, color=color, fontsize=11.5, fontweight="bold",
                  fontname=PASS_MAP_FONT, ha="center", va="center")

    return fig


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

    st.success(f"Scraped {report['n_events']} events — {home_name} vs {away_name}")

    st.download_button(
        label=f"Download {report['filename']}",
        data=report["wb_bytes"],
        file_name=report["filename"],
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

    tab0, tabA, tab1, tab2, tab3, tab4, tab5, tab6, tab7, tabP, tabPR = st.tabs(
        ["Totals", "Against", "Touches", "Passing", "Shot Creating Actions",
         "Defensive Actions", "Defensive Action Location", "Passing Pairs", "Shot Pairs",
         "Pass Map", "Passes Received"]
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
