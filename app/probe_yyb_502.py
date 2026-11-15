#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""YYB-GO /wxapp/getCode 可用性探测（pacing 合规版）。

用法（青龙容器内）：python3 probe_yyb_502.py [次数] [间隔秒数]
  次数    默认 3，硬上限 3（login_budget = 3 次 / 10 分钟）
  间隔    默认 4 秒，下限 3 秒（code_min_gap_ms = 3000）
          需要跨窗口长期采样时设为 600（login_window_ms）以上

pacing 参数来源：yyb-go 容器 /app/resource/db/tuning.json
  {"code_max_wait_ms": 20000, "code_min_gap_ms": 3000,
   "fresh_session_window_ms": 0, "login_budget": 3, "login_window_ms": 600000}

超过预算 / 间隔过短会触发 YYB-GO 本地限速 502，那不是节点故障，
据此算出的「失败率」没有参考价值 —— 所以本脚本在发请求前直接拦截。
"""
import json
import sys
import time
import urllib.error
import urllib.request

URL = "http://yyb-go:8000/wxapp/getCode"
APPID = "wx1b44c3ad181bde16"

# pacing 合规边界（与 yyb-go /app/resource/db/tuning.json 对齐）
LOGIN_BUDGET = 3            # login_budget
LOGIN_WINDOW_S = 600        # login_window_ms = 600000
CODE_MIN_GAP_S = 3          # code_min_gap_ms = 3000

MAX_N = LOGIN_BUDGET        # 硬上限：超过必然烧光预算
DEFAULT_N = 3
DEFAULT_GAP = 4             # > code_min_gap_ms，留 1 秒余量


def int_arg(pos: int, default: int, name: str) -> int:
    if len(sys.argv) <= pos:
        return default
    try:
        return int(sys.argv[pos])
    except ValueError:
        print("  ❌ %s 必须是整数，收到：%r" % (name, sys.argv[pos]))
        raise SystemExit(2)


N = int_arg(1, DEFAULT_N, "次数")
GAP = int_arg(2, DEFAULT_GAP, "间隔秒数")

# ★ 事前拦截：不合规直接拒绝运行，而不是事后打印一行提醒
if N < 1:
    print("  ❌ 拒绝运行：次数必须 ≥ 1（收到 %d）" % N)
    raise SystemExit(2)
if N > MAX_N:
    print("=" * 72)
    print("  ❌ 拒绝运行：请求次数 %d 超过 pacing 预算上限 %d" % (N, MAX_N))
    print("     yyb-go tuning.json：login_budget=%d 次 / %d 分钟" % (
        LOGIN_BUDGET, LOGIN_WINDOW_S // 60))
    print("     超限后的 502 是本地限速，不是节点故障，失败率没有参考价值。")
    print("     请改用 N ≤ %d；若要长期采样，把间隔设为 %d 秒以上分窗口跑。" % (
        MAX_N, LOGIN_WINDOW_S))
    print("=" * 72)
    raise SystemExit(2)
if GAP < CODE_MIN_GAP_S:
    print("=" * 72)
    print("  ❌ 拒绝运行：间隔 %ds 小于 code_min_gap_ms（%d 秒）" % (GAP, CODE_MIN_GAP_S))
    print("     请设 ≥ %d 秒；跨窗口采样建议 %d 秒。" % (CODE_MIN_GAP_S, LOGIN_WINDOW_S))
    print("=" * 72)
    raise SystemExit(2)

CROSS_WINDOW = GAP >= LOGIN_WINDOW_S


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
print("YYB-GO /wxapp/getCode 可用性探测  N=%d  间隔=%ds" % (N, GAP))
print("pacing：budget=%d/%ds，min_gap=%ds  →  %s" % (
    LOGIN_BUDGET, LOGIN_WINDOW_S, CODE_MIN_GAP_S,
    "跨窗口采样" if CROSS_WINDOW else "单窗口内合规采样"))
print("=" * 72)
ok = 0
for i in range(N):
    st, dt = once()
    if st == 200:
        ok += 1
    print("  #%-2d HTTP=%-4s %5.1fs %s" % (i + 1, st, dt, "✅" if st == 200 else "❌"))
    if i + 1 < N:
        print("     ⏳ 等待 %ds（满足 code_min_gap）" % GAP)
        time.sleep(GAP)
print()
print("  成功 %d / %d（失败率 %.0f%%）" % (ok, N, (N - ok) * 100.0 / N))
print()
if CROSS_WINDOW:
    print("  ✅ 跨窗口采样：每次请求落在独立的 %d 分钟窗口，不受 login_budget 挤占。" % (
        LOGIN_WINDOW_S // 60))
else:
    print("  ✅ 合规采样：%d 次请求正好用满单个窗口的 login_budget=%d，" % (N, LOGIN_BUDGET))
    print("     间隔 %ds ≥ code_min_gap %ds，未主动触发 YYB-GO 本地限速 502。" % (
        GAP, CODE_MIN_GAP_S))
    print("     ⚠️ 样本仅 %d 次且集中在同一窗口，只代表该时段，不能当全天成功率。" % N)
print("  参数来源：yyb-go /app/resource/db/tuning.json")
