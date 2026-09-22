#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# =========================================================
# name: Oclean签到
# 接口：POST https://mall.oclean.com/API/VshopProcess.ashx
# 青龙环境变量：OCLEAN_COOKIE   一行一个 Shop-Member 值，多账号一行一条
#               OCLEAN_SHOP_MEMBER  回退变量，单账号时可用（OCLEAN_COOKIE 为空才生效）
#               OCLEAN_NOTIFY  通知开关，默认开启；填 0/false/off/no 关闭
# cron: 5 8,16 * * *
#
# 通知推送：共用仓库根目录的 notify.py（青龙面板自带那份），见下方 send_notify()。
#   注意：notify 采用「延迟导入」——import 写在 _ensure_notify() / send_notify() 内部，
#   顶部 import 区看不到它。这样 notify.py 缺失时脚本仍能跑完，只是不推送，不会 ImportError 挂掉。
#   仓库根没有 notify.py 时会自动从 CDN 下载（三源回退），无需手工补文件。


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
# 通知推送（只推精简摘要，不推全量日志）
# ────────────────────────────────────────────
# 开关：默认开启，填 0/false/off/no 可关闭
NOTIFY = (os.getenv("OCLEAN_NOTIFY", "1") or "1").strip().lower() not in ("0", "false", "off", "no")

# 共享 notify 模块定位：仓库根目录放一份 notify.py，全部脚本共用（不再各目录放副本）
# 兼容旧布局：脚本同目录若已有 notify.py（老版本自愈下载留下的），优先用它
_NOTIFY_DIR = os.path.dirname(os.path.abspath(__file__))
if not os.path.exists(os.path.join(_NOTIFY_DIR, "notify.py")):
    _NOTIFY_DIR = os.path.dirname(_NOTIFY_DIR)   # 脚本同目录没有 → 用仓库根那份
if _NOTIFY_DIR not in sys.path:
    sys.path.insert(0, _NOTIFY_DIR)


def _ensure_notify():
    """确保共享的 notify.py 就位（缺失时从 CDN 自愈下载，订阅更新/容器重建后不用手动补）。"""
    try:
        import notify  # noqa: F401
        return True
    except Exception:
        pass
    target = os.path.join(_NOTIFY_DIR, "notify.py")
    for _url in ("https://cdn.jsdelivr.net/gh/whyour/qinglong@develop/sample/notify.py",
                 "https://raw.githubusercontent.com/whyour/qinglong/refs/heads/develop/sample/notify.py",
                 "https://ghproxy.net/https://raw.githubusercontent.com/whyour/qinglong/refs/heads/develop/sample/notify.py"):
        try:
            _r = requests.get(_url, timeout=15)
            if _r.status_code == 200 and "def send" in _r.text:
                with open(target, "wb") as _f:
                    _f.write(_r.content)
                log("已自愈下载 notify.py（" + _url.split("/")[2] + "）")
                return True
        except Exception as _e:
            log("notify.py 下载失败（" + _url.split("/")[2] + "）: " + str(_e))
    return False


def send_notify(title, content):
    """推送到青龙面板配置的通知渠道；失败只打日志，不影响脚本退出状态。"""
    if not NOTIFY:
        return
    try:
        if not _ensure_notify():
            log("未安装 notify.py，跳过推送")
            return
        from notify import send as _notify_send
        if len(content) > 3000:
            content = content[:3000] + "...(内容过长已截断)"
        _notify_send(title, content)
    except ImportError:
        log("未安装 notify.py，跳过推送")
    except Exception as e:
        log("推送失败: " + str(e))


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
        return False, "异常", " ".join(str(raw).split())[:80]
    st, detail = interpret(jr, raw)
    emoji = {"success": "✅", "already": "🟡", "expired": "🔴", "fail": "❌"}.get(st, "❔")
    print(f"│ {emoji} HTTP {status_code}  {detail}")
    print()
    return st in ("success", "already"), st, detail

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
        send_notify("Oclean签到 无账号可执行", "⚠️ 青龙环境变量 OCLEAN_COOKIE 未设置")
        return

    ok_count = 0
    expired_count = 0
    push_lines = []
    st_emoji = {"success": "✅", "already": "🟡", "expired": "🔴", "fail": "❌", "异常": "⚠️"}
    for i, ck in enumerate(cookies, start=1):
        ok, st, detail = run_one(i, ck)
        push_lines.append("账号%d: %s %s" % (i, st_emoji.get(st, "❔"), detail))
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

    # 推送精简摘要：每个账号一行，完整日志只留在青龙面板
    _title = "Oclean签到 %d/%d 成功" % (ok_count, total)
    if expired_count:
        _title += "  🔴失效%d" % expired_count
    send_notify(_title, "\n".join(push_lines))


if __name__ == "__main__":
    main()
