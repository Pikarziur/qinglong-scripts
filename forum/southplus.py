#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# =========================================================
# name:  南+ - 签到
# cron: 10 7,16 * * *
# =========================================================
"""
South+ bbs.south-plus.org 每日论坛任务自动完成 - 青龙脚本
============================================================
从抓包还原的任务流程（cid=14 为例）:
    job   (开始任务)
    job2  (中间步骤)
    job   (领取完成)
三个请求都带 verify 令牌 + nowtime(毫秒时间戳)。

环境变量:
    SOUTHPLUS_COOKIE  浏览器复制的完整 Cookie（关键登录态: eb9e6_winduser + eb9e6_cknum）
                      cf_clearance 会被自动剥离、由 curl_cffi 重新获取
    SOUTHPLUS_CID     要完成的任务 cid，默认 "14"
    MY_PROXY          可选，HTTP/HTTPS 代理，如 http://127.0.0.1:7890 或 socks5://127.0.0.1:7890
                      留空则直连

依赖 (青龙依赖管理里加一行):  curl_cffi
    （用于模拟 Chrome 过 Cloudflare，否则会被 403 拦）

用法:
    青龙 → 新建任务 → 命令:  python3 southplus_tasks.py
    定时:  0 6 * * *   (每天 6 点)
"""
import os
import re
import sys
import time

try:
    from curl_cffi import requests as cf_requests
except ImportError:
    print("❌ 缺少依赖 curl_cffi，请在青龙依赖管理安装：pip install curl_cffi")
    sys.exit(1)

BASE = "https://bbs.south-plus.org"
COOKIE = (os.environ.get("SOUTHPLUS_COOKIE") or "").strip()
CID = (os.environ.get("SOUTHPLUS_CID") or "14").strip()
PROXY = (os.environ.get("MY_PROXY") or "").strip()


def log(msg):
    print(msg, flush=True)


def put_cookies(session, cookie_str):
    """把字符串 cookie 写进 session 的 jar（domain 统一绑 south-plus.org）"""
    for part in cookie_str.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, v = part.split("=", 1)
        # 剥离会过期/冲突的 cf_clearance，交给 curl_cffi 自动获取
        if k.strip().lower() == "cf_clearance":
            continue
        session.cookies.set(k.strip(), v.strip(), domain=".south-plus.org")


def get_verify(session):
    """从任务列表页提取 verify 令牌（8 位 hex，job/job2/job 共用）"""
    try:
        r = session.get(BASE + "/plugin.php?H_name-tasks.html", timeout=20)
    except Exception as e:
        log("访问任务页失败: " + str(e))
        return None
    if r.status_code != 200:
        log("任务页返回 HTTP " + str(r.status_code))
    m = re.search(r'verify=([0-9a-f]{8})', r.text)
    return m.group(1) if m else None


def do_job(session, cid, verify, action):
    nowtime = int(time.time() * 1000)
    url = (BASE + "/plugin.php?H_name=tasks&action=ajax&actions=" + action +
           "&cid=" + str(cid) + "&nowtime=" + str(nowtime) + "&verify=" + verify)
    # job 的 Referer 是任务列表页；job2 的 Referer 是新任务页，按抓包如实还原
    if action == "job2":
        ref = BASE + "/plugin.php?H_name-tasks-actions-newtasks.html.html"
    else:
        ref = BASE + "/plugin.php?H_name-tasks.html"
    try:
        r = session.get(url, headers={"Referer": ref}, timeout=20)
    except Exception as e:
        log("  [" + action + "] 请求失败: " + str(e))
        return None
    return r


def judge(text):
    if "成功" in text or "完成" in text or "已领取" in text:
        return "✅ 成功/已完成"
    if "已经" in text or "已做" in text or "重复" in text:
        return "ℹ️ 已处理过"
    if "登录" in text or "未登录" in text or "login" in text.lower():
        return "❌ 未登录，请更新 SOUTHPLUS_COOKIE"
    return "❓ 结果未知，请查看上方响应"


def main():
    if not COOKIE:
        log("❌ 未配置环境变量 SOUTHPLUS_COOKIE")
        sys.exit(1)

    session = cf_requests.Session(impersonate="chrome")
    if PROXY:
        session.proxies = {"http": PROXY, "https": PROXY}
        log("🌐 使用代理: " + PROXY)
    else:
        log("🌐 直连（未配置 MY_PROXY）")
    session.headers.update({
        "accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
                   "image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7"),
        "accept-language": "zh-CN,zh;q=0.9",
        "upgrade-insecure-requests": "1",
        "user-agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
    })
    put_cookies(session, COOKIE)

    verify = get_verify(session)
    if not verify:
        log("⚠️ 未提取到 verify（Cookie 可能失效或页面改版，请重新抓 cookie）")
        sys.exit(1)
    log("verify = " + verify)

    log("── 处理任务 cid=" + CID)
    for action in ("job", "job2", "job"):
        r = do_job(session, CID, verify, action)
        if r is None:
            continue
        text = re.sub(r"\s+", " ", r.text)
        log("  [" + action + "] HTTP " + str(r.status_code) + " | " + text[:240])
        log("      判定: " + judge(text))
        time.sleep(1.5)  # 接口间留间隔，模拟真人、避免被风控
    log("🏁 任务请求已发出")


if __name__ == "__main__":
    main()
