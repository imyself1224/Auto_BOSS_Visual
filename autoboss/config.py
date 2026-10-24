"""配置加载：config.toml + .env。

只用标准库（tomllib 是 Python 3.11+ 内置）。.env 用手写解析，
避免为了一个小功能引入 python-dotenv。
"""
from __future__ import annotations

import os
import tomllib
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config.toml"
ENV_PATH = PROJECT_ROOT / ".env"

# 配置里允许缺失的键，用这里的默认值兜底，避免用户改配置时漏字段就崩。
_DEFAULTS: dict[str, dict[str, object]] = {
    "deepseek": {
        "api_key_env": "DEEPSEEK_API_KEY",
        "base_url": "https://api.deepseek.com",
        "text_model": "deepseek-flash",
        "vision_model": "deepseek-flash",
        "timeout_sec": 120,
        "thinking": False,
    },
    "search": {"query": "", "city": "100010000", "pages": 1},
    "source": {"mode": "recommend"},
    "resume": {"kb_path": "resume/kb.md"},
    "job_filter": {
        "require_internship": True,
        "min_daily_wage": 300.0,
        # 岗位学历要求的区间（不限 = 该边界不设限；空串同义）
        "education_min": "",
        "education_max": "",
        "work_days_per_month": 21.75,
        "allow_unparsable": False,
        "require_city": "北京",
        # 档位权重：模型按知识库把岗位判成这几档，这里决定每档的排序分。
        # 表里没有的档位（「不可用」）= 不打招呼。
        "fit_scores": {"最匹配": 3.0, "次匹配": 2.0, "可转投": 1.0},
        # 有门槛的档位：日薪区间最低值达不到就不打招呼
        "fit_min_low_wage": {},
        "fit_fallback": {},
        "prefer_districts": [],
        "prefer_district_bonus": 1.0,
        "boss_filters": [["求职类型", "实习"]],
    },
    "browser": {
        "executable_path": "", "cdp_port": 9222, "user_data_dir": "",
        "viewport_width": 1600, "viewport_height": 900,
    },
    "limits": {
        "daily_cap": 10, "min_interval_sec": 25, "max_interval_sec": 60,
        "max_empty_scans": 6, "max_dry_scans": 8, "max_dry_sources": 0,
        "active_hours": [9, 21],
    },
    "safety": {"dry_run": True, "stop_on_captcha": True},
    "qq": {
        "enabled": False, "ws_url": "ws://127.0.0.1:3001",
        "access_token_env": "ONEBOT_TOKEN", "owner_qq": 0, "owner_qq_env": "", "notify_each": True,
        "auto_launch": True, "snowluma_dir": "SnowLuma", "snowluma_webui_port": 5099,
        "qq_path": r"C:\Program Files\Tencent\QQNT\QQ.exe",
    },
}


def load_env(path: Path = ENV_PATH) -> dict[str, str]:
    """解析 KEY=VALUE 形式的 .env。已存在的真实环境变量优先。"""
    values: dict[str, str] = {}
    if path.exists():
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key, val = key.strip(), val.strip()
            # 去掉可能存在的引号
            if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
                val = val[1:-1]
            values[key] = val
    return values


class Config:
    """点号访问的只读配置包装。cfg.get("limits.daily_cap")。"""

    def __init__(self, raw: dict, env: dict[str, str]):
        self._raw = raw
        self._env = env

    def get(self, dotted: str, default=None):
        section, _, key = dotted.partition(".")
        table = self._raw.get(section, {})
        fallback = _DEFAULTS.get(section, {}).get(key, default)
        if not isinstance(table, dict):
            return fallback
        return table.get(key, fallback)

    def section(self, name: str) -> dict:
        merged = dict(_DEFAULTS.get(name, {}))
        merged.update(self._raw.get(name, {}) or {})
        return merged

    def env(self, name: str, default: str = "") -> str:
        """真实环境变量优先于 .env 文件。"""
        return os.environ.get(name) or self._env.get(name, default)

    def api_key(self) -> str:
        key = self.env(self.get("deepseek.api_key_env", "DEEPSEEK_API_KEY"))
        if not key or key.startswith("sk-xxxx"):
            raise SystemExit(
                "未找到 DEEPSEEK_API_KEY。\n"
                f"请复制 {PROJECT_ROOT / '.env.example'} 为 .env 并填入真实 key，\n"
                "或直接设置环境变量 DEEPSEEK_API_KEY。"
            )
        return key

    # --- 派生路径 ---------------------------------------------------
    @property
    def user_data_dir(self) -> Path:
        configured = self.get("browser.user_data_dir", "")
        return Path(configured) if configured else PROJECT_ROOT / "browser_profile"

    @property
    def shots_dir(self) -> Path:
        return PROJECT_ROOT / "shots"

    @property
    def logs_dir(self) -> Path:
        return PROJECT_ROOT / "logs"

    @property
    def db_path(self) -> Path:
        return PROJECT_ROOT / "autoboss.db"

    @property
    def stop_flag(self) -> Path:
        """QQ 遥控「停止」时创建；投递进程看到它就收工。"""
        return PROJECT_ROOT / ".stop_request"

    @property
    def lock_path(self) -> Path:
        """浏览器独占锁（见 autoboss/lock.py）。"""
        return PROJECT_ROOT / ".browser.lock"

    @property
    def qq_keywords_path(self) -> Path:
        """QQ 上设定、下次任务沿用的检索关键词（一行一个）。发「/关键词 清空」删除。"""
        return PROJECT_ROOT / ".qq_keywords.txt"

    @property
    def qq_prefs_path(self) -> Path:
        """QQ 上设定的学历区间 / 日薪（JSON）。发「/学历 不限 不限」「/薪资 不限」
        取消限制；删掉这个文件就全部回到 config.toml。"""
        return PROJECT_ROOT / ".qq_prefs.json"

    def search_url(self, page: int = 1, query: str | None = None,
                   degree: str = "") -> str:
        """职位搜索地址。

        `degree` 是 BOSS 原生的学历筛选参数（逗号分隔的代码，如 "203,204"）：
        直接写进地址，筛选由平台完成，不需要模型读学历、也不用点筛选栏。
        """
        from urllib.parse import quote
        q = quote(str(self.get("search.query", "") if query is None else query))
        # 用 /web/geek/jobs（复数）：实测 /job 会被 302 到 /jobs，直接写复数省一次跳转
        # 求职类型直接写进地址（实习 = jobType=1902，实测点筛选器后地址里就是它）。
        # 这样每个来源一打开就是实习池，不用再去点筛选栏 —— 少点几下，也少几次模型调用。
        job_type = str(self.get("search.job_type", "") or "")
        url = (
            "https://www.zhipin.com/web/geek/jobs"
            f"?query={q}&city={self.get('search.city')}&page={page}"
            + (f"&jobType={job_type}" if job_type else "")
        )
        degree = str(degree or "").strip()
        if degree:
            url += f"&degree={quote(degree, safe=',')}"
        return url


def load(path: Path = CONFIG_PATH) -> Config:
    if not path.exists():
        raise SystemExit(f"找不到配置文件：{path}")
    with path.open("rb") as fh:
        raw = tomllib.load(fh)
    return Config(raw, load_env())
