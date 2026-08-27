"""
Shared 'Kwest Thoughts' logo header, pinned to the top-right corner of
every Streamlit page in this project (dashboard_app.py,
combined_streamlit_app.py, batch_run_app.py, fotmob_streamlit_app.py,
streamlit_app.py). Kept in its own tiny module - rather than folded into
pitch_viz.py, which every one of those apps does NOT necessarily import,
and which itself avoids importing anything scraper-related - so any of
these apps can pull in "just the logo" without dragging in matplotlib,
scipy, or any other pitch_viz dependency.

Same image file as pitch_viz.py's own watermark logo (kwest_thoughts_
logo_v3.png, next to this module) - one shared asset, not two copies.

Streamlit has no built-in "corner" layout slot, so this works by injecting
a small fixed-position <img> tag via st.markdown - the standard workaround
for pinning something to a page corner regardless of scroll position.
"""
import base64
import os

import streamlit as st

LOGO_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "kwest_thoughts_logo_v3.png")


def render_logo_top_right(height_px=64):
    """
    Call once near the top of a page - right after st.set_page_config() -
    to pin the logo to that page's top-right corner. Silently does nothing
    if the logo file is missing/unreadable, so a bad path just means no
    logo rather than a crashed page.
    """
    try:
        with open(LOGO_PATH, "rb") as f:
            logo_b64 = base64.b64encode(f.read()).decode("ascii")
    except Exception:
        return

    st.markdown(
        f"""
        <style>
        .kwest-logo-top-right {{
            position: fixed;
            top: 0.6rem;
            right: 1.2rem;
            height: {height_px}px;
            z-index: 999;
            pointer-events: none;
        }}
        </style>
        <img class="kwest-logo-top-right"
             src="data:image/png;base64,{logo_b64}" />
        """,
        unsafe_allow_html=True,
    )
