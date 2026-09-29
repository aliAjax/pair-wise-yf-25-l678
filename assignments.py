"""分配事务模块：邀请、应答、评审提交，以及论文改轨。

与 track_perms.py 的边界：轨道成员/主席范围判定调用权限模块的辅助方法；
本模块只负责在事务中推进状态机。

改轨规则（paper.move_track）：
- 只有总主席可以改轨（专题主席本就绑定单轨，不存在"跨轨调整"）；
- 未接受的邀请（invited/declined）标记为 withdrawn，不计入新轨；
- 已接受/已完成的评审标记为 archived 留档（数据保留、审计可见），但不再计入新轨决定；
- 新轨决定必须由总主席/新轨主席重新补足两份"本轨有效意见"（archived=0 且 completed）。

改轨与评审提交互斥：两者都以 BEGIN IMMEDIATE 且 busy_timeout=0 抢写锁，
只有一边成功，另一边得到 SQLITE_BUSY，映射为 409 concurrent_modification。
"""
from __future__ import annotations

import sqlite3

from domain import BusinessError, utcnow
from track_perms import TrackPolicyMixin

# 评审提交与改轨互斥时，SQLite 返回的错误标识。
_BUSY_MESSAGES = ("database is locked", "database table is locked")


class AssignmentsMixin(TrackPolicyMixin):
    def _begin_nowait(self, conn: sqlite3.Connection) -> None:
        """开启一个不排队等待的写事务，抢锁失败即抛 409。"""
        conn.execute("PRAGMA busy_timeout = 0")
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            if str(exc) in _BUSY_MESSAGES:
                raise BusinessError("另一操作正在处理该论文，请稍后重试", 409, "concurrent_modification")
            raise

    # ---- 邀请 ----------------------------------------------------------------

    def assign(self, chair_id: str, paper_id: int, reviewer_id: str) -> dict:
        with self.connect() as conn:
            chair = self._chair_scope(conn, chair_id)
            # 角色/冲突/负载检查不需要持有写锁；拿到锁后再做唯一约束插入。
            paper = conn.execute("SELECT * FROM papers WHERE id=?", (paper_id,)).fetchone()
            if not paper or paper["status"] not in {"submitted", "under_review"}:
                raise BusinessError("论文不存在或不可分配", 409, "paper_unavailable")
            self._ensure_track_access(conn, chair, paper["track_id"])
            reviewer = self._user(conn, reviewer_id)
            self._require(reviewer, "reviewer")
            self._ensure_reviewer_in_track(conn, reviewer_id, paper["track_id"])
            if conn.execute(
                "SELECT 1 FROM conflicts WHERE reviewer_id=? AND paper_id=?",
                (reviewer_id, paper_id),
            ).fetchone():
                raise BusinessError("评审人与论文存在利益冲突", 409, "conflict_of_interest")
            try:
                conn.execute("BEGIN IMMEDIATE")
                # 锁内复查轨道：可能刚被改轨（与 move 的互斥保证锁内状态稳定）。
                locked = conn.execute("SELECT track_id,status FROM papers WHERE id=?", (paper_id,)).fetchone()
                if locked["track_id"] != paper["track_id"]:
                    raise BusinessError("论文刚刚改轨，请按新轨重新邀请", 409, "track_changed")
                load = conn.execute(
                    """SELECT COUNT(*) FROM assignments
                       WHERE reviewer_id=? AND status IN ('invited','accepted') AND archived=0""",
                    (reviewer_id,),
                ).fetchone()[0]
                if load >= reviewer["load_limit"]:
                    raise BusinessError("评审人已达到负载上限", 409, "reviewer_at_capacity")
                try:
                    cur = conn.execute(
                        """INSERT INTO assignments(paper_id,reviewer_id,track_id,created_at,updated_at)
                           VALUES(?,?,?,?,?)""",
                        (paper_id, reviewer_id, locked["track_id"], utcnow(), utcnow()),
                    )
                except sqlite3.IntegrityError:
                    raise BusinessError("该评审人对本轨论文已有未撤回的分配", 409, "assignment_exists")
                conn.execute("UPDATE papers SET status='under_review' WHERE id=?", (paper_id,))
                assignment_id = cur.lastrowid
                self._audit(conn, paper_id, chair_id, "assignment.invite", {
                    "assignment_id": assignment_id,
                    "reviewer_id": reviewer_id,
                    "track_id": locked["track_id"],
                })
                return {
                    "id": assignment_id,
                    "paper_id": paper_id,
                    "reviewer_id": reviewer_id,
                    "track_id": locked["track_id"],
                    "status": "invited",
                }
            except Exception:
                conn.rollback()
                raise

    # ---- 应答邀请 -------------------------------------------------------------

    def respond_assignment(self, reviewer_id: str, assignment_id: int, accepted: bool) -> dict:
        with self.connect() as conn:
            reviewer = self._user(conn, reviewer_id)
            self._require(reviewer, "reviewer")
            row = conn.execute("SELECT * FROM assignments WHERE id=?", (assignment_id,)).fetchone()
            if not row or row["reviewer_id"] != reviewer_id:
                raise BusinessError("分配不存在或不属于当前评审人", 404, "not_found")
            if row["status"] == "withdrawn":
                raise BusinessError("邀请已因论文改轨被撤回", 409, "invitation_withdrawn")
            if row["status"] != "invited":
                raise BusinessError("邀请已经处理", 409, "invitation_already_answered")
            status = "accepted" if accepted else "declined"
            # 条件更新：若改轨事务刚把它置为 withdrawn，rowcount=0，应答失败。
            cur = conn.execute(
                "UPDATE assignments SET status=?,updated_at=? WHERE id=? AND status='invited'",
                (status, utcnow(), assignment_id),
            )
            if cur.rowcount == 0:
                raise BusinessError("邀请已因论文改轨被撤回", 409, "invitation_withdrawn")
            self._audit(conn, row["paper_id"], reviewer_id, "assignment.respond", {
                "assignment_id": assignment_id,
                "status": status,
                "track_id": row["track_id"],
            })
            return {"id": assignment_id, "status": status}

    # ---- 提交评审 -------------------------------------------------------------

    def submit_review(self, reviewer_id: str, assignment_id: int, score: int, text: str) -> dict:
        if isinstance(score, bool) or not isinstance(score, int) or not 1 <= score <= 5:
            raise BusinessError("评分必须是 1 到 5 的整数", 422, "invalid_score")
        text = text.strip()
        if len(text) < 10:
            raise BusinessError("评审意见至少 10 字", 422, "review_too_short")
        with self.connect() as conn:
            reviewer = self._user(conn, reviewer_id)
            self._require(reviewer, "reviewer")
            # 先抢写锁再做任何读写：改轨进行中时评审提交直接 409（只让一边成功）。
            try:
                self._begin_nowait(conn)
                row = conn.execute("SELECT * FROM assignments WHERE id=?", (assignment_id,)).fetchone()
                if not row or row["reviewer_id"] != reviewer_id:
                    raise BusinessError("分配不存在或不属于当前评审人", 404, "not_found")
                paper = conn.execute("SELECT track_id FROM papers WHERE id=?", (row["paper_id"],)).fetchone()
                if row["archived"] or not paper or paper["track_id"] != row["track_id"]:
                    raise BusinessError(
                        "论文已离开本轨，旧评审只留档、不能再提交",
                        409,
                        "assignment_archived",
                    )
                if row["status"] == "withdrawn":
                    raise BusinessError("邀请已因论文改轨被撤回", 409, "invitation_withdrawn")
                if row["status"] != "accepted":
                    raise BusinessError("只有已接受邀请的评审人可以提交评审", 409, "invalid_assignment_state")
                conn.execute(
                    "UPDATE assignments SET status='completed',score=?,review_text=?,updated_at=? WHERE id=?",
                    (score, text, utcnow(), assignment_id),
                )
                self._audit(conn, row["paper_id"], reviewer_id, "review.submit", {
                    "assignment_id": assignment_id,
                    "score": score,
                    "track_id": row["track_id"],
                })
                conn.commit()
                return {"id": assignment_id, "status": "completed", "score": score, "track_id": row["track_id"]}
            except BusinessError:
                conn.rollback()
                raise
            except sqlite3.OperationalError as exc:
                conn.rollback()
                if str(exc) in _BUSY_MESSAGES:
                    raise BusinessError("论文改轨与评审提交冲突，本次评审提交未成功", 409, "concurrent_modification")
                raise

    # ---- 改轨 ----------------------------------------------------------------

    def move_paper(self, chair_id: str, paper_id: int, target_track_id: int, reason: str) -> dict:
        reason = reason.strip()
        if not reason:
            raise BusinessError("改轨原因不能为空", 422, "invalid_reason")
        with self.connect() as conn:
            chair = self._user(conn, chair_id)
            self._require(chair, "chair")
            if chair["track_id"] is not None:
                # 专题主席绑定单一轨道，跨轨调整只属于总主席。
                raise BusinessError("只有总主席可以跨轨调整论文", 403, "outside_track_scope")
            # 先抢写锁再做读写：评审提交进行中时改轨直接 409（只让一边成功）。
            try:
                self._begin_nowait(conn)
                target = conn.execute("SELECT * FROM tracks WHERE id=?", (target_track_id,)).fetchone()
                if not target:
                    raise BusinessError("目标轨道不存在", 404, "track_not_found")
                paper = conn.execute("SELECT * FROM papers WHERE id=?", (paper_id,)).fetchone()
                if not paper:
                    raise BusinessError("论文不存在", 404, "not_found")
                if paper["status"] not in {"submitted", "under_review"}:
                    raise BusinessError("论文已经决定，不能改轨", 409, "paper_decided")
                if paper["track_id"] == target_track_id:
                    raise BusinessError("论文已在目标轨道中", 409, "already_in_track")
                old_track_id = paper["track_id"]
                active = conn.execute(
                    "SELECT id, status FROM assignments WHERE paper_id=? AND archived=0",
                    (paper_id,),
                ).fetchall()
                withdrawn, archived = [], []
                for item in active:
                    if item["status"] in ("invited", "declined"):
                        conn.execute(
                            "UPDATE assignments SET status='withdrawn',updated_at=? WHERE id=?",
                            (utcnow(), item["id"]),
                        )
                        withdrawn.append(item["id"])
                    else:
                        # accepted / completed：保留原评分与意见，只留档、不计入新轨。
                        archived.append(item["id"])
                if archived:
                    conn.execute(
                        "UPDATE assignments SET archived=1,updated_at=? WHERE paper_id=? AND archived=0 AND status IN ('accepted','completed')",
                        (utcnow(), paper_id),
                    )
                remaining_valid = conn.execute(
                    "SELECT COUNT(*) FROM assignments WHERE paper_id=? AND archived=0 AND status='completed'",
                    (paper_id,),
                ).fetchone()[0]
                conn.execute("UPDATE papers SET track_id=? WHERE id=?", (target_track_id, paper_id))
                self._audit(conn, paper_id, chair_id, "paper.move_track", {
                    "from_track_id": old_track_id,
                    "to_track_id": target_track_id,
                    "reason": reason,
                    "handled_by": chair_id,
                    "withdrawn_assignment_ids": withdrawn,
                    "archived_assignment_ids": archived,
                    "valid_reviews_in_new_track": remaining_valid,
                })
                conn.commit()
                return {
                    "paper_id": paper_id,
                    "from_track_id": old_track_id,
                    "to_track_id": target_track_id,
                    "withdrawn_assignment_ids": withdrawn,
                    "archived_assignment_ids": archived,
                    "valid_reviews_in_new_track": remaining_valid,
                }
            except BusinessError:
                conn.rollback()
                raise
            except sqlite3.OperationalError as exc:
                conn.rollback()
                if str(exc) in _BUSY_MESSAGES:
                    raise BusinessError("评审提交与改轨冲突，本次改轨未成功", 409, "concurrent_modification")
                raise
