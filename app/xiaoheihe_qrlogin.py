#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# =========================================================
# name:  小黑盒扫码登录
# version: 2026-10-29a
# cron:  0 0 1 1 * *
# 积分有效期：永久有效
# =========================================================
#
# 用途：一次性辅助脚本，用小黑盒 App 扫码登录，换取新的 user_pkey。
#      登录凭据有效期约 7 天，过期后重跑本脚本再扫一次即可。
#
# 怎么用：
#   1. 青龙「定时任务」新建，命令填  task app/xiaoheihe_qrlogin.py
#   2. 手动运行，任务日志里会打印一个二维码
#   3. 用「小黑盒」App 扫这个二维码（120 秒内有效）
#   4. 扫完日志里会输出新的 user_pkey，把它填到环境变量 XHH_USER_PKEY
#
# 可控参数：
#   XHH_QR_PKEY   可选。扫码成功后把 pkey 写入这个环境变量名（留空则只打印）
#   XHH_QR_WAIT   可选。等待扫码的秒数，默认 120
#
# 依赖：requests（必装）；segno（可选，装了就在日志里直接显示二维码）
#   青龙 → 依赖管理 → Python3 → 新建 → 填：requests segno
#
# 日志规范：[LEVEL] [XHHQR] message   （LEVEL: INFO / WARN / ERROR）
# =========================================================

import json
from datetime import datetime
import os
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import parse_qs, urlencode, urlparse
# ========== 账号分隔标识 ==========
# 多账号同跑时, 用醒目横线把各账号日志隔开, 便于阅读与定位。
def now_text() -> str:
    # 兼容两种导入: from datetime import datetime / import datetime
    _dt = datetime.now() if hasattr(datetime, "now") else datetime.datetime.now()
    return _dt.strftime("%Y-%m-%d %H:%M:%S")
# ========== 启动信息 ==========
def log_startup(total: int = 0) -> None:
    log(ACC_RULE)
    log("📌 脚本版本: %s ｜ %s" % (SCRIPT_VER, now_text()))
    if total:
        log("🔢 账号数量: %d" % total)
    log(ACC_RULE)
SCRIPT_VER = "2026-10-29a"
ACC_RULE = "=" * 70


# 签名函数直接复用主脚本（保证与 xiaoheihe.py 完全一致，避免两处实现漂移）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from xiaoheihe import gen_hkey, gen_nonce
except Exception as _e:  # pragma: no cover
    print("[ERROR] [XHHQR] 无法导入同目录的 xiaoheihe.py（%s）" % _e)
    raise

# ────────────────────────────────────────────
# 内部配置：直接改这里，省得去面板设环境变量
# ────────────────────────────────────────────
# 扫码成功后把 pkey 写入哪个环境变量（留空=只打印不写）
# 默认写 XHH_USER_PKEY —— 主脚本 xiaoheihe.py 读的就是它，写完直接生效
AUTO_SET_ENV = os.getenv("XHH_QR_PKEY", "") or "XHH_USER_PKEY"
# 等待扫码的最长秒数（二维码本身 120 秒过期）
WAIT_SECONDS = int(os.getenv("XHH_QR_WAIT", "") or 120)

API = "https://api.xiaoheihe.cn"
QR_URL_API = API + "/account/get_qrcode_url/"
QR_STATE_API = API + "/account/qr_state/"

# 签名参数（web 端，与主脚本 xiaoheihe.py 保持一致）
BASE_PARAMS = {
    "os_type": "web",
    "version": "999.0.4",
    "web_version": "3.0",
    "x_app": "heybox_website",
    "x_client_type": "web",
    "x_os_type": "Windows",
    "client_type": "web",
    "app": "heybox",
}

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36")

TIMEOUT = 25
POLL_INTERVAL = 2

_QN_SITE = "小黑盒扫码登录"
_QN_ERRORS = []


# ────────────────────────────────────────────
# 统一日志
# ────────────────────────────────────────────
def _emit(level, msg):
    print("[%s] [XHHQR] %s" % (level, msg), flush=True)


def log(msg):
    _emit("INFO", msg)


def warn(msg):
    _emit("WARN", msg)


def err(msg):
    _emit("ERROR", msg)
    _QN_ERRORS.append(msg)


def flush_notify(site=None, summary="", logger=None, **_kw):
    """只推错误：有 ERROR 才推一次，走青龙自带 notify.py。"""
    if not _QN_ERRORS:
        return
    try:
        import notify
        notify.send(_QN_SITE, "扫码登录失败", "\n".join(_QN_ERRORS))
    except Exception:
        pass


# ────────────────────────────────────────────
# hkey 签名（web 端，直接复用主脚本 xiaoheihe.py 的实现）
# ────────────────────────────────────────────
def api_get(path, params=None):
    """带签名地 GET 一个接口。

    返回 (http状态码, dict, cookie字典)
    —— cookie 用来兜底取 pkey（服务端有时只在 Set-Cookie 里下发，不放在 JSON 里）
    """
    t = int(time.time())
    nonce = gen_nonce()
    q = dict(BASE_PARAMS)
    q["_time"] = str(t)
    q["nonce"] = nonce
    q["hkey"] = gen_hkey(path, t, nonce)
    if params:
        q.update(params)
    url = API + path + "?" + urlencode(q)
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": "https://xiaoheihe.cn/",
        "Origin": "https://xiaoheihe.cn",
    })
    hdrs = None
    try:
        resp = urllib.request.urlopen(req, timeout=TIMEOUT)
        code, body, hdrs = resp.status, resp.read().decode("utf-8", "replace"), resp.headers
    except urllib.error.HTTPError as e:
        code, body, hdrs = e.code, e.read().decode("utf-8", "replace"), e.headers
    except Exception as e:
        return 0, {"_exc": str(e)}, {}
    cookies = {}
    if hdrs is not None:
        try:
            raw_list = hdrs.get_all("Set-Cookie") or []
        except Exception:
            raw_list = []
        for raw in raw_list:
            for part in raw.split(";"):
                part = part.strip()
                if "=" in part:
                    k, v = part.split("=", 1)
                    cookies[k.strip()] = v.strip()
    try:
        return code, json.loads(body), cookies
    except Exception:
        return code, {"_raw": body[:300]}, cookies


# ────────────────────────────────────────────
# 二维码渲染
# ────────────────────────────────────────────
def ensure_segno():
    """确保 segno 可用。缺了就现场装一次（纯 Python 包，几秒）。"""
    try:
        import segno  # noqa: F401
        return True
    except ImportError:
        pass
    log("缺少 segno 依赖，正在自动安装…")
    try:
        import subprocess
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "--quiet", "segno"],
            check=False, timeout=120,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        import segno  # noqa: F401
        log("✅ segno 安装完成")
        return True
    except Exception as e:
        err("segno 安装失败（%s）：请到青龙「依赖管理 → Python3」手动添加 segno" % e)
        return False


def print_qr_ascii(text):
    """在终端渲染二维码（Unicode 块字符）。失败返回 False。"""
    if not ensure_segno():
        return False
    try:
        import segno
        qr = segno.make(text, error="m")
        print()
        # compact=True 用 █ ▀ ▄ 块字符画，日志里最清晰
        qr.terminal(out=sys.stdout, border=2, compact=True)
        print()
        return True
    except Exception as e:
        err("二维码渲染失败：%s" % e)
        return False


def save_qr_png(text):
    """已废弃：不再往磁盘落 PNG 文件（二维码只画在日志里，不留垃圾）。"""
    return ""


# ────────────────────────────────────────────
# 主流程
# ────────────────────────────────────────────
def main():
    log_startup()
    log("🚀 小黑盒扫码登录开始")

    # 1) 拿二维码
    code, js, _ck = api_get("/account/get_qrcode_url/", {"app": "web"})
    if code != 200 or not isinstance(js, dict):
        err("获取二维码失败（HTTP %s）：%s" % (code, js.get("_exc") or js.get("_raw") or js))
        flush_notify(_QN_SITE, "获取二维码失败")
        return 1
    if js.get("status") != "ok":
        err("获取二维码失败：%s" % (js.get("msg") or "未知错误"))
        flush_notify(_QN_SITE, "获取二维码失败")
        return 1

    result = js.get("result") or {}
    qr_url = result.get("qr_url") or ""
    if not qr_url:
        err("响应里没有 qr_url：%s" % json.dumps(js, ensure_ascii=False)[:200])
        flush_notify(_QN_SITE, "响应缺少 qr_url")
        return 1

    qr = (parse_qs(urlparse(qr_url).query).get("qr") or [""])[0]
    expire = result.get("expire") or 120
    log("二维码已生成（有效期 %s 秒）" % expire)
    log("扫码链接：%s" % qr_url)

    if not print_qr_ascii(qr_url):
        err("没能画出二维码 —— 请到青龙「依赖管理 → Python3」添加 segno 后重跑；"
            "或用上面那条链接在电脑上生成二维码再扫")

    print()
    log("👉 请用「小黑盒」App 扫描上面的二维码（%s 秒内有效）" % expire)
    log("   扫完还要在手机上点「确认登录」，脚本才能拿到凭据")

    # 2) 轮询
    #    状态机（对齐官方前端/heybox-cli 的实现）：
    #      顶层 status 恒为 "ok"，真正状态在 result.error：
    #        wait  = 还没扫
    #        ready = 已扫，等手机点确认
    #        ok    = 成功，result.pkey 就是新凭据
    #        其它  = 已失效/被拒绝
    deadline = time.time() + min(WAIT_SECONDS, int(expire) + 10)
    pkey = ""
    heybox_id = ""
    last_raw = {}
    scanned = False
    while time.time() < deadline:
        time.sleep(POLL_INTERVAL)
        code, js, ck = api_get("/account/qr_state/", {"qr": qr, "app": "web"})
        if code != 200 or not isinstance(js, dict):
            last_raw = js
            continue
        if js.get("status") == "need_google_check":
            err("该账号需要谷歌二次验证，扫码登录无法完成")
            flush_notify(_QN_SITE, "需要谷歌二次验证")
            return 1
        r = js.get("result") or {}
        state = r.get("error") or ""
        last_raw = r
        if state == "ok":
            # pkey 可能放在 result.pkey，也可能只在 Set-Cookie 里下发
            pkey = (r.get("pkey") or ""
                    or (r.get("session") or {}).get("pkey") or ""
                    or ck.get("user_pkey") or ck.get("pkey") or "")
            profile = r.get("profile") or {}
            detail = r.get("account_detail") or {}
            heybox_id = (profile.get("heybox_id") or detail.get("userid")
                         or r.get("heybox_id") or ck.get("heybox_id")
                         or ck.get("user_heybox_id") or "")
            if pkey:
                break
            err("服务端说登录成功，但没返回 pkey（原始响应：%s）"
                % json.dumps(r, ensure_ascii=False)[:300])
        elif state == "ready":
            if not scanned:
                scanned = True
                log("👀 已扫到，请在手机上点「确认登录」")
        elif state == "wait":
            if scanned:
                scanned = False
        else:
            err("二维码状态异常：error=%s msg=%s"
                % (state, r.get("error_msg") or js.get("msg") or ""))
            log("原始响应：%s" % json.dumps(r, ensure_ascii=False)[:300])
            flush_notify(_QN_SITE, "二维码状态异常：%s" % state)
            return 1

    if not pkey:
        err("等待超时（%s 秒）未完成扫码" % WAIT_SECONDS)
        err("最后一次服务端返回：%s" % json.dumps(last_raw, ensure_ascii=False)[:300])
        if not scanned:
            err("提示：全程没检测到「已扫描」—— 二维码可能没被扫到，"
                "或 App 端登录被风控拦截（官方 App 登录会提示「本次登录存在风险」）")
        flush_notify(_QN_SITE, "扫码超时")
        return 1

    # 3) 输出
    log("")
    log("=" * 62)
    log("🎉 扫码登录成功")
    log("  昵称/账号ID：%s" % (heybox_id or "（未返回）"))
    log("  新 user_pkey ：%s" % pkey)
    log("=" * 62)
    log("把上面这串 user_pkey 填到青龙环境变量 XHH_USER_PKEY 即可，"
        "有效期约 7 天，过期后再跑一次本脚本。")

    if AUTO_SET_ENV:
        if set_ql_env(AUTO_SET_ENV, pkey):
            log("✅ 已自动写入青龙环境变量 %s —— 主脚本下次运行直接生效，"
                "不用手动复制粘贴了" % AUTO_SET_ENV)
        else:
            warn("⚠️ 自动写入环境变量失败（找不到青龙的数据库），"
                "请手动把上面的 pkey 填到环境变量 %s" % AUTO_SET_ENV)

    flush_notify(_QN_SITE, "扫码登录成功")
    return 0


def set_ql_env(name, value):
    """尝试写入青龙环境变量（本地 SQLite）。失败返回 False。

    青龙 2.x：/ql/data/db/database.sqlite，表 envs(...)
    青龙 3.x：/ql/data/db/database.sqlite，表 envs(id, name, value, remarks, ...)
    """
    try:
        import sqlite3
    except ImportError:
        return False
    candidates = [
        "/ql/data/db/database.sqlite",
        "/ql/data/db/file.db",
    ]
    for db in candidates:
        if not os.path.exists(db):
            continue
        try:
            conn = sqlite3.connect(db, timeout=10)
        except Exception:
            continue
        try:
            cur = conn.execute("PRAGMA table_info(envs)")
            cols = [r[1] for r in cur.fetchall()]
            if not cols:
                conn.close()
                continue
            row = conn.execute(
                "SELECT id FROM envs WHERE name = ? LIMIT 1", (name,)).fetchone()
            if row:
                conn.execute("UPDATE envs SET value = ? WHERE id = ?", (value, row[0]))
            else:
                if "remarks" in cols:
                    conn.execute(
                        "INSERT INTO envs (name, value, remarks) VALUES (?, ?, ?)",
                        (name, value, "小黑盒扫码登录自动写入"))
                else:
                    conn.execute("INSERT INTO envs (name, value) VALUES (?, ?)",
                                 (name, value))
            conn.commit()
            conn.close()
            return True
        except Exception:
            try:
                conn.close()
            except Exception:
                pass
    return False


if __name__ == "__main__":
    try:
        sys_exit = main()
    except KeyboardInterrupt:
        err("已中断")
        sys_exit = 1
    except Exception as e:
        err("未捕获异常：%s" % e)
        sys_exit = 1
    raise SystemExit(sys_exit)
