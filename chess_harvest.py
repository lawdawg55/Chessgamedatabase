#!/usr/bin/env python3
"""
Chess Harvest – Daily sync of Chess.com + Lichess games into Supabase.
"""

import os
import sys
import time
import json
import traceback
import requests
import psycopg2
from psycopg2.extras import execute_values
from datetime import datetime, timezone
from typing import List, Dict, Any

# ---------------------------------------------------------------------------
# Configuration (all from environment / GitHub secrets)
# ---------------------------------------------------------------------------
CHESS_COM_USER = os.getenv("CHESS_COM_USER", "ColbyLaw42")
LICHESS_USER   = os.getenv("LICHESS_USER",   "ColbyLaw42")
EMAIL          = os.getenv("CONTACT_EMAIL",  "law.colby@gmail.com")

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    print("❌ DATABASE_URL environment variable is not set.")
    print("   Set it to the Supabase *Session pooler* connection string.")
    sys.exit(1)

USER_AGENT = f"ChessHarvest/1.0 (contact: {EMAIL})"

print("=" * 60)
print("Chess Harvest")
print(f"Chess.com user : {CHESS_COM_USER}")
print(f"Lichess user   : {LICHESS_USER}")
print(f"Email          : {EMAIL}")
# Only show partial URL for safety
print(f"DATABASE_URL   : {DATABASE_URL[:45]}...{DATABASE_URL[-15:]}")
print("=" * 60)

# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------
def get_connection():
    print("\n→ Connecting to database...")
    try:
        conn = psycopg2.connect(
            DATABASE_URL,
            sslmode="require",
            connect_timeout=20,
        )
        print("✓ Database connection successful")
        return conn
    except Exception as e:
        print("❌ DATABASE CONNECTION FAILED")
        print(f"   {type(e).__name__}: {e}")
        print("\nCommon fixes:")
        print("  • Use the *Session pooler* connection string from Supabase")
        print("    (not the direct db.xxxxx.supabase.co host)")
        print("  • Make sure the password is correct and the secret is set")
        raise

def create_tables(conn):
    print("\n→ Creating / verifying table...")
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS chess_games (
                id              SERIAL PRIMARY KEY,
                source          TEXT NOT NULL,
                username        TEXT NOT NULL,
                game_id         TEXT NOT NULL,
                white           TEXT,
                black           TEXT,
                result          TEXT,
                time_control    TEXT,
                time_class      TEXT,
                rated           BOOLEAN,
                rules           TEXT,
                pgn             TEXT,
                end_time        TIMESTAMPTZ,
                raw_json        JSONB,
                harvested_at    TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE (source, game_id)
            );
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_chess_games_username
                ON chess_games (username);
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_chess_games_end_time
                ON chess_games (end_time DESC);
        """)
    conn.commit()
    print("✓ Table 'chess_games' is ready")

def insert_games(conn, games: List[Dict[str, Any]]) -> int:
    if not games:
        print("  (no games to insert)")
        return 0

    print(f"\n→ Inserting {len(games)} games...")
    sql = """
        INSERT INTO chess_games (
            source, username, game_id, white, black, result,
            time_control, time_class, rated, rules, pgn, end_time, raw_json
        ) VALUES %s
        ON CONFLICT (source, game_id) DO NOTHING
    """
    values = [
        (
            g["source"],
            g["username"],
            g["game_id"],
            g.get("white"),
            g.get("black"),
            g.get("result"),
            g.get("time_control"),
            g.get("time_class"),
            g.get("rated"),
            g.get("rules"),
            g.get("pgn"),
            g.get("end_time"),
            json.dumps(g.get("raw_json")) if g.get("raw_json") is not None else None,
        )
        for g in games
    ]

    with conn.cursor() as cur:
        execute_values(cur, sql, values, page_size=100)
    conn.commit()
    print(f"✓ Insert completed ({len(games)} rows processed)")
    return len(games)

# ---------------------------------------------------------------------------
# Chess.com harvester
# ---------------------------------------------------------------------------
def chesscom_headers():
    return {"User-Agent": USER_AGENT, "Accept": "application/json"}

def get_chesscom_archives(username: str) -> List[str]:
    url = f"https://api.chess.com/pub/player/{username}/games/archives"
    r = requests.get(url, headers=chesscom_headers(), timeout=30)
    r.raise_for_status()
    return r.json().get("archives", [])

def get_chesscom_month(url: str) -> List[Dict]:
    r = requests.get(url, headers=chesscom_headers(), timeout=60)
    r.raise_for_status()
    return r.json().get("games", [])

def harvest_chesscom(username: str) -> List[Dict[str, Any]]:
    print(f"\n=== Chess.com – {username} ===")
    archives = get_chesscom_archives(username)
    print(f"Found {len(archives)} monthly archives")

    all_games = []
    for i, archive_url in enumerate(archives, 1):
        ym = "/".join(archive_url.split("/")[-2:])
        print(f"  [{i}/{len(archives)}] {ym}", end=" ", flush=True)
        try:
            month_games = get_chesscom_month(archive_url)
            for g in month_games:
                game_id = (
                    g.get("uuid")
                    or (g.get("url") or "").split("/")[-1]
                    or str(hash(g.get("pgn", "")))
                )
                white = g.get("white", {}).get("username")
                black = g.get("black", {}).get("username")
                result = g.get("white", {}).get("result") or "unknown"
                end_time = None
                if "end_time" in g:
                    end_time = datetime.fromtimestamp(g["end_time"], tz=timezone.utc)

                all_games.append({
                    "source": "chess.com",
                    "username": username,
                    "game_id": str(game_id),
                    "white": white,
                    "black": black,
                    "result": result,
                    "time_control": g.get("time_control"),
                    "time_class": g.get("time_class"),
                    "rated": g.get("rated"),
                    "rules": g.get("rules"),
                    "pgn": g.get("pgn"),
                    "end_time": end_time,
                    "raw_json": g,
                })
            print(f"→ {len(month_games)} games")
            time.sleep(0.6)  # be polite to the API
        except Exception as e:
            print(f"ERROR: {e}")
            continue

    print(f"Total Chess.com games: {len(all_games)}")
    return all_games

# ---------------------------------------------------------------------------
# Lichess harvester
# ---------------------------------------------------------------------------
def harvest_lichess(username: str, max_games: int = 500) -> List[Dict[str, Any]]:
    print(f"\n=== Lichess – {username} ===")
    url = f"https://lichess.org/api/games/user/{username}"
    params = {
        "max": max_games,
        "clocks": "false",
        "evals": "false",
        "opening": "false",
    }
    headers = {
        "Accept": "application/x-ndjson",
        "User-Agent": USER_AGENT,
    }

    games = []
    try:
        with requests.get(url, params=params, headers=headers, stream=True, timeout=120) as r:
            r.raise_for_status()
            for line in r.iter_lines(decode_unicode=True):
                if not line:
                    continue
                g = json.loads(line)
                players = g.get("players", {})
                white = players.get("white", {}).get("user", {}).get("name")
                black = players.get("black", {}).get("user", {}).get("name")
                end_time = None
                if "lastMoveAt" in g:
                    end_time = datetime.fromtimestamp(g["lastMoveAt"] / 1000, tz=timezone.utc)

                games.append({
                    "source": "lichess",
                    "username": username,
                    "game_id": g.get("id"),
                    "white": white,
                    "black": black,
                    "result": g.get("status"),
                    "time_control": (g.get("clock") or {}).get("initial"),
                    "time_class": g.get("speed"),
                    "rated": g.get("rated"),
                    "rules": g.get("variant"),
                    "pgn": None,
                    "end_time": end_time,
                    "raw_json": g,
                })
    except Exception as e:
        print(f"Lichess error: {e}")
        traceback.print_exc()

    print(f"Total Lichess games: {len(games)}")
    return games

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    conn = None
    try:
        conn = get_connection()
        create_tables(conn)

        # Chess.com
        chesscom_games = harvest_chesscom(CHESS_COM_USER)
        insert_games(conn, chesscom_games)

        # Lichess
        lichess_games = harvest_lichess(LICHESS_USER)
        insert_games(conn, lichess_games)

        # Final count
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM chess_games;")
            total = cur.fetchone()[0]
            print(f"\n✓ Final row count in chess_games: {total}")

        print("\n✅ Harvest finished successfully")

    except Exception as e:
        print("\n💥 SCRIPT FAILED")
        print(f"Final error: {e}")
        traceback.print_exc()
        sys.exit(1)          # important – makes GitHub Actions fail
    finally:
        if conn:
            conn.close()
            print("Database connection closed")

if __name__ == "__main__":
    main()
