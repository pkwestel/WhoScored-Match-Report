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
    return pd.DataFrame(records, columns=cols)


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
    ("xG", "Total xG"),
    ("Big Chances", "Big chances"),
    ("Corners", "Corners"),
]


def _fmt_match_summary_value(value, label):
    if value is None:
        return "-"
    try:
        if label == "Possession":
            return f"{float(value):.0f}%"
        if label == "xG":
            return f"{float(value):.2f}"
        if float(value) == int(float(value)):
            return str(int(float(value)))
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return str(value)


def fetch_match_summary(db: DB, match_id) -> pd.DataFrame:
    """
    Compact home-vs-away summary table for the match detail view's Team
    Totals tab: one row per stat (Goals, Shots, Shots on target, Shots
    inside box, Possession, xG, Big Chances, Corners), laid out as Home |
    Metric | Away rather than one row per team - plus a leading 'Team' row
    showing the two team names themselves, so the table reads top-to-bottom
    as a single side-by-side comparison instead of needing to cross-
    reference two separate rows.

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
    cols = ["Home", "Metric", "Away"]
    matches = fetch_matches(db)
    match_row = matches[matches["match_id"] == str(match_id)]
    if match_row.empty:
        return pd.DataFrame(columns=cols)
    match_row = match_row.iloc[0]
    home_team, away_team = match_row["home_team"], match_row["away_team"]

    cur = db.execute("SELECT team, extra_json FROM team_match_stats WHERE match_id = ?", (str(match_id),))
    fm_totals_by_team = {}
    for team, extra_json in cur.fetchall():
        extra = json.loads(extra_json) if extra_json else {}
        fm_totals_by_team[team] = extra.get("fm_totals") or {}

    home_stats = fm_totals_by_team.get(home_team, {})
    away_stats = fm_totals_by_team.get(away_team, {})

    records = [{"Home": home_team, "Metric": "Team", "Away": away_team}]
    for label, key in _MATCH_SUMMARY_FIELDS:
        records.append({
            "Home": _fmt_match_summary_value(home_stats.get(key), label),
            "Metric": label,
            "Away": _fmt_match_summary_value(away_stats.get(key), label),
        })
    return pd.DataFrame(records, columns=cols)


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
    above), keeps only the requested columns (in the given order - any
    column missing from the underlying data for every player, e.g. an older
    match saved before a newer stat existed, is filled with 0 rather than
    dropped, so the table shape never depends on which fields happen to be
    populated), then splits into (home, away) with a 'Team Total' row
    appended to each summing every numeric column - matching the requested
    layout (two separate tables, home on top / away below, each with a
    bottom total row).

    Returns {'home': DataFrame, 'away': DataFrame} - either can be empty if
    that team has no saved rows for these namespaces yet (e.g. an older
    match saved before this category existed).
    """
    df = _fetch_player_namespaces(db, match_id, namespaces)
    if df.empty:
        empty = pd.DataFrame(columns=["Player"] + columns)
        return {"home": empty, "away": empty}

    for c in columns:
        if c not in df.columns:
            df[c] = 0
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
        sub = df[df["Team"] == team_name][["Player"] + columns].reset_index(drop=True)
        if sub.empty:
            return sub
        total = {c: sub[c].sum() for c in columns}
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


def fetch_player_scoring_stats(db: DB, match_id, home_team, away_team) -> dict:
    """
    Scoring Stats category: Minutes Played, Goals, Assists, Shots, SCA,
    NPxG, PS-xG, xA, PK, PK Attempted, Sprints - one row per player, 'Team
    Total' row at the bottom. Minutes Played/Goals/Assists/Shots/NPxG/PS-xG/
    xA/PK/PK Attempted/Sprints all come from the 'fm_scoring' namespace
    (fotmob_report.compute_player_scoring_stats()); SCA is pulled in from
    'ws_passing' instead of being duplicated into fm_scoring, since it's
    also the Passing category's own SCA figure (fotmob_report.
    compute_passing()'s real Shot-Creating-Actions count, not a FotMob
    proxy) - one real number, shown on both tables.
    """
    return _player_category_table(db, match_id, home_team, away_team,
                                   ["fm_scoring", "ws_passing"], _SCORING_STATS_COLUMNS)


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


def fetch_season_shot_totals(db: DB):
    """
    Season-cumulative Shots/Goals/Total xG per team, broken down by shot
    'situation' (FotMob's own vocabulary - Open Play, Set Piece, Corner,
    ... - see fotmob_report.SITUATION_DISPLAY_MAP for the raw-to-display
    name mapping used by the dashboard's Shots tab), split into 'for'
    (that team's own shots) and 'against' (shots faced FROM whichever team
    they played in each match). Powers dashboard_app.py's season-wide Shots
    tab, which shows two small leaderboard tables (For/Against) rather than
    a per-match shot log.

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

    matches = fetch_matches(db)[["match_id", "home_team", "away_team"]]
    merged = shots.merge(matches, on="match_id", how="left")

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
def fetch_league_table(db: DB) -> pd.DataFrame:
    """
    Season standings: one row per team, with the standard league table
    columns (Played, W, D, L, GF, GA, GD, Points) plus this project's own
    xG/xGA/xGD, all built from team_match_stats' 'fm_totals' (Goals, Total
    xG) - the same source fetch_fixtures() already reads Score/xG from, so
    it inherits that function's team-name reconciliation (see
    build_db_stats()'s docstring in batch_lib.py) automatically, since
    matches.home_team/away_team and team_match_stats' team keys are already
    one consistent (WhoScored's) name per team.

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


def fetch_season_passing_totals(db: DB) -> pd.DataFrame:
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
    """
    cols = ["Team", "Passes Completed", "Passes Attempted", "Pass Completion %",
            "Passes Forward", "Headed", "Crosses Attempted", "Crosses Completed",
            "Cross Completion %", "Passes into Final 1/3", "Passes into the Box",
            "Progressive Passes", "Shot Assists", "SCA"]
    cur = db.execute("SELECT team, extra_json FROM player_match_stats")
    sums = {}
    for team, extra_json in cur.fetchall():
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


def fetch_season_defensive_totals(db: DB) -> pd.DataFrame:
    """
    Season-cumulative defensive totals per team, summed across every player
    and match - the team-level rollup of the WhoScored Defensive Actions
    tab (compute_defensive_actions()'s per-player stats, saved under the
    'ws_defensive' key). All four columns there are plain counts (no
    rates), so summing is exact.
    """
    cols = ["Team", "Tackles", "Interceptions", "Passes Blocked", "Shots Blocked"]
    cur = db.execute("SELECT team, extra_json FROM player_match_stats")
    sums = {}
    for team, extra_json in cur.fetchall():
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


def fetch_season_touches_totals(db: DB) -> pd.DataFrame:
    """
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

    touches = fetch_touches(db)
    if touches.empty:
        return pd.DataFrame(columns=cols)

    touches = touches.copy()
    touches["_third"] = touches["x"].apply(_pitch_third)
    touches["_in_box"] = touches.apply(lambda r: _in_attacking_box(r["x"], r["y"]), axis=1)

    # team_match_stats' 'ws_touches' -> Progressive Carries/Carries into
    # Final Third/Carries into Box, summed across every match a team appears
    # in (0 for a match saved before this key existed).
    team_carry_totals = {}
    cur = db.execute("SELECT team, extra_json FROM team_match_stats")
    for team, extra_json in cur.fetchall():
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
    cur = db.execute("SELECT team, extra_json FROM player_match_stats")
    for team, extra_json in cur.fetchall():
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
