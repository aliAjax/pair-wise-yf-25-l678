"""轨道（专题）权限与身份策略。

本模块只负责"谁能操作哪一轨"这类判断，以及 tracks / track_chairs /
track_reviewers 三张表的结构与种子数据。分配事务在 ``assignments.py``，
主席控制台页面在 ``web/chair.html``，三者分开维护。

身份约定：
- users.role = 'chair' 且未出现在 track_chairs 中 → 总主席，可跨轨；
- users.role = 'chair' 且出现在 track_chairs 中 → 专题主席，只能管本轨；
- 评审人通过 track_reviewers 加入轨道评审团，投标与邀请都必须落在同轨。
"""
from __future__ import annotations

import sqlite3

from domain import BusinessError


SCHEMA = """
CREATE TABLE IF NOT EXISTS tracks (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS track_chairs (
    track_id TEXT NOT NULL REFERENCES tracks(id),
    user_id TEXT NOT NULL REFERENCES users(id),
    PRIMARY KEY (track_id, user_id)
);
CREATE TABLE IF NOT EXISTS track_reviewers (
    track_id TEXT NOT NULL REFERENCES tracks(id),
    reviewer_id TEXT NOT NULL REFERENCES users(id),
    PRIMARY KEY (track_id, reviewer_id)
);
"""

SEED_TRACKS = [
    ("db", "数据库与数据管理"),
    ("sys", "系统与网络"),
]
SEED_TRACK_CHAIRS = [
    ("db", "chair_db"),
    ("sys", "chair_sys"),
]
SEED_TRACK_REVIEWERS = [
    ("db", "r1"),
    ("db", "r2"),
    ("db", "r3"),
    ("sys", "r2"),
    ("sys", "r3"),
]
SEED_USERS = [
    ("chair_db", "数据库专题主席", "chair", 0),
    ("chair_sys", "系统专题主席", "chair", 0),
]


def seed(conn: sqlite3.Connection, now: str) -> None:
    conn.executemany(
        "INSERT OR IGNORE INTO tracks(id,name,created_at) VALUES(?,?,?)",
        [(tid, name, now) for tid, name in SEED_TRACKS],
    )
    conn.executemany(
        "INSERT OR IGNORE INTO users(id,name,role,load_limit) VALUES(?,?,?,?)",
        SEED_USERS,
    )
    conn.executemany(
        "INSERT OR IGNORE INTO track_chairs(track_id,user_id) VALUES(?,?)",
        SEED_TRACK_CHAIRS,
    )
    conn.executemany(
        "INSERT OR IGNORE INTO track_reviewers(track_id,reviewer_id) VALUES(?,?)",
        SEED_TRACK_REVIEWERS,
    )


# ---------------------------------------------------------------- 身份判断

def chair_track(conn: sqlite3.Connection, user_id: str) -> str | None:
    """专题主席返回其轨道 id；总主席（无绑定轨道）返回 None；非主席抛 403。"""
    row = conn.execute("SELECT track_id FROM track_chairs WHERE user_id=?", (user_id,)).fetchone()
    return row["track_id"] if row else None


def is_general_chair(conn: sqlite3.Connection, user: sqlite3.Row) -> bool:
    if user["role"] != "chair":
        return False
    return conn.execute("SELECT 1 FROM track_chairs WHERE user_id=?", (user["id"],)).fetchone() is None


def require_chair_scope(conn: sqlite3.Connection, user: sqlite3.Row, track_id: str) -> None:
    """要求用户是主席且能管辖 track_id；专题主席越轨返回 403。"""
    if user["role"] != "chair":
        raise BusinessError("该操作仅允许 chair 角色", 403, "forbidden")
    bound = chair_track(conn, user["id"])
    if bound is not None and bound != track_id:
        raise BusinessError("专题主席只能处理本轨论文", 403, "track_scope_violation")


def is_reviewer_in_track(conn: sqlite3.Connection, reviewer_id: str, track_id: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM track_reviewers WHERE reviewer_id=? AND track_id=?",
        (reviewer_id, track_id),
    ).fetchone() is not None


def require_reviewer_in_track(conn: sqlite3.Connection, reviewer_id: str, track_id: str) -> None:
    if not is_reviewer_in_track(conn, reviewer_id, track_id):
        raise BusinessError("评审人不属于该轨道评审团，意向与邀请必须同轨", 409, "reviewer_not_in_track")


def require_track_exists(conn: sqlite3.Connection, track_id: str) -> None:
    if not conn.execute("SELECT 1 FROM tracks WHERE id=?", (track_id,)).fetchone():
        raise BusinessError("轨道不存在", 422, "unknown_track")


def list_tracks(conn: sqlite3.Connection) -> list[dict]:
    tracks = [dict(r) for r in conn.execute("SELECT id,name FROM tracks ORDER BY id")]
    chairs = {
        r["track_id"]: r["user_id"]
        for r in conn.execute("SELECT track_id,user_id FROM track_chairs")
    }
    for t in tracks:
        t["chair_id"] = chairs.get(t["id"])
        t["reviewer_ids"] = [
            r["reviewer_id"]
            for r in conn.execute(
                "SELECT reviewer_id FROM track_reviewers WHERE track_id=? ORDER BY reviewer_id",
                (t["id"],),
            )
        ]
    return tracks
