import json
import os
import sqlite3
from datetime import datetime
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

load_dotenv()

DB_FILE = os.getenv("DB_FILE", "stargrader.db")


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with get_connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT DEFAULT '',
                first_name TEXT DEFAULT 'Игрок',
                photo_url TEXT DEFAULT '',
                balance REAL DEFAULT 0.0,
                inventory_json TEXT DEFAULT '[]',
                history_json TEXT DEFAULT '[]',
                games INTEGER DEFAULT 0,
                wins INTEGER DEFAULT 0,
                losses INTEGER DEFAULT 0,
                total_wagered REAL DEFAULT 0.0,
                total_won REAL DEFAULT 0.0,
                loss_streak INTEGER DEFAULT 0,
                daily_day_index INTEGER DEFAULT 0,
                last_daily_ts INTEGER DEFAULT 0,
                updated_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS withdrawals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                username TEXT DEFAULT '',
                first_name TEXT DEFAULT 'Игрок',
                amount REAL NOT NULL,
                fee REAL NOT NULL,
                receive_amount REAL NOT NULL,
                status TEXT DEFAULT 'pending',
                created_at TEXT
            )
            """
        )
        conn.commit()


def get_or_create_user(
    user_id: int,
    username: str = "",
    first_name: str = "Игрок",
    photo_url: str = "",
) -> Dict[str, Any]:
    clean_username = username.lstrip("@").strip()
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()

        if row is None:
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            conn.execute(
                """
                INSERT INTO users (
                    user_id, username, first_name, photo_url,
                    balance, inventory_json, history_json,
                    games, wins, losses, total_wagered, total_won,
                    loss_streak, daily_day_index, last_daily_ts, updated_at
                )
                VALUES (?, ?, ?, ?, 0.0, '[]', '[]', 0, 0, 0, 0.0, 0.0, 0, 0, 0, ?)
                """,
                (user_id, clean_username, first_name or "Игрок", photo_url, now_str),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM users WHERE user_id = ?", (user_id,)
            ).fetchone()
        else:
            new_username = clean_username if clean_username else row["username"]
            new_first_name = (
                first_name if first_name and first_name != "Игрок" else row["first_name"]
            )
            new_photo = photo_url if photo_url else row["photo_url"]
            if (
                new_username != row["username"]
                or new_first_name != row["first_name"]
                or new_photo != row["photo_url"]
            ):
                conn.execute(
                    """
                    UPDATE users
                    SET username = ?, first_name = ?, photo_url = ?
                    WHERE user_id = ?
                    """,
                    (new_username, new_first_name, new_photo, user_id),
                )
                conn.commit()
                row = conn.execute(
                    "SELECT * FROM users WHERE user_id = ?", (user_id,)
                ).fetchone()

        return _row_to_dict(row)


def find_user_by_query(query: str) -> Optional[Dict[str, Any]]:
    q = query.strip()
    if not q:
        return None
    with get_connection() as conn:
        if q.isdigit():
            row = conn.execute(
                "SELECT * FROM users WHERE user_id = ?", (int(q),)
            ).fetchone()
            return _row_to_dict(row) if row else None

        uname = q.lstrip("@").lower()
        row = conn.execute(
            "SELECT * FROM users WHERE LOWER(username) = ?", (uname,)
        ).fetchone()
        return _row_to_dict(row) if row else None


def admin_change_balance(query: str, delta: float) -> Optional[Dict[str, Any]]:
    user = find_user_by_query(query)
    if not user:
        return None
    new_balance = max(0.0, round(user["balance"] + delta, 2))
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_connection() as conn:
        conn.execute(
            "UPDATE users SET balance = ?, updated_at = ? WHERE user_id = ?",
            (new_balance, now_str, user["user_id"]),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM users WHERE user_id = ?", (user["user_id"],)
        ).fetchone()
        return _row_to_dict(row)


def admin_add_gift(query: str, gift_item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    user = find_user_by_query(query)
    if not user:
        return None
    inv = list(user["inventory"])
    inv.insert(0, gift_item)
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_connection() as conn:
        conn.execute(
            "UPDATE users SET inventory_json = ?, updated_at = ? WHERE user_id = ?",
            (json.dumps(inv, ensure_ascii=False), now_str, user["user_id"]),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM users WHERE user_id = ?", (user["user_id"],)
        ).fetchone()
        return _row_to_dict(row)


def save_user_state(user_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
    existing = get_or_create_user(
        user_id=user_id,
        username=str(payload.get("username", "")),
        first_name=str(payload.get("first_name", "Игрок")),
        photo_url=str(payload.get("photo_url", "")),
    )

    balance = max(0.0, float(payload.get("balance", existing["balance"])))
    inventory = payload.get("inventory", existing["inventory"])
    history = payload.get("history", existing["history"])[:30]
    stats = payload.get("stats", {})

    games = int(stats.get("games", existing["stats"]["games"]))
    wins = int(stats.get("wins", existing["stats"]["wins"]))
    losses = int(stats.get("losses", existing["stats"]["losses"]))
    total_wagered = float(stats.get("totalWagered", existing["stats"]["totalWagered"]))
    total_won = float(stats.get("totalWon", existing["stats"]["totalWon"]))

    loss_streak = max(0, min(7, int(payload.get("lossStreak", existing["lossStreak"]))))
    daily_day_index = max(
        0, min(6, int(payload.get("dailyDayIndex", existing["dailyDayIndex"])))
    )
    last_daily_ts = int(payload.get("lastDailyTs", existing["lastDailyTs"]))
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with get_connection() as conn:
        conn.execute(
            """
            UPDATE users SET
                balance = ?,
                inventory_json = ?,
                history_json = ?,
                games = ?,
                wins = ?,
                losses = ?,
                total_wagered = ?,
                total_won = ?,
                loss_streak = ?,
                daily_day_index = ?,
                last_daily_ts = ?,
                updated_at = ?
            WHERE user_id = ?
            """,
            (
                round(balance, 2),
                json.dumps(inventory, ensure_ascii=False),
                json.dumps(history, ensure_ascii=False),
                games,
                wins,
                losses,
                round(total_wagered, 2),
                round(total_won, 2),
                loss_streak,
                daily_day_index,
                last_daily_ts,
                now_str,
                user_id,
            ),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        return _row_to_dict(row)


def create_withdrawal(
    user_id: int,
    username: str,
    first_name: str,
    amount: float,
    fee: float,
    receive_amount: float,
) -> int:
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO withdrawals (
                user_id, username, first_name, amount, fee, receive_amount, status, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)
            """,
            (
                user_id,
                username.lstrip("@"),
                first_name or "Игрок",
                round(amount, 2),
                round(fee, 2),
                round(receive_amount, 2),
                now_str,
            ),
        )
        conn.commit()
        return int(cur.lastrowid)


def get_withdrawal(w_id: int) -> Optional[Dict[str, Any]]:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM withdrawals WHERE id = ?", (w_id,)
        ).fetchone()
        return dict(row) if row else None


def get_pending_withdrawals() -> List[Dict[str, Any]]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM withdrawals WHERE status = 'pending' ORDER BY id DESC LIMIT 20"
        ).fetchall()
        return [dict(r) for r in rows]


def set_withdrawal_status(w_id: int, status: str) -> Optional[Dict[str, Any]]:
    with get_connection() as conn:
        conn.execute(
            "UPDATE withdrawals SET status = ? WHERE id = ?", (status, w_id)
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM withdrawals WHERE id = ?", (w_id,)
        ).fetchone()
        return dict(row) if row else None


def get_global_stats() -> Dict[str, Any]:
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT
                COUNT(*) AS total_users,
                COALESCE(SUM(balance), 0) AS total_balance,
                COALESCE(SUM(games), 0) AS total_games,
                COALESCE(SUM(wins), 0) AS total_wins
            FROM users
            """
        ).fetchone()
        pending_w = conn.execute(
            "SELECT COUNT(*) AS cnt FROM withdrawals WHERE status = 'pending'"
        ).fetchone()["cnt"]
        return {
            "total_users": int(row["total_users"]),
            "total_balance": float(row["total_balance"]),
            "total_games": int(row["total_games"]),
            "total_wins": int(row["total_wins"]),
            "pending_withdrawals": int(pending_w),
        }


def _row_to_dict(row: Optional[sqlite3.Row]) -> Dict[str, Any]:
    if row is None:
        return {}
    try:
        inv = json.loads(row["inventory_json"] or "[]")
    except Exception:
        inv = []
    try:
        hist = json.loads(row["history_json"] or "[]")
    except Exception:
        hist = []

    return {
        "user_id": row["user_id"],
        "username": row["username"] or "",
        "first_name": row["first_name"] or "Игрок",
        "photo_url": row["photo_url"] or "",
        "balance": float(row["balance"] or 0.0),
        "inventory": inv,
        "history": hist,
        "stats": {
            "games": int(row["games"] or 0),
            "wins": int(row["wins"] or 0),
            "losses": int(row["losses"] or 0),
            "totalWagered": float(row["total_wagered"] or 0.0),
            "totalWon": float(row["total_won"] or 0.0),
        },
        "lossStreak": int(row["loss_streak"] or 0),
        "dailyDayIndex": int(row["daily_day_index"] or 0),
        "lastDailyTs": int(row["last_daily_ts"] or 0),
        "updated_at": row["updated_at"] or "",
    }
