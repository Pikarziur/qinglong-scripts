#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# =========================================================
# name:  西集社 - 签到
# cron: 10 0,12 * * *
# =========================================================
"""
西集社 xsijishe.com 每日签到 - 青龙脚本 (Discuz! misign 插件)
============================================================
环境变量:
    XIJISHE_ACCOUNT   必填（推荐）。单变量存放「账号#密码」或「账号:密码」，
                      首次/失效时自动账号密码登录并把 Cookie 写本地缓存，之后无需再管
    XIJISHE_COOKIE_CACHE  可选。Cookie 缓存文件路径，默认 /ql/data/xsijishe.cookie
                      （青龙持久目录，订阅更新不受影响；可覆盖）
    MY_PROXY         可选，HTTP/HTTPS 代理地址，如 http://127.0.0.1:7890
                     或 socks5://127.0.0.1:7890；留空则直连
    XIJISHE_EXPIRE     可选，Cookie 预期过期日，如 2026-10-15。设了会提前3天提醒
    XIJISHE_NOTIFY      可选，通知开关，默认开启；填 0/false/off/no 关闭

依赖 (青龙依赖管理里加一行):  curl_cffi
    （用于模拟 Chrome 过 Cloudflare，否则会被 403 拦）

Cookie 获取逻辑（优先级）:
    1. 本地缓存文件 .xsijishe.cookie（登录成功后自动写入）
    2. 账号密码 XIJISHE_ACCOUNT 登录获取并写缓存
    3. 都没有 → 报错退出

🚀 首次使用：配 XIJISHE_ACCOUNT=你的账号#你的密码，运行一次即可自动登录+缓存。
   若自动登录失败（站点验证码 / CF 升级），可手动把 Cookie 写入缓存文件救急：
   在 XIJISHE_COOKIE_CACHE 指向的路径写入「前缀_auth=...; 前缀_saltkey=...」即可。

Discuz Cookie 结构说明:
    - {前缀}_auth     登录凭证（必须，HttpOnly）
    - {前缀}_saltkey   盐值（必须，HttpOnly）
    - 前缀是 Discuz 随机生成的，每个站点不同
    - cf_clearance    Cloudflare 验证 Cookie（不缓存，curl_cffi 每次自动重新获取）
"""
import os
import re
import sys
import urllib.parse

try:
    from curl_cffi import requests as cf_requests
except ImportError:
    print("❌ 缺少依赖 curl_cffi，请在青龙依赖管理安装：pip install curl_cffi")
    sys.exit(1)

BASE = "https://xsijishe.com"
PROXY = (os.environ.get("MY_PROXY") or "").strip()
XIJISHE_EXPIRE = (os.environ.get("XIJISHE_EXPIRE") or "").strip()  # 可选，如 "2026-10-15"
XIJISHE_NOTIFY = os.environ.get("XIJISHE_NOTIFY", "1").strip().lower() not in ("0", "false", "off", "no")

HERE = os.path.dirname(os.path.abspath(__file__))
COOKIE_CACHE = (os.environ.get("XIJISHE_COOKIE_CACHE") or "/ql/data/xsijishe.cookie")


def log(msg):
    print(msg, flush=True)


def load_cached_cookie():
    """读取本地缓存的 Cookie 字符串（登录成功后写入）"""
    try:
        if os.path.exists(COOKIE_CACHE):
            with open(COOKIE_CACHE, "r", encoding="utf-8") as f:
                return f.read().strip()
    except Exception:
        pass
    return ""


def save_cached_cookie(c):
    """把新 Cookie 落盘缓存（权限 0600）"""
    if not c:
        return
    try:
        d = os.path.dirname(COOKIE_CACHE)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(COOKIE_CACHE, "w", encoding="utf-8") as f:
            f.write(c)
        try:
            os.chmod(COOKIE_CACHE, 0o600)
        except Exception:
            pass
        log("💾 已把新 Cookie 缓存到本地文件")
    except Exception as e:
        log("⚠️ Cookie 缓存写入失败: " + str(e))


def check_cookie_expire(expire_str):
    """手动过期日检测（Discuz Cookie 无内置 exp，需用户手动配 XIJISHE_EXPIRE）"""
    if not expire_str:
        log("📅 未配置 XIJISHE_EXPIRE，仅在线检测生效（设 YYYY-MM-DD 开启日期提醒）")
        return
    try:
        from datetime import datetime, timedelta
        exp = datetime.strptime(expire_str, "%Y-%m-%d") + timedelta(hours=23, minutes=59, seconds=59)
    except ValueError:
        log("⚠️ XIJISHE_EXPIRE 格式错误，应为 YYYY-MM-DD")
        return
    now = datetime.now()
    delta = exp - now
    remain_days = delta.days + (1 if delta.seconds > 0 else 0)  # ceil
    status = ("剩余" + str(remain_days) + "天") if remain_days >= 0 else ("已过期" + str(-remain_days) + "天")
    log("📅 Cookie " + status + " | 过期: " + expire_str)
    if remain_days < 0:
        log("❌ Cookie 已过期，请重新登录抓取！")
        return False
    if remain_days <= 3:
        log("🔔 Cookie 即将过期（" + str(remain_days) + "天），建议尽快更新！")
    return True


def put_cookies(session, cookie_str):
    """把字符串 cookie 写进 session 的 jar（domain 统一绑西集社）"""
    for part in cookie_str.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, v = part.split("=", 1)
        # 剥离可能过期/冲突的 cf_clearance，交给 curl_cffi 自动获取
        if k.strip().lower() == "cf_clearance":
            continue
        session.cookies.set(k.strip(), v.strip(), domain=".xsijishe.com")


def extract_discuz_cookie(session):
    """从 curl_cffi session 的 cookie jar 提取 Discuz 的 auth + saltkey，拼成字符串"""
    auth = saltkey = prefix = None
    for c in session.cookies:
        n = c.name
        if n.endswith("_auth"):
            auth = c.value
            prefix = n[:-4]
        elif n.endswith("_saltkey"):
            saltkey = c.value
    if auth and saltkey:
        return "{p}_auth={a}; {p}_saltkey={s}".format(p=prefix, a=auth, s=saltkey)
    return None


def get_formhash(session):
    try:
        r = session.get(BASE + "/", timeout=20)
    except Exception as e:
        log("访问首页失败: " + str(e))
        return None
    if r.status_code != 200:
        log("首页返回 HTTP " + str(r.status_code))
    m = re.search(r'name="formhash" value="([0-9a-f]{8})"', r.text)
    if not m:
        m = re.search(r'formhash=([0-9a-f]{8})', r.text)
    return m.group(1) if m else None


def login_and_get_cookie(session, user, passwd):
    """账号密码登录兜底：清空旧态 → 取 formhash → POST 登录 → 从 jar 提取 auth+saltkey"""
    if not user or not passwd:
        return None
    try:
        session.cookies.clear()
        r = session.get(BASE + "/member.php?mod=logging&action=login", timeout=20)
        m = re.search(r'name="formhash" value="([0-9a-f]{8})"', r.text)
        if not m:
            m = re.search(r'formhash=([0-9a-f]{8})', r.text)
        if not m:
            log("⚠️ 登录页未找到 formhash（可能 CF 拦截或页面改版）")
            return None
        fh = m.group(1)
        body = ("formhash=" + fh +
                "&username=" + urllib.parse.quote(user) +
                "&password=" + urllib.parse.quote(passwd) +
                "&cookietime=2592000&questionid=0&answer=")
        login_url = (BASE + "/member.php?mod=logging&action=login&loginsubmit=yes"
                     "&infloat=yes&lssubmit=yes&inajax=1")
        session.post(login_url,
                     data=body,
                     headers={"Content-Type": "application/x-www-form-urlencoded",
                              "Referer": BASE + "/"},
                     timeout=20)
        cookie = extract_discuz_cookie(session)
        if cookie:
            log("🔑 账号密码登录成功，已刷新 Cookie")
            return cookie
        log("⚠️ 登录失败（账号密码错误 / 需验证码 / CF 拦截）")
        return None
    except Exception as e:
        log("⚠️ 登录异常: " + str(e))
        return None


def main():
    # 账号密码：单变量 XIJISHE_ACCOUNT（账号#密码 / 账号:密码）
    ACCOUNT = (os.environ.get("XIJISHE_ACCOUNT") or "").strip()
    USER, PASS = "", ""
    if ACCOUNT:
        # 以第一个 # 或 : 分割（账号/密码中一般不含这两个字符）
        sep = "#" if "#" in ACCOUNT else (":" if ":" in ACCOUNT else "#")
        idx = ACCOUNT.find(sep)
        USER = ACCOUNT[:idx].strip()
        PASS = ACCOUNT[idx + 1:].strip()

    session = cf_requests.Session(impersonate="chrome")
    if PROXY:
        session.proxies = {"http": PROXY, "https": PROXY}
        log("🌐 使用代理: " + PROXY)
    else:
        log("🌐 直连（未配置 MY_PROXY）")
    session.headers.update({
        "accept": "*/*",
        "accept-language": "zh-CN,zh;q=0.9",
        "x-requested-with": "XMLHttpRequest",
        "user-agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
    })

    # Cookie 来源：本地缓存为主；无缓存在账号密码登录；都没有报错
    cookie = load_cached_cookie()
    if cookie:
        log("使用本地缓存的 Cookie")
    if not cookie and USER and PASS:
        log("无 Cookie 也无缓存，尝试账号密码登录...")
        cookie = login_and_get_cookie(session, USER, PASS)
        if cookie:
            save_cached_cookie(cookie)
    if not cookie:
        log("❌ 未配置 Cookie（且无账号密码兜底，请配 XIJISHE_ACCOUNT）")
        sys.exit(1)
    put_cookies(session, cookie)

    check_cookie_expire(XIJISHE_EXPIRE)  # 手动过期日检测（不阻断，只提醒）

    # 签到执行（Cookie 失效时账号密码兜底重试一次）
    sign_result = ""
    tried_relogin = False
    for attempt in range(2):
        formhash = get_formhash(session)
        if not formhash:
            if USER and PASS and not tried_relogin:
                tried_relogin = True
                log("Cookie 可能已过期，尝试账号密码重新登录...")
                nc = login_and_get_cookie(session, USER, PASS)
                if nc:
                    save_cached_cookie(nc)
                    put_cookies(session, nc)
                    continue
                log("Cookie 已过期且登录兜底失败，请检查账号密码")
                sys.exit(1)
            log("⚠️ 未提取到 formhash（Cookie 失效或页面改版）")
            sys.exit(1)
        log("formhash = " + formhash)

        url = (BASE + "/k_misign-sign.html?operation=qiandao"
               "&format=global_usernav_extra&formhash=" + formhash +
               "&inajax=1&ajaxtarget=k_misign_topb")
        try:
            r = session.get(url, headers={"Referer": BASE + "/"}, timeout=20)
        except Exception as e:
            log("签到请求失败: " + str(e))
            sys.exit(1)

        text = re.sub(r"\s+", " ", r.text)
        log("HTTP " + str(r.status_code))

        if "成功" in text or "签到成功" in text:
            sign_result = "success"
            break
        elif "已签到" in text or "今日已" in text or "已经签到" in text:
            sign_result = "already"
            break
        elif "登录" in text or "未登录" in text or "login" in text.lower():
            if USER and PASS and not tried_relogin:
                tried_relogin = True
                log("Cookie 已过期，尝试账号密码重新登录后续签...")
                nc = login_and_get_cookie(session, USER, PASS)
                if nc:
                    save_cached_cookie(nc)
                    put_cookies(session, nc)
                    continue
                log("❌ Cookie 已过期，请检查账号密码")
                sys.exit(1)
            log("❌ 貌似未登录，请检查账号密码 / 站点验证码")
            sys.exit(1)
        else:
            log("❓ 结果未知")
            sys.exit(1)

    if sign_result == "success":
        log("✅ 签到成功")
    elif sign_result == "already":
        log("ℹ️ 今日已签到")


if __name__ == "__main__":
    main()
