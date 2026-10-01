"""
One-off diagnostic (v2): the first pass over this match flagged fouls with
ANY nearby opposite-team isTouch=True event, which pulled in a lot of noise
that isn't actually shot-creating - e.g. run against Hull vs Man Utd, none
of the flagged fouls in that first pass were the minute-36 foul that Pauly
confirmed by eye actually leads to a shot.

This version instead replicates compute_sca()'s OWN backward-walk from each
shot exactly (same BOUNDARY_TYPES/NON_BREAKING_DUEL_TYPES/OFFENSIVE_TYPES/
SHOT_TYPES/LOOSE_BALL_OPPONENT_TYPES constants, same break conditions) to
find only the fouls that are genuinely credited as a shot's SCA under the
CURRENT logic - i.e. exactly the fouls compute_sca() falls back to crediting
the shot-taker for today. For each one, it prints the foul, the shot it led
to, and every event in between (plus a little padding on each side),
looking for the "mirrored" isTouch=True event WhoScored logs for the player
who was actually fouled.

Run this the same way you'd run whoscored_report.py itself (same
environment, same Selenium/driver setup already working for your other
reports).

Usage:
    python diagnose_foul_touch.py "<whoscored match centre url>"

What to do with the output: paste it back (the minute-36 block especially)
so the real shape of the fouled-player event (its type.displayName, whether
it's genuinely isTouch=True, how closely its minute/second/x/y line up with
the Foul event, and which side of the foul it's on) can be confirmed rather
than guessed at.
"""
import sys
import whoscored_report as wr


def _fmt_row(df, idx, marker=""):
    row = df.loc[idx]
    qual_str = ", ".join(
        str(item.get('type', {}).get('displayName')) for item in row.get('qualifiers_parsed', [])
    )
    return (
        f"{marker}idx={idx!s:<5} min={row.get('minute')!s:>3}.{row.get('second', 0)!s:0>2}  "
        f"team={str(row.get('team'))[:18]:<18} player={str(row.get('playerName'))[:20]:<20} "
        f"type={row.get('type.displayName')!r:<16} outcome={row.get('outcomeType.displayName')!r:<14} "
        f"isTouch={row.get('isTouch')!s:<5} x={row.get('x')} y={row.get('y')}  quals=[{qual_str}]"
    )


def find_foul_scas(df):
    """
    Mirrors compute_sca()'s backward walk from each shot exactly, but
    instead of stopping at the first 2 found actions, records the case
    where a 'Foul' by the opponent is what compute_sca() would credit -
    returns a list of (foul_idx, shot_idx) pairs.
    """
    own_goal_mask = df.apply(wr.is_own_goal, axis=1)
    shots_idx = df.index[(df['isShot'] == True) & ~own_goal_mask].tolist()
    foul_scas = []
    for shot_i in shots_idx:
        shot_team = df.at[shot_i, 'team']
        found = []
        j = shot_i - 1
        while j >= 0 and len(found) < 2:
            row = df.loc[j]
            rtype = row['type.displayName']
            rteam = row['team']
            routcome = row['outcomeType.displayName']

            if rtype in wr.BOUNDARY_TYPES:
                break

            if rtype in wr.NON_BREAKING_DUEL_TYPES:
                if rtype in wr.COUNTABLE_DUEL_TYPES and rteam == shot_team and routcome == 'Successful':
                    found.append(j)
                j -= 1
                continue

            if rteam == shot_team:
                if rtype in wr.OFFENSIVE_TYPES:
                    found.append(j)
                    if rtype in wr.SHOT_TYPES:
                        break
            else:
                if rtype == 'Foul':
                    found.append(j)
                    foul_scas.append((j, shot_i))
                    break
                elif rtype in wr.LOOSE_BALL_OPPONENT_TYPES:
                    j -= 1
                    continue
                else:
                    break
            j -= 1
    return foul_scas


def main():
    if len(sys.argv) < 2:
        print('Usage: python diagnose_foul_touch.py "<whoscored match centre url>"')
        sys.exit(1)

    url = sys.argv[1]
    df, match_info = wr.scrape_match(url)
    print(f"Scraped {len(df)} events. Teams: {match_info.get('home_name')} vs {match_info.get('away_name')}")
    print()

    foul_scas = find_foul_scas(df)
    if not foul_scas:
        print("No shot in this match currently traces its SCA back to a 'Foul' event at all - "
              "nothing to inspect here.")
        return

    print(f"Found {len(foul_scas)} shot(s) whose SCA is currently (under the shot-taker fallback) "
          f"credited via a 'Foul' event:")
    print()

    for foul_idx, shot_idx in foul_scas:
        foul_row = df.loc[foul_idx]
        shot_row = df.loc[shot_idx]
        print(f"=== FOUL idx={foul_idx} (min {foul_row.get('minute')}) -> SHOT idx={shot_idx} "
              f"(min {shot_row.get('minute')}, {shot_row.get('playerName')}, "
              f"{shot_row.get('type.displayName')}) ===")

        # Print everything from a little before the foul through the shot
        # itself, so the mirrored event (wherever it lands) is visible.
        window_start = max(0, foul_idx - 2)
        window_end = shot_idx
        for idx in range(window_start, window_end + 1):
            marker = "[FOUL] " if idx == foul_idx else ("[SHOT] " if idx == shot_idx else "       ")
            print(_fmt_row(df, idx, marker=marker))
        print()


if __name__ == "__main__":
    main()
