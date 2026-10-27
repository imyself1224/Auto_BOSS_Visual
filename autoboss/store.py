"""本地状态库（SQLite，标准库自带）。

两个作用：
1. **去重** —— 同一个职位/公司绝不重复打招呼。这既是省配额，也是防封：
   对同一家公司反复发送是最容易被判定为骚扰的行为。
2. **审计** —— 每次运行、每条发出的招呼语、每张截图都留痕。
   万一账号出问题，这份记录是你复盘和申诉的依据。
"""
from __future__ import annotations

import hashlib
import sqlite3
import time
from datetime import datetime
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS greeted (
    job_key     TEXT PRIMARY KEY,
    title       TEXT,
    company     TEXT,
    salary      TEXT,
    location    TEXT,
    work_hours  TEXT,
    message     TEXT,
    status      TEXT,      -- sent / dry_run / failed / skipped
    note        TEXT,
    greeted_at  TEXT
);

CREATE TABLE IF NOT EXISTS runs (
    run_id      TEXT PRIMARY KEY,
    started_at  TEXT,
    ended_at    TEXT,
    greeted     INTEGER DEFAULT 0,
    skipped     INTEGER DEFAULT 0,
    failed      INTEGER DEFAULT 0,
    stopped_by  TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT,
    at          TEXT,
    level       TEXT,
    message     TEXT,
    shot        TEXT       -- 关联截图路径
);
"""


def job_key(title: str, company: str) -> str:
    """职位唯一键。标题+公司归一化后取哈希。

    不用 URL 里的 job id：纯视觉方案拿不到 DOM 属性，
    而标题+公司足够区分，且对页面改版免疫。
    """
    raw = f"{(title or '').strip()}|{(company or '').strip()}".lower()
    raw = "".join(raw.split())
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


class Store:
    def __init__(self, path: Path, run_id: str):
        self.path = Path(path)
        self.run_id = run_id
        # 多账号的数据库在 data/<name>/ 下，第一次跑 doctor 时目录还不存在。
        # sqlite3.connect 不会自动建父目录，这里补上。
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.executescript(_SCHEMA)
        # 旧数据库补列：location / work_hours 是后加的，不能要求用户重建库
        cols = {row[1] for row in self.conn.execute("PRAGMA table_info(greeted)")}
        for name in ("location", "work_hours"):
            if name not in cols:
                self.conn.execute(f"ALTER TABLE greeted ADD COLUMN {name} TEXT")
        self.conn.commit()
        self.conn.execute(
            "INSERT OR REPLACE INTO runs (run_id, started_at) VALUES (?, ?)",
            (run_id, datetime.now().isoformat(timespec="seconds")),
        )
        self.conn.commit()

    # --- 去重 --------------------------------------------------------
    def already_greeted(self, title: str, company: str) -> bool:
        """是否已经处理过。

        含 'already'：那些视觉上显示「继续沟通」、但本地库里没有的职位
        （比如你之前手动打过招呼），也要排除掉，不能重复骚扰。

        **不含 'dry_run'**：演练没有真的发出去。算进来的话，演练时挑中的
        好岗位到了实发时会被当成"已发过"永远跳过。同一轮里不重复由 agent 的
        `_attempted` 保证，不靠这里。
        """
        cur = self.conn.execute(
            "SELECT 1 FROM greeted WHERE job_key = ? "
            "AND status IN ('sent', 'already')",
            (job_key(title, company),),
        )
        return cur.fetchone() is not None

    def greeted_today(self) -> int:
        """今天真正发出去的条数（dry_run 不计入配额消耗）。

        以本地日期为准 —— 平台的配额按自然日 00:00 重置。
        """
        today = datetime.now().strftime("%Y-%m-%d")
        cur = self.conn.execute(
            "SELECT COUNT(*) FROM greeted "
            "WHERE status = 'sent' AND substr(greeted_at, 1, 10) = ?",
            (today,),
        )
        return int(cur.fetchone()[0])

    # --- 记录 --------------------------------------------------------
    def record(self, title: str, company: str, salary: str, message: str,
               status: str, note: str = "", location: str = "",
               work_hours: str = ""):
        self.conn.execute(
            "INSERT OR REPLACE INTO greeted "
            "(job_key, title, company, salary, location, work_hours, message, "
            "status, note, greeted_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                job_key(title, company), title, company, salary, location,
                work_hours, message, status, note,
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        self.conn.commit()

    def log(self, level: str, message: str, shot: str | None = None):
        self.conn.execute(
            "INSERT INTO events (run_id, at, level, message, shot) VALUES (?, ?, ?, ?, ?)",
            (self.run_id, datetime.now().isoformat(timespec="seconds"), level, message, shot),
        )
        self.conn.commit()

    def finish(self, greeted: int, skipped: int, failed: int, stopped_by: str):
        self.conn.execute(
            "UPDATE runs SET ended_at = ?, greeted = ?, skipped = ?, failed = ?, stopped_by = ? "
            "WHERE run_id = ?",
            (datetime.now().isoformat(timespec="seconds"), greeted, skipped, failed,
             stopped_by, self.run_id),
        )
        self.conn.commit()

    def close(self):
        try:
            self.conn.close()
        except Exception:
            pass
