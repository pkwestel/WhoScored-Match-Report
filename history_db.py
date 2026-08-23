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
                     (Passing, Defensive Actions, Plus Minus, ...) each
                     nested under their own key in extra_json.
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
            scraped_at    TEXT
        )
    """)
    # Migration for databases that already had a 'matches' table BEFORE the
    # referee column was added above - CREATE TABLE IF NOT EXISTS only
    # applies to brand new tables, so an existing one needs its own ALTER
    # TABLE. Wrapped in try/except because there's no portable
    # "ADD COLUMN IF NOT EXISTS" across SQLite and Postgres both - re-running
    # this against a database that already has the column just raises
    # "duplicate column"/"already exists", which is fine to ignore.
    try:
        db.execute("ALTER TABLE matches ADD COLUMN referee TEXT")
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
                  ws_events=None, fm_shots=None, referee=None):
    db.execute("""
        INSERT INTO matches (match_id, competition, match_date, home_team, away_team,
                              ws_events, fm_shots, referee, scraped_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(match_id) DO UPDATE SET
            competition = excluded.competition,
            match_date  = excluded.match_date,
            home_team   = excluded.home_team,
            away_team   = excluded.away_team,
            ws_events   = excluded.ws_events,
            fm_shots    = excluded.fm_shots,
            referee     = excluded.referee,
            scraped_at  = excluded.scraped_at
    """, (str(match_id), competition, match_date, home_team, away_team,
          ws_events, fm_shots, referee, datetime.now(timezone.utc).isoformat()))


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
            _num(r.get("Minute")), _num(r.get("Added Time")), r.get("Situation"),
            r.get("Body Part"), r.get("Outcome"),
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
            str(match_id), r.get("team"), r.get("passer"), r.get("receiver"),
            _num(r.get("minute")), _num(r.get("second")), _num(r.get("x")), _num(r.get("y")),
            _num(r.get("endX")), _num(r.get("endY")),
            int(bool(r.get("completed"))), int(bool(r.get("is_progressive"))),
            int(bool(r.get("is_key_pass"))), r.get("category"),
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
            str(match_id), r.get("team"), r.get("player"),
            _num(r.get("minute")), _num(r.get("second")), _num(r.get("x")), _num(r.get("y")),
        ))


def _num(v):
    """NaN/None -> None (so it round-trips through the DB as NULL, not the string 'nan')."""
    return None if v is None or (isinstance(v, float) and pd.isna(v)) else float(v)


def publish_report(db: DB, match_id, home_team, away_team, team_stats: dict, player_stats: dict,
                    shots_df: pd.DataFrame = None, passes_df: pd.DataFrame = None,
                    touches_df: pd.DataFrame = None,
                    competition=None, match_date=None, ws_events=None, fm_shots=None, referee=None):
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
    """
    try:
        upsert_match(db, match_id, home_team, away_team, competition, match_date, ws_events,
                     fm_shots, referee)
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


def fetch_fixtures(db: DB) -> pd.DataFrame:
    """
    One row per match, shaped for dashboard_app.py's Fixtures tab: match_id
    (kept for the clickable link to the match detail view, not meant to be
    shown as its own column), Date, Competition, Home Team, Home xG, Score,
    Away xG, Away Team, Referee.

    Score and xG aren't their own columns on the 'matches' table - they're
    pulled from each team's own 'fm_totals' entry inside team_match_stats.
    extra_json (Goals / 'Total xG'), which every match saved via
    combined_streamlit_app.py or batch_lib.py already carries. That means
    this works retroactively for every match already in the database - no
    re-scraping or backfill needed, unlike Referee (a genuinely new field -
    see fotmob_report.extract_referee()), which is None for anything saved
    before that existed.
    """
    matches = fetch_matches(db)
    cols = ["match_id", "Date", "Competition", "Home Team", "Home xG",
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


def fetch_player_stats_for_match(db: DB, match_id) -> pd.DataFrame:
    """Every player's stats (flattened from extra_json) for ONE match."""
    cur = db.execute("""
        SELECT team, player, extra_json
        FROM player_match_stats
        WHERE match_id = ?
    """, (str(match_id),))
    rows = [(r[0], r[1], r[2]) for r in cur.fetchall()]
    return _flatten_extra(rows, ["team", "player"])


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
