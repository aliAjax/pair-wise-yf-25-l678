import tempfile
import threading
import unittest
from pathlib import Path

from app import BusinessError, ReviewStore


class ReviewFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ReviewStore(Path(self.tmp.name) / "test.db")
        self.store.seed()

    def tearDown(self):
        self.tmp.cleanup()

    def _paper(self, track="db", author="alice"):
        return self.store.submit_paper(
            author, "可靠分布式提交协议",
            "本文提出一种用于弱网环境的可靠提交协议，并通过模拟实验验证其安全性和性能。",
            track,
        )["id"]

    def test_complete_flow_and_double_blind_view(self):
        paper_id = self._paper()
        a1 = self.store.assign("chair", paper_id, "r1")["id"]
        a2 = self.store.assign("chair", paper_id, "r2")["id"]
        self.store.respond_assignment("r1", a1, True)
        self.store.respond_assignment("r2", a2, True)
        self.store.submit_review("r1", a1, 4, "方法严谨，缺少与最近工作的对比。")
        self.store.submit_review("r2", a2, 3, "实验充分，但部分结论需要进一步解释。")
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
        self.assertEqual(ctx.exception.status, 403)


class TrackScopeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ReviewStore(Path(self.tmp.name) / "test.db")
        self.store.seed()

    def tearDown(self):
        self.tmp.cleanup()

    def _paper(self, track="db"):
        return self.store.submit_paper(
            "alice", "数据库索引新结构",
            "本文研究一种面向混合负载的自适应索引结构，并给出详细的实验对比结果。",
            track,
        )["id"]

    def test_submit_requires_existing_track(self):
        with self.assertRaises(BusinessError) as ctx:
            self.store.submit_paper(
                "alice", "标题足够长",
                "这是一段满足二十个字以上长度要求的合法摘要内容。", "nope")
        self.assertEqual(ctx.exception.code, "unknown_track")

    def test_track_chair_sees_only_own_track(self):
        db_paper = self._paper("db")
        sys_paper = self._paper("sys")
        db_ids = {p["id"] for p in self.store.list_papers("chair_db")}
        self.assertIn(db_paper, db_ids)
        self.assertNotIn(sys_paper, db_ids)
        all_ids = {p["id"] for p in self.store.list_papers("chair")}
        self.assertEqual(all_ids, {db_paper, sys_paper})
        with self.assertRaises(BusinessError) as ctx:
            self.store.assign("chair_db", sys_paper, "r2")
        self.assertEqual(ctx.exception.code, "track_scope_violation")

    def test_bid_and_invitation_must_be_same_track(self):
        paper_id = self._paper("db")
        # r1 只在 db 轨：db 投标成功。
        ok = self.store.bid("r1", paper_id, "want", "熟悉相关方向")
        self.assertEqual(ok["track_id"], "db")
        # sys 轨论文，r1 不在 sys 评审团，投标与邀请都被拒绝。
        sys_paper = self._paper("sys")
        with self.assertRaises(BusinessError) as ctx:
            self.store.bid("r1", sys_paper, "want")
        self.assertEqual(ctx.exception.code, "reviewer_not_in_track")
        with self.assertRaises(BusinessError) as ctx:
            self.store.assign("chair", sys_paper, "r1")
        self.assertEqual(ctx.exception.code, "reviewer_not_in_track")
        # 专题主席不能给 db 轨论文发 sys 评审团以外的邀请…… 这里 r2 同时在两轨，可以邀请。
        self.assertEqual(self.store.assign("chair_sys", sys_paper, "r2")["status"], "invited")


class TrackChangeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ReviewStore(Path(self.tmp.name) / "test.db")
        self.store.seed()

    def tearDown(self):
        self.tmp.cleanup()

    def _paper(self, track="db"):
        return self.store.submit_paper(
            "alice", "跨轨重分配场景研究",
            "本文讨论论文在专题之间调整时评审邀请与意见应如何处置的规则。",
            track,
        )["id"]

    def _statuses(self, paper_id):
        return {a["id"]: a["status"] for a in self.store.list_assignments("chair", paper_id)}

    def _review(self, reviewer, assignment_id, score=4):
        self.store.respond_assignment(reviewer, assignment_id, True)
        return self.store.submit_review(
            reviewer, assignment_id, score,
            "评审意见足够详细，方法和实验都给出了明确的评价。",
        )

    def test_move_withdraws_invites_archives_accepted_and_completed(self):
        paper_id = self._paper("db")
        invited = self.store.assign("chair", paper_id, "r1")["id"]       # 未接受
        accepted = self.store.assign("chair", paper_id, "r2")["id"]
        self.store.respond_assignment("r2", accepted, True)
        completed = self.store.assign("chair", paper_id, "r3")["id"]
        self._review("r3", completed, score=5)

        with self.assertRaises(BusinessError) as ctx:  # 专题主席无权改轨
            self.store.move_paper("chair_db", paper_id, "sys", "选题更适合系统轨道处理")
        self.assertEqual(ctx.exception.code, "track_scope_violation")
        with self.assertRaises(BusinessError) as ctx:  # 原因过短
            self.store.move_paper("chair", paper_id, "sys", "错轨")
        self.assertEqual(ctx.exception.code, "invalid_reason")

        result = self.store.move_paper("chair", paper_id, "sys", "选题更适合系统轨道处理")
        self.assertEqual(result["invitations_withdrawn"], 1)
        self.assertEqual(result["assignments_archived"], 2)

        statuses = self._statuses(paper_id)
        self.assertEqual(statuses[invited], "withdrawn")
        self.assertEqual(statuses[accepted], "archived")
        self.assertEqual(statuses[completed], "archived")

        # 撤回的邀请不能再接受，归档的评审不能补提。
        with self.assertRaises(BusinessError) as ctx:
            self.store.respond_assignment("r1", invited, True)
        self.assertEqual(ctx.exception.code, "invitation_withdrawn")
        with self.assertRaises(BusinessError) as ctx:
            self.store.submit_review("r2", accepted, 4, "论文已经改轨后这份意见不应被接受提交。")
        self.assertEqual(ctx.exception.code, "paper_moved")

    def test_archived_reviews_do_not_count_and_chair_rebuilds_two(self):
        paper_id = self._paper("db")
        a1 = self.store.assign("chair", paper_id, "r1")["id"]
        a2 = self.store.assign("chair", paper_id, "r2")["id"]
        self._review("r1", a1)
        self._review("r2", a2, score=2)

        # 改轨前可以决定；改轨后旧评审留档但归零，必须补足两份本轨意见。
        self.store.move_paper("chair", paper_id, "sys", "评审范围调整，转交系统专题处理。")
        with self.assertRaises(BusinessError) as ctx:
            self.store.decide("chair", paper_id, "accept", "意见足够")
        self.assertEqual(ctx.exception.code, "insufficient_reviews")

        s1 = self.store.assign("chair", paper_id, "r2")["id"]  # r2 在 sys 评审团
        s2 = self.store.assign("chair", paper_id, "r3")["id"]  # r3 在 sys 评审团
        self._review("r2", s1, score=4)
        self._review("r3", s2, score=3)

        # 同一评审人在旧轨有留档记录，仍可在新轨完成新评审（每纪元唯一）。
        statuses = self._statuses(paper_id)
        self.assertEqual(len([s for s in statuses.values() if s == "archived"]), 2)
        self.assertEqual(len([s for s in statuses.values() if s == "completed"]), 2)

        # 新轨专题主席可以决定，db 轨主席不可以。
        with self.assertRaises(BusinessError) as ctx:
            self.store.decide("chair_db", paper_id, "accept", "越轨决定")
        self.assertEqual(ctx.exception.code, "track_scope_violation")
        decision = self.store.decide("chair_sys", paper_id, "minor_revision", "新轨两份意见齐备。")
        self.assertEqual(decision["track_id"], "sys")

    def test_history_keeps_move_reason_and_handler(self):
        paper_id = self._paper("db")
        self.store.move_paper("chair", paper_id, "sys", "选题更适合系统轨道处理")
        entry = [e for e in self.store.history("chair", paper_id)
                 if e["action"] == "paper.track_change"][-1]
        self.assertEqual(entry["detail"]["from_track"], "db")
        self.assertEqual(entry["detail"]["to_track"], "sys")
        self.assertEqual(entry["detail"]["reason"], "选题更适合系统轨道处理")
        self.assertEqual(entry["detail"]["handled_by"], "chair")


class MoveVsReviewConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ReviewStore(Path(self.tmp.name) / "test.db")
        self.store.seed()

    def tearDown(self):
        self.tmp.cleanup()

    def _paper(self):
        return self.store.submit_paper(
            "alice", "改轨与提交相撞的一致性研究",
            "本文分析评审提交与轨道调整并发执行时的串行化与冲突返回语义。",
            "db",
        )["id"]

    def _review_text(self):
        return "评审意见足够详细，方法和实验都给出了明确的评价。"

    def test_review_first_then_move_review_is_archived_not_counted(self):
        # 顺序一：评审先成功提交，改轨随后成功 → 评审留档（archived），不计入新轨。
        paper_id = self._paper()
        a = self.store.assign("chair", paper_id, "r1")["id"]
        self.store.respond_assignment("r1", a, True)
        self.store.submit_review("r1", a, 4, self._review_text())
        self.store.move_paper("chair", paper_id, "sys", "评审提交后再改轨，验证留档语义。")
        with self.assertRaises(BusinessError) as ctx:
            self.store.decide("chair", paper_id, "accept", "尝试用旧轨意见决定")
        self.assertEqual(ctx.exception.code, "insufficient_reviews")

    def test_move_first_then_review_gets_409(self):
        # 顺序二：改轨先成功提交，评审随后提交 → 409 paper_moved，只有一边成功。
        paper_id = self._paper()
        a = self.store.assign("chair", paper_id, "r1")["id"]
        self.store.respond_assignment("r1", a, True)
        self.store.move_paper("chair", paper_id, "sys", "先完成改轨，再撞评审提交。")
        with self.assertRaises(BusinessError) as ctx:
            self.store.submit_review("r1", a, 4, self._review_text())
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(ctx.exception.code, "paper_moved")

    def test_parallel_move_and_review_only_one_side_wins(self):
        # 并发：两个写事务经 BEGIN IMMEDIATE 串行化。不变量是
        # 新轨有效评审数永远为 0：评审要么 409 失败，要么成功后被归档。
        verdict = {}

        def do_review(aid):
            try:
                self.store.submit_review("r1", aid, 4, self._review_text())
                verdict["review"] = "ok"
            except BusinessError as exc:
                verdict["review"] = exc.code

        def do_move(pid, barrier):
            barrier.wait()
            try:
                self.store.move_paper("chair", pid, "sys", "并发场景下验证改轨与评审互斥规则。")
                verdict["move"] = "ok"
            except BusinessError as exc:
                verdict["move"] = exc.code

        for _ in range(8):
            verdict.clear()
            paper_id = self._paper()
            aid = self.store.assign("chair", paper_id, "r1")["id"]
            self.store.respond_assignment("r1", aid, True)
            barrier = threading.Barrier(2)
            t = threading.Thread(target=do_move, args=(paper_id, barrier))
            t.start()
            barrier.wait()
            do_review(aid)
            t.join()
            self.assertEqual(verdict["move"], "ok")
            self.assertIn(verdict["review"], {"ok", "paper_moved"})
            with self.assertRaises(BusinessError) as ctx:
                self.store.decide("chair", paper_id, "accept", "新轨不能引用旧轨或失败的评审")
            self.assertEqual(ctx.exception.code, "insufficient_reviews")


if __name__ == "__main__":
    unittest.main()
