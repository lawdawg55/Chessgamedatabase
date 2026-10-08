#!/usr/bin/env python3
"""
Chess Harvest – Daily sync of Chess.com + Lichess games into Supabase.

What this version does:
    1. Harvests Chess.com games from the public API.
    2. Stores game-level information in chess_games.
    3. Parses Chess.com PGNs with python-chess.
    4. Flattens every game into individual moves in chess_moves.
    5. Stores FEN before/after every move.
    6. Stores SAN and UCI notation.
    7. Uses bulk inserts for performance.
    8. Prevents duplicate games and moves.
    9. Keeps the original PGN and raw API JSON.
   10. Continues to support Lichess games.

Required packages:
    requests
    psycopg2-binary
    python-chess
"""

import os
import sys
import time
import json
import traceback
import requests
import psycopg2
import chess
import chess.pgn

from psycopg2.extras import execute_values
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

CHESS_COM_USER = os.getenv("CHESS_COM_USER", "ColbyLaw42")
LICHESS_USER   = os.getenv("LICHESS_USER", "ColbyLaw42")
EMAIL          = os.getenv("CONTACT_EMAIL", "law.colby@gmail.com")

DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    print("❌ DATABASE_URL environment variable is not set.")
    print("   Set it to your Supabase Session Pooler connection string.")
    sys.exit(1)

USER_AGENT = f"ChessHarvest/1.1 (contact: {EMAIL})"


print("=" * 70)
print("Chess Harvest – Game + Move Database")
print(f"Chess.com user : {CHESS_COM_USER}")
print(f"Lichess user   : {LICHESS_USER}")
print(f"Email          : {EMAIL}")

# Only show partial database URL for safety
if len(DATABASE_URL) > 60:
    print(f"DATABASE_URL   : {DATABASE_URL[:45]}...{DATABASE_URL[-15:]}")
else:
    print("DATABASE_URL   : configured")

print("=" * 70)


# ===========================================================================
# DATABASE
# ===========================================================================

def get_connection():
    """Create PostgreSQL/Supabase connection."""

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
        print("  • Use the Supabase Session Pooler connection string")
        print("  • Make sure DATABASE_URL is set in GitHub Secrets")
        print("  • Verify the database password")
        print("  • Verify the Supabase project is running")

        raise


def create_tables(conn):
    """
    Create/verify the game and move tables.

    chess_games = one row per game
    chess_moves  = one row per half-move/ply
    """

    print("\n→ Creating / verifying database tables...")

    with conn.cursor() as cur:

        # -------------------------------------------------------------------
        # GAME TABLE
        # -------------------------------------------------------------------

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

        # -------------------------------------------------------------------
        # MOVE TABLE
        # -------------------------------------------------------------------
        #
        # ply:
        #   1 = White's first move
        #   2 = Black's first move
        #   3 = White's second move
        #
        # move_number:
        #   1 for 1.e4
        #   1 for 1...e5
        #   2 for 2.Nf3
        #
        # color:
        #   white / black
        #
        # fen_before:
        #   Position immediately before the move
        #
        # fen_after:
        #   Position immediately after the move
        #
        # These FENs are extremely useful for Stockfish.
        # -------------------------------------------------------------------

        cur.execute("""
            CREATE TABLE IF NOT EXISTS chess_moves (

                id              BIGSERIAL PRIMARY KEY,

                source          TEXT NOT NULL,
                game_id         TEXT NOT NULL,

                ply             INTEGER NOT NULL,
                move_number     INTEGER NOT NULL,

                color           TEXT NOT NULL,

                san             TEXT,
                uci             TEXT,

                fen_before      TEXT,
                fen_after       TEXT,

                created_at      TIMESTAMPTZ DEFAULT NOW(),

                UNIQUE (source, game_id, ply)
            );
        """)

        # -------------------------------------------------------------------
        # GAME INDEXES
        # -------------------------------------------------------------------

        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_chess_games_username
            ON chess_games (username);
        """)

        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_chess_games_end_time
            ON chess_games (end_time DESC);
        """)

        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_chess_games_source_game
            ON chess_games (source, game_id);
        """)

        # -------------------------------------------------------------------
        # MOVE INDEXES
        # -------------------------------------------------------------------

        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_chess_moves_game
            ON chess_moves (source, game_id);
        """)

        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_chess_moves_ply
            ON chess_moves (ply);
        """)

        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_chess_moves_color
            ON chess_moves (color);
        """)

    conn.commit()

    print("✓ Table 'chess_games' is ready")
    print("✓ Table 'chess_moves' is ready")


# ===========================================================================
# CHESS GAME INSERT
# ===========================================================================

def insert_games(
    conn,
    games: List[Dict[str, Any]]
) -> int:

    if not games:
        print("  (no games to insert)")
        return 0

    print(f"\n→ Inserting {len(games)} games...")

    sql = """
        INSERT INTO chess_games (
            source,
            username,
            game_id,
            white,
            black,
            result,
            time_control,
            time_class,
            rated,
            rules,
            pgn,
            end_time,
            raw_json
        )
        VALUES %s

        ON CONFLICT (source, game_id)
        DO NOTHING
    """

    values = []

    for g in games:

        raw_json = g.get("raw_json")

        values.append(
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
                json.dumps(raw_json)
                if raw_json is not None
                else None,
            )
        )

    with conn.cursor() as cur:

        execute_values(
            cur,
            sql,
            values,
            page_size=500
        )

    conn.commit()

    print(f"✓ Game insert completed ({len(games)} rows processed)")

    return len(games)


# ===========================================================================
# PGN PARSER
# ===========================================================================

def parse_pgn_to_moves(
    game: Dict[str, Any]
) -> List[Dict[str, Any]]:
    """
    Convert a PGN into flattened move records.

    Each half-move becomes one database row.

    Example:

        1. e4 e5 2. Nf3 Nc6

    becomes:

        ply 1 -> e4
        ply 2 -> e5
        ply 3 -> Nf3
        ply 4 -> Nc6
    """

    pgn_text = game.get("pgn")

    if not pgn_text:
        return []

    try:

        # StringIO lets python-chess treat the PGN text like a file.
        from io import StringIO

        pgn_io = StringIO(pgn_text)

        chess_game = chess.pgn.read_game(pgn_io)

        if chess_game is None:
            print(
                f"  ⚠ Could not parse PGN for "
                f"{game.get('game_id')}"
            )
            return []

        board = chess_game.board()

        moves = []

        ply = 0

        for move in chess_game.mainline_moves():

            ply += 1

            # Position BEFORE the move.
            fen_before = board.fen()

            # Determine side making the move.
            color = "white" if board.turn == chess.WHITE else "black"

            # Full move number.
            move_number = board.fullmove_number

            # Human-readable notation.
            san = board.san(move)

            # Machine-readable notation.
            uci = move.uci()

            # Make move.
            board.push(move)

            # Position AFTER the move.
            fen_after = board.fen()

            moves.append(
                {
                    "source": game["source"],
                    "game_id": game["game_id"],
                    "ply": ply,
                    "move_number": move_number,
                    "color": color,
                    "san": san,
                    "uci": uci,
                    "fen_before": fen_before,
                    "fen_after": fen_after,
                }
            )

        return moves

    except Exception as e:

        print(
            f"  ⚠ PGN parsing error for "
            f"{game.get('game_id')}: {e}"
        )

        return []


# ===========================================================================
# MOVE INSERT
# ===========================================================================

def insert_moves(
    conn,
    moves: List[Dict[str, Any]]
) -> int:
    """
    Bulk insert flattened move data.

    Duplicate moves are ignored using:

        source + game_id + ply
    """

    if not moves:
        return 0

    sql = """
        INSERT INTO chess_moves (
            source,
            game_id,
            ply,
            move_number,
            color,
            san,
            uci,
            fen_before,
            fen_after
        )
        VALUES %s

        ON CONFLICT (source, game_id, ply)
        DO NOTHING
    """

    values = [
        (
            move["source"],
            move["game_id"],
            move["ply"],
            move["move_number"],
            move["color"],
            move["san"],
            move["uci"],
            move["fen_before"],
            move["fen_after"],
        )
        for move in moves
    ]

    with conn.cursor() as cur:

        execute_values(
            cur,
            sql,
            values,
            page_size=1000
        )

    conn.commit()

    return len(moves)


# ===========================================================================
# FLATTEN ALL GAMES
# ===========================================================================

def flatten_and_insert_moves(
    conn,
    games: List[Dict[str, Any]]
) -> int:
    """
    Parse all PGNs and bulk insert their moves.

    We collect all moves first and then perform a bulk insert.
    This is much faster than inserting every move individually.
    """

    if not games:
        return 0

    print(
        f"\n→ Flattening PGNs for "
        f"{len(games)} games..."
    )

    all_moves = []

    parsed_games = 0
    failed_games = 0

    for index, game in enumerate(games, 1):

        pgn = game.get("pgn")

        if not pgn:
            continue

        moves = parse_pgn_to_moves(game)

        if moves:

            parsed_games += 1
            all_moves.extend(moves)

        else:

            failed_games += 1

        # Progress every 100 games.
        if index % 100 == 0:

            print(
                f"  Processed {index}/{len(games)} games "
                f"→ {len(all_moves)} moves"
            )

    print(
        f"✓ PGN parsing complete"
        f"\n  Games parsed : {parsed_games}"
        f"\n  Failed/empty : {failed_games}"
        f"\n  Total moves  : {len(all_moves)}"
    )

    if not all_moves:
        return 0

    print(
        f"\n→ Bulk inserting {len(all_moves)} "
        f"move records..."
    )

    inserted = insert_moves(conn, all_moves)

    print(
        f"✓ Move insert completed "
        f"({inserted} rows processed)"
    )

    return inserted


# ===========================================================================
# CHESS.COM API
# ===========================================================================

def chesscom_headers():

    return {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    }


def get_chesscom_archives(
    username: str
) -> List[str]:

    url = (
        f"https://api.chess.com/pub/player/"
        f"{username}/games/archives"
    )

    r = requests.get(
        url,
        headers=chesscom_headers(),
        timeout=30,
    )

    r.raise_for_status()

    return r.json().get("archives", [])


def get_chesscom_month(
    url: str
) -> List[Dict]:

    r = requests.get(
        url,
        headers=chesscom_headers(),
        timeout=60,
    )

    r.raise_for_status()

    return r.json().get("games", [])


def harvest_chesscom(
    username: str
) -> List[Dict[str, Any]]:

    print(f"\n=== Chess.com – {username} ===")

    archives = get_chesscom_archives(username)

    print(
        f"Found {len(archives)} monthly archives"
    )

    all_games = []

    for i, archive_url in enumerate(
        archives,
        1
    ):

        ym = "/".join(
            archive_url.split("/")[-2:]
        )

        print(
            f"  [{i}/{len(archives)}] {ym}",
            end=" ",
            flush=True,
        )

        try:

            month_games = get_chesscom_month(
                archive_url
            )

            for g in month_games:

                # -----------------------------------------------------------
                # Game ID
                # -----------------------------------------------------------

                game_id = (
                    g.get("uuid")
                    or (
                        g.get("url") or ""
                    ).split("/")[-1]
                    or str(
                        hash(
                            g.get("pgn", "")
                        )
                    )
                )

                # -----------------------------------------------------------
                # Players
                # -----------------------------------------------------------

                white = (
                    g.get("white", {})
                    .get("username")
                )

                black = (
                    g.get("black", {})
                    .get("username")
                )

                # -----------------------------------------------------------
                # Result
                # -----------------------------------------------------------

                result = (
                    g.get("white", {})
                    .get("result")
                    or "unknown"
                )

                # -----------------------------------------------------------
                # End time
                # -----------------------------------------------------------

                end_time = None

                if "end_time" in g:

                    end_time = (
                        datetime.fromtimestamp(
                            g["end_time"],
                            tz=timezone.utc,
                        )
                    )

                # -----------------------------------------------------------
                # Store game
                # -----------------------------------------------------------

                all_games.append(
                    {
                        "source": "chess.com",
                        "username": username,
                        "game_id": str(game_id),

                        "white": white,
                        "black": black,

                        "result": result,

                        "time_control":
                            g.get("time_control"),

                        "time_class":
                            g.get("time_class"),

                        "rated":
                            g.get("rated"),

                        "rules":
                            g.get("rules"),

                        "pgn":
                            g.get("pgn"),

                        "end_time":
                            end_time,

                        "raw_json":
                            g,
                    }
                )

            print(
                f"→ {len(month_games)} games"
            )

            # Be polite to Chess.com API.
            time.sleep(0.6)

        except Exception as e:

            print(
                f"ERROR: {e}"
            )

            continue

    print(
        f"Total Chess.com games: "
        f"{len(all_games)}"
    )

    return all_games


# ===========================================================================
# LICHESS API
# ===========================================================================

def harvest_lichess(
    username: str,
    max_games: int = 500
) -> List[Dict[str, Any]]:

    print(
        f"\n=== Lichess – {username} ==="
    )

    url = (
        f"https://lichess.org/api/games/user/"
        f"{username}"
    )

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

        with requests.get(
            url,
            params=params,
            headers=headers,
            stream=True,
            timeout=120,
        ) as r:

            r.raise_for_status()

            for line in r.iter_lines(
                decode_unicode=True
            ):

                if not line:
                    continue

                g = json.loads(line)

                players = g.get(
                    "players",
                    {}
                )

                white = (
                    players
                    .get("white", {})
                    .get("user", {})
                    .get("name")
                )

                black = (
                    players
                    .get("black", {})
                    .get("user", {})
                    .get("name")
                )

                end_time = None

                if "lastMoveAt" in g:

                    end_time = (
                        datetime.fromtimestamp(
                            g["lastMoveAt"] / 1000,
                            tz=timezone.utc,
                        )
                    )

                games.append(
                    {
                        "source": "lichess",
                        "username": username,

                        "game_id":
                            g.get("id"),

                        "white":
                            white,

                        "black":
                            black,

                        "result":
                            g.get("status"),

                        "time_control":
                            (
                                g.get("clock")
                                or {}
                            ).get("initial"),

                        "time_class":
                            g.get("speed"),

                        "rated":
                            g.get("rated"),

                        "rules":
                            g.get("variant"),

                        "pgn":
                            None,

                        "end_time":
                            end_time,

                        "raw_json":
                            g,
                    }
                )

    except Exception as e:

        print(
            f"Lichess error: {e}"
        )

        traceback.print_exc()

    print(
        f"Total Lichess games: "
        f"{len(games)}"
    )

    return games


# ===========================================================================
# DATABASE SUMMARY
# ===========================================================================

def print_database_summary(conn):

    print("\n" + "=" * 70)
    print("DATABASE SUMMARY")
    print("=" * 70)

    with conn.cursor() as cur:

        cur.execute(
            "SELECT COUNT(*) FROM chess_games;"
        )

        game_count = cur.fetchone()[0]

        cur.execute(
            "SELECT COUNT(*) FROM chess_moves;"
        )

        move_count = cur.fetchone()[0]

        cur.execute("""
            SELECT source, COUNT(*)
            FROM chess_games
            GROUP BY source
            ORDER BY source;
        """)

        game_sources = cur.fetchall()

        cur.execute("""
            SELECT source, COUNT(*)
            FROM chess_moves
            GROUP BY source
            ORDER BY source;
        """)

        move_sources = cur.fetchall()

    print(
        f"Total games : {game_count:,}"
    )

    print(
        f"Total moves : {move_count:,}"
    )

    print("\nGames by source:")

    for source, count in game_sources:

        print(
            f"  {source}: {count:,}"
        )

    print("\nMoves by source:")

    for source, count in move_sources:

        print(
            f"  {source}: {count:,}"
        )

    print("=" * 70)


# ===========================================================================
# MAIN
# ===========================================================================

def main():

    conn = None

    try:

        # ---------------------------------------------------------------
        # Database
        # ---------------------------------------------------------------

        conn = get_connection()

        create_tables(conn)

        # ---------------------------------------------------------------
        # Chess.com
        # ---------------------------------------------------------------

        chesscom_games = harvest_chesscom(
            CHESS_COM_USER
        )

        insert_games(
            conn,
            chesscom_games
        )

        # Flatten PGNs into chess_moves.
        #
        # This happens after the game records are inserted.
        #

        flatten_and_insert_moves(
            conn,
            chesscom_games
        )

        # ---------------------------------------------------------------
        # Lichess
        # ---------------------------------------------------------------

        lichess_games = harvest_lichess(
            LICHESS_USER
        )

        insert_games(
            conn,
            lichess_games
        )

        # ---------------------------------------------------------------
        # Summary
        # ---------------------------------------------------------------

        print_database_summary(
            conn
        )

        print(
            "\n✅ Harvest finished successfully"
        )

    except Exception as e:

        print(
            "\n💥 SCRIPT FAILED"
        )

        print(
            f"Final error: {e}"
        )

        traceback.print_exc()

        # Important:
        # This makes GitHub Actions report FAILURE.
        sys.exit(1)

    finally:

        if conn:

            conn.close()

            print(
                "Database connection closed"
            )


# ===========================================================================
# ENTRY POINT
# ===========================================================================

if __name__ == "__main__":
    main()
