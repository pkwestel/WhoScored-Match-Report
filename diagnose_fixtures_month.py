"""
One-off diagnostic (v3): confirms the exact mechanism behind the batch
app's "always shows the most recent month" bug, and finds the real,
CLICKABLE DOM element for WhoScored's "previous month" arrow on the
Fixtures page.

CONFIRMED SO FAR:
- (From Pauly pasting two real WhoScored URLs) the Fixtures page URL is
  IDENTICAL for a different month - WhoScored's month navigation is pure
  client-side JS, nothing in the URL encodes which month is showing.
- (From v1/v2 of this script, run against the real Premier League
  Fixtures page) the real "previous" control IS found by its stable ID:
  <button id="dayChangeBtn-prev" class="Calendar-module_dayChangeBtn__sEvC8">
  wrapping <div class="ThemeImage-module_themeImage__mmLe2"><img
  src=".../chevronLeft-...svg"></div>, itself inside
  <div class="Calendar-module_controller__Ke8vm"> inside
  <div class="Calendar-module_calendar__zKSqp">. BUT every click attempt
  (native Selenium click AND JS-dispatched click) on the icon AND every
  ancestor up to 4 levels failed/did nothing, and - critically -
  driver.find_element(By.TAG_NAME, "body").text NEVER once showed the
  actual calendar/month content anywhere in the whole v2 run (cold load,
  after accepting cookies, during every candidate test) - only the site's
  top language-switcher nav ("EN ES TR IT... WHOSCORED+"). That's the real
  puzzle this version digs into: not just "which element to click" but
  "why does .text never show the real page content at all", since a
  plain, non-Selenium fetch of this same URL DOES return real fixture
  data as static server-rendered HTML (confirmed earlier in this
  investigation). Candidate explanations: the cookie-consent step isn't
  fully completing, the real content needs more render time than this
  script has been waiting, or something is marking the real content
  non-visible to Selenium's text/visibility checks specifically.

This version stops guessing at ancestor click targets (the real element
is now known by ID) and instead: (1) waits longer and polls repeatedly for
real content to appear instead of one fixed sleep, (2) reads
driver.page_source directly (not just the visibility-filtered .text) to
settle whether the real month/fixture content exists in the DOM at all,
(3) dumps the FULL HTML of the Calendar-module_calendar container once
found, so the real month-label element is visible rather than guessed at,
and (4) runs getBoundingClientRect()/getComputedStyle() on the known
#dayChangeBtn-prev button to show its exact position/size/display state -
pinpointing exactly why it's "not interactable" - plus one more click
attempt via explicit mouse-move (ActionChains) as a last resort.

Run this the same way you'd run whoscored_report.py itself.

Usage:
    python diagnose_fixtures_month.py "<whoscored fixtures page url>"

What to do with the output: paste it back in full.
"""
import re
import sys
import time

from selenium.webdriver.common.by import By
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

from utils.driver import get_driver

_MONTH_RE = re.compile(
    r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{4}\b"
    r"|\b\d{1,2}\s*-\s*\d{1,2}\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{4}\b",
    re.IGNORECASE,
)


def _accept_cookies_if_present(driver):
    try:
        candidates = driver.find_elements(
            By.XPATH,
            "//*[self::button or self::a or self::div[@role='button']]"
            "[contains(translate(normalize-space(text()),'ACEPT','acept'),'accept')]",
        )
    except Exception as e:
        print(f"  (cookie-banner search errored: {e})")
        return False
    for el in candidates:
        try:
            text = (el.text or "").strip().lower()
        except Exception:
            continue
        if "accept" in text:
            try:
                el.click()
                print(f"  Clicked cookie-consent control: {text!r}")
                time.sleep(1)
                return True
            except Exception as e:
                print(f"  Found cookie-consent control {text!r} but click failed: {e}")
    return False


def _month_label_from_text(text):
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    for line in lines:
        if _MONTH_RE.search(line):
            return line
    return None


def _wait_for_real_content(driver, timeout=20, poll=1.5):
    """
    Polls both driver.find_element(...).text AND raw driver.page_source
    repeatedly (rather than one fixed sleep) until either a month-pattern
    shows up in the VISIBLE text, or in the raw HTML source, or the
    timeout is hit - reports which (if either) happened and how long it
    took, since the real cause of v2's "content never appears" puzzle
    might simply have been not waiting long enough for a slow-hydrating
    page.
    """
    start = time.time()
    last_text = ""
    while time.time() - start < timeout:
        try:
            last_text = driver.find_element(By.TAG_NAME, "body").text
        except Exception:
            last_text = ""
        label = _month_label_from_text(last_text)
        if label:
            return {"found_in": "visible text", "label": label, "elapsed": time.time() - start}

        src = driver.page_source
        m = _MONTH_RE.search(src)
        if m:
            return {"found_in": "page_source (NOT visible text)", "label": m.group(0),
                    "elapsed": time.time() - start, "source_len": len(src)}

        time.sleep(poll)

    return {"found_in": None, "elapsed": time.time() - start, "last_visible_text": last_text,
            "source_len": len(driver.page_source)}


def main():
    if len(sys.argv) < 2:
        print('Usage: python diagnose_fixtures_month.py "<whoscored fixtures page url>"')
        sys.exit(1)

    url = sys.argv[1]
    print(f"Opening {url} ...")
    with get_driver() as driver:
        driver.get(url)
        WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.TAG_NAME, "body"))
        )

        print("\n=== Cookie-consent check ===")
        clicked = _accept_cookies_if_present(driver)
        if not clicked:
            print("  No cookie-consent 'Accept all' control found.")

        print("\n=== Polling for real month/fixture content (up to 20s) ===")
        result = _wait_for_real_content(driver)
        print(result)

        print("\n=== Looking for the known Calendar container directly ===")
        try:
            calendar_els = driver.find_elements(By.XPATH, "//*[contains(@class,'Calendar-module_calendar')]")
        except Exception as e:
            calendar_els = []
            print(f"  (search errored: {e})")
        if not calendar_els:
            print("  No element with class containing 'Calendar-module_calendar' found at all.")
        else:
            cal = calendar_els[0]
            try:
                full_html = driver.execute_script("return arguments[0].outerHTML;", cal)
            except Exception as e:
                full_html = f"(couldn't read: {e})"
            print(f"  Found {len(calendar_els)} match(es). Full outerHTML of the first one:")
            print(full_html[:4000])

        print("\n=== Inspecting #dayChangeBtn-prev directly (position/size/visibility) ===")
        try:
            btn = driver.find_element(By.ID, "dayChangeBtn-prev")
        except Exception as e:
            btn = None
            print(f"  Couldn't find #dayChangeBtn-prev by ID: {e}")

        if btn is not None:
            try:
                rect = driver.execute_script("return arguments[0].getBoundingClientRect();", btn)
                style = driver.execute_script(
                    "var s = getComputedStyle(arguments[0]); "
                    "return {display: s.display, visibility: s.visibility, opacity: s.opacity, "
                    "pointerEvents: s.pointerEvents};",
                    btn,
                )
                at_point = None
                if rect and rect.get("width", 0) > 0 and rect.get("height", 0) > 0:
                    cx = rect["x"] + rect["width"] / 2
                    cy = rect["y"] + rect["height"] / 2
                    at_point = driver.execute_script(
                        "var el = document.elementFromPoint(arguments[0], arguments[1]); "
                        "return el ? el.outerHTML.slice(0, 200) : null;",
                        cx, cy,
                    )
                print(f"  getBoundingClientRect: {rect}")
                print(f"  computed style: {style}")
                print(f"  element actually at that point (elementFromPoint): {at_point}")
            except Exception as e:
                print(f"  Inspection failed: {e}")

            print("\n  --- Attempt: ActionChains move_to_element + click ---")
            before = driver.find_element(By.TAG_NAME, "body").text
            try:
                ActionChains(driver).move_to_element(btn).pause(0.3).click().perform()
                time.sleep(2)
                after = driver.find_element(By.TAG_NAME, "body").text
                before_label = _month_label_from_text(before)
                after_label = _month_label_from_text(after)
                print(f"  BEFORE label: {before_label}")
                print(f"  AFTER  label: {after_label}")
                print(f"  CHANGED: {before != after}")
            except Exception as e:
                print(f"  ActionChains click failed: {e}")

        print("\n=== Done. Paste everything above back. ===")


if __name__ == "__main__":
    main()
