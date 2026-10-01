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
# ———————————— 错误通知（可选项，想用则用）————————————
# 只推错误：本次运行出现 ERROR 日志才推送一次；正常跑完不打扰。
# 直接调用青龙自带的通知模块（容器内为 /ql/data/scripts/notify.py，仓库内为同目录/上一级的 notify.py）——
#   在青龙面板「通知设置」里配一次即可全站通用（该文件由青龙官方维护，支持其全部推送渠道）。
# 找不到该文件、或未配置任何通知渠道时，只在日志末尾提示一行，不报错、不中断。
import os as _os
import sys as _sys

_QN_SITE = "Oclean"
# 青龙官方通知渠道环境变量（任一存在即视为已配置），清单见青龙 sample/config.sample.sh
_QN_ENVS = (
    "PUSH_KEY", "BARK_PUSH", "TG_BOT_TOKEN", "DD_BOT_TOKEN", "QYWX_KEY", "QYWX_AM",
    "IGOT_PUSH_KEY", "PUSH_PLUS_TOKEN", "WE_PLUS_BOT_TOKEN", "GOBOT_URL", "GOTIFY_URL",
    "DEER_KEY", "CHAT_URL", "AIBOTK_KEY", "CHRONOCAT_URL", "SMTP_SERVER", "SMTP_EMAIL",
    "PUSHME_KEY", "FSKEY", "QMSG_KEY", "NTFY_URL", "WXPUSHER_APP_TOKEN",
    "WXPUSHER_SPT_LIST", "WEBHOOK_URL", "OPENILINK_APP_TOKEN", "WPUSH_APIKEY",
)
_qn_send = None
_qn_load_err = ""
_qn_dirs = ["/ql/data/scripts", "/ql/scripts"]
try:
    _qn_here = _os.path.dirname(_os.path.abspath(__file__))
    _qn_dirs += [_qn_here, _os.path.dirname(_qn_here)]
except NameError:
    pass
try:
    for _p in _qn_dirs:
        if _p and _os.path.isfile(_os.path.join(_p, "notify.py")):
            if _p not in _sys.path:
                _sys.path.insert(0, _p)
            from notify import send as _qn_send
            break
except Exception as _qn_e:
    _qn_send = None
    _qn_load_err = str(_qn_e)
_QN_NOMOD = (
    "[QL] 青龙通知模块 notify.py 加载失败（%s），错误日志未推送" % _qn_load_err
    if _qn_load_err else
    "[QL] 未找到青龙通知模块 notify.py，错误日志未推送"
)

_qn_errors = []
_qn_seen = set()
_qn_flushed = False


def collect_error(line):
    """由日志出口调用：收集 ERROR 行（去重，最多 30 条）。"""
    _s = str(line).strip()
    if not _s or _s in _qn_seen:
        return False
    _qn_seen.add(_s)
    if len(_qn_errors) < 30:
        _qn_errors.append(_s)
    return True


def flush_notify(_site=None, summary="", logger=None, **_kw):
    """收尾调用：本次有 ERROR 才推送；未配置或找不到青龙 notify 时只提示一行。

    前两个参数为兼容既有调用点而保留，站点名以 _QN_SITE 为准。
    """
    global _qn_flushed
    if _qn_flushed or not _qn_errors:
        return False
    _qn_flushed = True
    if _qn_send is None:
        print(_QN_NOMOD, flush=True)
        return False
    if not any(_os.getenv(_k) for _k in _QN_ENVS):
        print("[QL] 未配置通知渠道，错误日志未推送（可在青龙「通知设置」或环境变量中配置）", flush=True)
        return False
    _body = ((summary + "\n\n") if summary else "") + "\n".join(_qn_errors)
    try:
        _qn_send("【%s】执行出错" % _QN_SITE, _body)
        return True
    except Exception as _e:
        print("[QL] 错误日志推送失败：%s" % _e, flush=True)
        return False

import atexit as _atexit


def _qn_atexit():
    """兜底：脚本中途 return / sys.exit 时补一次（未配置则只提示一行）。"""
    if _qn_flushed:
        return
    if _qn_send is None:
        print(_QN_NOMOD, flush=True)
        return
    if not any(_os.getenv(_k) for _k in _QN_ENVS):
        print("[QL] 未配置通知渠道，错误日志未推送（可在青龙「通知设置」或环境变量中配置）", flush=True)
        return
    if _qn_errors:
        flush_notify(_QN_SITE, "脚本提前结束，未走到收尾汇总")


_atexit.register(_qn_atexit)


def _qn_excepthook(exc_type, exc, tb):
    """兜底：脚本顶层未捕获异常时，也把错误推一次。"""
    if exc_type is KeyboardInterrupt:
        _qn_sys_excepthook(exc_type, exc, tb)
        return
    try:
        _qn_errors.append("[ERROR] [%s] %s: %s" % (_QN_SITE, exc_type.__name__, exc))
    except Exception:
        pass
    if not _qn_flushed:
        flush_notify(_QN_SITE, "脚本异常中断（未捕获异常）")
    _qn_sys_excepthook(exc_type, exc, tb)


_qn_sys_excepthook = _sys.excepthook
_sys.excepthook = _qn_excepthook

def _emit(level, msg):
    line = f"[{level}] [OCLEAN] {msg}"
    print(line, flush=True)
    if level == "ERROR":
        collect_error(line)

def log(msg):   _emit("INFO", msg)
def warn(msg):  _emit("WARN", msg)
def err(msg):   _emit("ERROR", msg)

# ────────────────────────────────────────────
# 账号分隔标识
# 多账号同跑时，用醒目的横线 + 账号信息把各账号的日志隔开，便于阅读与定位。
# ────────────────────────────────────────────
_ACC_RULE = "=" * 70

def acc_banner(idx, total, ident=""):
    """打印账号分隔标识（开始）。"""
    log(_ACC_RULE)
    log("👤 账号 %d/%d%s" % (idx, total, (" ｜ " + str(ident)) if ident else ""))
    log(_ACC_RULE)

def acc_footer(idx, total):
    """打印账号分隔标识（结束）。"""
    log("🔚 账号 %d/%d 处理结束" % (idx, total))

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
        acc_banner(i, total, "Cookie: Shop-Member=" + ck[:8] + "..." + ck[-6:])
        ok, st, detail = run_one(i, ck)
        if ok:
            ok_count += 1
        elif st == "expired":
            expired_count += 1
        acc_footer(i, total)
        if i < total:
            time.sleep(1)

    tail = f"📊 [执行汇总] Oclean · 账号 {total} ｜ 成功 {ok_count} ｜ 失败 {total - ok_count}"
    if expired_count:
        tail += f" ｜ 🔴 失效 {expired_count}"
    log(_ACC_RULE)
    log(tail)
    log(_ACC_RULE)    
    # 错误日志推送：有错误才发；未配置通知渠道则只提示一行
    flush_notify(
        "Oclean",
        "账号 %d ｜ 成功 %d ｜ 失败 %d" % (total, ok_count, total - ok_count),
        logger=log,
    )


if __name__ == "__main__":
    main()
