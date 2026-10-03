"""提供可替换的 UTC 时钟。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Protocol


class Clock(Protocol):
    """定义服务所需的最小时钟接口。"""

    def now(self) -> datetime:
        """返回带时区的当前时间。"""


class SystemClock:
    """使用系统 UTC 时间。"""

    def now(self) -> datetime:
        """返回当前 UTC 时间。"""

        return datetime.now(timezone.utc)


class FixedClock:
    """为测试与离线验收提供固定时间。"""

    def __init__(self, value: datetime) -> None:
        if value.tzinfo is None:
            raise ValueError("固定时间必须包含时区")
        self._value = value.astimezone(timezone.utc)

    def now(self) -> datetime:
        """返回固定的 UTC 时间。"""

        return self._value


class MutableClock:
    """允许测试与验收按需推进的 UTC 时钟。"""

    def __init__(self, value: datetime) -> None:
        if value.tzinfo is None:
            raise ValueError("设定时间必须包含时区")
        self._value = value.astimezone(timezone.utc)

    def now(self) -> datetime:
        """返回当前设定的 UTC 时间。"""

        return self._value

    def set(self, value: datetime) -> None:
        """重新设定当前时间。"""

        if value.tzinfo is None:
            raise ValueError("设定时间必须包含时区")
        self._value = value.astimezone(timezone.utc)

    def advance(self, **kwargs: int) -> None:
        """按给定偏移推进当前时间。"""

        self._value = self._value + timedelta(**kwargs)
