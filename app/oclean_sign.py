#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# =========================================================
# name: Oclean签到
# 接口：POST https://mall.oclean.com/API/VshopProcess.ashx
# 青龙环境变量：OCLEAN_COOKIE  一行一个 Shop-Member 值，多账号一行一条
# cron: 11 12,20 * * *


# =========================================================

import os
import sys
import json
import time
import requests

API_URL = "https://mall.oclean.com/API/VshopProcess.ashx"
PAYLOAD = "action=SignIn&SignInSource=Appshop&clientType=2"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_1_2 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 OcleanCare/4.0.2",
    "Host": "mall.oclean.com",
    "Referer": "https://mall.oclean.com/appshop/AppPointActivity?repeterId=123",
    "Content-Type": "application/x-www-form-urlencoded",
}

# ────────────────────────────────────────────
# 日志美化
# ────────────────────────────────────────────
def _box(title, width=40):
    bar = "─" * (width - 2)
    print("╭" + bar + "╮")
    pad = (width - 2 - len(title))
    left, right = pad // 2, pad - pad // 2
    print("│" + (" " * left) + title + (" " * right) + "│")
    print("╰" + bar + "╯")

def log(msg):
    print("· " + msg)

# ────────────────────────────────────────────
# 解析青龙环境变量
# ────────────────────────────────────────────
def load_cookies():
    raw = os.getenv("OCLEAN_COOKIE", "")
    if not raw.strip():
        single = os.getenv("OCLEAN_SHOP_MEMBER", "")
        if single.strip():
            return [single.strip()]
        return []
    lines = [l.strip() for l in raw.splitlines() if l.strip()]
    return lines

# ────────────────────────────────────────────
# 签到核心
# ────────────────────────────────────────────
def sign_in(cookie):
    s = requests.Session()
    s.trust_env = False
    s.headers.update(HEADERS)
    s.headers["Cookie"] = "Shop-Member=" + cookie
    try:
        r = s.post(API_URL, data=PAYLOAD, timeout=(10, 20))
        text = (r.text or "").strip()
        try:
            jr = r.json()
        except Exception:
            jr = None
        return r.status_code, jr, text
    except Exception as e:
        return None, None, str(e)

# ────────────────────────────────────────────
# 判断结果（兼容 Oclean 大写字段 Status/Code/Message）
# ────────────────────────────────────────────
def interpret(jr, raw_text):
    """返回 (status, detail)"""
    if isinstance(jr, dict):
        # 兼容大写（Oclean）和小写（通用）两种字段名
        status = jr.get("Status", jr.get("status"))
        code   = jr.get("Code",   jr.get("code",   jr.get("result")))
        msg    = jr.get("Message",jr.get("message",jr.get("msg", jr.get("error", ""))))
        points = jr.get("Points", jr.get("points", jr.get("point", jr.get("integral"))))

        # Code=3 固定表示已签到
        if code == 3:
            return "already", msg or "今日已签到"
        # 消息内容含"已签/重复/今天"
        if any(k in str(msg) for k in ["已签", "重复", "今天", "today", "already"]):
            return "already", msg or "今日已签到"
        # 成功：Status=OK 或 Code 为 0/1/200
        if status in ("OK", "ok") or code in (0, 1, "0", "1", True, "success", 200):
            detail = msg or "签到成功"
            if points:
                detail += f" · 积分 {points}"
            return "success", detail
        # Cookie 失效
        if code in (401, 403) or any(k in str(msg).lower() for k in ["登录", "失效", "过期", "无效", "login", "expired", "invalid"]):
            return "expired", msg or "Cookie 已失效"
        # 其他失败
        if code in (0, "0", False, "fail", 500):
            return "fail", msg or f"Code={code}"
        return "unknown", f"Code={code} Status={status} Message={msg}"
    # 非 JSON 响应：兜底用 raw_text 判断
    lower = raw_text.lower()
    if "sign" in lower and ("ok" in lower or "success" in lower or "成功" in raw_text):
        return "success", "签到成功"
    if any(k in raw_text for k in ["已签", "重复", "今天"]):
        return "already", "今日已签到"
    if any(k in lower for k in ["login", "expired", "invalid", "登录", "失效", "过期"]):
        return "expired", "Cookie 已失效"
    return "unknown", raw_text[:120] if raw_text else "空响应"

# ────────────────────────────────────────────
def run_one(idx, cookie):
    _box(f"账号 #{idx}")
    short = cookie[:8] + "..." + cookie[-6:]
    log(f"Cookie: Shop-Member={short}")
    status_code, jr, raw = sign_in(cookie)
    if status_code is None:
        print("│ ❌ 请求异常: " + raw)
        print()
        return False, "异常"
    st, detail = interpret(jr, raw)
    emoji = {"success": "✅", "already": "🟡", "expired": "🔴", "fail": "❌"}.get(st, "❔")
    print(f"│ {emoji} HTTP {status_code}  {detail}")
    print()
    return st in ("success", "already"), st

# ────────────────────────────────────────────
def main():
    cookies = load_cookies()
    total = len(cookies)

    print()
    print("╔" + "═" * 40 + "╗")
    title = "Oclean 欧克林商城 · 每日签到"
    pad = 40 - 2 - len(title)
    print("║" + (" " * (pad // 2)) + title + (" " * (pad - pad // 2)) + "║")
    sub = f"共 {total} 个账号"
    pad2 = 40 - 2 - len(sub)
    print("║" + (" " * (pad2 // 2)) + sub + (" " * (pad2 - pad2 // 2)) + "║")
    print("╚" + "═" * 40 + "╝")
    print()

    if not cookies:
        print("⚠️  青龙环境变量 OCLEAN_COOKIE 未设置")
        print("   格式: 一行一个 Shop-Member 值")
        return

    ok_count = 0
    expired_count = 0
    for i, ck in enumerate(cookies, start=1):
        ok, st = run_one(i, ck)
        if ok:
            ok_count += 1
        elif st == "expired":
            expired_count += 1
        if i < total:
            time.sleep(1)

    # 汇总
    print("╔" + "═" * 40 + "╗")
    tail = f"🎉 完成  {ok_count}/{total}"
    if expired_count:
        tail += f"  🔴失效 {expired_count}"
    pad3 = 40 - 2 - len(tail)
    print("║" + (" " * max(0, pad3 // 2)) + tail + (" " * max(0, pad3 - pad3 // 2)) + "║")
    print("╚" + "═" * 40 + "╝")
    print()

if __name__ == "__main__":
    main()
