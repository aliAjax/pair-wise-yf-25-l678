"""轨道权限模块：专题主席/总主席边界与评审人轨道成员关系。

本模块只负责"谁能动哪条轨道"的判定与只读辅助，不包含分配事务；
分配、邀请撤回、改轨等写事务维护在 assignments.py 中。
"""
from __future__ import annotations

import sqlite3

from domain import BusinessError


class TrackPolicyMixin:
    # ---- 主席 ----------------------------------------------------------------

    def list_tracks(self, user_id: str) -> list[dict]:
        with self.connect() as conn:
            self._user(conn, user_id)  # 任意已登记用户均可查看轨道清单（投稿下拉框需要）。
            rows = conn.execute("SELECT * FROM tracks ORDER BY id").fetchall()
            return [dict(row) for row in rows]

    def _chair_scope(self, conn: sqlite3.Connection, chair_id: str) -> sqlite3.Row:
        """返回主席行，并限定其可管辖的轨道。

        总主席（track_id IS NULL）管辖所有轨道（scope_track=None 表示不限定）；
        专题主席只能管辖自己绑定的那一轨，跨轨动作一律 403。
        """
        chair = self._user(conn, chair_id)
        if chair["role"] != "chair":
            raise BusinessError("该操作仅允许 chair 角色", 403, "forbidden")
        return chair  # _ensure_track_access 再按 paper/track 做边界检查

    def _ensure_track_access(
        self, conn: sqlite3.Connection, chair: sqlite3.Row, track_id: int
    ) -> None:
        if chair["track_id"] is not None and chair["track_id"] != track_id:
            raise BusinessError("专题主席只能处理本轨论文", 403, "outside_track_scope")

    def _track_chair_view_filter(self, chair: sqlite3.Row):
        """list_papers 用：返回 (where_sql, params)。"""
        if chair["track_id"] is None:
            return "", ()
        return " WHERE track_id=?", (chair["track_id"],)

    # ---- 评审人 ---------------------------------------------------------------

    def _ensure_reviewer_in_track(
        self, conn: sqlite3.Connection, reviewer_id: str, track_id: int
    ) -> None:
        member = conn.execute(
            "SELECT 1 FROM track_reviewers WHERE reviewer_id=? AND track_id=?",
            (reviewer_id, track_id),
        ).fetchone()
        if not member:
            raise BusinessError("评审人不属于该论文所在轨道，评审意向与邀请都必须同轨", 403, "reviewer_not_in_track")

