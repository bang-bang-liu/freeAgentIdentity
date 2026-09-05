"""细粒度账号锁注册表，用于刷新登录等需要逐个账号申请释放锁的任务。"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Iterator


class AccountLockRegistry:
    """运行期账号锁注册表，与 task_runtime 调度器的 busy_account_keys 合并。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._busy: set[str] = set()

    def busy_keys(self) -> set[str]:
        """返回当前被占用的账号键快照。"""
        with self._lock:
            return self._busy.copy()

    @contextmanager
    def acquire(self, account_id: int) -> Iterator[None]:
        """申请账号锁；已被占用时抛出 AccountLockBusyError。"""
        key = f"account:{account_id}"
        with self._lock:
            if key in self._busy:
                raise AccountLockBusyError(f"Account {account_id} is locked by another task")
            self._busy.add(key)
        try:
            yield
        finally:
            with self._lock:
                self._busy.discard(key)


class AccountLockBusyError(Exception):
    """账号锁已被占用。"""


account_lock_registry = AccountLockRegistry()
