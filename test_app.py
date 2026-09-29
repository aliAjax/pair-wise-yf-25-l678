import sqlite3
import tempfile
import unittest
from pathlib import Path

from app import ReviewStore
from domain import BusinessError


class ReviewFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ReviewStore(Path(self.tmp.name) / "test.db")
        self.store.seed()

    def tearDown(self):
        self.tmp.cleanup()

    def _paper(self, track_id="distributed"):
        return self.store.submit_paper(
            "alice",
            "可靠分布式提交协议",
            "本文提出一种用于弱网环境的可靠提交协议，并通过模拟实验验证其安全性和性能。",
            track_id,
        )["id"]

    def _two_reviews(self, paper_id, reviewer_a="r1", reviewer_b="r2"):
        a1 = self.store.assign("chair", paper_id, reviewer_a)["id"]
        a2 = self.store.assign("chair", paper_id, reviewer_b)["id"]
        self.store.respond_assignment(reviewer_a, a1, True)
        self.store.respond_assignment(reviewer_b, a2, True)
        self.store.submit_review(reviewer_a, a1, 4, "方法严谨，缺少与最近工作的对比。")
        self.store.submit_review(reviewer_b, a2, 3, "实验充分，但部分结论需要进一步解释。")
        return a1, a2

    def test_complete_flow_and_double_blind_view(self):
        paper_id = self._paper()
        self._two_reviews(paper_id)
        self.store.submit_rebuttal("alice", paper_id, "感谢意见，我们将补充对比并解释实验结论。")
        result = self.store.decide("chair", paper_id, "minor_revision", "补充实验后接收。")
        self.assertEqual(result["decision"], "minor_revision")
        self.assertIsNone(self.store.get_paper("r1", paper_id)["author_id"])
        self.assertIsNotNone(self.store.get_paper("chair", paper_id)["author_id"])
        history = self.store.history("chair", paper_id)
        self.assertEqual(history[-1]["action"], "decision.record")
        self.assertGreaterEqual(len(history), 8)

    def test_conflict_blocks_assignment_and_role_is_enforced(self):
        paper_id = self._paper()
        self.store.add_conflict("chair", paper_id, "r1", "同一导师团队成员")
        with self.assertRaises(BusinessError) as ctx:
            self.store.assign("chair", paper_id, "r1")
        self.assertEqual(ctx.exception.code, "conflict_of_interest")
        with self.assertRaises(BusinessError) as ctx:
            self.store.assign("alice", paper_id, "r2")
        self.assertEqual(ctx.exception.status, 403)
        with self.assertRaises(BusinessError) as ctx:
            self.store.get_paper("r2", paper_id)
            # 改轨前 r2 与该论文没有任何同轨关系。
        self.assertEqual(ctx.exception.status, 403)

    # ---- 分轨：投稿与轨道成员 -------------------------------------------------

    def test_submit_requires_known_track(self):
        with self.assertRaises(BusinessError) as ctx:
            self.store.submit_paper("alice", "标题够长吗", "摘要内容一定要超过二十个汉字才能通过校验哦。", "nope")
        self.assertEqual(ctx.exception.code, "invalid_track")

    def test_bid_requires_same_track_membership(self):
        paper_id = self._paper("distributed")
        # r3 只属于人工智能轨，不能对分布式轨论文表达意向。
        with self.assertRaises(BusinessError) as ctx:
            self.store.bid("r3", paper_id, "want")
        self.assertEqual(ctx.exception.status, 403)
        self.assertEqual(ctx.exception.code, "reviewer_not_in_track")
        # r1 属于分布式轨，意向成功。
        ok = self.store.bid("r1", paper_id, "want")
        self.assertEqual(ok["track_id"], "distributed")

    # ---- 分轨：主席页面看到的管辖范围 ------------------------------------------

    def test_track_chair_is_scoped_to_own_track(self):
        ds_paper = self._paper("distributed")
        ai_paper = self._paper("ai")
        ids_ds = {p["id"] for p in self.store.list_papers("chair_ds")}
        self.assertIn(ds_paper, ids_ds)
        self.assertNotIn(ai_paper, ids_ds)
        # 专题主席不能对别轨论文登记冲突、发邀请。
        with self.assertRaises(BusinessError) as ctx:
            self.store.add_conflict("chair_ds", ai_paper, "r3", "试图越轨操作")
        self.assertEqual(ctx.exception.code, "outside_track_scope")
        with self.assertRaises(BusinessError) as ctx:
            self.store.assign("chair_ds", ai_paper, "r3")
        self.assertEqual(ctx.exception.code, "outside_track_scope")
        # 总主席跨轨可见、可操作。
        self.assertIn(ai_paper, {p["id"] for p in self.store.list_papers("chair")})

    def test_assignment_requires_same_track_reviewer(self):
        paper_id = self._paper("distributed")
        with self.assertRaises(BusinessError) as ctx:
            self.store.assign("chair", paper_id, "r3")  # r3 不在分布式轨
        self.assertEqual(ctx.exception.code, "reviewer_not_in_track")

    # ---- 分轨：改轨 ----------------------------------------------------------

    def test_only_general_chair_can_move_track(self):
        paper_id = self._paper("distributed")
        with self.assertRaises(BusinessError) as ctx:
            self.store.move_paper("chair_ds", paper_id, "ai", "专题主席不应改轨")
        self.assertEqual(ctx.exception.status, 403)

    def test_move_track_withdraws_invites_and_archives_reviews(self):
        paper_id = self._paper("distributed")
        invited = self.store.assign("chair", paper_id, "r1")["id"]
        accepted = self.store.assign("chair", paper_id, "r2")["id"]
        self.store.respond_assignment("r2", accepted, True)
        self.store.submit_review("r2", accepted, 4, "已经写完的完整评审意见，需要留档。")

        result = self.store.move_paper("chair", paper_id, "ai", "主题更贴近机器学习。")
        self.assertEqual(result["to_track_id"], "ai")
        self.assertEqual(result["withdrawn_assignment_ids"], [invited])
        self.assertEqual(result["archived_assignment_ids"], [accepted])
        self.assertEqual(result["valid_reviews_in_new_track"], 0)

        with self.store.connect() as conn:
            rows = {
                r["id"]: r
                for r in conn.execute("SELECT id,status,archived,track_id FROM assignments WHERE paper_id=?", (paper_id,))
            }
        self.assertEqual(rows[invited]["status"], "withdrawn")
        self.assertEqual(rows[accepted]["archived"], 1)
        self.assertEqual(rows[accepted]["status"], "completed")  # 内容保留，仅留档。
        self.assertEqual(self.store.get_paper("chair", paper_id)["track_id"], "ai")

        # 被撤回的邀请不能再接受。
        with self.assertRaises(BusinessError) as ctx:
            self.store.respond_assignment("r1", invited, True)
        self.assertEqual(ctx.exception.code, "invitation_withdrawn")

        # 历史中保留改轨原因和处置人。
        move_event = [e for e in self.store.history("chair", paper_id) if e["action"] == "paper.move_track"][-1]
        self.assertEqual(move_event["actor_id"], "chair")
        self.assertEqual(move_event["detail"]["reason"], "主题更贴近机器学习。")
        self.assertEqual(move_event["detail"]["handled_by"], "chair")
        self.assertEqual(move_event["detail"]["from_track_id"], "distributed")
        self.assertEqual(move_event["detail"]["to_track_id"], "ai")

    def test_archived_reviews_do_not_count_and_new_track_needs_two_valid(self):
        paper_id = self._paper("distributed")
        self._two_reviews(paper_id)
        self.store.move_paper("chair", paper_id, "ai", "改投人工智能轨。")

        # 旧轨两份完成评审只留档：不足两份本轨意见，不能决定。
        with self.assertRaises(BusinessError) as ctx:
            self.store.decide("chair_ai", paper_id, "accept")
        self.assertEqual(ctx.exception.code, "insufficient_reviews")

        # 新轨必须重新补足两份；r3 是 AI 轨成员，r2 同时属于两轨，可被重新邀请。
        a_new_1 = self.store.assign("chair_ai", paper_id, "r2")["id"]
        a_new_2 = self.store.assign("chair_ai", paper_id, "r3")["id"]
        self.store.respond_assignment("r2", a_new_1, True)
        self.store.respond_assignment("r3", a_new_2, True)
        self.store.submit_review("r2", a_new_1, 5, "新轨视角下的第一份有效评审意见。")
        self.store.submit_review("r3", a_new_2, 4, "新轨视角下的第二份有效评审意见。")
        decided = self.store.decide("chair_ai", paper_id, "accept", "两份本轨意见齐备。")
        self.assertEqual(decided["decision"], "accept")

        # 留档旧意见仍在数据库里可查（审计/档案），但 archived=1。
        with self.store.connect() as conn:
            archived = conn.execute(
                "SELECT COUNT(*) FROM assignments WHERE paper_id=? AND archived=1", (paper_id,)
            ).fetchone()[0]
            active = conn.execute(
                "SELECT COUNT(*) FROM assignments WHERE paper_id=? AND archived=0 AND status='completed'",
                (paper_id,),
            ).fetchone()[0]
        self.assertEqual(archived, 2)
        self.assertEqual(active, 2)

    def test_move_requires_reason_and_rejects_same_track(self):
        paper_id = self._paper("distributed")
        with self.assertRaises(BusinessError) as ctx:
            self.store.move_paper("chair", paper_id, "ai", "  ")
        self.assertEqual(ctx.exception.status, 422)
        with self.assertRaises(BusinessError) as ctx:
            self.store.move_paper("chair", paper_id, "distributed", "留在原处")
        self.assertEqual(ctx.exception.code, "already_in_track")

    # ---- 分轨：改轨与评审提交互斥（409） --------------------------------------

    def test_review_submit_returns_409_while_move_holds_write_lock(self):
        paper_id = self._paper("distributed")
        assignment_id = self.store.assign("chair", paper_id, "r1")["id"]
        self.store.respond_assignment("r1", assignment_id, True)

        # 用一个外部连接占住写锁，模拟改轨事务正在进行。
        holder = self.store.connect()
        holder.execute("PRAGMA busy_timeout = 0")
        holder.execute("BEGIN IMMEDIATE")
        try:
            with self.assertRaises(BusinessError) as ctx:
                self.store.submit_review("r1", assignment_id, 4, "评审提交与改轨同时发生，必须冲突失败。")
            self.assertEqual(ctx.exception.status, 409)
            self.assertEqual(ctx.exception.code, "concurrent_modification")

            with self.assertRaises(BusinessError) as ctx:
                self.store.move_paper("chair", paper_id, "ai", "评审提交占锁时改轨也失败。")
            self.assertEqual(ctx.exception.status, 409)
            self.assertEqual(ctx.exception.code, "concurrent_modification")
        finally:
            holder.rollback()
            holder.close()

        # 锁释放后评审可以正常提交。
        result = self.store.submit_review("r1", assignment_id, 4, "锁释放后补提的评审意见，内容足够。")
        self.assertEqual(result["status"], "completed")

    def test_review_submit_after_move_is_rejected_as_archived(self):
        paper_id = self._paper("distributed")
        invited = self.store.assign("chair", paper_id, "r1")["id"]
        accepted = self.store.assign("chair", paper_id, "r2")["id"]
        self.store.respond_assignment("r2", accepted, True)
        self.store.move_paper("chair", paper_id, "ai", "先完成改轨，评审随后到达。")
        with self.assertRaises(BusinessError) as ctx:
            self.store.submit_review("r2", accepted, 4, "改轨之后才提交的旧轨评审不应成功。")
        self.assertEqual(ctx.exception.code, "assignment_archived")
        with self.assertRaises(BusinessError) as ctx:
            self.store.respond_assignment("r1", invited, True)
        self.assertEqual(ctx.exception.code, "invitation_withdrawn")

    def test_old_track_bid_stops_authorizing_view_after_move(self):
        paper_id = self._paper("distributed")
        self.store.bid("r2", paper_id, "maybe")  # r2 同时属于两轨，但意向落在分布式轨
        self.assertIsNone(self.store.get_paper("r2", paper_id)["author_id"])
        self.store.move_paper("chair", paper_id, "ai", "改轨后旧意向失效。")
        # r2 是新轨成员，但旧轨意向不授权新轨视图；在新轨重新表达意向后恢复。
        with self.assertRaises(BusinessError):
            self.store.get_paper("r2", paper_id)
        self.store.bid("r2", paper_id, "want")
        self.assertIsNone(self.store.get_paper("r2", paper_id)["author_id"])


if __name__ == "__main__":
    unittest.main()
