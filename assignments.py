"""分配事务：邀请、回应、提交评审、改轨。

本模块把所有需要行锁的写操作集中到一起，统一使用 ``BEGIN IMMEDIATE``
事务串行化。与轨道权限模块 ``tracks.py``、主席页面 ``web/chair.html``
分开维护。

改轨语义（``move_paper``）：
- ``invited``（未接受）的邀请 → ``withdrawn``（撤回）；
- ``accepted`` / ``completed``（同轨旧邀请或旧轨评审）→ ``archived``（留档，
  不再计入新轨决定，决定仍需两份当前轨 ``completed`` 评审）；
- ``declined`` / 已 ``withdrawn`` 保持原样，仅属历史。

并发：改轨与评审提交都在写事务里先抢保留锁，再核对
``papers.track_seq`` 与 ``assignments.track_seq``。一边先提交后，另一边
读到的纪元已变化，评审提交返回 409（``paper_moved``），保证只让一边成功。
"""
from __future__ import annotations

import sqlite3

from domain import BusinessError, PAPER_OPEN_STATUSES, utcnow
from tracks import (
    chair_track,
    is_general_chair,
    require_chair_scope,
    require_reviewer_in_track,
    require_track_exists,
)


def _audit(conn, store, paper_id, actor, action, detail) -> None:
    store._audit(conn, paper_id, actor, action, detail)


def _current_track(conn: sqlite3.Connection, paper_id: int) -> sqlite3.Row:
    paper = conn.execute("SELECT track_id,track_seq,status FROM papers WHERE id=?", (paper_id,)).fetchone()
    if not paper:
        raise BusinessError("论文不存在", 404, "not_found")
    return paper


def invite(store, chair_id: str, paper_id: int, reviewer_id: str) -> dict:
    """主席邀请评审人。专题主席只能邀请本轨论文、本轨评审团成员。"""
    with store.connect() as conn:
        chair = store._user(conn, chair_id)
        try:
            conn.execute("BEGIN IMMEDIATE")
            paper = _current_track(conn, paper_id)
            if paper["status"] not in PAPER_OPEN_STATUSES:
                raise BusinessError("论文当前不可分配", 409, "paper_unavailable")
            require_chair_scope(conn, chair, paper["track_id"])
            reviewer = store._user(conn, reviewer_id)
            if reviewer["role"] != "reviewer":
                raise BusinessError("被邀请人必须是评审人", 403, "forbidden")
            require_reviewer_in_track(conn, reviewer_id, paper["track_id"])
            if conn.execute(
                "SELECT 1 FROM conflicts WHERE reviewer_id=? AND paper_id=?",
                (reviewer_id, paper_id),
            ).fetchone():
                raise BusinessError("评审人与论文存在利益冲突", 409, "conflict_of_interest")
            load = conn.execute(
                "SELECT COUNT(*) FROM assignments WHERE reviewer_id=? AND status IN ('invited','accepted')",
                (reviewer_id,),
            ).fetchone()[0]
            if load >= reviewer["load_limit"]:
                raise BusinessError("评审人已达到负载上限", 409, "reviewer_at_capacity")
            try:
                cur = conn.execute(
                    """INSERT INTO assignments
                       (paper_id,reviewer_id,track_id,track_seq,created_at,updated_at)
                       VALUES(?,?,?,?,?,?)""",
                    (paper_id, reviewer_id, paper["track_id"], paper["track_seq"], utcnow(), utcnow()),
                )
            except sqlite3.IntegrityError:
                raise BusinessError("该评审人在当前轨道已被分配此论文", 409, "assignment_exists")
            conn.execute("UPDATE papers SET status='under_review' WHERE id=?", (paper_id,))
            assignment_id = cur.lastrowid
            _audit(conn, store, paper_id, chair_id, "assignment.invite", {
                "assignment_id": assignment_id,
                "reviewer_id": reviewer_id,
                "track_id": paper["track_id"],
            })
            result = {
                "id": assignment_id,
                "paper_id": paper_id,
                "reviewer_id": reviewer_id,
                "track_id": paper["track_id"],
                "status": "invited",
            }
            conn.commit()
            return result
        except Exception:
            conn.rollback()
            raise


def respond(store, reviewer_id: str, assignment_id: int, accepted: bool) -> dict:
    with store.connect() as conn:
        reviewer = store._user(conn, reviewer_id)
        if reviewer["role"] != "reviewer":
            raise BusinessError("该操作仅允许 reviewer 角色", 403, "forbidden")
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM assignments WHERE id=?", (assignment_id,)).fetchone()
            if not row or row["reviewer_id"] != reviewer_id:
                raise BusinessError("分配不存在或不属于当前评审人", 404, "not_found")
            if row["status"] == "withdrawn":
                raise BusinessError("邀请已随改轨撤回，无法回应", 409, "invitation_withdrawn")
            if row["status"] == "archived":
                raise BusinessError("论文已改轨，该邀请只作留档，无法回应", 409, "assignment_archived")
            if row["status"] != "invited":
                raise BusinessError("邀请已经处理", 409, "invitation_already_answered")
            # 兜底：即便状态未更新，所属纪元也必须与论文当前轨道一致。
            paper = _current_track(conn, row["paper_id"])
            if paper["track_id"] != row["track_id"] or paper["track_seq"] != row["track_seq"]:
                raise BusinessError("论文已改轨，该邀请只作留档", 409, "assignment_archived")
            status = "accepted" if accepted else "declined"
            conn.execute("UPDATE assignments SET status=?,updated_at=? WHERE id=?",
                         (status, utcnow(), assignment_id))
            _audit(conn, store, row["paper_id"], reviewer_id, "assignment.respond",
                   {"assignment_id": assignment_id, "status": status})
            conn.commit()
            return {"id": assignment_id, "status": status}
        except Exception:
            conn.rollback()
            raise


def submit_review(store, reviewer_id: str, assignment_id: int, score: int, text: str) -> dict:
    if isinstance(score, bool) or not isinstance(score, int) or not 1 <= score <= 5:
        raise BusinessError("评分必须是 1 到 5 的整数", 422, "invalid_score")
    if len(text.strip()) < 10:
        raise BusinessError("评审意见至少 10 字", 422, "review_too_short")
    with store.connect() as conn:
        reviewer = store._user(conn, reviewer_id)
        if reviewer["role"] != "reviewer":
            raise BusinessError("该操作仅允许 reviewer 角色", 403, "forbidden")
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM assignments WHERE id=?", (assignment_id,)).fetchone()
            if not row or row["reviewer_id"] != reviewer_id:
                raise BusinessError("分配不存在或不属于当前评审人", 404, "not_found")
            paper = _current_track(conn, row["paper_id"])
            # 改轨先提交时，邀请可能已被批量归档；即便未被覆盖，纪元也已变化。
            if (paper["track_id"] != row["track_id"]
                    or paper["track_seq"] != row["track_seq"]
                    or row["status"] == "archived"):
                raise BusinessError("论文刚被改轨，评审提交失败，请等待新轨重新邀请",
                                    409, "paper_moved")
            if row["status"] == "withdrawn":
                raise BusinessError("邀请已撤回，不能提交评审", 409, "invitation_withdrawn")
            if row["status"] != "accepted":
                raise BusinessError("只有已接受邀请的评审人可以提交评审",
                                    409, "invalid_assignment_state")
            conn.execute(
                """UPDATE assignments
                   SET status='completed',score=?,review_text=?,updated_at=? WHERE id=?""",
                (score, text.strip(), utcnow(), assignment_id),
            )
            _audit(conn, store, row["paper_id"], reviewer_id, "review.submit",
                   {"assignment_id": assignment_id, "score": score, "track_id": paper["track_id"]})
            conn.commit()
            return {"id": assignment_id, "status": "completed", "score": score,
                    "track_id": paper["track_id"]}
        except Exception:
            conn.rollback()
            raise


def move_paper(store, actor_id: str, paper_id: int, target_track: str, reason: str) -> dict:
    """总主席跨轨改派；专题主席不能改轨。返回改轨处置明细。"""
    reason = reason.strip()
    if len(reason) < 5:
        raise BusinessError("改轨原因至少 5 字，且会写入审计历史", 422, "invalid_reason")
    with store.connect() as conn:
        actor = store._user(conn, actor_id)
        if actor["role"] != "chair":
            raise BusinessError("只有总主席可以调整论文轨道", 403, "forbidden")
        if not is_general_chair(conn, actor):
            raise BusinessError("专题主席只能处理本轨论文，改轨需由总主席执行",
                                403, "track_scope_violation")
        try:
            conn.execute("BEGIN IMMEDIATE")
            paper = conn.execute("SELECT * FROM papers WHERE id=?", (paper_id,)).fetchone()
            if not paper:
                raise BusinessError("论文不存在", 404, "not_found")
            if paper["status"] not in PAPER_OPEN_STATUSES:
                raise BusinessError("论文已决定或撤稿，不能改轨", 409, "paper_unavailable")
            require_track_exists(conn, target_track)
            if paper["track_id"] == target_track:
                raise BusinessError("论文已在该轨道，无需改轨", 409, "already_in_track")

            withdrawn = conn.execute(
                """UPDATE assignments SET status='withdrawn',updated_at=?
                   WHERE paper_id=? AND track_id=? AND track_seq=? AND status='invited'""",
                (utcnow(), paper_id, paper["track_id"], paper["track_seq"]),
            ).rowcount
            archived = conn.execute(
                """UPDATE assignments SET status='archived',updated_at=?
                   WHERE paper_id=? AND track_id=? AND track_seq=?
                     AND status IN ('accepted','completed')""",
                (utcnow(), paper_id, paper["track_id"], paper["track_seq"]),
            ).rowcount

            conn.execute(
                "UPDATE papers SET track_id=?,track_seq=track_seq+1 WHERE id=?",
                (target_track, paper_id),
            )
            _audit(conn, store, paper_id, actor_id, "paper.track_change", {
                "from_track": paper["track_id"],
                "to_track": target_track,
                "reason": reason,
                "handled_by": actor_id,
                "invitations_withdrawn": withdrawn,
                "assignments_archived": archived,
            })
            conn.commit()
            return {
                "paper_id": paper_id,
                "from_track": paper["track_id"],
                "to_track": target_track,
                "track_seq": paper["track_seq"] + 1,
                "invitations_withdrawn": withdrawn,
                "assignments_archived": archived,
                "reason": reason,
                "handled_by": actor_id,
            }
        except Exception:
            conn.rollback()
            raise


def count_valid_completed(conn: sqlite3.Connection, paper_id: int) -> int:
    """当前轨道、当前纪元的已完成评审数；改轨前的留档评审不计入。"""
    return conn.execute(
        """SELECT COUNT(*) FROM assignments a JOIN papers p ON p.id=a.paper_id
           WHERE a.paper_id=? AND a.status='completed'
             AND a.track_id=p.track_id AND a.track_seq=p.track_seq""",
        (paper_id,),
    ).fetchone()[0]
