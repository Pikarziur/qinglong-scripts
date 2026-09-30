#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# =========================================================
# name:  南+论坛
# cron: 10 0,12 * * *
# =========================================================
"""
南+论坛 (bbs.south-plus.org) 日常任务脚本
论坛架构：phpwind (PW)
============================================================================
基于用户提供的 HAR 逆向，关键请求：

  1. 任务页  GET  /plugin.php?H_name-tasks.html
             -> 页面 JS 含 var verifyhash = '82511105';（任务接口所需的 verify）
  2. 领任务  GET  /plugin.php?H_name=tasks&action=ajax&actions=job&cid=15
                   &nowtime=<ms>&verify=<verifyhash>
  3. 领奖励  GET  /plugin.php?H_name=tasks&action=ajax&actions=job2&cid=15
                   &nowtime=<ms>&verify=<verifyhash>

注：日常任务 cid 在 HAR 中实证为 15（"已申请[日常]"），如需适配其他任务改 CID 即可。
    日常任务为"每日登录"类，登录即完成，故 job 之后可直接 job2。

环境变量：
  SOUTHPLUS_COOKIE       必填。登录后的 Cookie 字符串（从浏览器复制，含 PW 相关字段）
  MY_PROXY              必填。请求代理，如 http://127.0.0.1:7890
                       南+ 不代理无法访问，未配置将直接报错退出
  SOUTHPLUS_NOTIFY      可选。通知开关，默认开启；填 0/false/off/no 关闭

依赖：requests（pip install requests）

🚀 Cookie 获取（登录 bbs.south-plus.org 后）：
    方式一：F12 → Application → Cookies → 选中站点，手动把全部 Cookie 的 "名称=值"
            拼成 "k=v; k2=v2; ..." 形式
    方式二（推荐，避开 HttpOnly 限制）：F12 → Network → 任意已登录请求 →
            右键 Copy → Copy as cURL，再从其中 -H 'Cookie: ...' 取出整段 Cookie 字符串
    把复制到的字符串填进 SOUTHPLUS_COOKIE 即可。
============================================================================
日志规范：[YYYY-MM-DD HH:MM:SS] [LEVEL] [SOUTHPLUS] message   （LEVEL: INFO / WARN / ERROR）
"""

import os
import sys
import time
import random
import requests
from datetime import datetime

SITE = "https://bbs.south-plus.org"
CID = 15  # 日常任务 cid（HAR 实证）
TASK_PAGE = SITE + "/plugin.php?H_name-tasks.html"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36")


# ========== 统一日志 ==========
def _ts():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def _emit(level, msg):
    print(f"[{_ts()}] [{level}] [SOUTHPLUS] {msg}", flush=True)

def log(msg):  _emit("INFO", msg)
def warn(msg): _emit("WARN", msg)
def err(msg):  _emit("ERROR", msg)


def notify_enabled():
    v = (os.environ.get("SOUTHPLUS_NOTIFY") or "").strip().lower()
    return v not in ("0", "false", "off", "no")


def slog(msg):
    """失败/异常日志（记录并通知）"""
    _emit("ERROR", msg)
    if notify_enabled():
        try:
            from notify import send  # 青龙 notify 模块（如有）
            send("南+任务", msg)
        except Exception:
            pass


def parse_cookie_string(s):
    """把 'k=v; k2=v2' 形式的 Cookie 字符串解析为 dict"""
    d = {}
    for part in s.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, v = part.split("=", 1)
        d[k.strip()] = v.strip()
    return d


# ---------- 会话 ----------
def build_session(proxy):
    s = requests.Session()
    if proxy:
        s.proxies = {"http": proxy, "https": proxy}
    s.headers.update({
        "User-Agent": UA,
        "Referer": SITE + "/",
        "Accept-Language": "zh-CN,zh;q=0.9",
    })
    return s


# ---------- 登录态检测 + 取 verifyhash ----------
def fetch_tasks_page(session):
    r = session.get(TASK_PAGE, timeout=30)
    return r.text


def is_logged_in(html):
    # 已登录的任务页含"退出"链接（action-quit-verify-...html）
    return "action-quit" in html


def extract_verifyhash(html):
    # 页面 JS: var verifyhash = '82511105';
    i = html.find("verifyhash")
    if i < 0:
        return None
    q1 = html.find("'", i)
    if q1 < 0:
        return None
    q2 = html.find("'", q1 + 1)
    if q2 < 0:
        return None
    return html[q1 + 1:q2]


# ---------- 领任务 / 领奖励 ----------
def call_task_api(session, action, verify):
    url = (SITE + "/plugin.php?H_name=tasks&action=ajax"
           f"&actions={action}&cid={CID}"
           f"&nowtime={int(time.time() * 1000)}&verify={verify}")
    r = session.get(url, timeout=30)
    return r.text


def extract_cdata(txt):
    """从 <ajax><![CDATA[...]]></ajax> 抽出可读内容（不展示原始 XML 标签）"""
    i = txt.find("<![CDATA[")
    if i < 0:
        return txt.strip()
    j = txt.find("]]>", i)
    if j < 0:
        return txt[i + 9:].strip()
    return txt[i + 9:j].strip()


def short_resp(txt):
    """响应简要信息（去标签/去制表符/截断），避免完整打印原始 XML"""
    msg = extract_cdata(txt).replace("\t", " ").replace("\n", " ").strip()
    if len(msg) > 60:
        msg = msg[:60] + "..."
    return msg


def do_job(session, verify):
    log("申请日常任务...")
    txt = call_task_api(session, "job", verify)
    if "success" in txt:
        log("申请成功")
        return "ok"
    # 冷却/已申请：如"上次申请[日常]还没超过 18 小时"= 今日已在冷却期内，
    # 即日常任务今日已完成，无需重复申请（视为成功）
    if "还没超过" in txt:
        log("日常任务今日已完成（冷却期内，无需重复申请）")
        return "done"
    warn("申请未成功: " + short_resp(txt))
    return "fail"


def do_job2(session, verify, job_status):
    log("领取日常任务奖励...")
    txt = call_task_api(session, "job2", verify)
    if "success" in txt:
        log("领奖成功")
        return "ok"
    # 若申请已是冷却/已完成态，则领奖返回"未申请任务"属预期（已领过），不算失败
    if job_status == "done" and "未申请任务" in txt:
        log("奖励今日已领取（无需重复领取）")
        return "done"
    warn("领奖未成功: " + short_resp(txt))
    return "fail"


# ---------- 主流程 ----------
def main():
    log("南+论坛日常任务脚本")

    cookie = (os.environ.get("SOUTHPLUS_COOKIE") or "").strip()
    if not cookie:
        slog("未配置 SOUTHPLUS_COOKIE（请设置环境变量为登录后的 Cookie 字符串）")
        return

    proxy = (os.environ.get("MY_PROXY") or "").strip()
    if not proxy:
        slog("未配置 MY_PROXY，南+ 不代理无法访问，请先配置代理")
        return

    session = build_session(proxy)

    # 载入 Cookie（绑定站点 domain）
    for k, v in parse_cookie_string(cookie).items():
        session.cookies.set(k, v, domain=".south-plus.org")

    # 检查登录态
    try:
        html = fetch_tasks_page(session)
    except Exception as e:
        slog("访问任务页失败（代理/网络异常）: " + str(e))
        return

    if not is_logged_in(html):
        slog("Cookie 未登录或已失效，请更新 SOUTHPLUS_COOKIE")
        return
    log("使用 SOUTHPLUS_COOKIE，已处于登录态")

    verify = extract_verifyhash(html)
    if not verify:
        slog("未能从任务页提取 verifyhash，登录态可能异常")
        return
    log("verifyhash = " + verify)

    j1 = do_job(session, verify)
    time.sleep(random.uniform(1.0, 2.5))
    j2 = do_job2(session, verify, j1)

    if j1 == "done":
        # 申请已是冷却/已完成态 → 今日任务确定已完成，领奖失败也属预期
        log("日常任务：今日已完成（无需重复操作）")
    elif j1 == "ok" and j2 in ("ok", "done"):
        log("日常任务：申请 + 领奖 成功")
    elif j1 == "ok" and j2 == "fail":
        warn("任务已申请，但领奖未成功（可能任务尚未完成）")
    elif j1 == "fail":
        slog("日常任务申请失败，请查看上方响应")
    else:
        slog("日常任务执行未完全成功，请查看上方响应")


if __name__ == "__main__":
    main()
