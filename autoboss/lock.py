"""浏览器独占锁：同一时间只能有一个进程在操作 BOSS 直聘的浏览器。

投递可以从 daily_greet.bat、命令行、QQ「投递」三个地方启动，它们用的是同一个
浏览器。两个一起跑会互相抢页面 —— 一个在点卡片、另一个在滚列表，结果点错岗位。

用的是操作系统的文件锁（Windows: msvcrt，其它: fcntl），不是"文件存在就算占用"：
进程不管是正常退出、崩溃还是被强杀，锁都由系统自动释放，不会留下需要手动删的死锁文件。
"""
from __future__ import annotations

import os
from pathlib import Path

if os.name == "nt":
    import msvcrt
else:
    import fcntl


class BrowserLock:
    def __init__(self, path: Path):
        self.path = path
        self._fh = None

    def acquire(self) -> bool:
        """拿到锁返回 True；已被别的进程占用返回 False（不等待）。"""
        fh = self.path.open("a+b")
        try:
            if os.name == "nt":
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False
        self._fh = fh
        return True

    def release(self):
        if self._fh is None:
            return
        try:
            if os.name == "nt":
                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        self._fh.close()
        self._fh = None

    def held_by_other(self) -> bool:
        """是否有别的进程正占着。只试探一下，拿到了马上放掉。"""
        if not self.acquire():
            return True
        self.release()
        return False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.release()
