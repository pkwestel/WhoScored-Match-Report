"""
SofaScore Match Report Generator
=================================

Drop-in replacement for fotmob_report.py that uses SofaScore's API
instead of FotMob's, intended for pre-2020-21 historical matches where
FotMob data isn't available. Provides the same public interface so
batch_lib.run_combined_report_sofascore() can call these functions
identically.

DATA ACCESS
-----------
SofaScore's API (api.sofascore.com/api/v1/event/{id}/...) blocks direct
requests (403) unless TLS fingerprinting matches a real browser. This
module uses Selenium + Chrome DevTools Protocol (CDP) network interception
- the same approach the existing sofascore/sofascore_selenium.py uses - to
load a SofaScore match page and capture the real API responses the page's
own JavaScript makes. This avoids any need for curl_cffi or custom TLS
impersonation.

SOFASCORE vs FOTMOB DATA MAPPING
----------------------------------
Available (equivalent data exists):
  - shots with xG, xGOT, coordinates, outcome, situation, body part
  - team statistics (possession, shots, passes, fouls, corners, etc.)
  - player minutes played (from lineups endpoint)
  - player cards (from incidents endpoint)
  - player positions (from lineups endpoint)
  - referee, kickoff time, matchweek/round
  - lineup data (starters/subs, age via dateOfBirthTimestamp)
  - player xA (from lineups player statistics, if available)
  - player npxG (from lineups player statistics, if available)

NOT available (FotMob-specific):
  - player sprints (physical tracking data - SofaScore doesn't expose this)
  - player line-breaking passes (FotMob proprietary stat)

These missing stats are handled gracefully - the functions return empty
DataFrames, same as FotMob does when a match lacks physical tracking data.

SOFASCORE MATCH ID
-------------------
SofaScore URLs look like:
  https://www.sofascore.com/football/match/<slug>/<code>#id:<match_id>
The numeric match_id (e.g. 14566764) is what the API endpoints use.
"""

import io
import json
import re
import time
from datetime import datetime, timezone

import pandas as pd

try:
    from openpyxl import Workbook
    from openpyxl.utils import get_column_letter
except ImportError:
    Workbook = None

from utils.driver import get_driver

# ============================================================
# CONSTANTS
# ============================================================

# SofaScore shot type → our canonical outcome
SHOT_TYPE_MAP = {
    'goal': 'Goal',
    'save': 'Saved',
    'saved': 'Saved',
    'miss': 'Off Target',
    'block': 'Blocked',
    'blocked': 'Blocked',
    'post': 'Hit Post',
    'woodwork': 'Hit Post',
}

# SofaScore body part → our canonical body part
BODY_PART_MAP = {
    'right-foot': 'Right foot',
    'left-foot': 'Left foot',
    'head': 'Head',
    'right foot': 'Right foot',
    'left foot': 'Left foot',
}

# SofaScore situation → our canonical situation
SITUATION_MAP = {
    'regular': 'Open play',
    'assisted': 'Open play',
    'fast-break': 'Fast break',
    'set-piece': 'Set piece',
    'corner': 'From corner',
    'free-kick': 'Set piece',
    'penalty': 'Penalty',
    'direct-freekick': 'Set piece',
}

# SofaScore position codes → our canonical labels
POSITION_MAP = {
    'G': 'GK',
    'D': 'DEF',
    'M': 'MID',
    'F': 'FWD',
}

# SofaScore card type → our canonical
CARD_TYPE_MAP = {
    'yellow': 'Yellow',
    'yellowcard': 'Yellow',
    'red': 'Red',
    'redcard': 'Red',
    'yellowred': 'YellowRed',
}

# Keys we look for in team statistics
STAT_KEY_MAP = {
    'ballPossession': 'Possession, %',
    'expectedGoals': 'Expected goals (xG)',
    'totalShots': 'Total shots',
    'shotsOnTarget': 'Shots on target',
    'shotsOffTarget': 'Shots off target',
    'blockedShots': 'Blocked shots',
    'cornerKicks': 'Corners',
    'fouls': 'Fouls',
    'totalPasses': 'Passes',
    'accuratePasses': 'Accurate passes',
    'accuratePassesPercentage': 'Accurate passes, %',
    'totalCrosses': 'Crosses',
    'accurateCrosses': 'Accurate crosses',
    'totalLongBalls': 'Long balls',
    'accurateLongBalls': 'Accurate long balls',
    'bigChances': 'Big chances',
    'bigChancesMissed': 'Big chances missed',
    'hitWoodwork': 'Hit woodwork',
    'shotsInsideBox': 'Shots inside box',
    'shotsOutsideBox': 'Shots outside box',
    'tackles': 'Tackles',
    'interceptions': 'Interceptions',
    'clearances': 'Clearances',
    'saves': 'Goalkeeper saves',
    'offsides': 'Offsides',
    'yellowCards': 'Yellow cards',
    'redCards': 'Red cards',
    'throwIns': 'Throw-ins',
    'freeKicks': 'Free kicks',
    'goalKicks': 'Goal kicks',
    'dpiYellow': 'Deliberate handballs',
}


# ============================================================
# 1. MATCH ID EXTRACTION
# ============================================================

def extract_match_id(url: str) -> str:
    """Extract the numeric SofaScore match/event ID from a URL."""
    # Pattern: #id:12345678
    m = re.search(r'#id:(\d+)', url)
    if m:
        return m.group(1)
    # Pattern: /match/.../12345678 or just trailing digits after last /
    m = re.search(r'/(\d{5,})(?:[/?#]|$)', url)
    if m:
        return m.group(1)
    # Try: the URL itself might just be a numeric ID
    if url.strip().isdigit():
        return url.strip()
    raise ValueError(f"Cannot extract SofaScore match ID from: {url}")


# ============================================================
# 2. SCRAPING - capture API responses via Selenium CDP
# ============================================================

def _capture_api_responses(driver, match_id, max_wait=15):
    """
    Wait for SofaScore's page JS to make API calls and capture the
    responses via Chrome DevTools Protocol performance logs.

    Returns a dict mapping endpoint suffix → parsed JSON, e.g.:
      '' → match details, '/shotmap' → shotmap, '/statistics' → stats, etc.
    """
    endpoints_wanted = {
        '': None,          # match details
        '/shotmap': None,
        '/statistics': None,
        '/lineups': None,
        '/incidents': None,
        '/graph': None,
    }

    target_prefix = f'/api/v1/event/{match_id}'

    deadline = time.time() + max_wait
    while time.time() < deadline:
        try:
            logs = driver.get_log('performance')
        except Exception:
            logs = []

        for log_entry in logs:
            try:
                message = json.loads(log_entry['message'])
                method = message['message']['method']
                if method != 'Network.responseReceived':
                    continue
                params = message['message']['params']
                response = params['response']
                url = response.get('url', '')
                if target_prefix not in url:
                    continue

                # Figure out which endpoint this is
                # URL looks like: https://api.sofascore.com/api/v1/event/12345/shotmap
                after_id = url.split(target_prefix, 1)[1] if target_prefix in url else ''
                # Strip query params
                after_id = after_id.split('?')[0]

                if after_id in endpoints_wanted:
                    request_id = params['requestId']
                    try:
                        body = driver.execute_cdp_cmd(
                            'Network.getResponseBody', {'requestId': request_id}
                        )
                        if body and 'body' in body:
                            endpoints_wanted[after_id] = json.loads(body['body'])
                    except Exception:
                        pass
            except (json.JSONDecodeError, KeyError):
                continue

        # Check progress - stop early once we have everything, or once
        # the critical ones (match details + at least lineups/statistics) are in
        all_found = all(v is not None for v in endpoints_wanted.values())
        if all_found:
            break
        # Have enough critical data to stop waiting early
        have_critical = (endpoints_wanted.get('') is not None
                         and (endpoints_wanted.get('/lineups') is not None
                              or endpoints_wanted.get('/statistics') is not None))

        time.sleep(0.5)

    return endpoints_wanted


def scrape_match(url: str, out_dir: str = '.'):
    """
    Scrape a SofaScore match page via Selenium, capturing the API
    responses the page's own JS makes.

    Returns (match_data_dict, match_id) where match_data_dict is a
    combined dict of all captured API endpoint responses, keyed by
    endpoint name.
    """
    match_id = extract_match_id(url)

    with get_driver(track_network=True) as driver:
        # Clear any existing logs
        try:
            driver.get_log('performance')
        except Exception:
            pass

        driver.get(url)
        time.sleep(2)

        # Scroll down to trigger lazy-loaded API calls
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(1)

        # Try clicking on "Statistics" and "Lineups" tabs if present
        for tab_text in ['Statistics', 'Lineups', 'Incidents']:
            try:
                driver.execute_script(f"""
                    var tabs = document.querySelectorAll('a, button, [role="tab"]');
                    for (var t of tabs) {{
                        if (t.textContent.trim() === '{tab_text}') {{
                            t.click();
                            break;
                        }}
                    }}
                """)
                time.sleep(0.5)
            except Exception:
                pass

        # Also try clicking the "Shot map" or shotmap-related element
        try:
            driver.execute_script("""
                var els = document.querySelectorAll('a, button, [role="tab"], div[class*="shot"]');
                for (var e of els) {
                    if (e.textContent && e.textContent.trim().toLowerCase().includes('shot')) {
                        e.click();
                        break;
                    }
                }
            """)
            time.sleep(0.5)
        except Exception:
            pass

        captured = _capture_api_responses(driver, match_id)

    # Build the combined match data dict
    match_data = {
        'match_id': match_id,
        'match_details': captured.get(''),
        'shotmap': captured.get('/shotmap'),
        'statistics': captured.get('/statistics'),
        'lineups': captured.get('/lineups'),
        'incidents': captured.get('/incidents'),
        'graph': captured.get('/graph'),
    }

    return match_data, match_id


# ============================================================
# 3. TEAM NAME EXTRACTION
# ============================================================

def extract_team_names(match_data):
    """
    Returns (home_name, away_name) from the match details.

    SofaScore match details structure:
      event.homeTeam.name / event.awayTeam.name
    """
    details = match_data.get('match_details') or {}
    event = details.get('event', details)

    home_team = event.get('homeTeam', {})
    away_team = event.get('awayTeam', {})

    home_name = home_team.get('name') or home_team.get('shortName', 'Home')
    away_name = away_team.get('name') or away_team.get('shortName', 'Away')

    return home_name, away_name


# ============================================================
# 4. SHOTS
# ============================================================

def compute_shots(match_data):
    """
    One row per shot, across both teams, sorted chronologically.
    Matches fotmob_report.compute_shots() output format exactly.
    """
    columns = ['Team', 'Player', 'Minute', 'Added Time', 'Period', 'Situation',
               'Body Part', 'Outcome', 'On Target', 'xG', 'xGOT', 'X', 'Y']

    shotmap_data = match_data.get('shotmap') or {}
    shots = shotmap_data.get('shotmap', shotmap_data.get('shots', []))
    if not isinstance(shots, list) or not shots:
        return pd.DataFrame(columns=columns)

    home_name, away_name = extract_team_names(match_data)

    rows = []
    for s in shots:
        is_home = s.get('isHome', False)
        team_name = home_name if is_home else away_name

        # Outcome
        shot_type = str(s.get('shotType', '')).lower()
        goal_type = str(s.get('goalType', '')).lower() if s.get('goalType') else None
        if goal_type == 'own':
            outcome = 'Own Goal'
        else:
            outcome = SHOT_TYPE_MAP.get(shot_type, shot_type.title() if shot_type else 'Unknown')

        # Body part
        body_part_raw = str(s.get('bodyPart', '')).lower()
        body_part = BODY_PART_MAP.get(body_part_raw, body_part_raw.title() if body_part_raw else 'Unknown')

        # Situation
        situation_raw = str(s.get('situation', '')).lower()
        situation = SITUATION_MAP.get(situation_raw, situation_raw.title() if situation_raw else 'Open play')

        # On target
        on_target = outcome in ('Goal', 'Saved')

        # Coordinates - SofaScore uses playerCoordinates
        coords = s.get('playerCoordinates', {})
        x = coords.get('x') if isinstance(coords, dict) else s.get('x')
        y = coords.get('y') if isinstance(coords, dict) else s.get('y')

        # Player name
        player_info = s.get('player', {})
        player_name = (player_info.get('name') or player_info.get('shortName', '')
                       if isinstance(player_info, dict) else str(player_info))

        # Time
        minute = s.get('time', s.get('minute'))
        added_time = s.get('addedTime', 0) or 0

        # Period - derive from minute
        if minute is not None:
            if minute <= 45 or (minute == 45 and added_time > 0):
                period = '1st half'
            elif minute <= 90 or (minute == 90 and added_time > 0):
                period = '2nd half'
            elif minute <= 105:
                period = 'Extra 1st half'
            else:
                period = 'Extra 2nd half'
        else:
            period = None

        rows.append({
            'Team': team_name,
            'Player': player_name,
            'Minute': minute,
            'Added Time': added_time,
            'Period': period,
            'Situation': situation,
            'Body Part': body_part,
            'Outcome': outcome,
            'On Target': on_target,
            'xG': s.get('xg', s.get('xG')),
            'xGOT': s.get('xgot', s.get('xGOT', 0)),
            'X': x,
            'Y': y,
        })

    df = pd.DataFrame(rows, columns=columns)
    sort_cols = [c for c in ['Minute', 'Added Time'] if c in df.columns]
    if sort_cols:
        df = df.sort_values(sort_cols, na_position='last').reset_index(drop=True)
    return df


# ============================================================
# 5. TOTALS (team-level stats)
# ============================================================

def _extract_team_stats(match_data):
    """
    Parse SofaScore's /statistics response into a flat dict of
    {stat_key: (home_value, away_value)}.
    """
    stats_data = match_data.get('statistics') or {}
    statistics = stats_data.get('statistics', [])
    if not isinstance(statistics, list):
        return {}

    result = {}
    for period_block in statistics:
        if not isinstance(period_block, dict):
            continue
        period = period_block.get('period', '')
        if period != 'ALL':
            continue

        for group in period_block.get('groups', []):
            if not isinstance(group, dict):
                continue
            for item in group.get('statisticsItems', []):
                if not isinstance(item, dict):
                    continue
                key = item.get('key', '')
                home_val = item.get('homeValue', item.get('homeTotal'))
                away_val = item.get('awayValue', item.get('awayTotal'))
                if home_val is None:
                    # Try the string values
                    home_str = str(item.get('home', ''))
                    away_str = str(item.get('away', ''))
                    try:
                        home_val = float(home_str.replace('%', ''))
                        away_val = float(away_str.replace('%', ''))
                    except (ValueError, TypeError):
                        continue
                result[key] = (home_val, away_val)

    return result


def compute_totals(match_data, shots_df, home_name, away_name):
    """
    Team-level Totals tab, matching fotmob_report.compute_totals() output.
    """
    teams_order = [t for t in [home_name, away_name] if t] or sorted(shots_df['Team'].dropna().unique())
    totals = pd.DataFrame({'team': teams_order}).set_index('team')

    if not shots_df.empty:
        countable = shots_df[shots_df['Outcome'] != 'Own Goal']
        agg = countable.groupby('Team').agg(
            Shots=('Outcome', 'size'),
            **{'Shots on Target': ('On Target', 'sum')},
            Goals=('Outcome', lambda s: int((s == 'Goal').sum())),
            **{'Total xG': ('xG', 'sum')},
            **{'Total post-shot xG': ('xGOT', 'sum')},
        )
        totals = totals.join(agg, how='left')

    totals = totals.fillna(0)
    for c in ['Shots', 'Shots on Target', 'Goals']:
        if c in totals.columns:
            totals[c] = totals[c].astype(int)
    for c in ['Total xG', 'Total post-shot xG']:
        if c in totals.columns:
            totals[c] = totals[c].round(2)

    # Override xG and Goals from SofaScore's own stats if available
    team_stats = _extract_team_stats(match_data)
    xg_vals = team_stats.get('expectedGoals')
    if xg_vals and home_name in totals.index:
        totals.loc[home_name, 'Total xG'] = round(float(xg_vals[0]), 2)
    if xg_vals and away_name in totals.index:
        totals.loc[away_name, 'Total xG'] = round(float(xg_vals[1]), 2)

    # Override Goals from match details final score
    home_score, away_score = extract_final_score(match_data, home_name, away_name)
    if home_score is not None and home_name in totals.index:
        totals.loc[home_name, 'Goals'] = int(home_score)
    if away_score is not None and away_name in totals.index:
        totals.loc[away_name, 'Goals'] = int(away_score)
    if 'Goals' in totals.columns:
        totals['Goals'] = totals['Goals'].fillna(0).astype(int)

    # Add all other team stats from SofaScore
    for key, (h_val, a_val) in team_stats.items():
        title = STAT_KEY_MAP.get(key, key)
        if title in totals.columns or title.lower() in ('expected goals (xg)', 'expected goals'):
            continue
        if home_name in totals.index:
            totals.loc[home_name, title] = h_val
        if away_name in totals.index:
            totals.loc[away_name, title] = a_val

    totals = totals.reindex(teams_order).reset_index()
    return totals


def extract_final_score(match_data, home_name, away_name):
    """Extract the final score from match details. Returns (home_score, away_score)."""
    details = match_data.get('match_details') or {}
    event = details.get('event', details)
    home_score = event.get('homeScore', {})
    away_score = event.get('awayScore', {})
    if isinstance(home_score, dict):
        h = home_score.get('current', home_score.get('display'))
    else:
        h = home_score
    if isinstance(away_score, dict):
        a = away_score.get('current', away_score.get('display'))
    else:
        a = away_score
    try:
        return int(h), int(a)
    except (TypeError, ValueError):
        return None, None


# ============================================================
# 6. PLAYER STATS FROM LINEUPS
# ============================================================

def _get_lineup_data(match_data):
    """Get the lineups response data."""
    lineups = match_data.get('lineups') or {}
    return lineups


def _iter_players_with_stats(match_data):
    """
    Yield (team_name, player_dict) for every player who has statistics
    in the lineups endpoint.
    """
    lineups = _get_lineup_data(match_data)
    home_name, away_name = extract_team_names(match_data)

    for side, team_name in [('home', home_name), ('away', away_name)]:
        side_data = lineups.get(side, {})
        if not isinstance(side_data, dict):
            continue
        players = side_data.get('players', [])
        for p in players:
            if not isinstance(p, dict):
                continue
            yield team_name, p


def _player_stat_value(player_dict, stat_key):
    """
    Extract a specific stat value from a player's statistics dict.
    SofaScore lineups put player stats under player_dict['statistics'].
    """
    stats = player_dict.get('statistics', {})
    if not isinstance(stats, dict):
        return None
    return stats.get(stat_key)


def extract_player_minutes(match_data):
    """
    Per-player minutes played. Returns columns: Team, Player, Minutes Played.
    """
    columns = ['Team', 'Player', 'Minutes Played']
    rows = []
    for team_name, p in _iter_players_with_stats(match_data):
        minutes = _player_stat_value(p, 'minutesPlayed')
        if minutes is None:
            continue
        player_info = p.get('player', {})
        name = player_info.get('name') or player_info.get('shortName', '')
        rows.append({'Team': team_name, 'Player': name, 'Minutes Played': int(minutes)})
    return pd.DataFrame(rows, columns=columns)


def extract_player_xa(match_data):
    """
    Per-player expected assists (xA). Returns columns: Team, Player, xA.
    SofaScore stores this as 'expectedAssists' in player statistics.
    """
    columns = ['Team', 'Player', 'xA']
    rows = []
    for team_name, p in _iter_players_with_stats(match_data):
        xa = _player_stat_value(p, 'expectedAssists')
        if xa is None:
            continue
        player_info = p.get('player', {})
        name = player_info.get('name') or player_info.get('shortName', '')
        rows.append({'Team': team_name, 'Player': name, 'xA': round(float(xa), 2)})
    return pd.DataFrame(rows, columns=columns)


def extract_player_npxg(match_data):
    """
    Per-player Non-Penalty xG. Returns columns: Team, Player, NPxG.
    SofaScore may store as 'expectedGoals' minus penalty xG, or as a
    separate stat. We compute from lineups expectedGoals minus penalty
    goals × 0.79 if no direct field exists.
    """
    columns = ['Team', 'Player', 'NPxG']
    rows = []
    for team_name, p in _iter_players_with_stats(match_data):
        # Try direct field first
        npxg = _player_stat_value(p, 'expectedGoalsNonPenalty')
        if npxg is None:
            npxg = _player_stat_value(p, 'nonPenaltyExpectedGoals')
        if npxg is None:
            # Fall back: xG minus penalty xG
            xg = _player_stat_value(p, 'expectedGoals')
            pen_goals = _player_stat_value(p, 'penaltyGoals') or 0
            if xg is not None:
                npxg = float(xg) - float(pen_goals) * 0.79
            else:
                continue
        player_info = p.get('player', {})
        name = player_info.get('name') or player_info.get('shortName', '')
        rows.append({'Team': team_name, 'Player': name, 'NPxG': round(float(npxg), 2)})
    return pd.DataFrame(rows, columns=columns)


def extract_player_positions(match_data):
    """
    Broad position group for every player who featured.
    Returns columns: Team, Player, Position.
    """
    columns = ['Team', 'Player', 'Position']
    rows = []
    for team_name, p in _iter_players_with_stats(match_data):
        minutes = _player_stat_value(p, 'minutesPlayed')
        if minutes is None or int(minutes) == 0:
            continue
        # Match position (outer), not career position (inner player.position)
        position = p.get('position')
        if not position:
            player_info = p.get('player', {})
            position = player_info.get('position', '')
        label = POSITION_MAP.get(position)
        if label is None:
            continue
        player_info = p.get('player', {})
        name = player_info.get('name') or player_info.get('shortName', '')
        rows.append({'Team': team_name, 'Player': name, 'Position': label})
    return pd.DataFrame(rows, columns=columns)


def extract_player_sprints(match_data):
    """
    SofaScore doesn't provide sprint data. Returns empty DataFrame.
    """
    return pd.DataFrame(columns=['Team', 'Player', 'Sprints'])


def extract_player_line_breaking_passes(match_data):
    """
    SofaScore doesn't provide line-breaking pass data. Returns empty DataFrame.
    """
    return pd.DataFrame(columns=['Team', 'Player', 'Line Breaking Passes'])


# ============================================================
# 7. LINEUP DATA (age, starter/sub)
# ============================================================

def extract_player_age_and_start(match_data):
    """
    Per-player age and starter-vs-substitute status.
    Returns columns: Team, Player, Age, Started.

    SofaScore lineups have 'substitute': bool on each player entry,
    and player.dateOfBirthTimestamp for computing age.
    """
    columns = ['Team', 'Player', 'Age', 'Started']
    lineups = _get_lineup_data(match_data)
    home_name, away_name = extract_team_names(match_data)

    # Get match date for age calculation
    details = match_data.get('match_details') or {}
    event = details.get('event', details)
    start_ts = event.get('startTimestamp')
    if start_ts:
        match_date = datetime.fromtimestamp(start_ts, tz=timezone.utc)
    else:
        match_date = None

    rows = []
    for side, team_name in [('home', home_name), ('away', away_name)]:
        side_data = lineups.get(side, {})
        if not isinstance(side_data, dict):
            continue
        players = side_data.get('players', [])
        for p in players:
            if not isinstance(p, dict):
                continue
            player_info = p.get('player', {})
            name = player_info.get('name') or player_info.get('shortName', '')
            if not name:
                continue

            # Age from dateOfBirthTimestamp
            dob_ts = player_info.get('dateOfBirthTimestamp')
            age = None
            if dob_ts and match_date:
                try:
                    dob = datetime.fromtimestamp(dob_ts, tz=timezone.utc)
                    age = (match_date - dob).days // 365
                except (OSError, ValueError):
                    pass

            if age is None:
                continue

            # Started = not a substitute
            is_sub = p.get('substitute', False)
            started = not bool(is_sub)

            rows.append({'Team': team_name, 'Player': name, 'Age': age, 'Started': started})

    return pd.DataFrame(rows, columns=columns)


# ============================================================
# 8. CARDS (from incidents)
# ============================================================

def extract_player_cards(match_data):
    """
    Per-player Yellow/Red card counts. Returns columns:
    Team, Player, Yellow Cards, Red Cards.
    """
    columns = ['Team', 'Player', 'Yellow Cards', 'Red Cards']
    incidents_data = match_data.get('incidents') or {}
    incidents = incidents_data.get('incidents', [])
    if not isinstance(incidents, list):
        return pd.DataFrame(columns=columns)

    home_name, away_name = extract_team_names(match_data)

    counts = {}  # (team, player) -> [yellow, red]
    for inc in incidents:
        if not isinstance(inc, dict):
            continue
        inc_type = str(inc.get('incidentType', inc.get('type', ''))).lower()
        if inc_type != 'card':
            continue

        card_type_raw = str(inc.get('incidentClass', inc.get('cardType',
                          inc.get('card', '')))).lower()
        # Determine team
        is_home = inc.get('isHome')
        team = home_name if is_home else away_name
        if not team:
            continue

        # Player name
        player = inc.get('player', {})
        if isinstance(player, dict):
            name = player.get('name') or player.get('shortName', '')
        elif isinstance(player, str):
            name = player
        else:
            continue
        if not name:
            continue

        yellow, red = counts.setdefault((team, name), [0, 0])
        if 'yellow' in card_type_raw and 'red' not in card_type_raw:
            yellow += 1
        elif 'red' in card_type_raw and 'yellow' in card_type_raw:
            # YellowRed = second yellow
            yellow += 1
            red += 1
        elif 'red' in card_type_raw:
            red += 1
        counts[(team, name)] = [yellow, red]

    rows = [
        {'Team': t, 'Player': p, 'Yellow Cards': y, 'Red Cards': r}
        for (t, p), (y, r) in counts.items()
    ]
    return pd.DataFrame(rows, columns=columns)


# ============================================================
# 9. METADATA
# ============================================================

def extract_referee(match_data):
    """Extract referee name from match details."""
    details = match_data.get('match_details') or {}
    event = details.get('event', details)
    referee = event.get('referee', {})
    if isinstance(referee, dict):
        return referee.get('name')
    return None


def _extract_kickoff_utc(match_data):
    """Extract kickoff as a UTC datetime from match details."""
    details = match_data.get('match_details') or {}
    event = details.get('event', details)
    ts = event.get('startTimestamp')
    if ts:
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        except (OSError, ValueError):
            pass
    return None


def extract_kickoff_local_str(match_data, tz_name='Europe/London'):
    """
    Kickoff datetime as a UK-local string, e.g. "2019-05-12 16:00".
    Same format as fotmob_report.extract_kickoff_local_str().
    """
    utc_dt = _extract_kickoff_utc(match_data)
    if utc_dt is None:
        return None
    try:
        import zoneinfo
        local_tz = zoneinfo.ZoneInfo(tz_name)
    except (ImportError, Exception):
        try:
            import pytz
            local_tz = pytz.timezone(tz_name)
        except ImportError:
            local_tz = timezone.utc
    local_dt = utc_dt.astimezone(local_tz)
    return local_dt.strftime('%Y-%m-%d %H:%M')


def extract_matchweek(match_data):
    """Extract the matchweek/round number."""
    details = match_data.get('match_details') or {}
    event = details.get('event', details)
    round_info = event.get('roundInfo', {})
    if isinstance(round_info, dict):
        return round_info.get('round')
    return None


def extract_league_name(match_data):
    """Extract the league/tournament name."""
    details = match_data.get('match_details') or {}
    event = details.get('event', details)
    tournament = event.get('tournament', {})
    if isinstance(tournament, dict):
        return tournament.get('name')
    return None


# ============================================================
# 10. PLAYER WINDOWS + PLUS MINUS
# ============================================================

def extract_player_windows(match_data, player_minutes=None, shots_df=None):
    """
    Per-player on-pitch window: Team, Player, start_minute, end_minute,
    Minutes Played, subbed_off. Same interface as fotmob_report's version.

    Uses SofaScore's lineups data (substitute flag) and incidents
    (substitution events) to determine when players were on/off.
    """
    columns = ['Team', 'Player', 'start_minute', 'end_minute', 'Minutes Played', 'subbed_off']

    lineups = _get_lineup_data(match_data)
    if not lineups:
        return pd.DataFrame(columns=columns)

    if player_minutes is None:
        player_minutes = extract_player_minutes(match_data)
    minutes_lookup = {(r['Team'], r['Player']): r['Minutes Played']
                      for _, r in player_minutes.iterrows()}

    # Build substitution events from incidents
    incidents_data = match_data.get('incidents') or {}
    incidents = incidents_data.get('incidents', [])
    home_name, away_name = extract_team_names(match_data)

    # sub_in: {(team, player_name): minute}, sub_out: {(team, player_name): minute}
    sub_in_map = {}
    sub_out_map = {}
    for inc in (incidents if isinstance(incidents, list) else []):
        if not isinstance(inc, dict):
            continue
        inc_type = str(inc.get('incidentType', inc.get('type', ''))).lower()
        if inc_type != 'substitution':
            continue
        is_home = inc.get('isHome')
        team = home_name if is_home else away_name
        minute = inc.get('time', inc.get('minute'))
        if minute is None:
            continue

        # Player coming on
        player_in = inc.get('playerIn', {})
        if isinstance(player_in, dict):
            name_in = player_in.get('name') or player_in.get('shortName', '')
            if name_in:
                sub_in_map[(team, name_in)] = minute

        # Player going off
        player_out = inc.get('playerOut', inc.get('player', {}))
        if isinstance(player_out, dict):
            name_out = player_out.get('name') or player_out.get('shortName', '')
            if name_out:
                sub_out_map[(team, name_out)] = minute

    fallback_end = 90.0
    if shots_df is not None and not shots_df.empty:
        eff = shots_df['Minute'].fillna(0) + shots_df['Added Time'].fillna(0)
        if len(eff):
            fallback_end = max(fallback_end, eff.max())

    rows = []
    for side, team_name in [('home', home_name), ('away', away_name)]:
        side_data = lineups.get(side, {})
        if not isinstance(side_data, dict):
            continue
        players = side_data.get('players', [])
        for p in players:
            if not isinstance(p, dict):
                continue
            player_info = p.get('player', {})
            name = player_info.get('name') or player_info.get('shortName', '')
            if not name:
                continue

            is_sub = p.get('substitute', False)
            minutes_played = minutes_lookup.get((team_name, name))

            # Check if actually appeared
            sub_in_time = sub_in_map.get((team_name, name))
            sub_out_time = sub_out_map.get((team_name, name))
            rating = _player_stat_value(p, 'rating')

            appeared = (minutes_played is not None or sub_in_time is not None
                        or sub_out_time is not None or rating is not None)
            if not appeared:
                continue

            # Start minute
            if is_sub and sub_in_time is not None:
                start = sub_in_time
            elif is_sub:
                # Sub who appeared but no sub event found
                start = 0  # best effort
            else:
                start = 0

            # End minute
            subbed_off = sub_out_time is not None
            if subbed_off:
                end = sub_out_time
            elif minutes_played is not None:
                end = max(start + minutes_played, fallback_end)
            else:
                end = fallback_end

            rows.append({
                'Team': team_name,
                'Player': name,
                'start_minute': start,
                'end_minute': end,
                'Minutes Played': round(minutes_played) if minutes_played is not None else round(end - start),
                'subbed_off': subbed_off,
            })

    return pd.DataFrame(rows, columns=columns)


def _combine_sequence_group(rows):
    """
    Combine a group of same-minute shots into a single probability.
    Same logic as fotmob_report._combine_sequence_group().
    """
    combined = 0.0
    chain_product = 1.0
    prev_outcome = None

    for xg, outcome in rows:
        xg = float(xg) if xg else 0.0
        # A new chain starts if the previous shot was Off Target, Goal, or Own Goal
        if prev_outcome in (None, 'Off Target', 'Goal', 'Own Goal'):
            combined += 1.0 - chain_product  # close out previous chain
            chain_product = 1.0
        chain_product *= (1.0 - xg)
        prev_outcome = outcome

    combined += 1.0 - chain_product  # close final chain
    return round(combined, 4)


def compute_plus_minus(shots_df, player_windows, home_name=None, away_name=None):
    """
    Plus Minus tab - same interface as fotmob_report.compute_plus_minus().
    """
    out_cols = ['Team', 'Player', 'Minutes Played', 'Goals For', 'Goals Against',
                'Shots', 'Shots Against', 'xG', 'xG Against']
    if shots_df.empty or player_windows.empty:
        return pd.DataFrame(columns=out_cols)

    work = shots_df[shots_df['Situation'] != 'Penalty'].copy()
    work['xG'] = work['xG'].fillna(0.0)
    work['Minute'] = work['Minute'].fillna(0)
    work['Added Time'] = work['Added Time'].fillna(0)
    work['_effective_minute'] = work['Minute'] + work['Added Time']

    own_goal_work = work[work['Outcome'] == 'Own Goal']
    work = work[work['Outcome'] != 'Own Goal']

    seq = (work.groupby(['Team', '_effective_minute']).apply(
                lambda g: _combine_sequence_group(zip(g['xG'], g['Outcome'])), include_groups=False)
           .reset_index(name='seq_xG'))

    teams_all = ([t for t in [home_name, away_name] if t is not None]
                 or sorted(player_windows['Team'].dropna().unique()))
    opponent = {teams_all[0]: teams_all[1], teams_all[1]: teams_all[0]} if len(teams_all) == 2 else {}

    records = []
    for _, prow in player_windows.iterrows():
        team = prow['Team']
        opp = opponent.get(team)
        start, end, subbed_off = prow['start_minute'], prow['end_minute'], prow['subbed_off']

        if subbed_off:
            mask = lambda m: (m >= start) & (m < end)
        else:
            mask = lambda m: (m >= start) & (m <= end)

        own_shots = work[(work['Team'] == team) & mask(work['_effective_minute'])]
        opp_shots = work[(work['Team'] == opp) & mask(work['_effective_minute'])] if opp else work.iloc[0:0]
        own_seq = seq[(seq['Team'] == team) & mask(seq['_effective_minute'])]
        opp_seq = seq[(seq['Team'] == opp) & mask(seq['_effective_minute'])] if opp else seq.iloc[0:0]

        own_og = own_goal_work[(own_goal_work['Team'] == team) & mask(own_goal_work['_effective_minute'])]
        opp_og = (own_goal_work[(own_goal_work['Team'] == opp) & mask(own_goal_work['_effective_minute'])]
                  if opp else own_goal_work.iloc[0:0])

        records.append({
            'Team': team,
            'Player': prow['Player'],
            'Minutes Played': prow['Minutes Played'],
            'Goals For': int((own_shots['Outcome'] == 'Goal').sum()) + int(len(opp_og)),
            'Goals Against': int((opp_shots['Outcome'] == 'Goal').sum()) + int(len(own_og)),
            'Shots': int(len(own_shots)),
            'Shots Against': int(len(opp_shots)),
            'xG': round(own_seq['seq_xG'].sum(), 2),
            'xG Against': round(opp_seq['seq_xG'].sum(), 2),
        })

    out = pd.DataFrame(records, columns=out_cols)
    out = out.sort_values(['Team', 'Minutes Played'], ascending=[True, False]).reset_index(drop=True)
    return out


# ============================================================
# 11. SHOT BREAKDOWNS + SCORING STATS
# ============================================================
PENALTY_XG = 0.79

SHOT_BREAKDOWN_DIMENSIONS = [
    ('By Player', 'Player'),
    ('By Situation', 'Situation'),
    ('By Body Part', 'Body Part'),
]


def compute_shot_breakdowns(shots_df, player_xa=None, player_minutes=None,
                             player_sprints=None, player_line_breaking_passes=None,
                             player_npxg=None):
    """
    Roll the shot map up along dimensions - same interface as
    fotmob_report.compute_shot_breakdowns().
    """
    # Import fotmob_report's implementation directly - it only depends
    # on the shots DataFrame format which we match exactly
    import fotmob_report as fr
    return fr.compute_shot_breakdowns(shots_df, player_xa, player_minutes,
                                       player_sprints, player_line_breaking_passes,
                                       player_npxg)


def compute_player_scoring_stats(match_data, shots_df, player_xa=None,
                                  player_minutes=None, player_sprints=None,
                                  player_npxg=None):
    """
    Full per-player scoring-stats table, same output format as
    fotmob_report.compute_player_scoring_stats():
    Team, Player, Minutes Played, Goals, Assists, Shots, NPxG, PS-xG,
    xA, PK, PK Attempted, Sprints.

    Assists and PS-xG come from SofaScore's lineup player statistics.
    """
    columns = ['Team', 'Player', 'Minutes Played', 'Goals', 'Assists', 'Shots',
               'NPxG', 'PS-xG', 'xA', 'PK', 'PK Attempted', 'Sprints']

    # Extract Assists and PS-xG from SofaScore lineups
    ss_rows = []
    for team_name, p in _iter_players_with_stats(match_data):
        player_info = p.get('player', {})
        name = player_info.get('name') or player_info.get('shortName', '')
        if not name:
            continue
        assists = _player_stat_value(p, 'goalAssist')
        if assists is None:
            assists = _player_stat_value(p, 'assists')
        psxg = _player_stat_value(p, 'expectedGoalsOnTarget')
        if psxg is None:
            psxg = _player_stat_value(p, 'goalsPrevented')
        if assists is not None or psxg is not None:
            row = {'Team': team_name, 'Player': name}
            if assists is not None:
                row['Assists'] = int(assists)
            if psxg is not None:
                row['PS-xG'] = round(float(psxg), 2)
            ss_rows.append(row)

    ss_df = pd.DataFrame(ss_rows, columns=['Team', 'Player', 'Assists', 'PS-xG'])
    npxg_df = (player_npxg[['Team', 'Player', 'NPxG']]
               if player_npxg is not None and not player_npxg.empty
               else pd.DataFrame(columns=['Team', 'Player', 'NPxG']))

    if shots_df is not None and not shots_df.empty:
        countable_shots = shots_df[shots_df['Outcome'] != 'Own Goal']
        shot_agg = countable_shots.groupby(['Team', 'Player']).agg(
            Shots=('Outcome', 'size'),
            Goals=('Outcome', lambda s: int((s == 'Goal').sum())),
        ).reset_index()
        pens = countable_shots[countable_shots['Situation'] == 'Penalty']
        if not pens.empty:
            pk_agg = pens.groupby(['Team', 'Player']).agg(
                **{'PK Attempted': ('Outcome', 'size'),
                   'PK': ('Outcome', lambda s: int((s == 'Goal').sum()))}
            ).reset_index()
        else:
            pk_agg = pd.DataFrame(columns=['Team', 'Player', 'PK Attempted', 'PK'])
        shot_agg = shot_agg.merge(pk_agg, on=['Team', 'Player'], how='left')
    else:
        shot_agg = pd.DataFrame(columns=['Team', 'Player', 'Shots', 'Goals', 'PK Attempted', 'PK'])

    roster_sources = []
    for df in [ss_df, npxg_df, shot_agg, player_xa, player_minutes]:
        if df is not None and not df.empty:
            roster_sources.append(df[['Team', 'Player']])
    if not roster_sources:
        return pd.DataFrame(columns=columns)
    roster = pd.concat(roster_sources).drop_duplicates().reset_index(drop=True)

    out = roster.merge(shot_agg, on=['Team', 'Player'], how='left')
    out = out.merge(ss_df, on=['Team', 'Player'], how='left')
    out = out.merge(npxg_df, on=['Team', 'Player'], how='left')
    out = out.merge(player_xa if player_xa is not None and not player_xa.empty
                     else pd.DataFrame(columns=['Team', 'Player', 'xA']),
                     on=['Team', 'Player'], how='left')
    out = out.merge(player_minutes if player_minutes is not None and not player_minutes.empty
                     else pd.DataFrame(columns=['Team', 'Player', 'Minutes Played']),
                     on=['Team', 'Player'], how='left')
    out['Sprints'] = 0

    for c in ['Goals', 'Shots', 'PK', 'PK Attempted', 'Sprints', 'Assists']:
        out[c] = pd.to_numeric(out[c], errors='coerce').fillna(0).astype(int)
    for c in ['NPxG', 'PS-xG', 'xA']:
        out[c] = pd.to_numeric(out[c], errors='coerce').fillna(0.0).round(2)
    out['Minutes Played'] = pd.to_numeric(out['Minutes Played'], errors='coerce')

    return (out[columns]
            .sort_values(['Team', 'Minutes Played'], ascending=[True, False])
            .reset_index(drop=True))


def compute_xg_breakdown(shots_df, home_name, away_name):
    """
    xG split by half and phase - same interface as
    fotmob_report.compute_xg_breakdown().
    """
    import fotmob_report as fr
    return fr.compute_xg_breakdown(shots_df, home_name, away_name)


# ============================================================
# 12. WORKBOOK BUILDER
# ============================================================

def build_workbook(shots_df, totals_df, home_name, away_name, match_id,
                   shot_breakdowns=None, xg_breakdown=None, plus_minus=None):
    """
    Build the same Excel workbook as fotmob_report.build_workbook().
    Reuses fotmob_report's implementation.
    """
    import fotmob_report as fr
    return fr.build_workbook(shots_df, totals_df, home_name, away_name, match_id,
                              shot_breakdowns, xg_breakdown, plus_minus)


# ============================================================
# 13. EXTRACT TEAM ID MAP (for compatibility)
# ============================================================

def extract_team_id_map(match_data):
    """
    Returns {team_id: team_name} mapping. Needed for some internal functions.
    """
    details = match_data.get('match_details') or {}
    event = details.get('event', details)
    result = {}
    for side_key in ('homeTeam', 'awayTeam'):
        team = event.get(side_key, {})
        if isinstance(team, dict):
            tid = team.get('id')
            tname = team.get('name') or team.get('shortName', '')
            if tid is not None:
                result[tid] = tname
    return result


# ============================================================
# MAIN (standalone testing)
# ============================================================

def main():
    import sys
    if len(sys.argv) < 2:
        print("Usage: python sofascore_report.py <sofascore_match_url>")
        sys.exit(1)

    url = sys.argv[1]
    print(f"Scraping SofaScore match: {url}")
    match_data, match_id = scrape_match(url)

    home_name, away_name = extract_team_names(match_data)
    print(f"Match: {home_name} vs {away_name}")

    referee = extract_referee(match_data)
    print(f"Referee: {referee}")

    kickoff = extract_kickoff_local_str(match_data)
    print(f"Kickoff: {kickoff}")

    matchweek = extract_matchweek(match_data)
    print(f"Matchweek: {matchweek}")

    shots_df = compute_shots(match_data)
    print(f"\nShots: {len(shots_df)}")
    if not shots_df.empty:
        print(shots_df.to_string(index=False))

    totals_df = compute_totals(match_data, shots_df, home_name, away_name)
    print(f"\nTotals:")
    print(totals_df.to_string(index=False))

    player_minutes = extract_player_minutes(match_data)
    print(f"\nPlayer Minutes: {len(player_minutes)} players")

    player_cards = extract_player_cards(match_data)
    print(f"\nCards: {len(player_cards)} carded players")

    print("\nDone.")


if __name__ == '__main__':
    main()
