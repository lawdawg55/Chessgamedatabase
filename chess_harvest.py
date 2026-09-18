#!/usr/bin/env python3
"""
Chess_harvest.py
Harvests games from Chess.com + Lichess and stores them in Supabase/Postgres.
"""

import os
import time
import json
import requests
import psycopg2
from psycopg2.extras import execute_values
from datetime import datetime
from typing import List, Dict, Any, Optional

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
CHESS_COM_USER = os.getenv("CHESS_COM_USER", "your_fallback_username")
LICHESS_USER   = os.getenv("LICHESS_USER",   "your_fallback_username")
EMAIL          = os.getenv("CONTACT_EMAIL",  "law.colby@gmail.com")
DATABASE_URL   = os.getenv(
    "DATABASE_URL",
    "postgresql://postgres:t57FCOqtVfsDKozV@db.zabecfyzzoqdzklatwlq.supabase.co:5432/postgres"
)

# Chess.com requires a descriptive User-Agent
USER_AGENT = f"ChessHarvest/1.0 (contact: {EMAIL})"

# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------
def get_connection():
    return psycopg2.connect(DATABASE_URL, sslmode="require")

def create_tables(conn):
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS chess_games (
                id              SERIAL PRIMARY KEY,
                source          TEXT NOT NULL,          -- 'chess.com' or 'lichess'
                username        TEXT NOT NULL,
                game_id         TEXT NOT NULL,
                white           TEXT,
                black           TEXT,
                result          TEXT,
                time_control    TEXT,
                time_class      TEXT,                   -- bullet/blitz/rapid/daily
                rated           BOOLEAN,
                rules           TEXT,                   -- standard, chess960, etc.
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
    print("✓ Tables ready")

def insert_games(conn, games: List[Dict[str, Any]]):
    if not games:
        return 0

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
            json.dumps(g.get("raw_json")) if g.get("raw_json") else None,
        )
        for g in games
    ]

    with conn.cursor() as cur:
        execute_values(cur, sql, values, page_size=200)
    conn.commit()
    return len(games)

# ---------------------------------------------------------------------------
# Chess.com harvester
# ---------------------------------------------------------------------------
def chesscom_headers() -> Dict[str, str]:
    return {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    }

def get_chesscom_archives(username: str) -> List[str]:
    url = f"https://api.chess.com/pub/player/{username}/games/archives"
    r = requests.get(url, headers=chesscom_headers(), timeout=30)
    r.raise_for_status()
    return r.json().get("archives", [])

def get_chesscom_month(url: str) -> List[Dict]:
    # Prefer the JSON endpoint so we get structured data
    json_url = url.rstrip("/")  # already ends with /YYYY/MM
    r = requests.get(json_url, headers=chesscom_headers(), timeout=60)
    r.raise_for_status()
    return r.json().get("games", [])

def harvest_chesscom(username: str) -> List[Dict[str, Any]]:
    print(f"\n=== Chess.com – {username} ===")
    archives = get_chesscom_archives(username)
    print(f"Found {len(archives)} monthly archives")

    all_games = []
    for i, archive_url in enumerate(archives, 1):
        print(f"  [{i}/{len(archives)}] {archive_url.split('/')[-2]}/{archive_url.split('/')[-1]}", end=" ")
        try:
            month_games = get_chesscom_month(archive_url)
            for g in month_games:
                # Extract a stable game_id (Chess.com uses uuid or url)
                game_id = g.get("uuid") or g.get("url", "").split("/")[-1] or str(hash(g.get("pgn", "")))
                white = g.get("white", {}).get("username")
                black = g.get("black", {}).get("username")
                result = None
                if white and black:
                    result = g.get("white", {}).get("result")  # win/loss/draw/…
                    if not result:
                        result = "unknown"

                end_time = None
                if "end_time" in g:
                    end_time = datetime.utcfromtimestamp(g["end_time"])

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
            time.sleep(0.8)  # be polite
        except Exception as e:
            print(f"ERROR: {e}")
            continue

    print(f"Total Chess.com games collected: {len(all_games)}")
    return all_games

# ---------------------------------------------------------------------------
# Lichess harvester
# ---------------------------------------------------------------------------
def harvest_lichess(username: str, max_games: Optional[int] = None) -> List[Dict[str, Any]]:
    print(f"\n=== Lichess – {username} ===")
    url = f"https://lichess.org/api/games/user/{username}"
    params = {
        "max": max_games or 300,          # safety limit; raise if you need more
        "clocks": "false",
        "evals": "false",
        "opening": "false",
        "literate": "false",
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
                    end_time = datetime.utcfromtimestamp(g["lastMoveAt"] / 1000)

                games.append({
                    "source": "lichess",
                    "username": username,
                    "game_id": g.get("id"),
                    "white": white,
                    "black": black,
                    "result": g.get("status"),          # mate, resign, draw, etc.
                    "time_control": g.get("clock", {}).get("initial") if g.get("clock") else None,
                    "time_class": g.get("speed"),        # bullet, blitz, rapid, classical
                    "rated": g.get("rated"),
                    "rules": g.get("variant"),
                    "pgn": None,                        # we asked for ndjson; can request PGN separately
                    "end_time": end_time,
                    "raw_json": g,
                })
    except Exception as e:
        print(f"Lichess error: {e}")

    print(f"Total Lichess games collected: {len(games)}")
    return games

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    print("Chess Harvest starting…")
    print(f"Chess.com user : {CHESS_COM_USER}")
    print(f"Lichess user   : {LICHESS_USER}")
    print(f"Contact email  : {EMAIL}")

    conn = get_connection()
    try:
        create_tables(conn)

        # Chess.com
        chesscom_games = harvest_chesscom(CHESS_COM_USER)
        inserted = insert_games(conn, chesscom_games)
        print(f"Inserted {inserted} new Chess.com games")

        # Lichess
        lichess_games = harvest_lichess(LICHESS_USER)
        inserted = insert_games(conn, lichess_games)
        print(f"Inserted {inserted} new Lichess games")

        print("\n✓ Harvest complete")
    finally:
        conn.close()

if __name__ == "__main__":
    main()
