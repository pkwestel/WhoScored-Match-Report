"""
history_db.py
==============
Persistence layer for building a season-long database out of individual
match reports (combined_streamlit_app.py, or the standalone WhoScored/FotMob
apps) - the "database of it all" piece, separate from the scraping itself.

Backend: SQLite locally (zero setup, good for testing) or Postgres once
you've got a hosted database (Supabase/Neon/etc.) - selected purely by the
DATABASE_URL you pass in:
    sqlite:///history.db                                   -> local SQLite
    postgresql://user:pass@host:port/dbname                -> hosted Postgres

No ORM (SQLAlchemy) is used deliberately - just the stdlib `sqlite3` module
plus `psycopg2` (only imported if you actually use a postgres:// URL, so
there's no hard dependency on it for local SQLite testing). Both backends
support the exact same SQL here (CREATE TABLE IF NOT EXISTS, INSERT ...
ON CONFLICT ... DO UPDATE), so there's one copy of every query, not two.

SCHEMA DESIGN
-------------
matches            - one row per match (match_id is FotMob's own numeric
                     matchId, since that's a stable global identifier both
                     the standalone FotMob app and the combined app already
                     have on hand). 'referee' is best-effort (see fotmob_
                     report.extract_referee()) - None for matches saved
                     before that field existed. Score/xG for the Fixtures
                     tab are NOT columns here - see fetch_fixtures(), which
                     pulls them from team_match_stats' 'fm_totals' instead.
team_match_stats   - one row per (match, team). Rather than hard-coding
                     every column name from every report variant (WhoScored's
                     Totals tab and FotMob's Totals tab don't share a column
                     schema, and combined_report.py keeps them separate on
                     purpose - see its own docstring), every stat that came
                     back from either source is stored as-is inside a single
                     JSON 'extra_json' column, namespaced by source
                     ({"ws_totals": {...}, "fm_totals": {...}}). This avoids
                     brittle guessing at exact column names now, and avoids a
                     schema migration every time FotMob adds/renames a stat
                     group (see compute_totals()'s own docstring - this
                     varies by competition/match already). A few of the most
                     commonly-needed fields (is_home) get a real column;
                     everything else lives in extra_json and gets flattened
                     back out by fetch_team_trends() below.
player_match_stats - same idea, one row per (match, team, player), with
                     per-player stats from whichever report tabs you publish
                     (Passing, Defensive Actions, Plus Minus, Touches, ...)
                     each nested under their own key in extra_json.
                     'ws_touches' (on BOTH team_match_stats and
                     player_match_stats - compute_touches()'s team_summary
                     and player_third tables respectively) carries
                     Progressive Carries/Carries into Final Third/Carries
                     into Box (team_match_stats only) and Passes Received/
                     Progressive Passes Received (player_match_stats only,
                     summed across players for a team total - see
                     fetch_season_touches_totals()) - added after the raw
                     `touches` table below already existed, so matches saved
                     before this key existed won't have it; those matches'
                     season Touches totals just show 0 for these specific
                     stats (re-save the match to backfill it), same pattern
                     as 'referee' being None for pre-existing matches.
shots              - the one genuinely well-known, stable shape across every
                     report (compute_shots()'s own column list), so this one
                     gets real columns instead of a JSON blob - it's the
                     table most worth querying/aggregating directly (season
                     shot maps, etc).
passes             - one row per pass (whoscored_report.compute_all_passes()),
                     with pitch coordinates - backs both single-match and
                     season-long Pass Map / Passes Received visuals.
touches            - one row per touch, any event type (compute_all_
                     touches()), with pitch coordinates - backs single-match
                     and season-long touch heat maps. Only matches saved
                     after this table existed will have rows here.

Everything here is UPSERT (INSERT ... ON CONFLICT DO UPDATE), keyed on a
natural key per table, so re-publishing the same match twice (e.g. you
re-ran a report) overwrites rather than duplicates.
"""

import json
import sqlite3
from datetime import datetime, timezone

import pandas as pd

# ============================================================
# Connection handling
# ============================================================
class DB:
    """
    Thin wrapper so the rest of this module can write ONE version of every
    query using '?' placeholders (sqlite3's style) - translated to '%s' for
    psycopg2 automatically. Not a query builder/ORM, just enough to avoid
    two copies of every SQL statement.

    Auto-reconnects on a dead Postgres connection (see execute() below) -
    dashboard_app.py caches one DB instance for the life of the whole
    Streamlit server process (@st.cache_resource), but Neon's free tier
    scales its compute down to zero after 5 minutes idle and closes the
    underlying connection when it does. Without reconnect logic, the very
    next query after any idle gap fails with psycopg2.OperationalError
    ("server closed the connection unexpectedly" or similar) - which looks
    like a real bug but is really just a stale connection to a database
    that went to sleep.
    """

    def __init__(self, database_url: str):
        self.database_url = database_url
        self.backend = "postgres" if database_url.startswith(("postgres://", "postgresql://")) else "sqlite"
        self._connect()

    def _connect(self):
        if self.backend == "postgres":
            import psycopg2  # only required if you actually use a postgres:// URL
            self._conn = psycopg2.connect(self.database_url)
        else:
            path = (self.database_url.replace("sqlite:///", "", 1)
                    if self.database_url.startswith("sqlite:///") else self.database_url)
            self._conn = sqlite3.connect(path)
            self._conn.execute("PRAGMA foreign_keys = ON")

    def execute(self, sql: str, params: tuple = ()):
        sql = sql.replace("?", "%s") if self.backend == "postgres" else sql
        try:
            cur = self._conn.cursor()
            cur.execute(sql, params)
            return cur
        except Exception as e:
            # Only Postgres connections go stale this way (Neon's scale-to-
            # zero) - a SQLite error means something else is actually wrong
            # (e.g. a genuine syntax error), so it's re-raised untouched.
            if self.backend != "postgres":
                raise
            import psycopg2
            if not isinstance(e, (psycopg2.OperationalError, psycopg2.InterfaceError)):
                raise
            self._connect()
            cur = self._conn.cursor()
            cur.execute(sql, params)
            return cur

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.commit()
        else:
            self.rollback()
        self.close()


def get_db(database_url: str) -> DB:
    return DB(database_url)


# ============================================================
# Schema
# ============================================================
def init_schema(db: DB):
    """Create every table if it doesn't already exist. Safe to call every run."""
    db.execute("""
        CREATE TABLE IF NOT EXISTS matches (
            match_id      TEXT PRIMARY KEY,
            competition   TEXT,
            match_date    TEXT,
            home_team     TEXT,
            away_team     TEXT,
            ws_events     INTEGER,
            fm_shots      INTEGER,
            referee       TEXT,
            matchweek     TEXT,
            scraped_at    TEXT
        )
    """)
    # Migration for databases that already had a 'matches' table BEFORE the
    # referee/matchweek columns were added above - CREATE TABLE IF NOT
    # EXISTS only applies to brand new tables, so an existing one needs its
    # own ALTER TABLE. Wrapped in try/except because there's no portable
    # "ADD COLUMN IF NOT EXISTS" across SQLite and Postgres both - re-running
    # this against a database that already has the column just raises
    # "duplicate column"/"already exists", which is fine to ignore.
    try:
        db.execute("ALTER TABLE matches ADD COLUMN referee TEXT")
        db.commit()
    except Exception:
        db.rollback()
    try:
        db.execute("ALTER TABLE matches ADD COLUMN matchweek TEXT")
        db.commit()
    except Exception:
        db.rollback()
    db.execute("""
        CREATE TABLE IF NOT EXISTS team_match_stats (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            match_id      TEXT NOT NULL REFERENCES matches(match_id),
            team          TEXT NOT NULL,
            is_home       INTEGER,
            extra_json    TEXT,
            UNIQUE(match_id, team)
        )
    """ if db.backend == "sqlite" else """
        CREATE TABLE IF NOT EXISTS team_match_stats (
            id            SERIAL PRIMARY KEY,
            match_id      TEXT NOT NULL REFERENCES matches(match_id),
            team          TEXT NOT NULL,
            is_home       INTEGER,
            extra_json    TEXT,
            UNIQUE(match_id, team)
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS player_match_stats (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            match_id      TEXT NOT NULL REFERENCES matches(match_id),
            team          TEXT NOT NULL,
            player        TEXT NOT NULL,
            extra_json    TEXT,
            UNIQUE(match_id, team, player)
        )
    """ if db.backend == "sqlite" else """
        CREATE TABLE IF NOT EXISTS player_match_stats (
            id            SERIAL PRIMARY KEY,
            match_id      TEXT NOT NULL REFERENCES matches(match_id),
            team          TEXT NOT NULL,
            player        TEXT NOT NULL,
            extra_json    TEXT,
            UNIQUE(match_id, team, player)
        )
    """)
    # NOTE on the UNIQUE key: it deliberately does NOT include x/y. Those are
    # NULL for combined_shots (compute_combined_shots() carries no pitch
    # coordinates - see combined_report.py), and NULL never equals NULL in
    # SQL, so a UNIQUE constraint involving a nullable column would silently
    # stop deduplicating for exactly the rows that need it most. 'outcome' is
    # always populated in every shot shape this gets fed, so it's used as
    # the tiebreaker instead (same player + same minute + same outcome twice
    # in one match is an extremely rare rebound edge case, and an acceptable
    # one to risk here).
    db.execute("""
        CREATE TABLE IF NOT EXISTS shots (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            match_id      TEXT NOT NULL REFERENCES matches(match_id),
            team          TEXT,
            player        TEXT,
            minute        REAL,
            added_time    REAL,
            situation     TEXT,
            body_part     TEXT,
            outcome       TEXT,
            on_target     INTEGER,
            xg            REAL,
            xgot          REAL,
            x             REAL,
            y             REAL,
            extra_json    TEXT,
            UNIQUE(match_id, team, player, minute, added_time, outcome)
        )
    """ if db.backend == "sqlite" else """
        CREATE TABLE IF NOT EXISTS shots (
            id            SERIAL PRIMARY KEY,
            match_id      TEXT NOT NULL REFERENCES matches(match_id),
            team          TEXT,
            player        TEXT,
            minute        REAL,
            added_time    REAL,
            situation     TEXT,
            body_part     TEXT,
            outcome       TEXT,
            on_target     INTEGER,
            xg            REAL,
            xgot          REAL,
            x             REAL,
            y             REAL,
            extra_json    TEXT,
            UNIQUE(match_id, team, player, minute, added_time, outcome)
        )
    """)
    # Every pass in the match (WhoScored's compute_all_passes()) - one row per
    # pass, both ends of it, so a single table backs both the Pass Map
    # (group by passer) and Passes Received (group by receiver) visuals on
    # dashboard_app.py without duplicating storage or re-deriving anything.
    # UNIQUE key deliberately doesn't include 'receiver' (it's NULL for
    # incomplete passes, and NULL never equals NULL - see the shots table
    # note above) - (match_id, team, passer, minute, second, x, y) already
    # pins down a specific event uniquely, since WhoScored's own timestamps
    # are only whole-second resolution but origin coordinates disambiguate
    # any same-second passes by the same player.
    db.execute("""
        CREATE TABLE IF NOT EXISTS passes (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            match_id        TEXT NOT NULL REFERENCES matches(match_id),
            team            TEXT,
            passer          TEXT,
            receiver        TEXT,
            minute          REAL,
            second          REAL,
            x               REAL,
            y               REAL,
            end_x           REAL,
            end_y           REAL,
            completed       INTEGER,
            is_progressive  INTEGER,
            is_key_pass     INTEGER,
            category        TEXT,
            UNIQUE(match_id, team, passer, minute, second, x, y)
        )
    """ if db.backend == "sqlite" else """
        CREATE TABLE IF NOT EXISTS passes (
            id              SERIAL PRIMARY KEY,
            match_id        TEXT NOT NULL REFERENCES matches(match_id),
            team            TEXT,
            passer          TEXT,
            receiver        TEXT,
            minute          REAL,
            second          REAL,
            x               REAL,
            y               REAL,
            end_x           REAL,
            end_y           REAL,
            completed       INTEGER,
            is_progressive  INTEGER,
            is_key_pass     INTEGER,
            category        TEXT,
            UNIQUE(match_id, team, passer, minute, second, x, y)
        )
    """)
    # Every touch in the match (WhoScored's compute_all_touches()) - one row
    # per touch, any event type, with pitch location. Backs a touch heat map
    # (single-match or season-long, once enough matches are published) the
    # same way the passes table backs the Pass Map - a per-third COUNT
    # (what player_match_stats' extra_json already stores) can't reconstruct
    # WHERE on the pitch those touches happened, only raw points can. Only
    # matches saved AFTER this table was added will have rows here - there's
    # no way to backfill touch locations for older matches without
    # re-scraping them (WhoScored match pages don't stay up forever, so
    # that's not always possible either).
    db.execute("""
        CREATE TABLE IF NOT EXISTS touches (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            match_id      TEXT NOT NULL REFERENCES matches(match_id),
            team          TEXT,
            player        TEXT,
            minute        REAL,
            second        REAL,
            x             REAL,
            y             REAL,
            UNIQUE(match_id, team, player, minute, second, x, y)
        )
    """ if db.backend == "sqlite" else """
        CREATE TABLE IF NOT EXISTS touches (
            id            SERIAL PRIMARY KEY,
            match_id      TEXT NOT NULL REFERENCES matches(match_id),
            team          TEXT,
            player        TEXT,
            minute        REAL,
            second        REAL,
            x             REAL,
            y             REAL,
            UNIQUE(match_id, team, player, minute, second, x, y)
        )
    """)
    db.commit()


# ============================================================
# Publish (upsert) helpers
# ============================================================
def upsert_match(db: DB, match_id, home_team, away_team, competition=None, match_date=None,
                  ws_events=None, fm_shots=None, referee=None, matchweek=None):
    db.execute("""
        INSERT INTO matches (match_id, competition, match_date, home_team, away_team,
                              ws_events, fm_shots, referee, matchweek, scraped_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(match_id) DO UPDATE SET
            competition = excluded.competition,
            match_date  = excluded.match_date,
            home_team   = excluded.home_team,
            away_team   = excluded.away_team,
            ws_events   = excluded.ws_events,
            fm_shots    = excluded.fm_shots,
            referee     = excluded.referee,
            matchweek   = excluded.matchweek,
            scraped_at  = excluded.scraped_at
    """, (str(match_id), competition, match_date, home_team, away_team,
          ws_events, fm_shots, referee,
          str(matchweek) if matchweek is not None else None,
          datetime.now(timezone.utc).isoformat()))


def upsert_team_stats(db: DB, match_id, team, extra: dict, is_home=None):
    """extra is a plain dict, e.g. {'ws_totals': {...}, 'fm_totals': {...}} - JSON-encoded as-is."""
    db.execute("""
        INSERT INTO team_match_stats (match_id, team, is_home, extra_json)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(match_id, team) DO UPDATE SET
            is_home    = excluded.is_home,
            extra_json = excluded.extra_json
    """, (str(match_id), team, int(bool(is_home)) if is_home is not None else None,
          json.dumps(extra, default=str)))


def upsert_player_stats(db: DB, match_id, team, player, extra: dict):
    db.execute("""
        INSERT INTO player_match_stats (match_id, team, player, extra_json)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(match_id, team, player) DO UPDATE SET
            extra_json = excluded.extra_json
    """, (str(match_id), team, player, json.dumps(extra, default=str)))


_SHOTS_KNOWN_COLS = {"Team", "Player", "Minute", "Added Time", "Situation", "Body Part",
                     "Outcome", "On Target", "xG", "xGOT", "PSxG", "X", "Y"}


def upsert_shots(db: DB, match_id, shots_df: pd.DataFrame):
    """
    Accepts either shot shape this project produces: compute_shots()'s own
    (Team, Player, Minute, Added Time, Situation, Body Part, Outcome,
    On Target, xG, xGOT, X, Y) or combined_report.compute_combined_shots()'s
    (no On Target/X/Y, 'PSxG' instead of 'xGOT', plus Distance (yd) and
    SCA1/2_Player/Action) - whichever of xGOT/PSxG is present is stored in
    the xgot column, and anything else not explicitly mapped (Distance (yd),
    the SCA columns) is kept in extra_json rather than dropped.
    """
    if shots_df is None or shots_df.empty:
        return
    extra_cols = [c for c in shots_df.columns if c not in _SHOTS_KNOWN_COLS]
    for _, r in shots_df.iterrows():
        post_shot_xg = r.get("xGOT")
        if post_shot_xg is None or (isinstance(post_shot_xg, float) and pd.isna(post_shot_xg)):
            post_shot_xg = r.get("PSxG")
        extra = {c: r.get(c) for c in extra_cols} if extra_cols else {}
        db.execute("""
            INSERT INTO shots (match_id, team, player, minute, added_time, situation,
                                body_part, outcome, on_target, xg, xgot, x, y, extra_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(match_id, team, player, minute, added_time, outcome) DO UPDATE SET
                situation  = excluded.situation,
                body_part  = excluded.body_part,
                on_target  = excluded.on_target,
                xg         = excluded.xg,
                xgot       = excluded.xgot,
                x          = excluded.x,
                y          = excluded.y,
                extra_json = excluded.extra_json
        """, (
            str(match_id), r.get("Team"), r.get("Player"),
            _num(r.get("Minute")), _num(r.get("Added Time")), _text(r.get("Situation")),
            _text(r.get("Body Part")), _text(r.get("Outcome")),
            int(bool(r.get("On Target"))) if pd.notna(r.get("On Target")) else None,
            _num(r.get("xG")), _num(post_shot_xg), _num(r.get("X")), _num(r.get("Y")),
            json.dumps(extra, default=str) if extra else None,
        ))


def upsert_passes(db: DB, match_id, passes_df: pd.DataFrame):
    """
    Bulk-loads whoscored_report.compute_all_passes()'s output (one row per
    pass, both passer and receiver already resolved). See that function's
    docstring for why passes are computed once for the whole match rather
    than per-player before being published here.
    """
    if passes_df is None or passes_df.empty:
        return
    for _, r in passes_df.iterrows():
        receiver = _text(r.get("receiver"))
        db.execute("""
            INSERT INTO passes (match_id, team, passer, receiver, minute, second,
                                 x, y, end_x, end_y, completed, is_progressive,
                                 is_key_pass, category)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(match_id, team, passer, minute, second, x, y) DO UPDATE SET
                receiver       = excluded.receiver,
                end_x          = excluded.end_x,
                end_y          = excluded.end_y,
                completed      = excluded.completed,
                is_progressive = excluded.is_progressive,
                is_key_pass    = excluded.is_key_pass,
                category       = excluded.category
        """, (
            str(match_id), r.get("team"), r.get("passer"), receiver,
            _num(r.get("minute")), _num(r.get("second")), _num(r.get("x")), _num(r.get("y")),
            _num(r.get("endX")), _num(r.get("endY")),
            int(bool(r.get("completed"))), int(bool(r.get("is_progressive"))),
            int(bool(r.get("is_key_pass"))), _text(r.get("category")),
        ))


def upsert_touches(db: DB, match_id, touches_df: pd.DataFrame):
    """
    Bulk-loads whoscored_report.compute_all_touches()'s output (one row per
    touch, any event type, with pitch location) - see that function's
    docstring for why this exists as its own raw table rather than only the
    per-third counts already stored in player_match_stats.extra_json.
    """
    if touches_df is None or touches_df.empty:
        return
    for _, r in touches_df.iterrows():
        db.execute("""
            INSERT INTO touches (match_id, team, player, minute, second, x, y)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(match_id, team, player, minute, second, x, y) DO NOTHING
        """, (
            str(match_id), _text(r.get("team")), _text(r.get("player")),
            _num(r.get("minute")), _num(r.get("second")), _num(r.get("x")), _num(r.get("y")),
        ))


def _num(v):
    """NaN/None -> None (so it round-trips through the DB as NULL, not the string 'nan')."""
    return None if v is None or (isinstance(v, float) and pd.isna(v)) else float(v)


def _text(v):
    """
    Same idea as _num() but for TEXT columns (situation, receiver, category,
    etc). A pandas/numpy NaN (e.g. from _pass_receiver_map()'s shift(-1)
    heuristic finding no valid receiver, or a merge with no match) is a
    float, not a string or None - passed straight into a TEXT column param,
    some drivers (psycopg2/Postgres in particular) coerce it into the
    literal text 'NaN' on insert rather than a real NULL. That string then
    round-trips forever as its own bogus value (a fake player/situation
    showing up in a dropdown). Catch it here before it ever reaches SQL.
    """
    if v is None:
        return None
    if isinstance(v, float) and pd.isna(v):
        return None
    return v


def _find_stale_duplicate_match_ids(db: DB, match_id, home_team, away_team, match_date) -> list:
    """
    Looks for OTHER match rows for the same real-world fixture (same
    home_team + away_team + calendar date) but a DIFFERENT match_id than
    the one this save is about to use - the signature of a match that got
    saved once, then re-scraped/re-saved later under a match_id that came
    out differently the second time around.

    This matters because match_id in this project is parsed straight out
    of the source URL (fotmob_report.extract_match_id() pulls the numeric
    id after '#' in the FotMob URL) rather than read from anything on the
    scraped page itself - it is NOT guaranteed to come out the same twice
    for "the same" real match if the pasted URL differs even slightly
    between saves (missing the '#1234567' fragment, a shortened or
    redirected link, etc. all parse to a different id). publish_report()'s
    own delete-then-insert step (below) only clears rows for the match_id
    THIS save actually computed, so a match_id drift like that leaves the
    PREVIOUS save's row sitting there as an untouched orphan under its own,
    different match_id - invisible on the Fixtures tab (which just happens
    to look like a duplicate row of the same fixture) but still fully
    summed into every season-wide Team Stats table right alongside the new
    row, silently doubling every number for that match's players and teams
    (confirmed happening in production - a re-saved match's Team Stats
    numbers came out exactly 2x).

    Matched on calendar date only (the first 10 characters of match_date,
    'YYYY-MM-DD') rather than exact match_date string equality, since the
    same real match can legitimately be saved once with a precise kickoff
    date+time and once with just a fallback date-only value (see
    save_report_to_db()'s own docstring on kickoff vs. the date picker) -
    exact-string matching would miss that as a duplicate. A match_date of
    None never matches anything here - too big a false-positive risk to
    treat every date-less match as a duplicate of every other.

    Returns a list of the OTHER match_id(s) found (usually zero or one,
    but not assumed to be at most one) - publish_report() fully deletes
    every one of them, across every child table AND the matches row
    itself, before proceeding with this save.
    """
    if not match_date:
        return []
    date_part = str(match_date)[:10]
    cur = db.execute("""
        SELECT match_id FROM matches
        WHERE home_team = ? AND away_team = ? AND match_id != ?
          AND substr(match_date, 1, 10) = ?
    """, (home_team, away_team, str(match_id), date_part))
    return [r[0] for r in cur.fetchall()]


def publish_report(db: DB, match_id, home_team, away_team, team_stats: dict, player_stats: dict,
                    shots_df: pd.DataFrame = None, passes_df: pd.DataFrame = None,
                    touches_df: pd.DataFrame = None,
                    competition=None, match_date=None, ws_events=None, fm_shots=None, referee=None,
                    matchweek=None):
    """
    One-call orchestrator for a full match publish, wrapped in a single
    transaction (all-or-nothing - if any part fails, nothing is written).

    team_stats:   {team_name: {'ws_totals': {...}, 'fm_totals': {...}}, ...}
    player_stats: {(team_name, player_name): {'ws_passing': {...}, ...}, ...}
    shots_df:     combined/whoscored/fotmob shots dataframe, or None to skip.
    passes_df:    whoscored_report.compute_all_passes() output, or None to skip
                  (e.g. publishing a FotMob-only report with no event data).
    touches_df:   whoscored_report.compute_all_touches() output, or None to
                  skip (same reasoning as passes_df).
    referee:      fotmob_report.extract_referee() output, or None if unknown.
    matchweek:    fotmob_report.extract_matchweek() output, or None if unknown -
                  powers the Fixtures tab's matchweek filter.
    """
    try:
        # See _find_stale_duplicate_match_ids()'s own docstring - this
        # catches a re-saved match whose match_id came out DIFFERENT from a
        # previous save (a URL/parsing quirk, not a real second fixture),
        # which the match_id-scoped delete-then-insert below can't catch on
        # its own since it only knows about ITS OWN match_id. Deleting the
        # stale row(s) entirely - matches row included - before this save
        # proceeds guarantees at most one row per real-world fixture,
        # regardless of what match_id string this particular scrape/URL
        # happened to produce.
        for _stale_id in _find_stale_duplicate_match_ids(db, match_id, home_team, away_team, match_date):
            for _table in ("shots", "passes", "touches", "team_match_stats", "player_match_stats", "matches"):
                db.execute(f"DELETE FROM {_table} WHERE match_id = ?", (_stale_id,))

        upsert_match(db, match_id, home_team, away_team, competition, match_date, ws_events,
                     fm_shots, referee, matchweek)
        # A re-save of an already-published match is meant to be a full
        # replace, not a merge: every child table below is keyed on tuples
        # like (match_id, team, player, minute, second, x, y) or similar, so
        # if the newly computed rows don't line up EXACTLY with the old
        # ones - a receiver that resolves differently now, a fixed team-name
        # alias, a code change that drops/adds a row, or literally any
        # difference between the two runs - the old rows that no longer
        # match anything new just sit there forever as orphans, still
        # counted in every season table/graphic. Clearing everything tied to
        # this match_id first (inside the same transaction as the upserts
        # below, so a failed save still rolls back cleanly) guarantees the
        # old version can never linger and pollute aggregates.
        for _table in ("shots", "passes", "touches", "team_match_stats", "player_match_stats"):
            db.execute(f"DELETE FROM {_table} WHERE match_id = ?", (str(match_id),))
        for team, extra in team_stats.items():
            upsert_team_stats(db, match_id, team, extra, is_home=(team == home_team))
        for (team, player), extra in player_stats.items():
            upsert_player_stats(db, match_id, team, player, extra)
        if shots_df is not None:
            upsert_shots(db, match_id, shots_df)
        if passes_df is not None:
            upsert_passes(db, match_id, passes_df)
        if touches_df is not None:
            upsert_touches(db, match_id, touches_df)
        db.commit()
    except Exception:
        db.rollback()
        raise


# ============================================================
# Read helpers (used by dashboard_app.py)
# ============================================================
def fetch_matches(db: DB) -> pd.DataFrame:
    cur = db.execute("SELECT * FROM matches ORDER BY match_date DESC, scraped_at DESC")
    cols = [d[0] for d in cur.description]
    return pd.DataFrame(cur.fetchall(), columns=cols)


def _season_label(date_str):
    """
    Derives a 'YYYY/YY' season label (e.g. '2026/27') from a match's date -
    there's no real 'season' field scraped from anywhere (FotMob's match
    JSON doesn't carry one - see fotmob_report.extract_league_name()'s
    docstring), so this is computed on the fly from match_date instead of
    stored. Uses the standard European football season convention (roughly
    July - June): a date in July or later belongs to the season starting
    that year, anything before July belongs to the season that started the
    PREVIOUS year. Returns None if match_date is missing/unparseable rather
    than raising - a match with a bad/blank date just won't have a season
    filter value.
    """
    if not date_str:
        return None
    try:
        year, month = int(str(date_str)[:4]), int(str(date_str)[5:7])
    except (ValueError, IndexError):
        return None
    start_year = year if month >= 7 else year - 1
    return f"{start_year}/{(start_year + 1) % 100:02d}"


def fetch_fixtures(db: DB) -> pd.DataFrame:
    """
    One row per match, shaped for dashboard_app.py's Fixtures tab: match_id
    (kept for the clickable link to the match detail view, not meant to be
    shown as its own column), Date, Competition, Matchweek, Season, Home
    Team, Home xG, Score, Away xG, Away Team, Referee.

    Score and xG aren't their own columns on the 'matches' table - they're
    pulled from each team's own 'fm_totals' entry inside team_match_stats.
    extra_json (Goals / 'Total xG'), which every match saved via
    combined_streamlit_app.py or batch_lib.py already carries. That means
    this works retroactively for every match already in the database - no
    re-scraping or backfill needed, unlike Referee/Matchweek (genuinely new
    fields - see fotmob_report.extract_referee()/extract_matchweek()),
    which are None for anything saved before those existed. Season needs no
    backfill at all since it's derived from match_date, which every match
    has always had.
    """
    matches = fetch_matches(db)
    cols = ["match_id", "Date", "Competition", "Matchweek", "Season", "Home Team", "Home xG",
            "Score", "Away xG", "Away Team", "Referee"]
    if matches.empty:
        return pd.DataFrame(columns=cols)

    cur = db.execute("SELECT match_id, team, extra_json FROM team_match_stats")
    fm_totals_by_match = {}
    for match_id, team, extra_json in cur.fetchall():
        extra = json.loads(extra_json) if extra_json else {}
        fm_totals_by_match.setdefault(match_id, {})[team] = extra.get("fm_totals") or {}

    records = []
    for _, m in matches.iterrows():
        team_totals = fm_totals_by_match.get(m["match_id"], {})
        home_totals = team_totals.get(m["home_team"], {})
        away_totals = team_totals.get(m["away_team"], {})
        home_goals, away_goals = home_totals.get("Goals"), away_totals.get("Goals")
        score = (f"{int(home_goals)} - {int(away_goals)}"
                 if home_goals is not None and away_goals is not None else None)
        records.append({
            "match_id": m["match_id"],
            "Date": m["match_date"],
            "Competition": m["competition"],
            "Matchweek": m.get("matchweek"),
            "Season": _season_label(m["match_date"]),
            "Home Team": m["home_team"],
            "Home xG": home_totals.get("Total xG"),
            "Score": score,
            "Away xG": away_totals.get("Total xG"),
            "Away Team": m["away_team"],
            "Referee": m.get("referee"),
        })
    # Ascending by Date - oldest match first, newest at the bottom - even
    # though fetch_matches() itself (used above) is newest-first (that order
    # suits other callers, e.g. season-cumulative tables that want to short-
    # circuit on recent matches). A match with no date at all sorts last.
    return (pd.DataFrame(records, columns=cols)
            .sort_values("Date", na_position="last")
            .reset_index(drop=True))


def fetch_team_match_log(db: DB, team, season) -> pd.DataFrame:
    """
    One team's own slice of fetch_fixtures() - identical columns/row shape,
    filtered down to just the matches this team played in this season.
    Powers the Team Page's match log table, which is deliberately built to
    look exactly like the Fixtures tab's table (same renderer - see
    dashboard_app._render_fixtures_like_table()). Already in ascending date
    order since fetch_fixtures() itself is.
    """
    fixtures = fetch_fixtures(db)
    if fixtures.empty:
        return fixtures
    return fixtures[
        ((fixtures["Home Team"] == team) | (fixtures["Away Team"] == team))
        & (fixtures["Season"] == season)
    ].reset_index(drop=True)


def _flatten_extra(rows, id_cols):
    """Turn a list of (id_col_values..., extra_json) rows into one wide DataFrame,
    with each namespaced JSON key flattened to '<namespace>.<stat>' columns."""
    records = []
    for row in rows:
        *ids, extra_json = row
        rec = dict(zip(id_cols, ids))
        extra = json.loads(extra_json) if extra_json else {}
        for namespace, stats in extra.items():
            if isinstance(stats, dict):
                for k, v in stats.items():
                    rec[f"{namespace}.{k}"] = v
        records.append(rec)
    return pd.DataFrame(records)


def fetch_team_trends(db: DB, team: str) -> pd.DataFrame:
    """Every match_date + all of this team's stats (flattened from extra_json), oldest first."""
    cur = db.execute("""
        SELECT m.match_date, t.match_id, t.team, t.is_home, t.extra_json
        FROM team_match_stats t JOIN matches m ON m.match_id = t.match_id
        WHERE t.team = ?
        ORDER BY m.match_date ASC
    """, (team,))
    rows = [(r[0], r[1], r[2], r[3], r[4]) for r in cur.fetchall()]
    df = _flatten_extra(rows, ["match_date", "match_id", "team", "is_home"])
    return df


def fetch_team_stats_for_match(db: DB, match_id) -> pd.DataFrame:
    """
    Both teams' stats (flattened from extra_json) for ONE match - the
    match detail view's equivalent of fetch_team_trends(), scoped to a
    single match instead of a single team across the whole season.
    """
    cur = db.execute("""
        SELECT team, is_home, extra_json
        FROM team_match_stats
        WHERE match_id = ?
    """, (str(match_id),))
    rows = [(r[0], r[1], r[2]) for r in cur.fetchall()]
    return _flatten_extra(rows, ["team", "is_home"])


# (display label, fm_totals key) - the key names are exactly what
# compute_totals() in fotmob_report.py produces, confirmed against a real
# match. 'Shots on target' is deliberately FotMob's own published stat
# (lowercase 't'), not the shot-map-derived 'Shots on Target' (capital
# 'T') that also lives in the same fm_totals dict under a different key -
# same "prefer FotMob's own published figure" reasoning as 'Total xG' (see
# compute_totals()'s own docstring), and confirmed to matter in practice:
# the two disagreed by a wide margin on a real match (13 vs 5).
_MATCH_SUMMARY_FIELDS = [
    ("Goals", "Goals"),
    ("Shots", "Shots"),
    ("Shots on target", "Shots on target"),
    ("Shots inside box", "Shots inside box"),
    ("Possession", "Ball possession"),
    ("Big Chances", "Big chances"),
    ("Corners", "Corners"),
    ("Fouls", "Fouls committed"),
]


def _fmt_match_summary_value(value, label):
    if value is None:
        return "-"
    try:
        if label == "Possession":
            return f"{float(value):.0f}%"
        if float(value) == int(float(value)):
            return str(int(float(value)))
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return str(value)


def fetch_match_summary(db: DB, match_id) -> pd.DataFrame:
    """
    Compact home-vs-away summary table for the match detail view's Team
    Totals tab: one row per stat (Goals, Shots, Shots on target, Shots
    inside box, Possession, Big Chances, Corners, Fouls), laid out as
    <home team name> | Metric | <away team name> - the two team names ARE
    the column headers (rather than generic 'Home'/'Away' headers plus a
    separate leading 'Team' data row repeating them), so the table reads
    top-to-bottom as a single side-by-side comparison with no redundant
    first row. xG deliberately isn't repeated here - it already shows in
    the page header above this table (see _render_match_detail()).

    Reads straight from team_match_stats.extra_json's 'fm_totals' namespace
    (compute_totals()'s own output) by field name, rather than going
    through fetch_team_stats_for_match()'s generic flattened-everything
    table - see _MATCH_SUMMARY_FIELDS above for why that distinction
    matters for 'Shots on target' specifically.

    A stat an older saved match doesn't have (Possession/Big Chances/
    Corners/Shots inside box/FotMob's own Shots on target weren't captured
    at all before a fix to where FotMob's stat groups actually live in the
    raw JSON - see fotmob_report._get_fotmob_stat_groups()) shows as '-'
    rather than a misleading 0 - re-save that match to backfill it.
    """
    matches = fetch_matches(db)
    match_row = matches[matches["match_id"] == str(match_id)]
    if match_row.empty:
        return pd.DataFrame(columns=["Home", "Metric", "Away"])
    match_row = match_row.iloc[0]
    home_team, away_team = match_row["home_team"], match_row["away_team"]
    cols = [home_team, "Metric", away_team]

    cur = db.execute("SELECT team, extra_json FROM team_match_stats WHERE match_id = ?", (str(match_id),))
    fm_totals_by_team = {}
    for team, extra_json in cur.fetchall():
        extra = json.loads(extra_json) if extra_json else {}
        fm_totals_by_team[team] = extra.get("fm_totals") or {}

    home_stats = fm_totals_by_team.get(home_team, {})
    away_stats = fm_totals_by_team.get(away_team, {})

    records = []
    for label, key in _MATCH_SUMMARY_FIELDS:
        records.append({
            home_team: _fmt_match_summary_value(home_stats.get(key), label),
            "Metric": label,
            away_team: _fmt_match_summary_value(away_stats.get(key), label),
        })
    return pd.DataFrame(records, columns=cols)


def _fmt_adv_plain_int(value):
    """Formats a plain count (Shots, Duels won, Number of sprints, 10+ Pass
    Sequences, ...) as a whole number - '-' if genuinely missing rather than
    a misleading 0, matching _fmt_match_summary_value()'s convention."""
    if value is None:
        return "-"
    try:
        return str(int(round(float(value))))
    except (TypeError, ValueError):
        return str(value)


def _fmt_adv_decimal(value, dp=2):
    """Formats a decimal stat (PPDA, Passes per Sequence, the xG family) to
    a fixed number of decimal places. Handles FotMob's own xG fields, which
    are already-formatted STRINGS like '1.01' rather than floats (see
    fotmob_report._get_fotmob_stat_groups()'s raw JSON shape), just as
    happily as WhoScored's own float fields."""
    if value is None:
        return "-"
    try:
        return f"{float(value):.{dp}f}"
    except (TypeError, ValueError):
        return str(value)


def _fmt_adv_percent(value, dp=1):
    """Field Tilt - stored as a plain float (e.g. 62.3), '%' added here."""
    if value is None:
        return "-"
    try:
        return f"{float(value):.{dp}f}%"
    except (TypeError, ValueError):
        return str(value)


def _fmt_adv_metres(value, dp=1):
    """Defensive Action Height - already stored in metres."""
    if value is None:
        return "-"
    try:
        return f"{float(value):.{dp}f}m"
    except (TypeError, ValueError):
        return str(value)


def _fmt_adv_km_from_m(value, dp=1):
    """FotMob's own Distance covered/Sprinting distance stats are published
    in raw METRES (e.g. 118485) - converted to km here for a readable
    scoreboard-style number ('118.5 km') rather than a 6-digit metre count."""
    if value is None:
        return "-"
    try:
        return f"{float(value) / 1000:.{dp}f} km"
    except (TypeError, ValueError):
        return str(value)


def _fmt_adv_passthrough(value):
    """FotMob already formats some stats as a ready-to-display string
    ('28 (60%)' for Ground/Aerial duels won and Successful dribbles) -
    passed straight through rather than reformatted."""
    return "-" if value is None else str(value)


# Each entry: (table title, [(display label, source namespace, source key,
# formatter function), ...]). 'ws_totals'/'fm_totals' are the two namespaces
# batch_lib.build_db_stats() saves under team_match_stats.extra_json per
# team - 'ws_totals' is whoscored_report.compute_totals()'s output (Field
# Tilt/PPDA/10+ Pass Sequences/Avg Passes per Sequence/Defensive Action
# Height are WhoScored/Opta-style advanced metrics FotMob doesn't publish
# at all), 'fm_totals' is fotmob_report.compute_totals()'s output
# (everything else here is FotMob's own published stat, preferred over
# recomputing it ourselves wherever FotMob publishes it at all - same
# "trust FotMob's own number" reasoning as Total xG/Goals there). Backs
# fetch_advanced_stats_tables() below - the match detail view's Advanced
# Stats tab.
_ADVANCED_STATS_TABLES = [
    ("Team Style", [
        ("Field Tilt", "ws_totals", "Field Tilt %", _fmt_adv_percent),
        ("PPDA", "ws_totals", "PPDA", lambda v: _fmt_adv_decimal(v, 2)),
        ("10+ Pass Sequences", "ws_totals", "10+ Pass Sequences", _fmt_adv_plain_int),
        ("Passes per Sequence", "ws_totals", "Avg Passes per Sequence", lambda v: _fmt_adv_decimal(v, 2)),
        ("Def Line Height", "ws_totals", "Defensive Action Height (m)", lambda v: _fmt_adv_metres(v, 1)),
    ]),
    ("Shots", [
        ("Shots", "fm_totals", "Shots", _fmt_adv_plain_int),
        ("Shots on Target", "fm_totals", "Shots on target", _fmt_adv_plain_int),
        ("Shots Inside box", "fm_totals", "Shots inside box", _fmt_adv_plain_int),
        ("Shots outside box", "fm_totals", "Shots outside box", _fmt_adv_plain_int),
        ("Hit woodwork", "fm_totals", "Hit woodwork", _fmt_adv_plain_int),
        ("Shots blocked", "fm_totals", "Blocked shots", _fmt_adv_plain_int),
    ]),
    ("Expected Goals", [
        ("Total xG", "fm_totals", "Total xG", lambda v: _fmt_adv_decimal(v, 2)),
        ("non-penalty xG", "fm_totals", "xG non-penalty", lambda v: _fmt_adv_decimal(v, 2)),
        ("Post-Shot xG", "fm_totals", "xG on target (xGOT)", lambda v: _fmt_adv_decimal(v, 2)),
        ("Open Play xG", "fm_totals", "xG open play", lambda v: _fmt_adv_decimal(v, 2)),
        ("Set Piece xG", "fm_totals", "xG set play", lambda v: _fmt_adv_decimal(v, 2)),
    ]),
    ("Duels", [
        ("Duels won", "fm_totals", "Duels won", _fmt_adv_plain_int),
        ("Ground duels won", "fm_totals", "Ground duels won", _fmt_adv_passthrough),
        ("Aerial duels won", "fm_totals", "Aerial duels won", _fmt_adv_passthrough),
        ("Successful dribbles", "fm_totals", "Successful dribbles", _fmt_adv_passthrough),
    ]),
    ("Physical", [
        ("Number of sprints", "fm_totals", "Number of sprints", _fmt_adv_plain_int),
        ("Total sprinting distance", "fm_totals", "Sprinting distance", lambda v: _fmt_adv_km_from_m(v, 2)),
        ("Total distance covered", "fm_totals", "Distance covered", lambda v: _fmt_adv_km_from_m(v, 1)),
    ]),
]


def fetch_advanced_stats_tables(db: DB, match_id) -> dict:
    """
    The match detail view's Advanced Stats tab: five small Home/Metric/Away
    tables (Team Style, Shots, Expected Goals, Duels, Physical), each shaped
    exactly like fetch_match_summary()'s Team Totals table (the two team
    names as the column headers, one row per stat, no separate 'Team' row)
    so dashboard_app.py can render every one of them with that same shared
    HTML-table styling helper - just smaller, laid out up to 3 across per
    row. See _ADVANCED_STATS_TABLES above for the exact field-to-namespace/
    key mapping and why each field lives where it does.

    Returns an (insertion-ordered) dict of {table title: DataFrame}, always
    all 5 titles even for a match with zero saved stats (each such
    DataFrame is then empty) - dashboard_app.py can treat "no rows" as
    "show an info box" uniformly rather than a missing dict key. A stat
    this match's save doesn't have (an older save from before that stat was
    captured, or a competition/match FotMob simply didn't publish it for)
    shows as '-' per-cell rather than a misleading 0/blank row, same
    convention as fetch_match_summary().
    """
    empty_result = {title: pd.DataFrame(columns=["Home", "Metric", "Away"])
                     for title, _ in _ADVANCED_STATS_TABLES}

    matches = fetch_matches(db)
    match_row = matches[matches["match_id"] == str(match_id)]
    if match_row.empty:
        return empty_result
    match_row = match_row.iloc[0]
    home_team, away_team = match_row["home_team"], match_row["away_team"]
    cols = [home_team, "Metric", away_team]

    cur = db.execute("SELECT team, extra_json FROM team_match_stats WHERE match_id = ?", (str(match_id),))
    stats_by_team = {}
    for team, extra_json in cur.fetchall():
        stats_by_team[team] = json.loads(extra_json) if extra_json else {}

    home_extra = stats_by_team.get(home_team, {})
    away_extra = stats_by_team.get(away_team, {})

    result = {}
    for title, fields in _ADVANCED_STATS_TABLES:
        records = []
        for label, namespace, key, fmt in fields:
            home_val = (home_extra.get(namespace) or {}).get(key)
            away_val = (away_extra.get(namespace) or {}).get(key)
            records.append({
                home_team: fmt(home_val),
                "Metric": label,
                away_team: fmt(away_val),
            })
        result[title] = pd.DataFrame(records, columns=cols)
    return result


def fetch_player_stats_for_match(db: DB, match_id) -> pd.DataFrame:
    """Every player's stats (flattened from extra_json) for ONE match."""
    cur = db.execute("""
        SELECT team, player, extra_json
        FROM player_match_stats
        WHERE match_id = ?
    """, (str(match_id),))
    rows = [(r[0], r[1], r[2]) for r in cur.fetchall()]
    return _flatten_extra(rows, ["team", "player"])


def _fetch_player_namespaces(db: DB, match_id, namespaces: list) -> pd.DataFrame:
    """
    Pulls just the given namespace(s) out of every player_match_stats row
    for this match - e.g. ['ws_passing', 'fm_line_breaking_passes'] - merged
    into one 'Team'/'Player' + stat-columns DataFrame, rather than going
    through fetch_player_stats_for_match()'s everything-flattened table.
    Used by each of the category-specific fetch_player_*() functions below
    (Scoring Stats/Possession/Passing/Defensive Actions/Defensive Action
    Locations) so each only pulls the couple of namespaces it actually
    needs, and gets back plain field names (no 'namespace.' prefix, no
    underscores) ready to show as real column headers.
    """
    cur = db.execute("""
        SELECT team, player, extra_json
        FROM player_match_stats
        WHERE match_id = ?
    """, (str(match_id),))
    records = []
    for team, player, extra_json in cur.fetchall():
        extra = json.loads(extra_json) if extra_json else {}
        rec = {"Team": team, "Player": player}
        found_any = False
        for ns in namespaces:
            ns_dict = extra.get(ns)
            if isinstance(ns_dict, dict):
                found_any = True
                rec.update(ns_dict)
        if found_any:
            records.append(rec)
    return pd.DataFrame(records)


def _player_category_table(db: DB, match_id, home_team, away_team, namespaces: list,
                            columns: list) -> dict:
    """
    Shared plumbing for every Player Stats category table on the match
    detail view: pulls the given namespace(s) (see _fetch_player_namespaces()
    above), keeps only the requested columns (in the given order), then
    splits into (home, away) with a 'Team Total' row appended to each
    summing every numeric column - matching the requested layout (two
    separate tables, home on top / away below, each with a bottom total
    row).

    A column that's ENTIRELY missing for this match - e.g. 'fm_scoring'
    (Scoring Stats) or 'ws_defensive_locations' (Defensive Action Locations)
    not existing at all for a match saved before those namespaces were
    added - shows '-' for every player AND the Team Total, rather than a
    fabricated 0 that would misleadingly read as "confirmed zero shots/
    sprints/etc for every single player". A column that DOES exist but is
    blank for one specific player (a real, individually-confirmed zero -
    e.g. a substitute with 0 Tackles) still shows a real 0. Re-save the
    match to backfill a genuinely '-' column.

    Returns {'home': DataFrame, 'away': DataFrame} - either can be empty if
    that team has no saved rows for these namespaces yet at all (e.g. an
    older match saved before this category existed).
    """
    df = _fetch_player_namespaces(db, match_id, namespaces)
    if df.empty:
        empty = pd.DataFrame(columns=["Player"] + columns)
        return {"home": empty, "away": empty}

    missing_cols = [c for c in columns if c not in df.columns]
    present_cols = [c for c in columns if c in df.columns]

    for c in present_cols:
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)
        # xG/xA-style decimal stats keep 2 decimal places; every other
        # column here is a plain count, shown as a whole number. Decided by
        # a fixed name list rather than "is every value in this particular
        # match a whole number" so a column's display type can't flip
        # between matches depending on what happened to be scored that day.
        if c in _DECIMAL_STAT_COLUMNS:
            df[c] = df[c].round(2)
        else:
            df[c] = df[c].astype(int)

    def _one(team_name):
        sub = df[df["Team"] == team_name][["Player"] + present_cols].reset_index(drop=True)
        if sub.empty:
            return sub
        for c in missing_cols:
            sub[c] = "-"
        sub = sub[["Player"] + columns]
        total = {c: (sub[c].sum() if c in present_cols else "-") for c in columns}
        total["Player"] = "Team Total"
        return pd.concat([sub, pd.DataFrame([total])], ignore_index=True)

    return {"home": _one(home_team), "away": _one(away_team)}


# Column order for each Player Stats category table - see the docstrings on
# fetch_player_scoring_stats()/fetch_player_possession()/fetch_player_passing()/
# fetch_player_defensive_actions()/fetch_player_defensive_locations() below
# for where each field is sourced from.
_SCORING_STATS_COLUMNS = ["Minutes Played", "Goals", "Assists", "Shots", "SCA",
                           "NPxG", "PS-xG", "xA", "PK", "PK Attempted", "Sprints"]
_DECIMAL_STAT_COLUMNS = {"NPxG", "PS-xG", "xA"}
_POSSESSION_COLUMNS = ["Total Touches", "Own third", "Middle third", "Final third",
                        "Attacking Box", "Progressive Carries", "Carries into Final Third",
                        "Carries into Box", "Passes Received", "Progressive Passes Received"]
_PASSING_COLUMNS = ["Passes Completed", "Passes Attempted", "Passes Forward", "Headed",
                     "Crosses Attempted", "Crosses Completed", "Passes into Final 1/3",
                     "Passes into the Box", "Progressive Passes", "Shot Assists", "SCA",
                     "Line Breaking Passes"]
_DEFENSIVE_ACTIONS_COLUMNS = ["Tackles", "Interceptions", "Passes Blocked", "Shots Blocked"]
_DEFENSIVE_LOCATIONS_COLUMNS = [
    f"{stat} {third}" for stat in ("Tackles", "Interceptions", "Passes Blocked", "Ball Recoveries")
    for third in ("Own Third", "Middle Third", "Final Third")
]


def _insert_g_pk_column(df):
    """
    Inserts the derived 'G-PK' column (non-penalty goals - Goals minus PK,
    i.e. penalties actually SCORED, not PK Attempted) into a Scoring Stats
    table (see fetch_player_scoring_stats()), immediately to the right of
    Assists, per request. df is one side's DataFrame (home or away, already
    including its own 'Team Total' row) from _player_category_table() -
    its Goals/PK Team Total values are already sums-across-players, and
    Goals - PK computed on that summed row gives the correct season total
    directly (sum-then-subtract is mathematically identical to subtracting
    each player's own difference and summing those, same reasoning as this
    project's other derived difference columns).

    Shows '-' for every row if Goals or PK is entirely missing for this
    match (an older match saved before 'fm_scoring' existed - see
    _player_category_table()'s own docstring on when a column is entirely
    '-') rather than fabricating a number from missing data.
    """
    if df.empty or "Goals" not in df.columns or "PK" not in df.columns:
        return df
    df = df.copy()
    if (df["Goals"] == "-").any() or (df["PK"] == "-").any():
        g_pk = "-"
    else:
        g_pk = df["Goals"] - df["PK"]
    df.insert(df.columns.get_loc("Assists") + 1, "G-PK", g_pk)
    return df


def fetch_player_scoring_stats(db: DB, match_id, home_team, away_team) -> dict:
    """
    Scoring Stats category: Minutes Played, Goals, Assists, G-PK, Shots,
    SCA, NPxG, PS-xG, xA, PK, PK Attempted, Sprints - one row per player,
    'Team Total' row at the bottom. Minutes Played/Goals/Assists/Shots/
    NPxG/PS-xG/xA/PK/PK Attempted/Sprints all come from the 'fm_scoring'
    namespace (fotmob_report.compute_player_scoring_stats()); SCA is
    pulled in from 'ws_passing' instead of being duplicated into
    fm_scoring, since it's also the Passing category's own SCA figure
    (fotmob_report.compute_passing()'s real Shot-Creating-Actions count,
    not a FotMob proxy) - one real number, shown on both tables. G-PK is
    a derived column (Goals minus PK) added afterward - see
    _insert_g_pk_column().
    """
    tables = _player_category_table(db, match_id, home_team, away_team,
                                     ["fm_scoring", "ws_passing"], _SCORING_STATS_COLUMNS)
    return {side: _insert_g_pk_column(side_df) for side, side_df in tables.items()}


def fetch_player_possession(db: DB, match_id, home_team, away_team) -> dict:
    """
    Possession category: the same per-player fields as the WhoScored report's
    own Touches tab (Total Touches, thirds, Attacking Box, Progressive
    Carries/Carries into Final Third/Box, Passes Received, Progressive
    Passes Received) - read straight from the 'ws_touches' namespace
    (whoscored_report.compute_touches()'s player_third table).
    """
    return _player_category_table(db, match_id, home_team, away_team,
                                   ["ws_touches"], _POSSESSION_COLUMNS)


def fetch_player_passing(db: DB, match_id, home_team, away_team) -> dict:
    """
    Passing category: the same per-player fields as the WhoScored report's
    own Passing tab (Passes Completed/Attempted/Forward, Headed, Crosses
    Attempted/Completed, Passes into Final 1/3/the Box, Progressive Passes,
    Shot Assists, SCA - from the 'ws_passing' namespace, whoscored_report.
    compute_passing()), plus Line Breaking Passes (FotMob's own figure,
    'fm_line_breaking_passes' namespace) folded in as an extra column per
    the request to add it to this tab specifically.
    """
    return _player_category_table(db, match_id, home_team, away_team,
                                   ["ws_passing", "fm_line_breaking_passes"], _PASSING_COLUMNS)


def fetch_player_defensive_actions(db: DB, match_id, home_team, away_team) -> dict:
    """
    Defensive Actions category: the same per-player fields as the
    WhoScored report's own Defensive Actions tab (Tackles, Interceptions,
    Passes Blocked, Shots Blocked) - read straight from the 'ws_defensive'
    namespace (whoscored_report.compute_defensive_actions()).
    """
    return _player_category_table(db, match_id, home_team, away_team,
                                   ["ws_defensive"], _DEFENSIVE_ACTIONS_COLUMNS)


def fetch_player_defensive_locations(db: DB, match_id, home_team, away_team) -> dict:
    """
    Defensive Action Locations category: the same per-player fields as the
    WhoScored report's own Defensive Action Location tab (Tackles/
    Interceptions/Passes Blocked/Ball Recoveries, each broken down by Own/
    Middle/Final Third) - read straight from the 'ws_defensive_locations'
    namespace (whoscored_report.compute_defensive_action_location()). Only
    populated for matches saved after this namespace was added - an older
    match saved before then shows an all-zero table rather than raising
    (see _player_category_table()'s own docstring on missing columns).
    """
    return _player_category_table(db, match_id, home_team, away_team,
                                   ["ws_defensive_locations"], _DEFENSIVE_LOCATIONS_COLUMNS)


def fetch_distinct_players(db: DB) -> list:
    """
    Every distinct player name that's ever appeared in player_match_stats,
    sorted alphabetically - lets dashboard_app.py offer a dropdown instead
    of a free-text "type the exact name" box for Player Trends. A typed
    name has to match fetch_player_trends()'s WHERE p.player = ? exactly,
    including case and whitespace, with no fuzzy fallback - a dropdown
    populated from the real saved names removes that whole class of typo/
    capitalization bugs instead of just warning about it.
    """
    cur = db.execute("SELECT DISTINCT player FROM player_match_stats ORDER BY player")
    return [r[0] for r in cur.fetchall()]


def fetch_player_trends(db: DB, player: str) -> pd.DataFrame:
    cur = db.execute("""
        SELECT m.match_date, p.match_id, p.team, p.player, p.extra_json
        FROM player_match_stats p JOIN matches m ON m.match_id = p.match_id
        WHERE p.player = ?
        ORDER BY m.match_date ASC
    """, (player,))
    rows = [(r[0], r[1], r[2], r[3], r[4]) for r in cur.fetchall()]
    df = _flatten_extra(rows, ["match_date", "match_id", "team", "player"])
    return df


def fetch_shots(db: DB, match_id=None, team=None, player=None) -> pd.DataFrame:
    sql = "SELECT * FROM shots WHERE 1=1"
    params = []
    if match_id is not None:
        sql += " AND match_id = ?"
        params.append(str(match_id))
    if team is not None:
        sql += " AND team = ?"
        params.append(team)
    if player is not None:
        sql += " AND player = ?"
        params.append(player)
    cur = db.execute(sql, tuple(params))
    cols = [d[0] for d in cur.description]
    return pd.DataFrame(cur.fetchall(), columns=cols)


# Same column set/order as combined_report.COMBINED_SHOTS_COLUMNS - see
# fetch_shot_creating_actions() below for why this is reconstructed from
# the shots table rather than read as one of its own real columns.
_COMBINED_SHOTS_DISPLAY_COLUMNS = [
    'Minute', 'Added Time', 'Player', 'Team', 'xG', 'PSxG', 'Outcome', 'Distance (yd)',
    'Body Part', 'Situation', 'SCA 1 (Player)', 'SCA 1 (Action)', 'SCA 2 (Player)', 'SCA 2 (Action)',
]


def _fmt_shots_decimal(value):
    """
    Formats a Shots-tab xG/PSxG value to exactly two decimal places (e.g.
    "0.34", "1.00") for display. A genuinely missing value (None/NaN) is
    left as None rather than being coerced into a fabricated "0.00" -
    compute_combined_shots() deliberately leaves a shot's FotMob-sourced
    fields (including xG/PSxG) blank when that shot couldn't be matched to
    a FotMob shot (e.g. a shot-count mismatch leaves the extra WhoScored
    shot(s) without FotMob data), and this formatting step must preserve
    that "genuinely unknown" signal rather than overwrite it.
    """
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except TypeError:
        pass
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return value


def fetch_shot_creating_actions(db: DB, match_id) -> pd.DataFrame:
    """
    The match detail view's Shots tab: reconstructs exactly the table the
    combined report's own 'Shot Creating Actions' tab shows
    (combined_report.compute_combined_shots()'s output - WhoScored's shot
    list, enriched with the SCA1/SCA2 contributing actions, plus FotMob's
    Minute/Added Time/xG/PSxG/Outcome/Situation attached to each row).

    Every match saved via publish_report() already carries this data -
    shots_df passed in there is always report['combined_shots'] - but the
    'shots' table's own fixed columns (see upsert_shots()'s docstring) only
    cover Team/Player/Minute/Added Time/Situation/Body Part/Outcome/xG/
    xGOT-or-PSxG; Distance (yd) and the four SCA1/SCA2 fields aren't among
    those fixed columns, so upsert_shots() stashes them in shots.extra_json
    instead of dropping them. This just reads that JSON back out and
    reshapes the result into the exact same column set/order the combined
    report itself used, rather than the DB's own storage shape - so this is
    a reformat of already-saved data, not a new computation, and works
    retroactively for every match saved via the combined report (no
    re-save needed).
    """
    cur = db.execute("""
        SELECT team, player, minute, added_time, situation, body_part, outcome, xg, xgot, extra_json
        FROM shots
        WHERE match_id = ?
    """, (str(match_id),))
    records = []
    for team, player, minute, added_time, situation, body_part, outcome, xg, xgot, extra_json in cur.fetchall():
        extra = json.loads(extra_json) if extra_json else {}
        records.append({
            'Minute': minute,
            'Added Time': added_time,
            'Player': player,
            'Team': team,
            'xG': _fmt_shots_decimal(xg),
            'PSxG': _fmt_shots_decimal(xgot),
            'Outcome': outcome,
            'Distance (yd)': extra.get('Distance (yd)'),
            'Body Part': body_part,
            'Situation': situation,
            'SCA 1 (Player)': extra.get('SCA1_Player'),
            'SCA 1 (Action)': extra.get('SCA1_Action'),
            'SCA 2 (Player)': extra.get('SCA2_Player'),
            'SCA 2 (Action)': extra.get('SCA2_Action'),
        })
    df = pd.DataFrame(records, columns=_COMBINED_SHOTS_DISPLAY_COLUMNS)
    if df.empty:
        return df
    return df.sort_values(['Team', 'Minute', 'Added Time'], na_position='first').reset_index(drop=True)


def fetch_season_shot_totals(db: DB, competition=None):
    """
    Season-cumulative Shots/Goals/Total xG per team, broken down by shot
    'situation' (FotMob's own vocabulary - Open Play, Set Piece, Corner,
    ... - see fotmob_report.SITUATION_DISPLAY_MAP for the raw-to-display
    name mapping used by the dashboard's Shots tab), split into 'for'
    (that team's own shots) and 'against' (shots faced FROM whichever team
    they played in each match). Powers dashboard_app.py's season-wide Shots
    tab, which shows two small leaderboard tables (For/Against) rather than
    a per-match shot log.

    competition: optional matches.competition filter (see the League
    Overview tab's league dropdown) - None (the default) includes every
    saved match regardless of competition.

    'against' isn't a real column anywhere - a row in the shots table only
    knows which team took the shot, not who it was against - so this is
    derived here by joining each shot's match_id to that match's
    home_team/away_team and attributing the shot to whichever side ISN'T
    the shooting team. A shot whose own team doesn't match either the
    match's home_team or away_team (a genuine, unresolved team-name
    mismatch - see build_db_stats()'s docstring in batch_lib.py for the
    usual cause of this) can't be safely attributed to an opponent, so it's
    just dropped from the 'against' table only (still counted normally in
    'for').

    Returns (for_df, against_df), each with columns Team, Situation, Shots,
    Goals, Total xG - one row per (team, situation) combination that
    actually has at least one shot; a combination with zero shots simply
    has no row (callers should treat a missing combination as zero, not
    error). Situation is the RAW FotMob value (e.g. 'RegularPlay') -
    dashboard_app.py handles converting that to a friendly display label.
    """
    cols = ["Team", "Situation", "Shots", "Goals", "Total xG"]
    shots = fetch_shots(db)
    if shots.empty:
        return pd.DataFrame(columns=cols), pd.DataFrame(columns=cols)

    shots = shots.copy()
    # Some shots saved before upsert_shots() sanitized this already have the
    # literal string 'NaN' stored in the situation column (Postgres's own
    # text rendering of a stray float NaN that slipped through on insert -
    # see upsert_shots()'s docstring/comment) rather than a real NULL, so a
    # plain fillna() alone won't catch it - fold it into "Unknown" too.
    shots["situation"] = shots["situation"].replace(
        {"NaN": "Unknown", "nan": "Unknown", "": "Unknown"}
    )
    shots["situation"] = shots["situation"].fillna("Unknown")

    matches = fetch_matches(db)[["match_id", "home_team", "away_team", "competition"]]
    if competition is not None:
        matches = matches[matches["competition"] == competition]
    # Inner join (rather than the previous left join against every match)
    # so a competition filter actually drops shots from other competitions'
    # matches, not just leaves their home_team/away_team blank - every real
    # shot's match_id has a matching row in the (possibly filtered) matches
    # table, so this changes nothing when competition=None.
    merged = shots.merge(matches, on="match_id", how="inner")

    def _agg(df, team_col):
        if df.empty:
            return pd.DataFrame(columns=cols)
        out = (df.groupby([team_col, "situation"])
                 .agg(Shots=("id", "size"),
                      Goals=("outcome", lambda s: int((s == "Goal").sum())),
                      **{"Total xG": ("xg", "sum")})
                 .reset_index()
                 .rename(columns={team_col: "Team", "situation": "Situation"}))
        out["Total xG"] = out["Total xG"].round(2)
        return out[cols]

    for_df = _agg(merged, "team")

    def _opponent(r):
        if r["team"] == r["home_team"]:
            return r["away_team"]
        if r["team"] == r["away_team"]:
            return r["home_team"]
        return None  # unresolved team-name mismatch - can't attribute safely

    against_raw = merged.copy()
    against_raw["opponent"] = against_raw.apply(_opponent, axis=1)
    against_raw = against_raw.dropna(subset=["opponent"])
    against_df = _agg(against_raw, "opponent")

    return for_df, against_df


def fetch_passes(db: DB, match_id=None, passer=None, receiver=None, completed_only=False,
                  team=None) -> pd.DataFrame:
    """
    Passes for one match (pass a match_id), or across EVERY published match
    (leave match_id=None) for a season-long Pass Map/Passes Received map -
    optionally filtered down to one passer (Pass Map) or one receiver
    (Passes Received map), and optionally to one team (mostly useful
    alongside match_id=None, since a player who's transferred mid-season
    would otherwise mix passes from two different teams into one map).
    Column names come back as end_x/end_y (the DB's column names) rather
    than endX/endY (whoscored_report.py's own dataframe convention) -
    dashboard_app.py renames them back before handing the result to
    pitch_viz.plot_pass_map(), which expects the endX/endY spelling.
    """
    sql = "SELECT * FROM passes WHERE 1=1"
    params = []
    if match_id is not None:
        sql += " AND match_id = ?"
        params.append(str(match_id))
    if team is not None:
        sql += " AND team = ?"
        params.append(team)
    if passer is not None:
        sql += " AND passer = ?"
        params.append(passer)
    if receiver is not None:
        sql += " AND receiver = ?"
        params.append(receiver)
    if completed_only:
        sql += " AND completed = 1"
    cur = db.execute(sql, tuple(params))
    cols = [d[0] for d in cur.description]
    df = pd.DataFrame(cur.fetchall(), columns=cols)
    if not df.empty:
        df["completed"] = df["completed"].astype(bool)
        df["is_progressive"] = df["is_progressive"].astype(bool)
        df["is_key_pass"] = df["is_key_pass"].astype(bool)
        # Some passes saved before upsert_passes() sanitized this have the
        # literal string 'NaN' stored in receiver (Postgres's own text
        # rendering of a stray float NaN from _pass_receiver_map()'s
        # next-event heuristic finding no receiver - see upsert_passes()'s
        # comment) rather than a real NULL. dashboard_app.py's player
        # dropdowns already .dropna() this column, so turning it into a
        # real null here is enough to make it disappear as a "player".
        df["receiver"] = df["receiver"].replace({"NaN": None, "nan": None, "": None})
    return df


def fetch_touches(db: DB, match_id=None, player=None, team=None) -> pd.DataFrame:
    """
    Touches for one match (pass a match_id), or across EVERY published match
    (leave match_id=None) for a season-long touch heat map - optionally
    filtered to one player and/or one team (same transfer-mid-season
    reasoning as fetch_passes()'s team filter).
    """
    sql = "SELECT * FROM touches WHERE 1=1"
    params = []
    if match_id is not None:
        sql += " AND match_id = ?"
        params.append(str(match_id))
    if team is not None:
        sql += " AND team = ?"
        params.append(team)
    if player is not None:
        sql += " AND player = ?"
        params.append(player)
    cur = db.execute(sql, tuple(params))
    cols = [d[0] for d in cur.description]
    return pd.DataFrame(cur.fetchall(), columns=cols)


# ============================================================
# Season-cumulative TEAM totals (dashboard_app.py's default "Team Totals" tab)
# ============================================================
def fetch_league_table(db: DB, competition=None) -> pd.DataFrame:
    """
    Season standings: one row per team, with the standard league table
    columns (Played, W, D, L, GF, GA, GD, Points) plus this project's own
    xG/xGA/xGD, all built from team_match_stats' 'fm_totals' (Goals, Total
    xG) - the same source fetch_fixtures() already reads Score/xG from, so
    it inherits that function's team-name reconciliation (see
    build_db_stats()'s docstring in batch_lib.py) automatically, since
    matches.home_team/away_team and team_match_stats' team keys are already
    one consistent (WhoScored's) name per team.

    competition: optional matches.competition filter (see the League
    Overview tab's league dropdown) - None (the default) includes every
    saved match regardless of competition, same as before this parameter
    existed.

    A match missing fm_totals.Goals for EITHER side (not yet saved with
    FotMob data, or a genuinely unresolved team-name mismatch) is skipped
    entirely rather than counted as a 0-0 draw or a partial result - an
    incomplete match should reduce both teams' Played count implicitly (by
    not counting it), not silently corrupt their record.

    Sorted by Points, then Goal Difference, then Goals For, all descending -
    the standard football league table tiebreaker order.
    """
    cols = ["Team", "Played", "W", "D", "L", "GF", "GA", "GD", "Points", "xG", "xGA", "xGD"]
    matches = fetch_matches(db)
    if matches.empty:
        return pd.DataFrame(columns=cols)
    if competition is not None:
        matches = matches[matches["competition"] == competition]
        if matches.empty:
            return pd.DataFrame(columns=cols)

    cur = db.execute("SELECT match_id, team, extra_json FROM team_match_stats")
    fm_totals_by_match = {}
    for match_id, team, extra_json in cur.fetchall():
        extra = json.loads(extra_json) if extra_json else {}
        fm_totals_by_match.setdefault(match_id, {})[team] = extra.get("fm_totals") or {}

    stats = {}

    def _team_row(team):
        return stats.setdefault(team, {"Played": 0, "W": 0, "D": 0, "L": 0,
                                        "GF": 0, "GA": 0, "xG": 0.0, "xGA": 0.0})

    for _, m in matches.iterrows():
        team_totals = fm_totals_by_match.get(m["match_id"], {})
        home_totals = team_totals.get(m["home_team"], {})
        away_totals = team_totals.get(m["away_team"], {})
        home_goals, away_goals = home_totals.get("Goals"), away_totals.get("Goals")
        if home_goals is None or away_goals is None:
            continue  # incomplete data for this match - don't guess, just skip it

        home_row = _team_row(m["home_team"])
        away_row = _team_row(m["away_team"])
        home_row["Played"] += 1
        away_row["Played"] += 1
        home_row["GF"] += home_goals
        home_row["GA"] += away_goals
        away_row["GF"] += away_goals
        away_row["GA"] += home_goals
        home_row["xG"] += home_totals.get("Total xG") or 0.0
        home_row["xGA"] += away_totals.get("Total xG") or 0.0
        away_row["xG"] += away_totals.get("Total xG") or 0.0
        away_row["xGA"] += home_totals.get("Total xG") or 0.0

        if home_goals > away_goals:
            home_row["W"] += 1
            away_row["L"] += 1
        elif home_goals < away_goals:
            away_row["W"] += 1
            home_row["L"] += 1
        else:
            home_row["D"] += 1
            away_row["D"] += 1

    records = []
    for team, s in stats.items():
        gd = s["GF"] - s["GA"]
        points = s["W"] * 3 + s["D"]
        records.append({
            "Team": team, "Played": s["Played"], "W": s["W"], "D": s["D"], "L": s["L"],
            "GF": s["GF"], "GA": s["GA"], "GD": gd, "Points": points,
            "xG": round(s["xG"], 2), "xGA": round(s["xGA"], 2),
            "xGD": round(s["xG"] - s["xGA"], 2),
        })
    df = pd.DataFrame(records, columns=cols)
    return df.sort_values(["Points", "GD", "GF"], ascending=False).reset_index(drop=True)


def ordinal(n):
    """1 -> '1st', 2 -> '2nd', 3 -> '3rd', 4 -> '4th', 11/12/13 -> '11th'/
    '12th'/'13th' (the standard English-ordinal exception for the teens),
    21 -> '21st', etc. Returns '-' for None (a rank that couldn't be
    computed - see fetch_team_page_stats()'s docstring)."""
    if n is None:
        return "-"
    n = int(n)
    if 11 <= (n % 100) <= 13:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def fetch_available_seasons(db: DB) -> list:
    """
    Every season label (see _season_label()) with at least one saved match,
    most recent first - backs the Team Page's season dropdown. Just one
    entry today (this project only has one season's worth of data so far),
    but the dropdown is wired up now so a second season needs no UI changes
    later, just more saved matches.
    """
    matches = fetch_matches(db)
    if matches.empty:
        return []
    seasons = matches["match_date"].apply(_season_label).dropna().unique().tolist()
    return sorted(seasons, reverse=True)


def fetch_available_competitions(db: DB) -> list:
    """
    Every distinct matches.competition value with at least one saved match,
    alphabetically - backs the League Overview tab's league dropdown (see
    dashboard_app.py). Just one entry today ('Premier League' - the free-
    text value the 'Save to Database' form defaults to), but the dropdown
    and every season-cumulative fetch function it feeds (fetch_league_
    table(), fetch_season_shot_totals(), etc. - all take an optional
    competition= filter now) are wired up so a second competition needs no
    further code changes later, just matches saved under a different
    Competition value.
    """
    matches = fetch_matches(db)
    if matches.empty:
        return []
    return sorted(matches["competition"].dropna().unique().tolist())


def _match_ids_for_competition(db: DB, competition) -> set:
    """
    The set of match_id strings belonging to one matches.competition value
    (exact string match) - shared plumbing for scoping any season-
    cumulative fetch function (League Table, Team Stats' category tables)
    to one league via the League Overview tab's dropdown. Returns None
    (meaning "no filter, include every match") when competition is None -
    every caller below treats that as leave-everything-in, so passing
    competition=None preserves each function's original all-matches
    behavior exactly.
    """
    if competition is None:
        return None
    matches = fetch_matches(db)
    if matches.empty:
        return set()
    return set(matches.loc[matches["competition"] == competition, "match_id"].astype(str))


def _build_team_season_table(db: DB, season, competition) -> pd.DataFrame:
    """
    One row per team playing in this exact (season, competition) - the
    superset of stats the Team Page needs to show ONE of these teams plus
    rank it against the rest: overall/home/away W-D-L-Points, goals for/
    against, and the 5 headline subheading stats (Avg Possession, Shots,
    xG, Shots Against, xGA - all as per-game averages). Same skip-if-
    incomplete convention as fetch_league_table() (a match missing either
    side's fm_totals.Goals is left out entirely, rather than guessed at).

    Scoped to one competition (not just one season) because a team can play
    in more than one competition (league + cup) in the same season - mixing
    those into one table would rank a team against opponents it isn't
    really competing against for the league table's own purposes. See
    fetch_team_page_stats() for how a team's own primary competition this
    season is chosen.
    """
    cols = ["Team", "Played", "W", "D", "L", "GF", "GA", "GD", "Points", "PPG",
            "GF_PG", "GA_PG", "HPlayed", "HW", "HD", "HL", "HPoints",
            "APlayed", "AW", "AD", "AL", "APoints",
            "Possession", "Shots_PG", "ShotsA_PG", "xG_PG", "xGA_PG"]
    matches = fetch_matches(db)
    if matches.empty:
        return pd.DataFrame(columns=cols)
    matches = matches.copy()
    matches["season"] = matches["match_date"].apply(_season_label)
    scoped = matches[(matches["season"] == season) & (matches["competition"] == competition)]
    if scoped.empty:
        return pd.DataFrame(columns=cols)

    cur = db.execute("SELECT match_id, team, extra_json FROM team_match_stats")
    extra_by_match = {}
    for match_id, team, extra_json in cur.fetchall():
        extra_by_match.setdefault(match_id, {})[team] = json.loads(extra_json) if extra_json else {}

    stats = {}

    def _row(team):
        return stats.setdefault(team, {
            "Played": 0, "W": 0, "D": 0, "L": 0, "GF": 0, "GA": 0,
            "HPlayed": 0, "HW": 0, "HD": 0, "HL": 0,
            "APlayed": 0, "AW": 0, "AD": 0, "AL": 0,
            "xG": 0.0, "xGA": 0.0, "Shots": 0, "ShotsA": 0,
            "PossessionSum": 0.0, "PossessionN": 0,
        })

    for _, m in scoped.iterrows():
        team_extras = extra_by_match.get(m["match_id"], {})
        home_fm = (team_extras.get(m["home_team"]) or {}).get("fm_totals") or {}
        away_fm = (team_extras.get(m["away_team"]) or {}).get("fm_totals") or {}
        home_goals, away_goals = home_fm.get("Goals"), away_fm.get("Goals")
        if home_goals is None or away_goals is None:
            continue  # incomplete data for this match - don't guess, just skip it

        home_row, away_row = _row(m["home_team"]), _row(m["away_team"])
        home_row["Played"] += 1
        away_row["Played"] += 1
        home_row["HPlayed"] += 1
        away_row["APlayed"] += 1
        home_row["GF"] += home_goals
        home_row["GA"] += away_goals
        away_row["GF"] += away_goals
        away_row["GA"] += home_goals
        home_row["xG"] += home_fm.get("Total xG") or 0.0
        home_row["xGA"] += away_fm.get("Total xG") or 0.0
        away_row["xG"] += away_fm.get("Total xG") or 0.0
        away_row["xGA"] += home_fm.get("Total xG") or 0.0
        home_row["Shots"] += home_fm.get("Shots") or 0
        home_row["ShotsA"] += away_fm.get("Shots") or 0
        away_row["Shots"] += away_fm.get("Shots") or 0
        away_row["ShotsA"] += home_fm.get("Shots") or 0

        home_poss, away_poss = home_fm.get("Ball possession"), away_fm.get("Ball possession")
        if home_poss is not None:
            home_row["PossessionSum"] += float(home_poss)
            home_row["PossessionN"] += 1
        if away_poss is not None:
            away_row["PossessionSum"] += float(away_poss)
            away_row["PossessionN"] += 1

        if home_goals > away_goals:
            home_row["W"] += 1
            home_row["HW"] += 1
            away_row["L"] += 1
            away_row["AL"] += 1
        elif home_goals < away_goals:
            away_row["W"] += 1
            away_row["AW"] += 1
            home_row["L"] += 1
            home_row["HL"] += 1
        else:
            home_row["D"] += 1
            home_row["HD"] += 1
            away_row["D"] += 1
            away_row["AD"] += 1

    records = []
    for team, s in stats.items():
        played = s["Played"] or 0
        points = s["W"] * 3 + s["D"]
        records.append({
            "Team": team, "Played": played, "W": s["W"], "D": s["D"], "L": s["L"],
            "GF": s["GF"], "GA": s["GA"], "GD": s["GF"] - s["GA"], "Points": points,
            "PPG": round(points / played, 2) if played else 0.0,
            "GF_PG": round(s["GF"] / played, 2) if played else 0.0,
            "GA_PG": round(s["GA"] / played, 2) if played else 0.0,
            "HPlayed": s["HPlayed"], "HW": s["HW"], "HD": s["HD"], "HL": s["HL"],
            "HPoints": s["HW"] * 3 + s["HD"],
            "APlayed": s["APlayed"], "AW": s["AW"], "AD": s["AD"], "AL": s["AL"],
            "APoints": s["AW"] * 3 + s["AD"],
            "Possession": (round(s["PossessionSum"] / s["PossessionN"], 1)
                            if s["PossessionN"] else None),
            "Shots_PG": round(s["Shots"] / played, 2) if played else 0.0,
            "ShotsA_PG": round(s["ShotsA"] / played, 2) if played else 0.0,
            "xG_PG": round(s["xG"] / played, 2) if played else 0.0,
            "xGA_PG": round(s["xGA"] / played, 2) if played else 0.0,
        })
    return pd.DataFrame(records, columns=cols)


def fetch_team_page_stats(db: DB, team, season=None) -> dict:
    """
    Everything the Team Page needs for one team/season: overall/home/away
    record + points, goals for/against (with per-game rates), league rank,
    and the 5 headline stats (Avg Possession, Shots, xG, Shots Against,
    xGA - all per-game) each with this team's rank among every OTHER team
    in the same competition this season (Possession/Shots/xG ranked highest-
    first, Shots Against/xGA ranked lowest-first - allowing fewer shots/xG
    against is the good direction there).

    season defaults to this team's own most recent season if not given.
    This team's "competition" for ranking purposes is whichever competition
    value is most common across its matches this season (the main league,
    for a team that's also played the odd cup match this season) - see
    _build_team_season_table()'s docstring for why ranking needs to be
    scoped to one competition, not just one season.

    Returns None if this team has no saved matches at all for the resolved
    season (an unknown team, or a season with no data yet). A rank that
    couldn't be computed (this team has no value for that stat at all - an
    old-enough gap in saved data) comes back as None in the '..._rank'
    fields - callers should show '-' rather than a fabricated rank.
    """
    matches = fetch_matches(db)
    if matches.empty:
        return None
    matches = matches.copy()
    matches["season"] = matches["match_date"].apply(_season_label)
    team_matches = matches[(matches["home_team"] == team) | (matches["away_team"] == team)]
    if team_matches.empty:
        return None

    if season is None:
        available = sorted(team_matches["season"].dropna().unique(), reverse=True)
        if not available:
            return None
        season = available[0]
    team_matches = team_matches[team_matches["season"] == season]
    if team_matches.empty:
        return None

    competition_counts = team_matches["competition"].dropna()
    if competition_counts.empty:
        return None
    competition = competition_counts.mode().iloc[0]

    table = _build_team_season_table(db, season, competition)
    if table.empty or team not in table["Team"].values:
        return None

    table = table.sort_values(["Points", "GD", "GF"], ascending=False).reset_index(drop=True)
    league_rank = int(table.index[table["Team"] == team][0]) + 1
    row = table[table["Team"] == team].iloc[0]

    def _rank_of(col, ascending):
        if pd.isna(row[col]):
            return None
        ranked = table[col].rank(method="min", ascending=ascending)
        return int(ranked[table["Team"] == team].iloc[0])

    return {
        "season": season,
        "team": team,
        "competition": competition,
        "n_teams": len(table),
        "played": int(row["Played"]), "w": int(row["W"]), "d": int(row["D"]), "l": int(row["L"]),
        "points": int(row["Points"]), "ppg": row["PPG"], "league_rank": league_rank,
        "home": {"played": int(row["HPlayed"]), "w": int(row["HW"]), "d": int(row["HD"]),
                 "l": int(row["HL"]), "points": int(row["HPoints"])},
        "away": {"played": int(row["APlayed"]), "w": int(row["AW"]), "d": int(row["AD"]),
                 "l": int(row["AL"]), "points": int(row["APoints"])},
        "goals_for": int(row["GF"]), "goals_for_pg": row["GF_PG"],
        "goals_against": int(row["GA"]), "goals_against_pg": row["GA_PG"],
        "possession": row["Possession"], "possession_rank": _rank_of("Possession", ascending=False),
        "shots_pg": row["Shots_PG"], "shots_rank": _rank_of("Shots_PG", ascending=False),
        "xg_pg": row["xG_PG"], "xg_rank": _rank_of("xG_PG", ascending=False),
        "shots_against_pg": row["ShotsA_PG"],
        "shots_against_rank": _rank_of("ShotsA_PG", ascending=True),
        "xga_pg": row["xGA_PG"], "xga_rank": _rank_of("xGA_PG", ascending=True),
    }


# Yellow/Red Cards - their own 'fm_cards' namespace (fotmob_report.
# extract_player_cards(), read from matchFacts.events rather than
# playerStats like every _SCORING_STATS_COLUMNS field) - kept as a separate
# list rather than folded into _SCORING_STATS_COLUMNS since that constant
# also backs the per-match Scoring Stats category table on the match detail
# view (fetch_player_scoring_stats()), which was NOT asked to grow these
# columns - only this season-cumulative Team Page table was.
_CARD_COLUMNS = ["Yellow Cards", "Red Cards"]

# The 6 stats fetch_team_season_scoring_stats() also shows as a Per-90 rate,
# in the exact order requested. Each name here must be a key already
# present in that function's per-player totals dict (i.e. in
# _SCORING_STATS_COLUMNS) - "SCA" is the only one of these NOT sourced from
# 'fm_scoring' itself (it's summed in alongside the fm_scoring columns from
# 'ws_passing' instead - see _fetch_player_namespaces()'s docstring), which
# still works fine since the totals dict doesn't care which namespace a
# number originally came from.
_PER90_STATS = ["Goals", "Assists", "Shots", "NPxG", "xA", "SCA"]


def _per90(stat_total, minutes_total):
    """
    (stat / Minutes Played) * 90, rounded to 2 decimals - the standard
    "rate as if they'd played a full 90" normalization. Returns '-' (same
    convention as this table's own Age column) rather than dividing by
    zero for a player with 0 total minutes (an unused substitute who's
    only ever been an unused sub) - a per-90 RATE is genuinely undefined
    there, not zero.
    """
    if not minutes_total or minutes_total <= 0:
        return "-"
    return round(stat_total / minutes_total * 90, 2)


def fetch_team_season_scoring_stats(db: DB, team, season) -> pd.DataFrame:
    """
    Season-cumulative version of the match report's own Scoring Stats
    category table (see fetch_player_scoring_stats()) for the Team Page:
    every player who's appeared for this team in this season, with their
    Goals/Assists/Shots/SCA/NPxG/PS-xG/xA/PK/PK Attempted/Sprints/Minutes
    (FotMob's own 'Minutes Played', relabeled 'Minutes' here per request)
    SUMMED across every one of this team's matches this season, rather than
    shown for just one match - plus three columns the match report doesn't
    have at all: Age, Appearances, Starts (in that order, right after
    Player - Age specifically per request; Appearances/Starts because they
    only make sense season-wide, not per-match). Column labels otherwise
    match the match report's own exactly (no 'Total ...' relabeling) even
    though every number here is a season sum. Only counts what this player
    did while playing for THIS team - a mid-season transfer's stats at
    their previous club aren't included.

    Three column GROUPS, per request - purely a display concern (this
    function just returns one flat DataFrame; dashboard_app._render_
    grouped_stats_table() is what actually draws the merged group-header
    row above them):
      - Playing Time: Appearances, Starts, Minutes.
      - Totals: Goals through Red Cards (everything summed as a plain
        running total across the season - Yellow Cards/Red Cards summed
        the exact same simple way as Goals/Shots/etc, just from a
        different source namespace - see _CARD_COLUMNS above).
      - Per 90: Goals/Assists/Shots/NPxG/xA/SCA, each recomputed as
        (season total / season Minutes) * 90 - see _per90() above. These
        columns intentionally reuse the SAME display labels as their
        Totals-block counterparts (e.g. two separate "Goals" columns) -
        standard practice on stats sites (FBref etc.) for a totals-vs-per90
        pair, and disambiguated by which group header they sit under
        rather than by the column label itself. Internally these are kept
        as distinctly-named columns ('Goals (Per 90)', etc.) since a plain
        DataFrame/dict can't hold two columns with the identical name -
        dashboard_app._render_grouped_stats_table() strips the ' (Per 90)'
        suffix back off before display.

    Age is fundamentally different from every summed column here - FotMob
    only ever reports "this player's age AS OF this match's date", never a
    real birthdate (see fotmob_report.extract_player_age_and_start()), so
    it can't be summed/averaged the way Goals or Minutes can, and it WOULD
    silently increment mid-season if simply re-read from each new match
    (a real birthday can fall between two of this team's matches). Per
    request that this column "shouldn't change during the season", this
    uses whichever of this team's saved matches is EARLIEST in the season
    for a given player and freezes that reading for the rest of the season,
    regardless of how many later matches get saved afterward - the first
    real match of a season and "the first day of the season" are close
    enough in practice that this is normally exact, and it's guaranteed
    stable either way. Appearances counts matches with Minutes Played > 0
    (the standard definition - being an unused substitute doesn't count);
    Starts counts matches FotMob's own lineup marked this player as part of
    the starting XI (fm_lineup's 'Started' flag) regardless of how long
    they were on the pitch. 'Team Total' sums Appearances/Starts/every
    other Totals-block column the same way the match report's own Team
    Total row does; Age has no sensible team total, so it shows '-' there,
    same as every individual player row with no Age reading at all. The
    Team Total row's OWN Per-90 figures are recomputed from the team's
    SUMMED totals (its own summed Goals / its own summed Minutes * 90),
    never by averaging each player's individual per-90 rate - same
    "recompute the rate from summed raw counts, don't average rates"
    principle as fetch_season_passing_totals()'s Pass Completion %, which
    avoids wrongly weighting a 5-minute cameo's rate the same as a full 90.

    A match missing 'fm_scoring'/'ws_passing'/'fm_lineup'/'fm_cards'
    entirely (an older save from before those namespaces existed) simply
    doesn't contribute to that player's numbers for that match - there's no
    clean per-cell way to flag "an early match's data is missing from this
    total" the way a single match's '-' convention works, so every summed
    number here is always a real (if potentially undercounted for very old
    data) total, never '-'. (Yellow Cards/Red Cards specifically default to
    0 for a match with no fm_cards namespace, exactly like a match where a
    player genuinely wasn't carded - there's no way to tell those two cases
    apart from this data alone.)

    Sorted by Minutes, descending (most-used players first). Returns an
    empty DataFrame if this team has no matches saved for this season.
    """
    sum_cols = _SCORING_STATS_COLUMNS + _CARD_COLUMNS  # internal accumulation keeps the source "Minutes Played" name
    out_cols = (
        ["Player", "Age", "Appearances", "Starts"]
        + ["Minutes" if c == "Minutes Played" else c for c in sum_cols]
        + [f"{stat} (Per 90)" for stat in _PER90_STATS]
    )

    matches = fetch_matches(db)
    if matches.empty:
        return pd.DataFrame(columns=out_cols)
    matches = matches.copy()
    matches["season"] = matches["match_date"].apply(_season_label)
    team_matches = matches[
        ((matches["home_team"] == team) | (matches["away_team"] == team))
        & (matches["season"] == season)
    ].sort_values("match_date")  # chronological - "earliest match with an Age reading" needs this order
    if team_matches.empty:
        return pd.DataFrame(columns=out_cols)

    sums, appearances, starts, ages = {}, {}, {}, {}
    for match_id in team_matches["match_id"]:
        df = _fetch_player_namespaces(
            db, match_id, ["fm_scoring", "ws_passing", "fm_lineup", "fm_cards"]
        )
        if df.empty:
            continue
        sub = df[df["Team"] == team]
        if sub.empty:
            continue
        present_cols = [c for c in sum_cols if c in sub.columns]
        for _, r in sub.iterrows():
            player = r["Player"]
            row_totals = sums.setdefault(player, {c: 0.0 for c in sum_cols})
            for c in present_cols:
                v = pd.to_numeric(r[c], errors="coerce")
                row_totals[c] += 0.0 if pd.isna(v) else float(v)

            minutes = pd.to_numeric(r.get("Minutes Played"), errors="coerce")
            if pd.notna(minutes) and minutes > 0:
                appearances[player] = appearances.get(player, 0) + 1

            if r.get("Started") is True:
                starts[player] = starts.get(player, 0) + 1

            if player not in ages:
                age_val = r.get("Age")
                if pd.notna(age_val):
                    ages[player] = int(age_val)

    if not sums:
        return pd.DataFrame(columns=out_cols)

    records = []
    for player, totals in sums.items():
        rec = {
            "Player": player,
            "Age": ages.get(player, "-"),
            "Appearances": appearances.get(player, 0),
            "Starts": starts.get(player, 0),
        }
        for c in sum_cols:
            v = totals[c]
            out_name = "Minutes" if c == "Minutes Played" else c
            rec[out_name] = round(v, 2) if c in _DECIMAL_STAT_COLUMNS else int(round(v))
        for stat in _PER90_STATS:
            rec[f"{stat} (Per 90)"] = _per90(totals[stat], totals["Minutes Played"])
        records.append(rec)

    out = (pd.DataFrame(records, columns=out_cols)
           .sort_values("Minutes", ascending=False)
           .reset_index(drop=True))

    team_total = {"Player": "Team Total", "Age": "-"}
    for c in out_cols:
        if c in ("Player", "Age") or c.endswith("(Per 90)"):
            continue
        s = out[c].sum()
        team_total[c] = round(s, 2) if c in _DECIMAL_STAT_COLUMNS else int(round(s))
    for stat in _PER90_STATS:
        team_total[f"{stat} (Per 90)"] = _per90(team_total[stat], team_total["Minutes"])

    return pd.concat([out, pd.DataFrame([team_total], columns=out_cols)], ignore_index=True)


# The 8 stats fetch_team_season_plus_minus() shows, in the order requested
# (Goal Difference/xG Difference slotted in right after their own For/
# Against pair) - also the exact list it shows a Per 90 rate for.
_PLUS_MINUS_STATS = ["Goals For", "Goals Against", "Goal Difference", "Shots", "Shots Against",
                      "xG", "xG Against", "xG Difference"]
_PLUS_MINUS_DECIMAL_COLUMNS = {"xG", "xG Against", "xG Difference"}


def fetch_team_season_plus_minus(db: DB, team, season) -> pd.DataFrame:
    """
    Season-cumulative version of the match report's own FM Plus/Minus
    table (fotmob_report.compute_plus_minus() - Goals For/Against, Shots/
    Against, xG/Against, each totaled ONLY across the minutes a player was
    actually on the pitch for, not the match's grand total - see that
    function's own docstring) for the Team Page's 'Playing Time' table.
    Every player who's appeared for this team this season, with each of
    compute_plus_minus()'s own numbers SUMMED across every match, plus two
    derived columns - Goal Difference (Goals For - Goals Against) and xG
    Difference (xG - xG Against) - and a Per 90 rate for every one of
    those 8 stats.

    Same '(stat / Minutes) * 90' formula, and '-' for a player with 0 total
    minutes, as fetch_team_season_scoring_stats()'s own Per 90 columns -
    see _per90() above; this reuses that exact same helper. UNLIKE that
    function, though, 'Team Total' here is NOT out[c].sum() across the
    player rows above - each player row already carries the WHOLE team's
    Shots/Goals/xG for whatever window they were on the pitch (that's the
    entire point of a plus/minus table), so naively summing every player's
    row would multiply the real season total by roughly however many
    players were on the pitch at once. Team Total is instead computed
    independently, straight from the shots table, using the exact same
    non-penalty + plain-per-shot-xG-sum rule compute_plus_minus() applies
    per player window (see that function's own docstring on why this is
    a plain sum rather than sequence-combined - by request, so a player
    who played every minute of a match shows the same non-penalty xG as
    that match's own Team Totals figure) - just over each WHOLE match
    instead of one player's slice of it, then summed across this team's
    matches this season. Its own 'Minutes' is match-count × 90 (not
    summed player-minutes), so its Per 90 figures come out as a "per
    game" rate rather than being divided by an inflated denominator to
    compensate.

    Goal Difference/xG Difference (both the per-player rows AND Team
    Total) are computed from each one's own Goals For/Against and xG/xG
    Against (sum-then-subtract) - mathematically identical to subtracting
    each match's own difference and then summing those (subtraction is
    linear), so there's exactly one 'true' season Goal Difference/xG
    Difference here, not an approximation of one.

    Two-row merged-header display (a 'Totals' group for every summed
    column, a 'Per 90' group for the 8 rate columns) is purely a display
    concern, same as fetch_team_season_scoring_stats() - this function
    just returns one flat DataFrame; dashboard_app._render_playing_time_
    table() is what actually draws the merged group-header row.

    Sorted by Minutes, descending, same convention as General Stats.
    Returns an empty DataFrame if this team has no matches saved for this
    season.
    """
    sum_cols = ["Minutes Played", "Goals For", "Goals Against", "Shots", "Shots Against", "xG", "xG Against"]
    out_cols = (
        ["Player", "Minutes", "Goals For", "Goals Against", "Goal Difference",
         "Shots", "Shots Against", "xG", "xG Against", "xG Difference"]
        + [f"{stat} (Per 90)" for stat in _PLUS_MINUS_STATS]
    )

    matches = fetch_matches(db)
    if matches.empty:
        return pd.DataFrame(columns=out_cols)
    matches = matches.copy()
    matches["season"] = matches["match_date"].apply(_season_label)
    team_matches = matches[
        ((matches["home_team"] == team) | (matches["away_team"] == team))
        & (matches["season"] == season)
    ]
    if team_matches.empty:
        return pd.DataFrame(columns=out_cols)

    sums = {}
    for match_id in team_matches["match_id"]:
        df = _fetch_player_namespaces(db, match_id, ["fm_plus_minus"])
        if df.empty:
            continue
        sub = df[df["Team"] == team]
        if sub.empty:
            continue
        present_cols = [c for c in sum_cols if c in sub.columns]
        for _, r in sub.iterrows():
            player = r["Player"]
            row_totals = sums.setdefault(player, {c: 0.0 for c in sum_cols})
            for c in present_cols:
                v = pd.to_numeric(r[c], errors="coerce")
                row_totals[c] += 0.0 if pd.isna(v) else float(v)

    if not sums:
        return pd.DataFrame(columns=out_cols)

    records = []
    for player, totals in sums.items():
        minutes = totals["Minutes Played"]
        goal_diff = totals["Goals For"] - totals["Goals Against"]
        xg_diff = totals["xG"] - totals["xG Against"]
        rec = {
            "Player": player,
            "Minutes": int(round(minutes)),
            "Goals For": int(round(totals["Goals For"])),
            "Goals Against": int(round(totals["Goals Against"])),
            "Goal Difference": int(round(goal_diff)),
            "Shots": int(round(totals["Shots"])),
            "Shots Against": int(round(totals["Shots Against"])),
            "xG": round(totals["xG"], 2),
            "xG Against": round(totals["xG Against"], 2),
            "xG Difference": round(xg_diff, 2),
        }
        stat_values = {
            "Goals For": totals["Goals For"], "Goals Against": totals["Goals Against"],
            "Goal Difference": goal_diff, "Shots": totals["Shots"],
            "Shots Against": totals["Shots Against"], "xG": totals["xG"],
            "xG Against": totals["xG Against"], "xG Difference": xg_diff,
        }
        for stat in _PLUS_MINUS_STATS:
            rec[f"{stat} (Per 90)"] = _per90(stat_values[stat], minutes)
        records.append(rec)

    out = (pd.DataFrame(records, columns=out_cols)
           .sort_values("Minutes", ascending=False)
           .reset_index(drop=True))

    # Team Total is deliberately NOT out[c].sum() the way every other
    # Team-Total row in this module is computed - see this function's own
    # docstring for why: each player row above already carries the WHOLE
    # team's Shots/Goals/xG for whatever window they were on the pitch, so
    # a team that used only 11 players all game would have each of them
    # showing the SAME real match totals, and summing all 11 rows would
    # overstate the season total roughly 11x (confirmed bug: a team whose
    # real season total was 53 shots showed 583). Team Total is computed
    # independently here instead, straight from the shots table, using the
    # exact same non-penalty + plain-per-shot-xG-sum rule fotmob_report.
    # compute_plus_minus() uses for each player's own window (deliberately
    # NOT sequence-combined - see that function's own docstring) - just
    # applied to the WHOLE match rather than one player's slice of it,
    # then summed across every one of this team's matches this season.
    # 'Minutes' here is match-count * 90 (this season's total team playing
    # time), not summed player-minutes, so the Per 90 figures come out as
    # a sensible "per game" rate rather than being divided by an ~11x-
    # inflated denominator to match.
    team_minutes = len(team_matches) * 90
    goals_for = goals_against = shots_for = shots_against = 0
    xg_for = xg_against = 0.0

    shots = fetch_shots(db)
    if not shots.empty:
        shots = shots[shots["match_id"].isin(set(team_matches["match_id"].astype(str)))].copy()
        shots = shots[shots["situation"] != "Penalty"]
        shots["xg"] = pd.to_numeric(shots["xg"], errors="coerce").fillna(0.0)

        for _, m in team_matches.iterrows():
            if m["home_team"] == team:
                opponent = m["away_team"]
            elif m["away_team"] == team:
                opponent = m["home_team"]
            else:
                continue
            match_shots = shots[shots["match_id"] == str(m["match_id"])]
            own = match_shots[match_shots["team"] == team]
            opp = match_shots[match_shots["team"] == opponent]

            shots_for += len(own)
            shots_against += len(opp)
            goals_for += int((own["outcome"] == "Goal").sum())
            goals_against += int((opp["outcome"] == "Goal").sum())
            xg_for += own["xg"].sum()
            xg_against += opp["xg"].sum()

    goal_diff = goals_for - goals_against
    xg_diff = xg_for - xg_against
    team_total = {
        "Player": "Team Total",
        "Minutes": team_minutes,
        "Goals For": int(round(goals_for)),
        "Goals Against": int(round(goals_against)),
        "Goal Difference": int(round(goal_diff)),
        "Shots": int(round(shots_for)),
        "Shots Against": int(round(shots_against)),
        "xG": round(xg_for, 2),
        "xG Against": round(xg_against, 2),
        "xG Difference": round(xg_diff, 2),
    }
    stat_values = {
        "Goals For": goals_for, "Goals Against": goals_against, "Goal Difference": goal_diff,
        "Shots": shots_for, "Shots Against": shots_against, "xG": xg_for, "xG Against": xg_against,
        "xG Difference": xg_diff,
    }
    for stat in _PLUS_MINUS_STATS:
        team_total[f"{stat} (Per 90)"] = _per90(stat_values[stat], team_minutes)

    return pd.concat([out, pd.DataFrame([team_total], columns=out_cols)], ignore_index=True)


def _fetch_team_season_category_table(db: DB, team, season, namespaces: list, columns: list) -> pd.DataFrame:
    """
    Season-cumulative version of _player_category_table() above (the match
    detail view's per-match Player Stats category tables): every player
    who's appeared for TEAM in SEASON, with each of `columns` SUMMED across
    every one of the team's saved matches this season rather than shown for
    just one match, plus a 'Team Total' row at the bottom summing every
    column the same way. Backs the Team Page's season Possession/Passing/
    Defensive Actions/Defensive Action Locations tables - see
    fetch_team_season_possession()/fetch_team_season_passing()/
    fetch_team_season_defensive_actions()/fetch_team_season_defensive_
    locations() below for which namespace/columns each one uses (the exact
    same ones their per-match fetch_player_*() counterpart already reads).

    Unlike fetch_team_season_scoring_stats() (which this deliberately
    doesn't share code with, despite the similar shape), there's no Age/
    Appearances/Starts/Per-90/card-count special-casing here - every column
    across these 4 categories is a plain running count, so this stays a
    much simpler "sum everything, sort by the first column" helper. Sorted
    descending by whichever column is FIRST in `columns` (each category's
    own natural "most involved player first" metric - Total Touches for
    Possession, Passes Completed for Passing, etc.) rather than one shared
    sort key like Minutes, since these 4 tables don't all share a column.

    A match missing one of `namespaces` entirely (an older save from before
    that namespace existed) simply doesn't contribute to that player's
    numbers for that match - same reasoning as fetch_team_season_scoring_
    stats()'s own docstring on why summed season totals can't cleanly show
    '-' for "some underlying match's data is missing" the way a single
    match's _player_category_table() can. Returns an empty DataFrame if
    this team has no matches saved for this season.
    """
    out_cols = ["Player"] + columns

    matches = fetch_matches(db)
    if matches.empty:
        return pd.DataFrame(columns=out_cols)
    matches = matches.copy()
    matches["season"] = matches["match_date"].apply(_season_label)
    team_matches = matches[
        ((matches["home_team"] == team) | (matches["away_team"] == team))
        & (matches["season"] == season)
    ]
    if team_matches.empty:
        return pd.DataFrame(columns=out_cols)

    sums = {}
    for match_id in team_matches["match_id"]:
        df = _fetch_player_namespaces(db, match_id, namespaces)
        if df.empty:
            continue
        sub = df[df["Team"] == team]
        if sub.empty:
            continue
        present_cols = [c for c in columns if c in sub.columns]
        for _, r in sub.iterrows():
            player = r["Player"]
            row_totals = sums.setdefault(player, {c: 0.0 for c in columns})
            for c in present_cols:
                v = pd.to_numeric(r[c], errors="coerce")
                row_totals[c] += 0.0 if pd.isna(v) else float(v)

    if not sums:
        return pd.DataFrame(columns=out_cols)

    records = []
    for player, totals in sums.items():
        rec = {"Player": player}
        for c in columns:
            rec[c] = int(round(totals[c]))
        records.append(rec)

    out = (pd.DataFrame(records, columns=out_cols)
           .sort_values(columns[0], ascending=False)
           .reset_index(drop=True))

    team_total = {"Player": "Team Total"}
    for c in columns:
        team_total[c] = int(round(out[c].sum()))

    return pd.concat([out, pd.DataFrame([team_total], columns=out_cols)], ignore_index=True)


def fetch_team_season_possession(db: DB, team, season) -> pd.DataFrame:
    """Season-cumulative version of fetch_player_possession() for the Team Page."""
    return _fetch_team_season_category_table(db, team, season, ["ws_touches"], _POSSESSION_COLUMNS)


def fetch_team_season_passing(db: DB, team, season) -> pd.DataFrame:
    """Season-cumulative version of fetch_player_passing() for the Team Page."""
    return _fetch_team_season_category_table(
        db, team, season, ["ws_passing", "fm_line_breaking_passes"], _PASSING_COLUMNS
    )


def fetch_team_season_defensive_actions(db: DB, team, season) -> pd.DataFrame:
    """Season-cumulative version of fetch_player_defensive_actions() for the Team Page."""
    return _fetch_team_season_category_table(
        db, team, season, ["ws_defensive"], _DEFENSIVE_ACTIONS_COLUMNS
    )


def fetch_team_season_defensive_locations(db: DB, team, season) -> pd.DataFrame:
    """Season-cumulative version of fetch_player_defensive_locations() for the Team Page."""
    return _fetch_team_season_category_table(
        db, team, season, ["ws_defensive_locations"], _DEFENSIVE_LOCATIONS_COLUMNS
    )


def fetch_season_passing_totals(db: DB, competition=None) -> pd.DataFrame:
    """
    Season-cumulative passing totals per team, summed across every player
    and match - the team-level rollup of the WhoScored Passing tab
    (compute_passing()'s per-player stats, saved per player_match_stats row
    under the 'ws_passing' key). Every underlying column there is a plain
    count (confirmed: no rate/percentage column exists in compute_passing()
    itself), so summing across players and matches is exact - Pass
    Completion % and Cross Completion % are recomputed here from the
    SUMMED raw counts (SUM(Completed)/SUM(Attempted)*100) rather than
    averaging each match's own percentage, since a flat average would
    wrongly weight a 10-attempt match the same as a 500-attempt match.

    competition: optional matches.competition filter (see the League
    Overview tab's league dropdown) - None (the default) includes every
    saved match regardless of competition.
    """
    cols = ["Team", "Passes Completed", "Passes Attempted", "Pass Completion %",
            "Passes Forward", "Headed", "Crosses Attempted", "Crosses Completed",
            "Cross Completion %", "Passes into Final 1/3", "Passes into the Box",
            "Progressive Passes", "Shot Assists", "SCA"]
    valid_ids = _match_ids_for_competition(db, competition)
    cur = db.execute("SELECT match_id, team, extra_json FROM player_match_stats")
    sums = {}
    for match_id, team, extra_json in cur.fetchall():
        if valid_ids is not None and str(match_id) not in valid_ids:
            continue
        extra = json.loads(extra_json) if extra_json else {}
        passing = extra.get("ws_passing")
        if not passing:
            continue
        agg = sums.setdefault(team, {})
        for k, v in passing.items():
            if isinstance(v, (int, float)):
                agg[k] = agg.get(k, 0) + v

    if not sums:
        return pd.DataFrame(columns=cols)

    records = []
    for team, agg in sums.items():
        completed = agg.get("Passes Completed", 0)
        attempted = agg.get("Passes Attempted", 0)
        crosses_c = agg.get("Crosses Completed", 0)
        crosses_a = agg.get("Crosses Attempted", 0)
        records.append({
            "Team": team,
            "Passes Completed": completed,
            "Passes Attempted": attempted,
            "Pass Completion %": round(completed / attempted * 100, 1) if attempted else 0.0,
            "Passes Forward": agg.get("Passes Forward", 0),
            "Headed": agg.get("Headed", 0),
            "Crosses Attempted": crosses_a,
            "Crosses Completed": crosses_c,
            "Cross Completion %": round(crosses_c / crosses_a * 100, 1) if crosses_a else 0.0,
            "Passes into Final 1/3": agg.get("Passes into Final 1/3", 0),
            "Passes into the Box": agg.get("Passes into the Box", 0),
            "Progressive Passes": agg.get("Progressive Passes", 0),
            "Shot Assists": agg.get("Shot Assists", 0),
            "SCA": agg.get("SCA", 0),
        })
    return pd.DataFrame(records, columns=cols).sort_values(
        "Passes Attempted", ascending=False).reset_index(drop=True)


def fetch_season_defensive_totals(db: DB, competition=None) -> pd.DataFrame:
    """
    Season-cumulative defensive totals per team, summed across every player
    and match - the team-level rollup of the WhoScored Defensive Actions
    tab (compute_defensive_actions()'s per-player stats, saved under the
    'ws_defensive' key). All four columns there are plain counts (no
    rates), so summing is exact.

    competition: optional matches.competition filter (see the League
    Overview tab's league dropdown) - None (the default) includes every
    saved match regardless of competition.
    """
    cols = ["Team", "Tackles", "Interceptions", "Passes Blocked", "Shots Blocked"]
    valid_ids = _match_ids_for_competition(db, competition)
    cur = db.execute("SELECT match_id, team, extra_json FROM player_match_stats")
    sums = {}
    for match_id, team, extra_json in cur.fetchall():
        if valid_ids is not None and str(match_id) not in valid_ids:
            continue
        extra = json.loads(extra_json) if extra_json else {}
        defensive = extra.get("ws_defensive")
        if not defensive:
            continue
        agg = sums.setdefault(team, {})
        for k, v in defensive.items():
            if isinstance(v, (int, float)):
                agg[k] = agg.get(k, 0) + v

    if not sums:
        return pd.DataFrame(columns=cols)

    records = [{
        "Team": team,
        "Tackles": agg.get("Tackles", 0),
        "Interceptions": agg.get("Interceptions", 0),
        "Passes Blocked": agg.get("Passes Blocked", 0),
        "Shots Blocked": agg.get("Shots Blocked", 0),
    } for team, agg in sums.items()]
    return pd.DataFrame(records, columns=cols).sort_values(
        "Tackles", ascending=False).reset_index(drop=True)


def fetch_season_defensive_location_totals(db: DB, competition=None) -> pd.DataFrame:
    """
    Season-cumulative defensive-action-by-pitch-third totals per team,
    summed across every player and match - the team-level rollup of the
    WhoScored Defensive Action Location tab (compute_defensive_action_
    location()'s per-player stats, saved under the 'ws_defensive_locations'
    key). All twelve columns are plain counts, so summing is exact.

    competition: optional matches.competition filter (see the League
    Overview tab's league dropdown) - None (the default) includes every
    saved match regardless of competition.

    This namespace was only added partway through this project, so a match
    saved before then contributes nothing here even if it has other
    defensive stats - re-save it in the combined report app to backfill.
    """
    cols = ["Team"] + _DEFENSIVE_LOCATIONS_COLUMNS
    valid_ids = _match_ids_for_competition(db, competition)
    cur = db.execute("SELECT match_id, team, extra_json FROM player_match_stats")
    sums = {}
    for match_id, team, extra_json in cur.fetchall():
        if valid_ids is not None and str(match_id) not in valid_ids:
            continue
        extra = json.loads(extra_json) if extra_json else {}
        locations = extra.get("ws_defensive_locations")
        if not locations:
            continue
        agg = sums.setdefault(team, {})
        for k, v in locations.items():
            if isinstance(v, (int, float)):
                agg[k] = agg.get(k, 0) + v

    if not sums:
        return pd.DataFrame(columns=cols)

    records = [
        {"Team": team, **{c: agg.get(c, 0) for c in _DEFENSIVE_LOCATIONS_COLUMNS}}
        for team, agg in sums.items()
    ]
    return pd.DataFrame(records, columns=cols).sort_values(
        "Team").reset_index(drop=True)


# Duplicated from whoscored_report.py's own third()/in_box() pitch-zone
# logic (same reasoning as pitch_viz.py duplicating PITCH_LEN_M/PITCH_WID_M
# rather than importing whoscored_report.py - see that file's own
# docstring: importing it here would pull in its top-level selenium/
# utils.driver dependencies, which this module has no other reason to
# need). Used by fetch_season_touches_totals() below to bucket the raw
# touches table's (x, y) coordinates into thirds/attacking-box the exact
# same way the WhoScored Touches tab does, so a season total here means the
# same thing it would mean on a single match's own Touches tab.
_PITCH_LEN_M = 105.0
_PITCH_WID_M = 68.0
_M_TO_YD = 1.09361
_OWN_THIRD_MAX = 33.3
_MIDDLE_THIRD_MAX = 66.6
_BOX_DEPTH_YD = 18
_BOX_WIDTH_YD = 44
_GOAL_X_YD = 100 / 100.0 * _PITCH_LEN_M * _M_TO_YD
_GOAL_Y_YD = 50 / 100.0 * _PITCH_WID_M * _M_TO_YD
_BOX_X_MIN = _GOAL_X_YD - _BOX_DEPTH_YD
_BOX_Y_MIN = _GOAL_Y_YD - _BOX_WIDTH_YD / 2
_BOX_Y_MAX = _GOAL_Y_YD + _BOX_WIDTH_YD / 2


def _pitch_third(x):
    if x < _OWN_THIRD_MAX:
        return "Own third"
    elif x < _MIDDLE_THIRD_MAX:
        return "Middle third"
    return "Final third"


def _in_attacking_box(x, y):
    x_yd = x / 100.0 * _PITCH_LEN_M * _M_TO_YD
    y_yd = y / 100.0 * _PITCH_WID_M * _M_TO_YD
    return _BOX_X_MIN <= x_yd and _BOX_Y_MIN <= y_yd <= _BOX_Y_MAX


def fetch_season_touches_totals(db: DB, competition=None) -> pd.DataFrame:
    """
    competition: optional matches.competition filter (see the League
    Overview tab's league dropdown) - None (the default) includes every
    saved match regardless of competition.

    Season-cumulative touch totals per team, bucketed into pitch thirds and
    the attacking box, PLUS Progressive Carries/Carries into Final Third/
    Carries into Box/Passes Received/Progressive Passes Received - the full
    team-level slice of the WhoScored Touches tab.

    Two different sources are combined here:
      - Total Touches/Own/Middle/Final Third/Attacking Box (+ their %s) are
        rebuilt from the raw `touches` table's (x, y) coordinates, using the
        exact same thresholds WhoScored's own Touches tab uses (see
        _pitch_third()/_in_attacking_box() above). This works for EVERY
        match that has any touch data at all, since it only needs plain
        (x, y) coordinates.
      - Progressive Carries, Carries into Final Third, Carries into Box come
        from team_match_stats' 'ws_touches' key (compute_touches()'s own
        team_summary table, saved by build_db_stats() - see batch_lib.py).
        Passes Received/Progressive Passes Received come from
        player_match_stats' 'ws_touches' key (compute_touches()'s
        player_third table), summed across every player on a team. Both of
        these can only be computed from carry-detection/pass-event data
        that the raw touches table's plain (team, player, minute, second,
        x, y) rows don't capture on their own - they were added to the
        database more recently than the touches table itself, so a match
        saved BEFORE that addition simply contributes 0 to these five
        columns specifically (its Total Touches/thirds/box numbers are
        unaffected, since those come from the always-available raw touches
        table) - re-save an older match to backfill these five columns for
        it.
    """
    cols = ["Team", "Total Touches", "Own Third", "Middle Third", "Final Third", "Attacking Box",
            "Progressive Carries", "Carries into Final Third", "Carries into Box",
            "Passes Received", "Progressive Passes Received",
            "Own Third %", "Middle Third %", "Final Third %", "Attacking Box %"]

    valid_ids = _match_ids_for_competition(db, competition)
    touches = fetch_touches(db)
    if touches.empty:
        return pd.DataFrame(columns=cols)
    if valid_ids is not None:
        touches = touches[touches["match_id"].astype(str).isin(valid_ids)]
        if touches.empty:
            return pd.DataFrame(columns=cols)

    touches = touches.copy()
    touches["_third"] = touches["x"].apply(_pitch_third)
    touches["_in_box"] = touches.apply(lambda r: _in_attacking_box(r["x"], r["y"]), axis=1)

    # team_match_stats' 'ws_touches' -> Progressive Carries/Carries into
    # Final Third/Carries into Box, summed across every match a team appears
    # in (0 for a match saved before this key existed).
    team_carry_totals = {}
    cur = db.execute("SELECT match_id, team, extra_json FROM team_match_stats")
    for match_id, team, extra_json in cur.fetchall():
        if valid_ids is not None and str(match_id) not in valid_ids:
            continue
        extra = json.loads(extra_json) if extra_json else {}
        ws_touches = extra.get("ws_touches")
        if not ws_touches:
            continue
        agg = team_carry_totals.setdefault(team, {"Progressive Carries": 0,
                                                    "Carries into Final Third": 0,
                                                    "Carries into Box": 0})
        for k in ("Progressive Carries", "Carries into Final Third", "Carries into Box"):
            v = ws_touches.get(k)
            if isinstance(v, (int, float)):
                agg[k] += v

    # player_match_stats' 'ws_touches' -> Passes Received/Progressive Passes
    # Received, summed across every player on a team across every match.
    player_received_totals = {}
    cur = db.execute("SELECT match_id, team, extra_json FROM player_match_stats")
    for match_id, team, extra_json in cur.fetchall():
        if valid_ids is not None and str(match_id) not in valid_ids:
            continue
        extra = json.loads(extra_json) if extra_json else {}
        ws_touches = extra.get("ws_touches")
        if not ws_touches:
            continue
        agg = player_received_totals.setdefault(team, {"Passes Received": 0,
                                                         "Progressive Passes Received": 0})
        for k in ("Passes Received", "Progressive Passes Received"):
            v = ws_touches.get(k)
            if isinstance(v, (int, float)):
                agg[k] += v

    records = []
    for team, g in touches.groupby("team"):
        total = len(g)
        own = int((g["_third"] == "Own third").sum())
        mid = int((g["_third"] == "Middle third").sum())
        final = int((g["_third"] == "Final third").sum())
        box = int(g["_in_box"].sum())
        carry_totals = team_carry_totals.get(team, {})
        received_totals = player_received_totals.get(team, {})
        records.append({
            "Team": team, "Total Touches": total,
            "Own Third": own, "Middle Third": mid, "Final Third": final, "Attacking Box": box,
            "Progressive Carries": carry_totals.get("Progressive Carries", 0),
            "Carries into Final Third": carry_totals.get("Carries into Final Third", 0),
            "Carries into Box": carry_totals.get("Carries into Box", 0),
            "Passes Received": received_totals.get("Passes Received", 0),
            "Progressive Passes Received": received_totals.get("Progressive Passes Received", 0),
            "Own Third %": round(own / total * 100, 1) if total else 0.0,
            "Middle Third %": round(mid / total * 100, 1) if total else 0.0,
            "Final Third %": round(final / total * 100, 1) if total else 0.0,
            "Attacking Box %": round(box / total * 100, 1) if total else 0.0,
        })
    return pd.DataFrame(records, columns=cols).sort_values(
        "Total Touches", ascending=False).reset_index(drop=True)
