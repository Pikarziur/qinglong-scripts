#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# =========================================================
# name:  南+ - 签到
# cron: 10 7,16 * * *
# =========================================================
"""
South+ bbs.south-plus.org 每日论坛任务自动完成 + 自动登录 - 单文件版
============================================================
任务流程（cid=14 为例）:
    job → job2 → job 三步，共用 verify 令牌。

Cookie 自动管理（无需任何手动 cookie 变量）:
   脚本用 SOUTHPLUS_USER + SOUTHPLUS_PASS 自动登录，把 cookie 缓存到同目录
   southplus_cookie.txt；之后每次运行优先用缓存，过期才重新登录。

两种用法:
  ① 定时/正常签到:   python3 southplus.py
       首次运行自动登录并缓存 cookie；cookie 失效时自动重新登录，全程无需介入。
  ② 手动触发登录/验证: python3 southplus.py login
       立即用账号密码登录，保存 cookie 到文件（用于首次配置或验证账号/代理是否可用）。

重要: 南+ 是 GFW 封锁站点，必须走代理，请在青龙配置 MY_PROXY
      （如 http://192.168.31.233:7890）。

环境变量:
    SOUTHPLUS_USER    南+ 账号（必须，用于自动登录获取/刷新 cookie）
    SOUTHPLUS_PASS    南+ 密码（必须）
    SOUTHPLUS_CID     要完成的任务 cid，默认 "14"
    MY_PROXY          可选 HTTP/SOCKS 代理，留空直连

依赖: curl_cffi  （青龙依赖管理加一行: curl_cffi）
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
CID = (os.environ.get("SOUTHPLUS_CID") or "14").strip()
PROXY = (os.environ.get("MY_PROXY") or "").strip()
USER = (os.environ.get("SOUTHPLUS_USER") or "").strip()
PWD = (os.environ.get("SOUTHPLUS_PASS") or "").strip()
COOKIE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "southplus_cookie.txt")

HEADERS = {
    "accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
               "image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7"),
    "accept-language": "zh-CN,zh;q=0.9",
    "upgrade-insecure-requests": "1",
    "user-agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
}


def log(msg):
    print(msg, flush=True)


# ---------- cookie 读写 ----------
def put_cookies(session, cookie_str):
    """把字符串 cookie 写进 session 的 jar（domain 绑 south-plus.org，剥 cf_clearance）"""
    for part in cookie_str.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, v = part.split("=", 1)
        if k.strip().lower() == "cf_clearance":
            continue
        session.cookies.set(k.strip(), v.strip(), domain=".south-plus.org")


def build_cookie_str(session):
    """从 session 的 cookie jar 重建 cookie 字符串（剥 cf_clearance）"""
    parts = []
    for c in session.cookies:
        if c.domain and "south-plus.org" in c.domain and c.name.lower() != "cf_clearance":
            parts.append(c.name + "=" + c.value)
    return "; ".join(parts)


def save_cookie(cookie_str):
    try:
        with open(COOKIE_FILE, "w", encoding="utf-8") as f:
            f.write(cookie_str)
        log("💾 已缓存 cookie 到 " + COOKIE_FILE)
    except Exception as e:
        log("⚠️ 缓存 cookie 失败: " + str(e))


def load_cookie():
    if os.path.exists(COOKIE_FILE):
        try:
            return open(COOKIE_FILE, "r", encoding="utf-8").read().strip()
        except Exception:
            return ""
    return ""


def build_session(cookie_str, proxy):
    session = cf_requests.Session(impersonate="chrome")
    if proxy:
        session.proxies = {"http": proxy, "https": proxy}
        log("🌐 使用代理: " + proxy)
    else:
        log("🌐 直连（未配置 MY_PROXY）")
    session.headers.update(HEADERS)
    if cookie_str:
        put_cookies(session, cookie_str)
    return session


# ---------- 账号密码登录（Discuz!） ----------
def login_and_get_cookie(user, pwd, proxy=""):
    """用账号密码登录南+，成功返回 cookie 字符串，失败返回 None 并打印诊断。"""
    session = cf_requests.Session(impersonate="chrome")
    if proxy:
        session.proxies = {"http": proxy, "https": proxy}
    session.headers.update(HEADERS)

    # 1) 取登录页，拿 formhash（Discuz! 的 CSRF 令牌）
    try:
        r = session.get(BASE + "/member.php?mod=logging&action=login", timeout=25)
    except Exception as e:
        print("❌ 登录页获取失败: " + str(e))
        return None
    print("登录页 HTTP " + str(r.status_code) + " 长度 " + str(len(r.text)))
    m = re.search(r'name="formhash" value="([0-9a-f]{8})"', r.text)
    if not m:
        m = re.search(r"formhash=([0-9a-f]{8})", r.text)
    formhash = m.group(1) if m else None
    if not formhash:
        low = r.text.lower()
        if "cf-" in low or "challenge" in low or "just a moment" in low:
            print("⚠️ 登录页被 Cloudflare 拦截，代理出口 IP 可能被标记，请换 mihomo 节点")
        else:
            print("⚠️ 未提取到 formhash，可能页面改版或登录页结构变化")
        return None
    print("formhash = " + formhash)

    # 2) 提交登录（Discuz! ajax 登录）
    data = {
        "formhash": formhash,
        "username": user,
        "password": pwd,
        "cookietime": "2592000",
        "loginfield": "username",
        "questionid": "0",
        "answer": "",
    }
    url = (BASE + "/member.php?mod=logging&action=login&loginsubmit=yes"
           "&handlekey=login&infloat=yes&inajax=1")
    try:
        r2 = session.post(url, data=data,
                          headers={"Referer": BASE + "/member.php?mod=logging&action=login"},
                          timeout=25)
    except Exception as e:
        print("❌ 登录提交失败: " + str(e))
        return None
    txt = re.sub(r"\s+", " ", r2.text)
    print("登录提交 HTTP " + str(r2.status_code) + " | 片段: " + txt[:400])

    if "验证码" in txt or "seccode" in txt.lower() or "captcha" in txt.lower():
        print("⚠️ 触发验证码，自动登录无法进行，请改用浏览器登录后手动维护 southplus_cookie.txt")
        return None
    if "密码错误" in txt or "登录失败" in txt:
        print("⚠️ 账号或密码错误，请检查 SOUTHPLUS_USER / SOUTHPLUS_PASS")
        return None
    if "succeed" in txt.lower() or "登录成功" in txt or "欢迎" in txt or "winduser" in txt.lower():
        print("✅ 登录成功")
    else:
        print("ℹ️ 未明确识别登录结果，继续尝试取 cookie（若下方 cookie 为空则需手动）")

    cookie = build_cookie_str(session)
    if not cookie:
        print("⚠️ 登录后未拿到任何 cookie，可能登录未真正成功")
        return None
    return cookie


# ---------- 任务流程 ----------
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
    if not m:
        # 诊断：把返回内容前 600 字符打出来，便于判断是未登录 / CF 拦截 / 页面改版
        snippet = re.sub(r"\s+", " ", r.text)[:600]
        log("诊断-任务页 HTTP " + str(r.status_code) + " 长度 " + str(len(r.text)))
        log("诊断-内容片段: " + snippet)
        low = r.text.lower()
        if "登录" in r.text or "login" in low or "请先登录" in r.text:
            log("诊断-疑似【未登录 / Cookie 失效】，将尝试自动登录刷新")
        elif "cf-" in low or "challenge" in low or "just a moment" in low or "verify you are human" in low:
            log("诊断-疑似【被 Cloudflare 拦截】，可能代理出口 IP 被标记")
        else:
            log("诊断-疑似【页面改版】，verify 正则对不上")
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
        return "❌ 未登录，请检查 SOUTHPLUS_USER/PASS 或换 mihomo 节点"
    return "❓ 结果未知，请查看上方响应"


# ---------- 两种入口 ----------
def sign_in():
    if not load_cookie() and not (USER and PWD):
        log("❌ 没有缓存 cookie，且未配置 SOUTHPLUS_USER/PASS，无法运行（请先填账号密码）")
        sys.exit(1)

    cookie = load_cookie()

    def refresh():
        if not (USER and PWD):
            return None
        log("🔄 尝试用账号密码自动登录刷新 cookie…")
        newck = login_and_get_cookie(USER, PWD, PROXY)
        if newck:
            save_cookie(newck)
        return newck

    session = build_session(cookie, PROXY)
    verify = get_verify(session)
    if not verify:
        newck = refresh()
        if newck:
            session = build_session(newck, PROXY)
            verify = get_verify(session)
    if not verify:
        log("⚠️ 仍未提取到 verify，请查看上方诊断（多数情况需检查账号密码或换 mihomo 节点）")
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


def capture_only():
    if not USER or not PWD:
        print("❌ 请先在青龙配置 SOUTHPLUS_USER 和 SOUTHPLUS_PASS")
        sys.exit(1)
    ck = login_and_get_cookie(USER, PWD, PROXY)
    if ck:
        print("\n✅ 登录成功，cookie 已保存到 southplus_cookie.txt")
        print("   主脚本（python3 southplus.py）会自动读取，无需其他操作。")
        try:
            with open(COOKIE_FILE, "w", encoding="utf-8") as f:
                f.write(ck)
        except Exception as e:
            print("保存文件失败: " + str(e))
    else:
        print("❌ 未获取到 cookie，请查看上方诊断")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "login":
        capture_only()
    else:
        sign_in()


if __name__ == "__main__":
    main()
