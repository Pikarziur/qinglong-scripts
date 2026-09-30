#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
南+论坛 (bbs.south-plus.org) 日常任务脚本
论坛架构：phpwind (PW)
name：南+论坛
cron：10 0,12 * * *
============================================================================
基于用户提供的 HAR 逆向，关键请求（均已实锤对应 HAR 条目）：

  1. 验证码  GET  /ck.php?nowtime=<ms>
             -> 返回验证码图片（~15KB），用 ddddocr 识别出 gdcode
  2. 登录    POST /login.php?
             body: lgt=2&pwuser=<邮箱>&pwpwd=<密码>&gdcode=<识别码>
                   &hideid=0&forward=<跳回地址>&jumpurl=<跳回地址>
                   &step=2&cktime=31536000
             -> 成功时返回 <meta http-equiv="refresh" ...> 跳转页
  3. 任务页  GET  /plugin.php?H_name-tasks.html
             -> 页面 JS 含 var verifyhash = '82511105';（任务接口所需的 verify）
  4. 领任务  GET  /plugin.php?H_name=tasks&action=ajax&actions=job&cid=15
                   &nowtime=<ms>&verify=<verifyhash>
             -> <ajax><![CDATA[success 已经申请[日常]完成,请赶紧去完成任务吧!]]></ajax>
  5. 领奖励  GET  /plugin.php?H_name=tasks&action=ajax&actions=job2&cid=15
                   &nowtime=<ms>&verify=<verifyhash>
             -> <ajax><![CDATA[success 你[日常]已经完成! 15]]></ajax>

注：日常任务 cid 在 HAR 中实证为 15（"已申请[日常]"），如需适配其他任务改 CID 即可。
    日常任务为"每日登录"类，登录即完成，故 job 之后可直接 job2，无需中间手动动作。

环境变量：
  SOUTHPLUS_ACCOUNT       必填(推荐)。格式 邮箱#密码，如 your@mail.com#yourpass
  MY_PROXY                必填。请求代理，如 http://127.0.0.1:7890
                         南+ 不代理无法访问，未配置将直接报错退出
  SOUTHPLUS_COOKIE_CACHE  可选。Cookie 缓存文件路径，默认 /ql/data/southplus.cookies
                         （青龙持久目录，订阅更新不受影响；可覆盖）
  SOUTHPLUS_NOTIFY       可选。通知开关，默认开启；填 0/false/off/no 关闭

依赖：requests、ddddocr（pip install ddddocr）
============================================================================
"""

import os
import sys
import json
import time
import random
import requests

SITE = "https://bbs.south-plus.org"
CID = 15  # 日常任务 cid（HAR 实证）
TASK_PAGE = SITE + "/plugin.php?H_name-tasks.html"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36")

CACHE = os.environ.get("SOUTHPLUS_COOKIE_CACHE") or "/ql/data/southplus.cookies"


def notify_enabled():
    v = (os.environ.get("SOUTHPLUS_NOTIFY") or "").strip().lower()
    return v not in ("0", "false", "off", "no")


def log(msg):
    print(msg)
    sys.stdout.flush()


def slog(msg):
    """失败/异常日志（默认也通知）"""
    print("❌ " + msg)
    sys.stdout.flush()
    if notify_enabled():
        try:
            from notify import send  # 青龙 notify 模块（如有）
            send("南+任务", msg)
        except Exception:
            pass


# ---------- 缓存 Cookie（requests cookie jar <-> json） ----------
def load_cookies(path):
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data.get("cookies", {})
    except Exception:
        pass
    return {}


def save_cookies(path, cookiejar):
    try:
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        d2 = {"cookies": dict(cookiejar)}
        with open(path, "w", encoding="utf-8") as f:
            json.dump(d2, f)
        try:
            os.chmod(path, 0o600)
        except Exception:
            pass
        log("💾 已把登录 Cookie 缓存到本地文件")
    except Exception as e:
        log("⚠️ Cookie 缓存写入失败: " + str(e))


# ---------- ddddocr 延迟初始化 ----------
_OCR = None


def get_ocr():
    global _OCR
    if _OCR is None:
        try:
            import ddddocr
            _OCR = ddddocr.DdddOcr(show_ad=False)
        except ImportError:
            slog("未安装 ddddocr，请先 pip install ddddocr")
            sys.exit(1)
        except Exception as e:
            slog("ddddocr 初始化失败: " + str(e))
            sys.exit(1)
    return _OCR


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


# ---------- 登录（带验证码识别重试） ----------
def login(session, user, pwd, max_try=6):
    # 先访问一个普通页面初始化会话（贴合 HAR 中 thread.php 前置访问）
    try:
        session.get(SITE + "/thread.php?fid-9.html", timeout=30)
    except Exception:
        pass

    ocr = get_ocr()
    for attempt in range(1, max_try + 1):
        try:
            # 取验证码图片（每次重新生成）
            ck_url = SITE + "/ck.php?nowtime=" + str(int(time.time() * 1000))
            img = session.get(ck_url, timeout=30).content
            code = ocr.classification(img).strip()
            log(f"   验证码识别(第{attempt}次): {code}")

            data = {
                "lgt": "2",
                "pwuser": user,
                "pwpwd": pwd,
                "gdcode": code,
                "hideid": "0",
                "forward": SITE + "/thread.php?fid-9.html",
                "jumpurl": SITE + "/thread.php?fid-9.html",
                "step": "2",
                "cktime": "31536000",
            }
            r = session.post(SITE + "/login.php?", data=data,
                             allow_redirects=False, timeout=30)
            html = r.text
            # 登录成功：phpwind 返回 <meta http-equiv="refresh" ...> 跳转页
            if "refresh" in html:
                return True
            if "验证码" in html:
                log(f"   验证码识别错误，重试")
                continue
            if "用户名或密码" in html or "密码错误" in html:
                log(f"   账号或密码错误，停止重试")
                return False
            log(f"   登录失败(第{attempt}次)，重试")
        except Exception as e:
            log(f"   登录异常(第{attempt}次): {e}")
    return False


# ---------- 领任务 / 领奖励 ----------
def call_task_api(session, action, verify):
    url = (SITE + "/plugin.php?H_name=tasks&action=ajax"
           f"&actions={action}&cid={CID}"
           f"&nowtime={int(time.time() * 1000)}&verify={verify}")
    r = session.get(url, timeout=30)
    return r.text


def do_job(session, verify):
    log("🔧 申请日常任务...")
    txt = call_task_api(session, "job", verify)
    ok = "success" in txt
    log("   响应: " + txt[:160].replace("\n", " "))
    return ok


def do_job2(session, verify):
    log("🎁 领取日常任务奖励...")
    txt = call_task_api(session, "job2", verify)
    ok = "success" in txt
    log("   响应: " + txt[:160].replace("\n", " "))
    return ok


# ---------- 主流程 ----------
def main():
    log("🚀 南+论坛日常任务脚本")

    acc = (os.environ.get("SOUTHPLUS_ACCOUNT") or "").strip()
    if not acc or "#" not in acc:
        slog("未配置 SOUTHPLUS_ACCOUNT（格式：邮箱#密码）")
        return
    user, pwd = acc.split("#", 1)

    proxy = (os.environ.get("MY_PROXY") or "").strip()
    if not proxy:
        slog("未配置 MY_PROXY，南+ 不代理无法访问，请先配置代理")
        return

    session = build_session(proxy)

    # 载入缓存 Cookie
    session.cookies.update(load_cookies(CACHE))

    # 检查登录态
    try:
        html = fetch_tasks_page(session)
    except Exception as e:
        slog("访问任务页失败（代理/网络异常）: " + str(e))
        return

    if is_logged_in(html):
        log("✅ 使用缓存 Cookie，已处于登录态")
    else:
        log("🔑 缓存无效，开始账号密码登录（含验证码识别）...")
        if not login(session, user, pwd):
            slog("登录失败：验证码多次识别错误或账号密码有误，请检查 SOUTHPLUS_ACCOUNT")
            return
        save_cookies(CACHE, session.cookies)
        log("✅ 登录成功")
        try:
            html = fetch_tasks_page(session)
        except Exception:
            pass

    verify = extract_verifyhash(html)
    if not verify:
        slog("未能从任务页提取 verifyhash，登录态可能异常")
        return
    log(f"🔖 verifyhash = {verify}")

    j1 = do_job(session, verify)
    time.sleep(random.uniform(1.0, 2.5))
    j2 = do_job2(session, verify)

    if j1 and j2:
        log("🏁 日常任务：申请 + 领奖 均成功")
    elif j1 and not j2:
        log("⚠️ 任务已申请，但领奖未成功（可能任务尚未完成或已领过）")
    else:
        slog("日常任务执行未完全成功，请查看上方响应")


if __name__ == "__main__":
    main()
