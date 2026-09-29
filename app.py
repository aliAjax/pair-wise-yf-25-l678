"""学术会议同行评审系统：标准库 + SQLite 的可运行示例。

代码按关注点分文件维护：
- domain.py：异常与时间等共享内核；
- track_perms.py：轨道权限（专题主席/总主席边界、评审人同轨要求）；
- assignments.py：分配事务（邀请、应答、评审提交、改轨）；
- app.py：schema、种子数据、投稿/意向/冲突/反驳/决定与 HTTP 路由；
- web/chair.html：主席工作台页面（与作者演示页分开维护）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from assignments import AssignmentsMixin
from domain import BusinessError, utcnow

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DB = BASE_DIR / "review.db"
VALID_DECISIONS = {"accept", "reject", "minor_revision", "major_revision"}

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS tracks (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('author','reviewer','chair')),
    load_limit INTEGER NOT NULL DEFAULT 3 CHECK (load_limit >= 0),
    -- 总主席为 NULL；专题主席绑定且仅能管辖一条轨道。
    track_id TEXT REFERENCES tracks(id)
);
CREATE TABLE IF NOT EXISTS track_reviewers (
    track_id TEXT NOT NULL REFERENCES tracks(id),
    reviewer_id TEXT NOT NULL REFERENCES users(id),
    PRIMARY KEY (track_id, reviewer_id)
);
CREATE TABLE IF NOT EXISTS papers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    author_id TEXT NOT NULL REFERENCES users(id),
    track_id TEXT NOT NULL REFERENCES tracks(id),
    title TEXT NOT NULL,
    abstract TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'submitted'
        CHECK (status IN ('submitted','under_review','decided','withdrawn')),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS paper_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    paper_id INTEGER NOT NULL REFERENCES papers(id),
    version INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (paper_id, version)
);
CREATE TABLE IF NOT EXISTS conflicts (
    reviewer_id TEXT NOT NULL REFERENCES users(id),
    paper_id INTEGER NOT NULL REFERENCES papers(id),
    reason TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL,
    PRIMARY KEY (reviewer_id, paper_id)
);
CREATE TABLE IF NOT EXISTS bids (
    reviewer_id TEXT NOT NULL REFERENCES users(id),
    paper_id INTEGER NOT NULL REFERENCES papers(id),
    -- 意向落轨快照：改轨后旧轨意向不再授权查看新轨论文。
    track_id TEXT NOT NULL REFERENCES tracks(id),
    interest TEXT NOT NULL CHECK (interest IN ('want','maybe','decline')),
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    PRIMARY KEY (reviewer_id, paper_id)
);
CREATE TABLE IF NOT EXISTS assignments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    paper_id INTEGER NOT NULL REFERENCES papers(id),
    reviewer_id TEXT NOT NULL REFERENCES users(id),
    -- 分配落轨快照；与论文当前轨道不同即为改轨留档记录。
    track_id TEXT NOT NULL REFERENCES tracks(id),
    status TEXT NOT NULL DEFAULT 'invited'
        CHECK (status IN ('invited','accepted','declined','completed','withdrawn')),
    -- 改轨后旧轨记录 archived=1：数据保留供审计，但不计入新轨决定。
    archived INTEGER NOT NULL DEFAULT 0 CHECK (archived IN (0,1)),
    score INTEGER CHECK (score IS NULL OR score BETWEEN 1 AND 5),
    review_text TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
-- 同一评审人对一篇论文在当前轨道只能有一条"有效"分配；
-- 撤回/留档的旧记录不阻塞在新轨重新邀请。（索引在迁移补列之后创建，见 init_schema）
CREATE TABLE IF NOT EXISTS rebuttals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    paper_id INTEGER NOT NULL UNIQUE REFERENCES papers(id),
    author_id TEXT NOT NULL REFERENCES users(id),
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    paper_id INTEGER NOT NULL UNIQUE REFERENCES papers(id),
    decision TEXT NOT NULL CHECK (decision IN ('accept','reject','minor_revision','major_revision')),
    note TEXT NOT NULL DEFAULT '',
    decided_by TEXT NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    paper_id INTEGER,
    actor_id TEXT NOT NULL,
    action TEXT NOT NULL,
    detail TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (paper_id) REFERENCES papers(id)
);
"""


class ReviewStore(AssignmentsMixin):
    """领域逻辑。每个公开方法使用独立连接，避免 HTTP 线程共享 SQLite 连接。"""

    def __init__(self, db_path: str | Path = DEFAULT_DB):
        self.db_path = str(db_path)
        self._schema_lock = threading.Lock()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 10000")
        return conn

    def init_schema(self) -> None:
        with self._schema_lock, self.connect() as conn:
            # 先迁移旧库（可能重建 users），再执行完整建表脚本，避免新表外键悬挂到被重命名的旧表。
            self._migrate(conn)
            conn.executescript(SCHEMA_SQL)
            # 依赖 archived 列：必须在旧库迁移补列之后才能创建。
            conn.execute(
                """CREATE UNIQUE INDEX IF NOT EXISTS idx_assignments_active
                   ON assignments(paper_id, reviewer_id) WHERE archived=0 AND status!='withdrawn'"""
            )

    @staticmethod
    def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
        return any(row[1] == column for row in conn.execute(f"PRAGMA table_info({table})"))

    def _table_exists(self, conn: sqlite3.Connection, table: str) -> bool:
        return conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone() is not None

    def _migrate(self, conn: sqlite3.Connection) -> None:
        """把早于分轨功能的旧库升级到当前结构（演示库可删库重建，这里尽力兼容）。"""
        # 全新库（关键表尚不存在）交给完整 SCHEMA_SQL 处理，不算迁移。
        if not self._table_exists(conn, "papers") or not self._table_exists(conn, "users"):
            return
        papers_is_legacy = self._has_column(conn, "papers", "track_id") is False
        users_is_legacy = self._has_column(conn, "users", "track_id") is False
        if not papers_is_legacy and not users_is_legacy:
            return  # 当前结构，无需迁移兜底（避免新库凭空多出综合轨道）。
        # 旧库先把轨道表建出来（完整 SCHEMA_SQL 稍后以 IF NOT EXISTS 幂等再跑一次）。
        conn.execute(
            "CREATE TABLE IF NOT EXISTS tracks (id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT OR IGNORE INTO tracks(id,name,created_at) VALUES('default','综合轨道',?)",
            (utcnow(),),
        )
        # users.role 旧 CHECK 只有三种角色且无 track_id，需要重建表才能放开结构。
        if not self._has_column(conn, "users", "track_id"):
            conn.executescript(
                """
                ALTER TABLE users RENAME TO users_legacy;
                CREATE TABLE users (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    role TEXT NOT NULL CHECK (role IN ('author','reviewer','chair')),
                    load_limit INTEGER NOT NULL DEFAULT 3 CHECK (load_limit >= 0),
                    track_id TEXT REFERENCES tracks(id)
                );
                INSERT INTO users(id,name,role,load_limit,track_id)
                    SELECT id,name,role,load_limit,NULL FROM users_legacy;
                DROP TABLE users_legacy;
                """
            )
        if not self._has_column(conn, "papers", "track_id"):
            conn.execute("ALTER TABLE papers ADD COLUMN track_id TEXT REFERENCES tracks(id)")
        conn.execute("UPDATE papers SET track_id='default' WHERE track_id IS NULL")
        if not self._has_column(conn, "assignments", "track_id"):
            conn.executescript(
                """
                ALTER TABLE assignments RENAME TO assignments_legacy;
                CREATE TABLE assignments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    paper_id INTEGER NOT NULL REFERENCES papers(id),
                    reviewer_id TEXT NOT NULL REFERENCES users(id),
                    track_id TEXT NOT NULL REFERENCES tracks(id),
                    status TEXT NOT NULL DEFAULT 'invited'
                        CHECK (status IN ('invited','accepted','declined','completed','withdrawn')),
                    archived INTEGER NOT NULL DEFAULT 0 CHECK (archived IN (0,1)),
                    score INTEGER CHECK (score IS NULL OR score BETWEEN 1 AND 5),
                    review_text TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                INSERT INTO assignments(id,paper_id,reviewer_id,track_id,status,archived,score,review_text,created_at,updated_at)
                    SELECT a.id,a.paper_id,a.reviewer_id,p.track_id,a.status,0,a.score,a.review_text,a.created_at,a.updated_at
                    FROM assignments_legacy a JOIN papers p ON p.id=a.paper_id;
                DROP TABLE assignments_legacy;
                """
            )
        if not self._has_column(conn, "bids", "track_id"):
            conn.execute("ALTER TABLE bids ADD COLUMN track_id TEXT REFERENCES tracks(id)")
            conn.execute("UPDATE bids SET track_id=(SELECT track_id FROM papers WHERE papers.id=bids.paper_id)")

    def seed(self) -> None:
        self.init_schema()
        tracks = [
            ("distributed", "分布式系统"),
            ("ai", "人工智能"),
        ]
        users = [
            ("alice", "Alice 作者", "author", 0, None),
            ("bob", "Bob 作者", "author", 0, None),
            ("r1", "评审人一号", "reviewer", 3, None),
            ("r2", "评审人二号", "reviewer", 3, None),
            ("r3", "评审人三号", "reviewer", 2, None),
            ("chair", "程序委员会总主席", "chair", 0, None),
            ("chair_ds", "分布式轨主席", "chair", 0, "distributed"),
            ("chair_ai", "人工智能轨主席", "chair", 0, "ai"),
        ]
        with self.connect() as conn:
            conn.executemany("INSERT OR IGNORE INTO tracks(id,name,created_at) VALUES(?,?,?)",
                             [("distributed", "分布式系统", utcnow()), ("ai", "人工智能", utcnow())])
            conn.executemany(
                "INSERT OR IGNORE INTO users(id,name,role,load_limit,track_id) VALUES(?,?,?,?,?)",
                users,
            )
            memberships = [
                ("distributed", "r1"),
                ("distributed", "r2"),
                ("ai", "r2"),
                ("ai", "r3"),
            ]
            conn.executemany(
                "INSERT OR IGNORE INTO track_reviewers(track_id,reviewer_id) VALUES(?,?)",
                memberships,
            )

    def _user(self, conn: sqlite3.Connection, user_id: str | None) -> sqlite3.Row:
        if not user_id:
            raise BusinessError("缺少 X-User-Id 请求头", 401, "authentication_required")
        row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if not row:
            raise BusinessError("用户不存在", 401, "unknown_user")
        return row

    @staticmethod
    def _require(row: sqlite3.Row, role: str) -> None:
        if row["role"] != role:
            raise BusinessError(f"该操作仅允许 {role} 角色", 403, "forbidden")

    def _audit(self, conn: sqlite3.Connection, paper_id: int | None, actor: str, action: str, detail: dict) -> None:
        conn.execute(
            "INSERT INTO audit_log(paper_id,actor_id,action,detail,created_at) VALUES(?,?,?,?,?)",
            (paper_id, actor, action, json.dumps(detail, ensure_ascii=False, sort_keys=True), utcnow()),
        )

    # ---- 投稿 ----------------------------------------------------------------

    def submit_paper(self, user_id: str, title: str, abstract: str, track_id: int) -> dict:
        title, abstract = title.strip(), abstract.strip()
        if len(title) < 3 or len(abstract) < 20:
            raise BusinessError("标题至少 3 字，摘要至少 20 字", 422, "invalid_paper")
        digest = hashlib.sha256(f"{title}\n{abstract}".encode()).hexdigest()
        with self.connect() as conn:
            user = self._user(conn, user_id)
            self._require(user, "author")
            track = conn.execute("SELECT id FROM tracks WHERE id=?", (track_id,)).fetchone()
            if not track:
                raise BusinessError("投稿轨道不存在", 422, "invalid_track")
            cur = conn.execute(
                "INSERT INTO papers(author_id,track_id,title,abstract,created_at) VALUES(?,?,?,?,?)",
                (user_id, track_id, title, abstract, utcnow()),
            )
            paper_id = cur.lastrowid
            conn.execute(
                "INSERT INTO paper_versions(paper_id,version,content_hash,created_at) VALUES(?,?,?,?)",
                (paper_id, 1, digest, utcnow()),
            )
            self._audit(conn, paper_id, user_id, "paper.submit", {
                "version": 1,
                "sha256": digest,
                "track_id": track_id,
            })
            return {"id": paper_id, "track_id": track_id, "status": "submitted", "version": 1, "sha256": digest}

    def _paper_view(self, conn: sqlite3.Connection, paper: sqlite3.Row, viewer: sqlite3.Row) -> dict:
        data = {
            "id": paper["id"],
            "title": paper["title"],
            "abstract": paper["abstract"],
            "track_id": paper["track_id"],
            "status": paper["status"],
            "created_at": paper["created_at"],
        }
        if viewer["role"] == "chair" or viewer["id"] == paper["author_id"]:
            data["author_id"] = paper["author_id"]
        else:
            data["author_id"] = None  # 双盲：评审人看不到作者身份。
        return data

    def list_papers(self, user_id: str) -> list[dict]:
        with self.connect() as conn:
            user = self._user(conn, user_id)
            if user["role"] == "chair":
                where, params = self._track_chair_view_filter(user)
                rows = conn.execute(f"SELECT * FROM papers{where} ORDER BY id", params).fetchall()
            elif user["role"] == "author":
                rows = conn.execute(
                    "SELECT * FROM papers WHERE author_id=? ORDER BY id", (user_id,)
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT DISTINCT p.* FROM papers p
                       LEFT JOIN assignments a ON a.paper_id=p.id AND a.reviewer_id=?
                       LEFT JOIN bids b ON b.paper_id=p.id AND b.reviewer_id=?
                       WHERE (a.id IS NOT NULL AND a.archived=0 AND a.status!='withdrawn'
                              AND a.track_id=p.track_id)
                          OR (b.paper_id IS NOT NULL AND b.track_id=p.track_id)
                       ORDER BY p.id""",
                    (user_id, user_id),
                ).fetchall()
            return [self._paper_view(conn, row, user) for row in rows]

    def get_paper(self, user_id: str, paper_id: int) -> dict:
        with self.connect() as conn:
            user = self._user(conn, user_id)
            paper = conn.execute("SELECT * FROM papers WHERE id=?", (paper_id,)).fetchone()
            if not paper:
                raise BusinessError("论文不存在", 404, "not_found")
            if user["role"] == "reviewer":
                allowed = conn.execute(
                    """SELECT 1 FROM assignments
                       WHERE paper_id=? AND reviewer_id=? AND archived=0 AND status!='withdrawn' AND track_id=?
                       UNION
                       SELECT 1 FROM bids WHERE paper_id=? AND reviewer_id=? AND track_id=?
                       LIMIT 1""",
                    (paper_id, user_id, paper["track_id"],
                     paper_id, user_id, paper["track_id"]),
                ).fetchone()
                if not allowed:
                    raise BusinessError("评审人未获授权查看该论文（意向与邀请须同轨有效）", 403, "forbidden")
            elif user["role"] == "author" and paper["author_id"] != user_id:
                raise BusinessError("作者只能查看自己的论文", 403, "forbidden")
            elif user["role"] == "chair":
                self._ensure_track_access(conn, user, paper["track_id"])
            return self._paper_view(conn, paper, user)

    # ---- 利益冲突（由主席在论文所在轨登记） ------------------------------------

    def add_conflict(self, chair_id: str, paper_id: int, reviewer_id: str, reason: str) -> dict:
        if not reason.strip():
            raise BusinessError("利益冲突原因不能为空", 422, "invalid_reason")
        with self.connect() as conn:
            chair = self._chair_scope(conn, chair_id)
            paper = conn.execute("SELECT track_id FROM papers WHERE id=?", (paper_id,)).fetchone()
            if not paper:
                raise BusinessError("论文不存在", 404, "not_found")
            self._ensure_track_access(conn, chair, paper["track_id"])
            reviewer = self._user(conn, reviewer_id)
            self._require(reviewer, "reviewer")
            try:
                conn.execute(
                    "INSERT INTO conflicts(reviewer_id,paper_id,reason,created_by,created_at) VALUES(?,?,?,?,?)",
                    (reviewer_id, paper_id, reason.strip(), chair_id, utcnow()),
                )
            except sqlite3.IntegrityError:
                raise BusinessError("利益冲突已登记", 409, "conflict_exists")
            self._audit(conn, paper_id, chair_id, "conflict.add", {
                "reviewer_id": reviewer_id,
                "reason": reason.strip(),
                "track_id": paper["track_id"],
            })
            return {"paper_id": paper_id, "reviewer_id": reviewer_id, "reason": reason.strip()}

    # ---- 评审意向（必须同轨） -------------------------------------------------

    def bid(self, reviewer_id: str, paper_id: int, interest: str, note: str = "") -> dict:
        if interest not in {"want", "maybe", "decline"}:
            raise BusinessError("意向必须为 want、maybe 或 decline", 422, "invalid_interest")
        with self.connect() as conn:
            reviewer = self._user(conn, reviewer_id)
            self._require(reviewer, "reviewer")
            paper = conn.execute("SELECT * FROM papers WHERE id=?", (paper_id,)).fetchone()
            if not paper or paper["status"] not in {"submitted", "under_review"}:
                raise BusinessError("论文不存在或当前不可表达意向", 409, "paper_unavailable")
            # 评审意向落在论文当前轨：非该轨成员不能投标。
            self._ensure_reviewer_in_track(conn, reviewer_id, paper["track_id"])
            if conn.execute(
                "SELECT 1 FROM conflicts WHERE reviewer_id=? AND paper_id=?",
                (reviewer_id, paper_id),
            ).fetchone():
                raise BusinessError("存在利益冲突，不能表达评审意向", 409, "conflict_of_interest")
            conn.execute(
                """INSERT INTO bids(reviewer_id,paper_id,track_id,interest,note,created_at) VALUES(?,?,?,?,?,?)
                   ON CONFLICT(reviewer_id,paper_id) DO UPDATE SET
                       track_id=excluded.track_id,interest=excluded.interest,note=excluded.note,created_at=excluded.created_at""",
                (reviewer_id, paper_id, paper["track_id"], interest, note.strip(), utcnow()),
            )
            self._audit(conn, paper_id, reviewer_id, "bid.set", {
                "interest": interest,
                "note": note.strip(),
                "track_id": paper["track_id"],
            })
            return {"paper_id": paper_id, "reviewer_id": reviewer_id, "track_id": paper["track_id"], "interest": interest}

    # ---- Rebuttal（只看本轨有效完成的评审） ------------------------------------

    def submit_rebuttal(self, author_id: str, paper_id: int, content: str) -> dict:
        if len(content.strip()) < 10:
            raise BusinessError("Rebuttal 至少 10 字", 422, "rebuttal_too_short")
        with self.connect() as conn:
            author = self._user(conn, author_id)
            self._require(author, "author")
            paper = conn.execute("SELECT * FROM papers WHERE id=?", (paper_id,)).fetchone()
            if not paper or paper["author_id"] != author_id:
                raise BusinessError("论文不存在或不属于当前作者", 404, "not_found")
            completed = conn.execute(
                "SELECT COUNT(*) FROM assignments WHERE paper_id=? AND status='completed' AND archived=0",
                (paper_id,),
            ).fetchone()[0]
            if completed < 1:
                raise BusinessError("至少收到一份本轨完整评审后才能提交 Rebuttal", 409, "reviews_not_ready")
            try:
                cur = conn.execute(
                    "INSERT INTO rebuttals(paper_id,author_id,content,created_at) VALUES(?,?,?,?)",
                    (paper_id, author_id, content.strip(), utcnow()),
                )
            except sqlite3.IntegrityError:
                raise BusinessError("每篇论文只能提交一次 Rebuttal", 409, "rebuttal_exists")
            self._audit(conn, paper_id, author_id, "rebuttal.submit", {"rebuttal_id": cur.lastrowid})
            return {"id": cur.lastrowid, "paper_id": paper_id, "content": content.strip()}

    # ---- 决定（必须两份本轨有效意见） ------------------------------------------

    def decide(self, chair_id: str, paper_id: int, decision: str, note: str = "") -> dict:
        if decision not in VALID_DECISIONS:
            raise BusinessError("决定值不合法", 422, "invalid_decision")
        with self.connect() as conn:
            chair = self._chair_scope(conn, chair_id)
            try:
                conn.execute("BEGIN IMMEDIATE")
                paper = conn.execute("SELECT * FROM papers WHERE id=?", (paper_id,)).fetchone()
                if not paper or paper["status"] not in {"submitted", "under_review"}:
                    raise BusinessError("论文不存在或已经决定", 409, "paper_decided")
                self._ensure_track_access(conn, chair, paper["track_id"])
                completed = conn.execute(
                    """SELECT COUNT(*) FROM assignments
                       WHERE paper_id=? AND status='completed' AND archived=0 AND track_id=?""",
                    (paper_id, paper["track_id"]),
                ).fetchone()[0]
                if completed < 2:
                    raise BusinessError(
                        "至少需要两份本轨有效（非留档）已完成评审才能作出决定",
                        409,
                        "insufficient_reviews",
                    )
                cur = conn.execute(
                    "INSERT INTO decisions(paper_id,decision,note,decided_by,created_at) VALUES(?,?,?,?,?)",
                    (paper_id, decision, note.strip(), chair_id, utcnow()),
                )
                conn.execute("UPDATE papers SET status='decided' WHERE id=?", (paper_id,))
                self._audit(conn, paper_id, chair_id, "decision.record", {
                    "decision": decision,
                    "note": note.strip(),
                    "track_id": paper["track_id"],
                })
                return {"id": cur.lastrowid, "paper_id": paper_id, "decision": decision, "note": note.strip()}
            except Exception:
                conn.rollback()
                raise

    def history(self, user_id: str, paper_id: int) -> list[dict]:
        self.get_paper(user_id, paper_id)  # 权限检查（含专题主席轨道边界）。
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM audit_log WHERE paper_id=? ORDER BY id", (paper_id,)).fetchall()
            return [dict(row) | {"detail": json.loads(row["detail"])} for row in rows]


class ReviewHandler(BaseHTTPRequestHandler):
    server_version = "AcademicReview/1.0"

    def _store(self) -> ReviewStore:
        return self.server.store  # type: ignore[attr-defined]

    def _send(self, status: int, payload) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_page(self, name: str) -> None:
        html = (BASE_DIR / "web" / name).read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(html)))
        self.end_headers()
        self.wfile.write(html)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length == 0:
            return {}
        try:
            return json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise BusinessError("请求体必须是合法 JSON", 400, "invalid_json")

    def _user_id(self) -> str:
        return self.headers.get("X-User-Id", "")

    def _dispatch(self, method: str) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        if method == "GET" and path == "/":
            return self._serve_page("index.html")
        if method == "GET" and path == "/chair":
            return self._serve_page("chair.html")
        if method == "GET" and path == "/health":
            return self._send(200, {"ok": True})
        store = self._store()
        parts = [p for p in path.split("/") if p]
        if not parts or parts[0] != "api":
            raise BusinessError("接口不存在", 404, "not_found")
        if parts == ["api", "tracks"] and method == "GET":
            return self._send(200, {"items": store.list_tracks(self._user_id())})
        if parts == ["api", "papers"] and method == "GET":
            return self._send(200, {"items": store.list_papers(self._user_id())})
        if parts == ["api", "papers"] and method == "POST":
            data = self._body()
            return self._send(201, store.submit_paper(
                self._user_id(), data.get("title", ""), data.get("abstract", ""), data.get("track_id")
            ))
        if len(parts) >= 3 and parts[:2] == ["api", "papers"]:
            paper_id = int(parts[2])
            if len(parts) == 3 and method == "GET":
                return self._send(200, store.get_paper(self._user_id(), paper_id))
            if len(parts) == 4 and parts[3] == "bids" and method == "POST":
                data = self._body()
                return self._send(201, store.bid(
                    self._user_id(), paper_id, data.get("interest", ""), data.get("note", "")
                ))
            if len(parts) == 4 and parts[3] == "conflicts" and method == "POST":
                data = self._body()
                return self._send(201, store.add_conflict(
                    self._user_id(), paper_id, data.get("reviewer_id", ""), data.get("reason", "")
                ))
            if len(parts) == 4 and parts[3] == "assignments" and method == "POST":
                data = self._body()
                return self._send(201, store.assign(
                    self._user_id(), paper_id, data.get("reviewer_id", "")
                ))
            if len(parts) == 4 and parts[3] == "move" and method == "POST":
                data = self._body()
                target = data.get("target_track_id", data.get("track_id"))
                return self._send(200, store.move_paper(
                    self._user_id(), paper_id, target, data.get("reason", "")
                ))
            if len(parts) == 4 and parts[3] == "rebuttal" and method == "POST":
                data = self._body()
                return self._send(201, store.submit_rebuttal(
                    self._user_id(), paper_id, data.get("content", "")
                ))
            if len(parts) == 4 and parts[3] == "decision" and method == "POST":
                data = self._body()
                return self._send(201, store.decide(
                    self._user_id(), paper_id, data.get("decision", ""), data.get("note", "")
                ))
            if len(parts) == 4 and parts[3] == "history" and method == "GET":
                return self._send(200, {"items": store.history(self._user_id(), paper_id)})
        if len(parts) == 4 and parts[:2] == ["api", "assignments"] and method == "POST":
            assignment_id = int(parts[2])
            data = self._body()
            if parts[3] == "respond":
                return self._send(200, store.respond_assignment(
                    self._user_id(), assignment_id, bool(data.get("accepted"))
                ))
            if parts[3] == "review":
                return self._send(201, store.submit_review(
                    self._user_id(), assignment_id, data.get("score"), data.get("text", "")
                ))
        raise BusinessError("接口不存在", 404, "not_found")

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_DELETE(self):
        self._handle("DELETE")

    def _handle(self, method: str) -> None:
        try:
            self._dispatch(method)
        except BusinessError as exc:
            self._send(exc.status, {"error": {"code": exc.code, "message": exc.message}})
        except (ValueError, TypeError):
            self._send(400, {"error": {"code": "invalid_path", "message": "路径参数或请求体格式错误"}})
        except Exception as exc:
            self._send(500, {"error": {"code": "internal_error", "message": str(exc)}})

    def log_message(self, fmt, *args):
        print(f"{self.address_string()} - {fmt % args}")


class ReviewServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, store: ReviewStore):
        self.store = store
        super().__init__(address, ReviewHandler)


def parse_args():
    parser = argparse.ArgumentParser(description="学术会议同行评审系统")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--port", type=int, default=8101)
    parser.add_argument("--init", action="store_true", help="初始化数据库")
    parser.add_argument("--seed", action="store_true", help="写入演示数据")
    parser.add_argument("--no-init", action="store_true", help="启动时不自动初始化")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    store = ReviewStore(args.db)
    if args.init or args.seed or not args.no_init:
        store.init_schema()
    if args.seed:
        store.seed()
    if args.init or args.seed:
        print(f"数据库已初始化: {args.db}")
        return
    server = ReviewServer(("127.0.0.1", args.port), store)
    print(f"评审系统运行于 http://127.0.0.1:{args.port}（主席工作台 /chair）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
