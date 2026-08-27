"""
Shared 'Kwest Thoughts' logo asset for dashboard_app.py - the only app
in this project that still shows it. The other 4 standalone scraper/
report apps (combined_streamlit_app.py, batch_run_app.py, fotmob_
streamlit_app.py, streamlit_app.py) dropped it entirely per request,
since they have no equivalent page-header/title-bar structure worth
anchoring a logo to.

dashboard_app.py has 3 different page states - the main tabbed dashboard,
a team's Team Page, and one match's Match Report - each wanting the logo
placed right next to a DIFFERENT existing piece of UI (the page title,
the season dropdown, the 'Back to Fixtures' button) rather than one fixed
position floating the same way on every page. Because of that, this
module doesn't render anything itself - it just hands back a ready-to-
embed '<img>' HTML snippet (the image base64-encoded inline, so there's
no separate file for the browser to fetch), and dashboard_app.py drops
that snippet into whatever st.columns()/alignment wrapper fits each
specific spot.

(An earlier version of this module pinned the logo to a fixed screen
corner via a small JS trick - st.components.v1.html() injecting the
image directly into the parent page's <body>, needed because Streamlit
wraps its app in a container with a CSS transform that breaks plain
position:fixed. That's no longer needed now that the logo sits inline
next to specific page content instead of floating independently of it.)
"""
import base64
import os

LOGO_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "kwest_thoughts_logo_v3.png")


def logo_img_tag(height_px=60):
    """
    Returns a ready-to-embed '<img src="data:image/png;base64,...">' HTML
    snippet for the logo - or '' if the logo file is missing/unreadable,
    so a caller can just drop the (possibly empty) result straight into an
    f-string without a separate existence check, and a bad path quietly
    means no logo rather than a crashed page.
    """
    try:
        with open(LOGO_PATH, "rb") as f:
            logo_b64 = base64.b64encode(f.read()).decode("ascii")
    except Exception:
        return ""
    return f'<img src="data:image/png;base64,{logo_b64}" style="height:{height_px}px; display:inline-block;" />'
