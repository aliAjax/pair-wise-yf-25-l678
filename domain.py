"""共享领域内核：异常与时间工具，供各业务模块复用。"""
from __future__ import annotations

from datetime import datetime, timezone


class BusinessError(Exception):
    def __init__(self, message: str, status: int = 400, code: str = "bad_request"):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
