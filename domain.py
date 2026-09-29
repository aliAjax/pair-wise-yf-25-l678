"""领域共享内核：错误类型、时间与常量。

轨道权限见 ``tracks.py``，分配事务见 ``assignments.py``，主席页面见 ``web/chair.html``。
"""
from __future__ import annotations

from datetime import datetime, timezone

VALID_DECISIONS = {"accept", "reject", "minor_revision", "major_revision"}
PAPER_OPEN_STATUSES = ("submitted", "under_review")


class BusinessError(Exception):
    def __init__(self, message: str, status: int = 400, code: str = "bad_request"):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
