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
"""

import os

import matplotlib.pyplot as plt
import matplotlib.image as mpimg
import matplotlib.patheffects as path_effects
from matplotlib.patches import Arc, Rectangle
import streamlit as st

import whoscored_report as wr

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
