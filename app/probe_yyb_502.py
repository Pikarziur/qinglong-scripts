#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""YYB-GO 502 时段聚集性探测脚本。

用途：怀疑当前时段 YYB 节点池整体不健康时，跑一下看连续请求的失败率。
用法（青龙容器内）：python3 probe_yyb_502.py [次数]
"""
import json
import sys
import time
import urllib.error
import urllib.request

URL = "http://yyb-go:8000/wxapp/getCode"
APPID = "wx1b44c3ad181bde16"
N = int(sys.argv[1]) if len(sys.argv) > 1 else 10


def once(ref=1):
    body = json.dumps({"ref": str(ref), "app_id": APPID}).encode()
    req = urllib.request.Request(
        URL, data=body, headers={"Content-Type": "application/json"}, method="POST")
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            return r.status, time.time() - t0
    except urllib.error.HTTPError as e:
        e.read()
        return e.code, time.time() - t0
    except Exception:
        return 0, time.time() - t0


print("=" * 72)
print("YYB-GO /wxapp/getCode 连续 %d 次（间隔 2s）" % N)
print("=" * 72)
ok = 0
for i in range(N):
    st, dt = once()
    if st == 200:
        ok += 1
    print("  #%-2d HTTP=%-4s %5.1fs %s" % (i + 1, st, dt, "✅" if st == 200 else "❌"))
    time.sleep(2)
print()
print("  成功 %d / %d（失败率 %.0f%%）" % (ok, N, (N - ok) * 100.0 / N))
print()
print("  ⚠️ 重要提醒：YYB-GO 自身有 pacing 限速（db/tuning.json）：")
print("     login_budget = 3 次 / 10 分钟，code_min_gap = 3 秒")
print("     → 本脚本连打 N 次会自己烧光预算，后面的 502 是限速不是节点故障！")
print("     → N > 3 的结果不可信；要测真实可用性，间隔请拉到 10 分钟以上，或只打 3 次")
