#!/usr/bin/env python3
# =========================================================
# name: Oclean签到
# cron: 5 7,16 * * *
#
# 任务流程：
#   1. 读取 OCLEAN_COOKIE（或回退 OCLEAN_SHOP_MEMBER）中的 Shop-Member 值
#   2. 逐个账号 POST 签到接口完成每日签到
#   3. 解析响应判断成功 / 已签 / Cookie 失效，并打印积分
#   4. 末尾输出执行汇总
# 可控参数：
#   OCLEAN_COOKIE       必填。一行一个 Shop-Member 值，多账号换行
#   OCLEAN_SHOP_MEMBER  可选。单账号回退变量，OCLEAN_COOKIE 为空时才生效
#
# 日志规范：[LEVEL] [OCLEAN] message   （LEVEL: INFO / WARN / ERROR）
# =========================================================

import os
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
# 统一日志
# ────────────────────────────────────────────
def _emit(level, msg):
    print(f"[{level}] [OCLEAN] {msg}", flush=True)

def log(msg):   _emit("INFO", msg)
def warn(msg):  _emit("WARN", msg)
def err(msg):   _emit("ERROR", msg)

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
    short = cookie[:8] + "..." + cookie[-6:]
    log(f"👤 账号 #{idx} · Cookie: Shop-Member={short}")
    status_code, jr, raw = sign_in(cookie)
    if status_code is None:
        brief = " ".join(str(raw).split())[:80]
        err(f"❌ 账号 #{idx} 请求异常: {brief}")
        return False, "异常", brief
    st, detail = interpret(jr, raw)
    icon = {"success": "✅", "already": "🟡", "expired": "🔴", "fail": "❌", "unknown": "❔"}.get(st, "❔")
    line = f"{icon} 账号 #{idx} · HTTP {status_code} {detail}"
    if st in ("success", "already"):
        log(line)
    else:
        err(line)
    return st in ("success", "already"), st, detail

# ────────────────────────────────────────────
def main():
    cookies = load_cookies()
    total = len(cookies)

    log(f"🚀 Oclean 欧克林商城 每日签到开始 · 共 {total} 个账号")

    if not cookies:
        err("🚫 青龙环境变量 OCLEAN_COOKIE 未设置（格式: 一行一个 Shop-Member 值）")
        return

    ok_count = 0
    expired_count = 0
    for i, ck in enumerate(cookies, start=1):
        ok, st, detail = run_one(i, ck)
        if ok:
            ok_count += 1
        elif st == "expired":
            expired_count += 1
        if i < total:
            time.sleep(1)

    tail = f"🏁 Oclean 执行汇总 · 账号 {total}｜成功 {ok_count}｜失败 {total - ok_count}"
    if expired_count:
        tail += f"｜🔴 失效 {expired_count}"
    log(tail)


if __name__ == "__main__":
    main()
