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
    XIJISHE_COOKIE  浏览器复制的完整 Cookie（有 saltkey + auth 即可，
                    cf_clearance 会被自动剥离、由 curl_cffi 重新获取）
    MY_PROXY         可选，HTTP/HTTPS 代理地址，如 http://127.0.0.1:7890
                     或 socks5://127.0.0.1:7890；留空则直连
    XIJISHE_EXPIRE     可选，Cookie 预期过期日，如 2026-10-15。设了会提前3天提醒
    XIJISHE_NOTIFY      可选，通知开关，默认开启；填 0/false/off/no 关闭

依赖 (青龙依赖管理里加一行):  curl_cffi
    （用于模拟 Chrome 过 Cloudflare，否则会被 403 拦）

用法:
    青龙 → 新建任务 → 命令:  python3 xsijishe_sign.py
    定时:  0 6 * * *   (每天 6 点)

🚀 Cookie 一键获取（登录 xsijishe.com 后 F12 → Console 执行）:

    // 方法 A: 复制全部 Cookie（Discuz 所有字段，cf_clearance 脚本会自动剥离）
    copy(document.cookie)

    // 方法 B: 精准抓 Discuz 登录关键字段（推荐，干净不冗余）
    copy((()=>{const m=document.cookie.match(/([a-zA-Z0-9_]+)_(auth|saltkey)=([^;]+)/g)||[];return m.join("; ");})())

    // 方法 C: Application 面板 → Cookies → xsijishe.com → Value 列全选复制合并

Discuz Cookie 结构说明:
    - {前缀}_auth     登录凭证（必须，HttpOnly）
    - {前缀}_saltkey   盐值（必须，HttpOnly）
    - 前缀是 Discuz 随机生成的，每个站点不同
    - cf_clearance    Cloudflare 验证 Cookie（脚本会剥离，curl_cffi 自动重新获取）
"""
import os
import re
import sys

try:
    from curl_cffi import requests as cf_requests
except ImportError:
    print("❌ 缺少依赖 curl_cffi，请在青龙依赖管理安装：pip install curl_cffi")
    sys.exit(1)

BASE = "https://xsijishe.com"
COOKIE = (os.environ.get("XIJISHE_COOKIE") or "").strip()
PROXY = (os.environ.get("MY_PROXY") or "").strip()
XIJISHE_EXPIRE = (os.environ.get("XIJISHE_EXPIRE") or "").strip()  # 可选，如 "2026-10-15"
XIJISHE_NOTIFY = os.environ.get("XIJISHE_NOTIFY", "1").strip().lower() not in ("0", "false", "off", "no")


def log(msg):
    print(msg, flush=True)

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
    if not COOKIE:
        log("❌ 未配置环境变量 XIJISHE_COOKIE")
        sys.exit(1)

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
    put_cookies(session, COOKIE)

    check_cookie_expire(XIJISHE_EXPIRE)  # 手动过期日检测（不阻断，只提醒）
    formhash = get_formhash(session)
    if not formhash:
        log("⚠️ 未提取到 formhash（Cookie 可能失效或页面改版，请重新抓 cookie）")
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
    log("响应片段: " + text[:400])

    if "成功" in text or "签到成功" in text:
        log("✅ 签到成功")
    elif "已签到" in text or "今日已" in text or "已经签到" in text:
        log("ℹ️ 今日已签到")
    elif "登录" in text or "未登录" in text or "login" in text.lower():
        log("❌ 貌似未登录，请更新 XIJISHE_COOKIE")
    else:
        log("❓ 结果未知，请查看上方响应片段")


if __name__ == "__main__":
    main()
