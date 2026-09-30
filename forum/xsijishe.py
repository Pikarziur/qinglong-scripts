#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# =========================================================
# name:  西集社
# cron: 10 0,12 * * *
# =========================================================
"""
西集社 xsijishe.com 每日签到 - 青龙脚本 (Discuz! misign 插件)
============================================================
环境变量:
    XIJISHE_COOKIE    必填。登录后的 Cookie 字符串（含 Discuz auth/saltkey 字段）
    MY_PROXY         必填，HTTP/HTTPS 代理地址，如 http://192.168.31.233:7890
                   或 socks5://192.168.31.233:7890；西集社需走代理才能访问，未配置将报错退出
    XIJISHE_EXPIRE     可选，Cookie 预期过期日，如 2026-10-15。设了会提前3天提醒

依赖 (青龙依赖管理里加一行):  curl_cffi
    （用于模拟 Chrome 过 Cloudflare，否则会被 403 拦）

🚀 Cookie 获取（登录 xsijishe.com 后 F12 → Console，结果会直接打印，确认有数据后手动复制）：
    console.log(['前缀_auth','前缀_saltkey'].map(k=>{const m=document.cookie.match(new RegExp(k+'=([^;]+)'));return m?k+'='+m[1]:null;}).filter(Boolean).join('; '))
    把"前缀"替换为站点实际 cookie 前缀（F12→Application→Cookies 里看 auth/saltkey 的前缀）。
    注：若 auth/saltkey 被设为 HttpOnly，document.cookie 读不到，打印结果为空——此时改用
        Network → 任意已登录请求 → 右键 Copy as cURL，从 -H 'Cookie: ...' 取完整串。

Discuz Cookie 结构说明:
    - {前缀}_auth     登录凭证（必须，HttpOnly）
    - {前缀}_saltkey   盐值（必须，HttpOnly）
    - 前缀是 Discuz 随机生成的，每个站点不同
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


def log(msg):
    print(msg, flush=True)


def check_cookie_expire(expire_str):
    """手动过期日检测（Discuz Cookie 无内置 exp，需用户手动配 XIJISHE_EXPIRE）"""
    if not expire_str:
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
    elif remain_days <= 3:
        log("🔔 Cookie 即将过期（" + str(remain_days) + "天），建议尽快更新！")


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


def main():
    cookie = (os.environ.get("XIJISHE_COOKIE") or "").strip()
    if not cookie:
        log("❌ 未配置 Cookie（请设置环境变量 XIJISHE_COOKIE）")
        sys.exit(1)

    session = cf_requests.Session(impersonate="chrome")
    if not PROXY:
        log("❌ 未配置 MY_PROXY，西集社需走代理才能访问，请先配置代理")
        sys.exit(1)
    session.proxies = {"http": PROXY, "https": PROXY}
    log("🌐 使用代理: " + PROXY)
    session.headers.update({
        "accept": "*/*",
        "accept-language": "zh-CN,zh;q=0.9",
        "x-requested-with": "XMLHttpRequest",
        "user-agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
    })

    put_cookies(session, cookie)
    check_cookie_expire(XIJISHE_EXPIRE)

    formhash = get_formhash(session)
    if not formhash:
        log("⚠️ 未提取到 formhash（Cookie 失效或页面改版），请更新 XIJISHE_COOKIE")
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
        log("✅ 签到成功")
    elif "已签到" in text or "今日已" in text or "已经签到" in text:
        log("ℹ️ 今日已签到")
    elif "登录" in text or "未登录" in text or "login" in text.lower():
        log("❌ 貌似未登录，请检查/更新 XIJISHE_COOKIE")
        sys.exit(1)
    else:
        log("❓ 结果未知: " + text[:200])
        sys.exit(1)


if __name__ == "__main__":
    main()
