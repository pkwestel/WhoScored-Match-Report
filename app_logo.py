"""
Shared 'Kwest Thoughts' logo header, pinned to the top-left corner of
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
Top-LEFT specifically (not top-right, where this originally sat) since
Streamlit's own toolbar (hamburger menu / "Deploy" button, top-right of
every app) was covering it there - none of these apps use st.sidebar, so
top-left is clear of any of Streamlit's own native UI.

PLAIN position:fixed alone isn't enough here and was observed scrolling
away with the rest of the page instead of staying put - Streamlit wraps
its whole app in a container that applies a CSS transform for its own
page-load fade-in animation, and per the CSS spec, ANY ancestor with a
transform becomes the containing block for a fixed-position descendant
(the descendant is then positioned relative to THAT ancestor, not the
browser viewport) - which is indistinguishable from "not actually fixed"
once that ancestor itself scrolls.

The fix needs actual JavaScript, which rules out plain st.markdown() -
its unsafe_allow_html injects raw HTML via the same underlying mechanism
as the DOM's innerHTML, and browsers deliberately never execute <script>
tags inserted that way (a markdown-embedded <script> silently does
nothing). st.components.v1.html() is the real (and standard/documented)
way to run custom JS in a Streamlit app - it renders inside its own
iframe, where scripts execute normally. That iframe's OWN document isn't
the visible page though, so the script below reaches back out to
window.parent.document (Streamlit's actual iframe/embedding setup makes
the real app page same-origin and reachable this way - this exact
'window.parent.document' pattern is the standard trick used for the
various "custom JS in Streamlit" hacks floating around) and creates the
logo <img> directly there, as a plain child of <body> - clear of the
transformed container entirely, so position:fixed on it then measures
against the real browser viewport like it's supposed to.

Every render_logo_top_left() call reruns this same script (Streamlit
reruns the whole page on every interaction), so it also removes whatever
it injected into the parent document on the PREVIOUS run first -
otherwise each rerun would leave one more orphaned copy of the logo
sitting in the page forever (anything moved into window.parent.document
directly is invisible to Streamlit's own component diffing, which only
tracks nodes still inside ITS managed tree).
"""
import base64
import os

import streamlit.components.v1 as components

LOGO_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "kwest_thoughts_logo_v3.png")


def render_logo_top_left(height_px=90):
    """
    Call once near the top of a page - right after st.set_page_config() -
    to pin the logo to that page's top-left corner, immune to scrolling
    (see this module's own docstring for why a plain CSS position:fixed
    class alone doesn't achieve that inside Streamlit, and why this needs
    st.components.v1.html() rather than st.markdown()). Silently does
    nothing if the logo file is missing/unreadable, so a bad path just
    means no logo rather than a crashed page.
    """
    try:
        with open(LOGO_PATH, "rb") as f:
            logo_b64 = base64.b64encode(f.read()).decode("ascii")
    except Exception:
        return

    components.html(
        f"""
        <script>
        (function() {{
            var old = window.parent.document.getElementById('kwest-logo-fixed-injected');
            if (old) {{ old.remove(); }}
            var img = window.parent.document.createElement('img');
            img.id = 'kwest-logo-fixed-injected';
            img.src = 'data:image/png;base64,{logo_b64}';
            img.style.cssText =
                'display:block; position:fixed; top:0.6rem; left:1.2rem; ' +
                'height:{height_px}px; z-index:999; pointer-events:none;';
            window.parent.document.body.appendChild(img);
        }})();
        </script>
        """,
        height=0,
        width=0,
    )
