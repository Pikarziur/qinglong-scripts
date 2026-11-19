#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
name:   清空青龙今日执行统计
version: 2026-11-19a
desc:   删除 CrontabStats 表中当日的统计记录，让青龙首页统计卡片归零。
        只清统计聚合数据，不影响定时任务、脚本、环境变量。
        作为禁用任务存在，仅在手动点“运行”时执行。
"""
import os
import sqlite3
import sys
from datetime import date

# 数据库路径（容器内默认，可用环境变量覆盖便于本地调试）
DB_PATH = os.environ.get("QL_DB_PATH", "/ql/data/db/database.sqlite")
# 清哪一天：默认今天；可传 YYYY-MM-DD 指定（一般不用）
TARGET_DATE = sys.argv[1] if len(sys.argv) > 1 else date.today().isoformat()


def main():
    if not os.path.exists(DB_PATH):
        print(f"[错误] 数据库不存在：{DB_PATH}")
        sys.exit(1)
    conn = sqlite3.connect(DB_PATH)
    try:
        before = conn.execute(
            "SELECT COUNT(*) FROM CrontabStats WHERE date=?", (TARGET_DATE,)
        ).fetchone()[0]
        cur = conn.execute("DELETE FROM CrontabStats WHERE date=?", (TARGET_DATE,))
        conn.commit()
        print(f"[清空] {TARGET_DATE}：删除 {cur.rowcount} 条统计记录（原有 {before} 条）")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
