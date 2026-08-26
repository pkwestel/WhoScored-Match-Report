"""
pitch_viz.py
============
Shared pass-map pitch drawing (matplotlib) - the exact same visual used by
both streamlit_app.py's own Pass Map/Passes Received tabs and
dashboard_app.py's read-only versions of the same charts. Pulled out into
its own module (rather than living inside streamlit_app.py, or being copy-
pasted into dashboard_app.py) for one reason: streamlit_app.py has a lot of
its own top-level Streamlit UI calls (st.set_page_config, st.title,
st.text_input, ...) that run immediately on import - importing streamlit_app
itself from dashboard_app.py would execute all of that too, which is not
what you want. This module has no top-level UI code at all, just constants
and drawing functions, so both apps can import it safely.

Real 105m x 68m pitch dimensions (same PITCH_LEN_M/PITCH_WID_M convention
whoscored_report.py uses elsewhere) - Opta's 0-100 normalized x/y are
converted to metres before plotting so the pitch/center circle/boxes render
with correct real-world proportions. Orientation: vertical, attacking goal
at the TOP of the image - the horizontal axis is pitch WIDTH (from
normalized y), the vertical axis is pitch LENGTH (from normalized x, since
higher x is always a team's own attacking direction per WhoScored/Opta
convention already used elsewhere in whoscored_report.py).

NOTE on NOT importing whoscored_report.py here: this module used to do
`import whoscored_report as wr` purely to read its PITCH_LEN_M/PITCH_WID_M
constants. That's a trap for dashboard_app.py specifically - it's the one
app in this project deliberately built with NO scraping code, meant to
deploy cleanly to Streamlit Community Cloud. But whoscored_report.py itself
imports selenium (and, transitively, utils/driver.py's own dependencies
like fake_useragent) at the top of the file, purely to support its scraper -
none of that is ever actually used here. Importing whoscored_report.py from
this module meant dashboard_app.py silently required the ENTIRE scraper
dependency stack just to draw a pitch, which surfaced as a real
ModuleNotFoundError in production (fake_useragent, found missing only after
deploying - and the next transitive import after that would've been next).
Duplicating these two float constants directly below removes that entire
dependency chain for good, rather than playing whack-a-mole with
requirements.txt one missing package at a time.
"""

import os

import numpy as np
import matplotlib.pyplot as plt
import matplotlib.image as mpimg
import matplotlib.patheffects as path_effects
import matplotlib.colors as mcolors
from matplotlib.patches import Arc, Rectangle
import streamlit as st

# Duplicated from whoscored_report.py (see the module docstring above for
# why) - keep these in sync if the real pitch dimensions ever change there.
PITCH_LEN_M = 105.0
PITCH_WID_M = 68.0

PITCH_LIGHT = "#eeeeee"
PITCH_DARK = "#dcdcdc"
# The area outside the pitch's own black touchlines, AND the whole figure's
# background behind the title/subtitle/stat-line - plain white, per request.
# The striping INSIDE the pitch boundary (the PITCH_LIGHT/PITCH_DARK bands
# below) is untouched by this - only the surrounding page background changed.
MAIN_BG = "#ffffff"
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

# Touch map colormap - reuses colors already established elsewhere in this
# palette (the Progressive blue and Incomplete orange from PASS_CATEGORY_
# COLORS, plus the Key Pass purple as the "hottest" end) rather than
# introducing an unrelated new hue just for this one chart. set_bad() makes
# masked-out (low-density) cells fully transparent, so the density shading
# only tints the pitch where touches are actually concentrated - see
# plot_touch_map()'s masking below for why that matters.
TOUCHMAP_CMAP = mcolors.LinearSegmentedColormap.from_list(
    "kwest_heat", ["#2f9bf0", "#d9754a", "#7b1fa2"]
)
TOUCHMAP_CMAP.set_bad(alpha=0)
TOUCHMAP_POINT_COLOR = "#7b1fa2"  # fallback scatter color when there's too little data for a real KDE

# Home/away team name colors for the subtitle line under the main title
# (e.g. "Arsenal vs Chelsea") - per request, so the two team names read as
# clearly distinct at a glance rather than one flat color.
HOME_TEAM_COLOR = "#DC143C"  # Crimson
AWAY_TEAM_COLOR = "#1E90FF"  # Dodger Blue - more saturated/vivid than Cornflower Blue, still pops on white

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
    return x / 100.0 * PITCH_LEN_M


def _to_m_y(y):
    """
    Normalized (0-100) y -> metres along the pitch WIDTH (horizontal axis).

    Flipped (100 - y) rather than used directly: WhoScored/Opta's x AND y
    are both given from the team's OWN attacking perspective (a full
    180-degree rotation per team/per half, not just an x-only mirror - see
    this module's docstring on x), matching the standard "imagine you're
    the coach, standing behind your own goal, facing the way your team is
    attacking" analytics convention. Under that convention, a team's real
    right side should render on the image's right when the chart is drawn
    vertically with their attack going up - which requires this flip.
    Confirmed against a real match where a player's well-known real-life
    wing (right) was rendering on the wrong side (image-left) before this
    fix - every Pass Map/Passes Received/Touch Map chart drawn before this
    was mirrored left-right, for every player, on both teams.
    """
    return (100.0 - y) / 100.0 * PITCH_WID_M


def draw_pitch(ax):
    length, width = PITCH_LEN_M, PITCH_WID_M
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


def _draw_split_subtitle(fig, y, parts, fontsize=14):
    """
    Draws one horizontally-centered line of text made of several colored
    pieces (e.g. the home team's name, then " vs ", then the away team's
    name, each in its own color) - matplotlib has no built-in way to color
    parts of a single Text object differently, so each piece gets its own
    fig.text() call, measured with the figure's renderer and then
    repositioned side-by-side so the whole line still reads as one
    centered unit rather than each piece being independently centered.
    parts is a list of (text, color) tuples; every piece is drawn bold in
    PASS_MAP_FONT at the given fontsize.
    """
    fig.canvas.draw()  # ensures a renderer exists, sized to this exact figure
    renderer = fig.canvas.get_renderer()
    texts, widths = [], []
    for text, color in parts:
        t = fig.text(0, y, text, color=color, fontsize=fontsize, fontweight="bold",
                      fontname=PASS_MAP_FONT, ha="left", va="center")
        bbox = t.get_window_extent(renderer)
        widths.append(bbox.width / fig.bbox.width)
        texts.append(t)
    total_width = sum(widths)
    cur_x = 0.5 - total_width / 2
    for t, w in zip(texts, widths):
        t.set_x(cur_x)
        cur_x += w
    return texts


def plot_pass_map(passes_df, player_name, home_name, away_name, stat_items, title_suffix="Pass Map",
                   subtitle=None):
    """
    Shared drawing code for both the outgoing Pass Map and the Passes
    Received map - same pitch/logo/watermark, same per-category coloring
    (passes_df just needs 'category', 'x', 'y', 'endX', 'endY' columns).
    title_suffix distinguishes the two ("Pass Map" vs "Passes Received") in
    the figure's title. stat_items is a caller-built list of (text, color)
    tuples for the stat line below the pitch, since the two charts don't
    track the same stats (e.g. "Completion %" doesn't apply to a received-
    passes view, where everything shown is already complete by definition).

    subtitle overrides the default "{home_name} vs {away_name}" subtitle -
    pass home_name=None, away_name=None, subtitle="Season - N matches" for a
    season-long map aggregated across several matches, where there's no one
    fixture to name (see dashboard_app.py's season tabs). When the default
    subtitle IS used (subtitle=None with both team names given), the home
    team's name is drawn in HOME_TEAM_COLOR (Crimson) and the away team's in
    AWAY_TEAM_COLOR (Powder Blue) rather than one flat color - an explicit
    subtitle string is always drawn as plain bold text, since there's no
    single home/away pair to color in a season-aggregated view.
    """
    length, width = PITCH_LEN_M, PITCH_WID_M
    pad = PITCH_PAD_M
    # Figure size matches the pitch's real aspect ratio exactly, so there's
    # no leftover whitespace inside the axes from a mismatched box shape -
    # this is what actually "zooms in": the pitch fills the whole frame.
    # Title/subtitle/stat-line and the color key get small, fixed-inch
    # margins (not a fraction of the whole figure) so there's no dead space
    # between the pitch and any of them.
    pitch_h = 10.0
    top_pad_in = 0.95   # suptitle + subtitle only (no key, no stat line up here anymore) - bumped
                        # up from 0.8 to give the now-larger title/subtitle text more headroom
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

    fig.suptitle(f"{player_name} - {title_suffix}", color=TITLE_COLOR, fontsize=20,
                 fontweight="bold", fontname=PASS_MAP_FONT, y=1 - 0.28 / fig_h)
    subtitle_y = 1 - 0.78 / fig_h
    if subtitle is None and home_name and away_name:
        # Default case: color the home/away team names distinctly rather
        # than one flat-colored "{home} vs {away}" string.
        _draw_split_subtitle(fig, subtitle_y, [
            (home_name, HOME_TEAM_COLOR), (" vs ", TITLE_COLOR), (away_name, AWAY_TEAM_COLOR),
        ])
    else:
        if subtitle is None:
            subtitle = f"{home_name} vs {away_name}"
        fig.text(0.5, subtitle_y, subtitle, color=TITLE_COLOR, fontsize=14, fontweight="bold",
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


def plot_touch_map(touches_df, player_name, home_name=None, away_name=None,
                    title_suffix="Touch Map", subtitle=None, stat_items=None):
    """
    Touch map: every (x, y) touch location in touches_df plotted on the
    pitch, shaded with a smoothed density estimate (scipy's gaussian_kde,
    evaluated on a grid and drawn with imshow) when there's enough data to
    support one, on the same pitch/logo/watermark as plot_pass_map() for
    visual consistency.
    touches_df needs 'x' and 'y' columns, normalized 0-100 (whoscored_
    report.compute_all_touches()'s own output, or history_db.fetch_touches()'s -
    single match or, with rows from several match_ids concatenated
    together, a season-long map over the exact same pitch).

    home_name/away_name build the default "{home} vs {away}" subtitle for
    a single match; pass subtitle= directly instead for a season view
    (e.g. "Season - 12 matches"), where there's no one fixture to name.
    stat_items defaults to a single "N Touches" stat if not given - pass
    your own list of (text, color) tuples for anything more specific.
    """
    length, width = PITCH_LEN_M, PITCH_WID_M
    pad = PITCH_PAD_M
    pitch_h = 10.0
    top_pad_in = 0.95   # bumped up from 0.8 to give the now-larger title/subtitle text more headroom
    bottom_pad_in = 0.5
    fig_h = pitch_h + top_pad_in + bottom_pad_in
    fig_w = pitch_h * (width + 2 * pad) / (length + 2 * pad)
    fig = plt.figure(figsize=(fig_w, fig_h))
    fig.patch.set_facecolor(MAIN_BG)

    bottom_frac = bottom_pad_in / fig_h
    axes_h_frac = pitch_h / fig_h
    ax = fig.add_axes([0.03, bottom_frac, 0.94, axes_h_frac])
    draw_pitch(ax)

    logo_img = _load_logo()
    if logo_img is not None:
        logo_h_in = 0.65
        margin_in = 0.12
        aspect = logo_img.shape[1] / logo_img.shape[0]
        logo_w_in = logo_h_in * aspect
        logo_ax = fig.add_axes([
            margin_in / fig_w,
            1 - (margin_in + logo_h_in) / fig_h,
            logo_w_in / fig_w,
            logo_h_in / fig_h,
        ])
        logo_ax.imshow(logo_img)
        logo_ax.axis("off")

    xs = _to_m_y(touches_df["y"].to_numpy(dtype=float))
    ys = _to_m_x(touches_df["x"].to_numpy(dtype=float))

    drew_kde = False
    if len(xs) >= 5 and np.std(xs) > 1e-6 and np.std(ys) > 1e-6:
        # A degenerate point cloud (near-zero spread, or a singular
        # covariance matrix scipy can't invert) raises inside gaussian_kde
        # rather than producing a meaningless density - caught below so a
        # handful of touches all bunched at one spot falls back to the
        # scatter plot instead of crashing the whole page.
        try:
            from scipy.stats import gaussian_kde
            kde = gaussian_kde(np.vstack([xs, ys]))
            grid_x, grid_y = np.mgrid[0:width:100j, 0:length:154j]
            density = kde(np.vstack([grid_x.ravel(), grid_y.ravel()])).reshape(grid_x.shape)
            # Mask out the low end of the density range so the shading only
            # tints the pitch where touches are actually concentrated - a
            # Gaussian KDE's support is technically the whole plane, so
            # without this the entire pitch would show a faint tint even far
            # from any real touch.
            threshold = density.max() * 0.12
            density_masked = np.ma.masked_where(density < threshold, density)
            ax.imshow(density_masked.T, origin="lower", extent=[0, width, 0, length],
                      cmap=TOUCHMAP_CMAP, alpha=0.85, zorder=1.2, aspect="auto")
            drew_kde = True
        except Exception:
            pass
    if not drew_kde:
        ax.scatter(xs, ys, s=70, color=TOUCHMAP_POINT_COLOR, alpha=0.55,
                   edgecolors="white", linewidths=0.8, zorder=2)

    fig.suptitle(f"{player_name} - {title_suffix}", color=TITLE_COLOR, fontsize=20,
                 fontweight="bold", fontname=PASS_MAP_FONT, y=1 - 0.28 / fig_h)
    subtitle_y = 1 - 0.78 / fig_h
    if subtitle is None and home_name and away_name:
        _draw_split_subtitle(fig, subtitle_y, [
            (home_name, HOME_TEAM_COLOR), (" vs ", TITLE_COLOR), (away_name, AWAY_TEAM_COLOR),
        ])
    else:
        subtitle = subtitle if subtitle is not None else ""
        fig.text(0.5, subtitle_y, subtitle, color=TITLE_COLOR, fontsize=14, fontweight="bold",
                  fontname=PASS_MAP_FONT, ha="center")

    if stat_items is None:
        stat_items = [(f"{len(touches_df)} Touches", TITLE_COLOR)]
    stats_y = 0.22 / fig_h
    n = len(stat_items)
    for i, (text, color) in enumerate(stat_items):
        x = (i + 0.5) / n
        fig.text(x, stats_y, text, color=color, fontsize=11.5, fontweight="bold",
                  fontname=PASS_MAP_FONT, ha="center", va="center")

    return fig
