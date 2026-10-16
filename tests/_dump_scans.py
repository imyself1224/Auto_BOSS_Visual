"""临时排查脚本：把某次 run 扫到的所有岗位连同档位判定打出来。"""
import json
import sqlite3
import sys
from pathlib import Path

run_ids = sys.argv[1:] or ["20261017_000957"]
conn = sqlite3.connect(str(Path(__file__).resolve().parent.parent / "autoboss.db"))

for run in run_ids:
    print("=" * 30, run)
    for at, message in conn.execute(
            "SELECT at, message FROM events WHERE run_id=? AND message LIKE 'list_p1%' ORDER BY id",
            (run,)):
        body = message[message.find("{"):]
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            print("  ! 解析失败:", message[:120])
            continue
        print(f"  -- {at}")
        for job in data.get("jobs") or []:
            print(f"    {str(job.get('fit')):<4} {str(job.get('title'))[:24]:<26}"
                  f"{str(job.get('company'))[:14]:<16}{str(job.get('salary')):<13}"
                  f"{str(job.get('job_type')):<4}btn={'Y' if job.get('greet_click') else 'N'} "
                  f"c={job.get('confidence')} | {job.get('fit_reason')}")
