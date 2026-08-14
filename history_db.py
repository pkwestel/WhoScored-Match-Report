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
                     have on hand).
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
            scraped_at    TEXT
        )
    """)
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
    db.commit()


# ============================================================
# Publish (upsert) helpers
# ============================================================
def upsert_match(db: DB, match_id, home_team, away_team, competition=None, match_date=None,
                  ws_events=None, fm_shots=None):
    db.execute("""
        INSERT INTO matches (match_id, competition, match_date, home_team, away_team,
                              ws_events, fm_shots, scraped_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(match_id) DO UPDATE SET
            competition = excluded.competition,
            match_date  = excluded.match_date,
            home_team   = excluded.home_team,
            away_team   = excluded.away_team,
            ws_events   = excluded.ws_events,
            fm_shots    = excluded.fm_shots,
            scraped_at  = excluded.scraped_at
    """, (str(match_id), competition, match_date, home_team, away_team,
          ws_events, fm_shots, datetime.now(timezone.utc).isoformat()))


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


def _num(v):
    """NaN/None -> None (so it round-trips through the DB as NULL, not the string 'nan')."""
    return None if v is None or (isinstance(v, float) and pd.isna(v)) else float(v)


def publish_report(db: DB, match_id, home_team, away_team, team_stats: dict, player_stats: dict,
                    shots_df: pd.DataFrame = None, passes_df: pd.DataFrame = None,
                    competition=None, match_date=None, ws_events=None, fm_shots=None):
    """
    One-call orchestrator for a full match publish, wrapped in a single
    transaction (all-or-nothing - if any part fails, nothing is written).

    team_stats:   {team_name: {'ws_totals': {...}, 'fm_totals': {...}}, ...}
    player_stats: {(team_name, player_name): {'ws_passing': {...}, ...}, ...}
    shots_df:     combined/whoscored/fotmob shots dataframe, or None to skip.
    passes_df:    whoscored_report.compute_all_passes() output, or None to skip
                  (e.g. publishing a FotMob-only report with no event data).
    """
    try:
        upsert_match(db, match_id, home_team, away_team, competition, match_date, ws_events, fm_shots)
        for team, extra in team_stats.items():
            upsert_team_stats(db, match_id, team, extra, is_home=(team == home_team))
        for (team, player), extra in player_stats.items():
            upsert_player_stats(db, match_id, team, player, extra)
        if shots_df is not None:
            upsert_shots(db, match_id, shots_df)
        if passes_df is not None:
            upsert_passes(db, match_id, passes_df)
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


def fetch_passes(db: DB, match_id, passer=None, receiver=None, completed_only=False) -> pd.DataFrame:
    """
    Passes for one match, optionally filtered down to one passer (for a Pass
    Map) or one receiver (for a Passes Received map). Column names come back
    as end_x/end_y (the DB's column names) rather than endX/endY (whoscored_
    report.py's own dataframe convention) - dashboard_app.py renames them
    back before handing the result to pitch_viz.plot_pass_map(), which
    expects the endX/endY spelling.
    """
    sql = "SELECT * FROM passes WHERE match_id = ?"
    params = [str(match_id)]
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
