#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# =========================================================
# name:  中免会员（cdf）
# 接口：/api/session/wxSession/v2（登录）+ /api/user/sign（签到）
# 青龙环境变量：YYB_SERVER = yyb-go:8000@1  多账号一行一条
# 新手配置：文件顶部 YYB_ONLY_REFS / SIGN_LNG / SIGN_LAT
# cron: 1 12,20 * * *

=========================================================

import os, re, time, random, traceback, json
import requests

# ============== 新手配置区 ==============
YYB_ONLY_REFS = ["1", "2"]   # 填上前两个账号的 ref 值
APP_ID = "wxdf26125d1f97992c"
SIGN_LNG = ""
SIGN_LAT = ""
CDF_VERSION = "5.5.98"
APP_VERSION_PATH = "176"
HOST = "cdfmbrapi.cdfg.com.cn"
UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 16_1_2 like Mac OS X) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Mobile/15E148 MicroMessenger/8.0.75(0x18004b66) NetType/WIFI Language/zh_CN")
# ========================================

TOKEN_FILE = "cdf_token.json"
LOGIN_URL       = "https://" + HOST + "/api/session/wxSession/v2"
SIGN_URL        = "https://" + HOST + "/api/user/sign"
SIGN_RECORD_URL = "https://" + HOST + "/api/user/signRecord"
SIGN_TEXT_URL   = "https://" + HOST + "/api/user/signText"

# ———————————— 美化小工具 ————————————
_BAR = "─" * 42
def _box(title, width=40):
    """打印一个圆角小盒子，包住一行标题"""
    pad = width - 2 - len(title)
    print("╭" + _BAR + "╮")
    if pad >= 0:
        left = pad // 2
        right = pad - left
        print("│" + (" " * left) + title + (" " * right) + "│")
    else:
        print("│ " + title + " │")
    print("╰" + _BAR + "╯")

def log(msg):
    """普通日志（左边一个小竖点对齐）"""
    print("· " + msg)

def today_str():
    y, m, d = time.localtime()[:3]
    # 抓包里是 2026-9-2 格式，对齐返回（日期匹配 + 显示）
    return (
        str(y) + "-" + str(m) + "-" + str(d),
        str(y) + "-" + str(m).zfill(2) + "-" + str(d).zfill(2),
        time.strftime("%Y-%m-%d"),
    )

# ———————————— 基础组件（和上版一样但更精简） ————————————
def _base_dir():
    try:
        return os.path.dirname(os.path.abspath(__file__))
    except Exception:
        return os.getcwd()

def load_tokens():
    p = os.path.join(_base_dir(), TOKEN_FILE)
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_tokens(obj):
    p = os.path.join(_base_dir(), TOKEN_FILE)
    try:
        with open(p, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

class YYBClient:
    def __init__(self, appid):
        self.appid = appid
    @staticmethod
    def parse_entry(entry):
        entry = (entry or "").strip()
        if "@" not in entry:
            raise ValueError("格式应为 host:port@ref，实际: " + entry)
        server, ref = entry.rsplit("@", 1)
        server = server.strip().rstrip("/")
        if not server.startswith("http"):
            server = "http://" + server
        return server, ref.strip()
    def entries(self):
        raw = os.getenv("YYB_SERVER") or ""
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                server, ref = self.parse_entry(line)
                if YYB_ONLY_REFS and ref not in YYB_ONLY_REFS:
                    continue
                yield server, ref, line
            except ValueError:
                pass
    def get_code(self, server, ref):
        try:
            s = requests.Session(); s.trust_env = False
            r = s.post(server + "/wxapp/getCode",
                       json={"ref": ref, "app_id": self.appid}, timeout=(10, 90))
            r.raise_for_status()
            body = r.json()
            if isinstance(body.get("code"), int) and body["code"] not in (0, None):
                raise RuntimeError(body.get("msg") or str(body))
            data = body.get("data") or {}
            result = body.get("result") or (isinstance(data, dict) and data.get("result")) or {}
            code = (isinstance(result, dict) and result.get("code")) or None
            if not code:
                raise RuntimeError("返回空 code")
            openid = (isinstance(data, dict) and data.get("openid")) or None
            return code, openid
        except Exception as e:
            return None, str(e)

def _common_headers(token=None, page="pages/main/main"):
    h = {
        "Host": HOST,
        "User-Agent": UA,
        "Referer": ("https://servicewechat.com/" + APP_ID + "/"
                    + APP_VERSION_PATH + "/page-frame.html"),
        "pageUrl": page,
        "cdf-v": CDF_VERSION,
        "Accept-Encoding": "gzip,compress,br,deflate",
    }
    if token:
        h["x-access-token"] = token
    return h

def _parse(resp):
    try:
        return resp.json()
    except Exception:
        pass
    m = re.search(r"\{.*\}", (resp.text or "").strip(), re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None

def _ok(jr):
    """返回 (ok, msg, already)"""
    if not isinstance(jr, dict):
        return False, "响应非 JSON", False
    code = jr.get("code")
    msg = (jr.get("msg") or "") + " " + (jr.get("message") or "")
    success = jr.get("success")
    already = any(k in msg for k in ["已签", "重复", "已经", "already", "repeat", "明天再来", "无需重复"])
    if already:
        return True, msg.strip() or "今日已签到", True
    if success is False:
        return False, msg.strip() or "success=false", False
    if code != 1:
        return False, msg.strip() or ("code=" + str(code)), False
    return True, msg.strip() or "成功", False

def login_with_code(wx_code):
    h = _common_headers(token=None, page="pages/main/main")
    h["content-type"] = "application/x-www-form-urlencoded"
    body = "code=" + requests.utils.quote(wx_code) + "&moduleId=&moduleType="
    try:
        r = requests.post(LOGIN_URL, data=body, headers=h, timeout=(10, 30))
    except Exception as e:
        return None, "请求异常: " + str(e)
    jr = _parse(r)
    ok, msg, _ = _ok(jr)
    if not ok:
        return None, msg or ("HTTP " + str(r.status_code))
    data = jr.get("data") if isinstance(jr.get("data"), dict) else {}
    token = data.get("token")
    if not token:
        return None, "登录响应里没拿到 token"
    return token, None

def token_still_good(token):
    h = _common_headers(token, page="packages/game/signin/signin")
    h["content-type"] = "application/x-www-form-urlencoded"
    try:
        r = requests.post(SIGN_TEXT_URL, data="next=0", headers=h, timeout=(10, 25))
        jr = _parse(r)
        ok, msg, _ = _ok(jr)
        if ok:
            return True
        if any(k in (msg or "").lower() for k in ["login", "token", "auth", "过期", "未登录"]):
            return False
        return True
    except Exception:
        return False

def do_sign(token):
    h = _common_headers(token, page="packages/game/signin/signin")
    h["content-type"] = "application/x-www-form-urlencoded"
    data = ("repairSignDate=&randomizedIdMap=&lng=" + SIGN_LNG
            + "&lat=" + SIGN_LAT + "&repairSignType=")
    extra_lines = []
    try:
        r = requests.post(SIGN_URL, data=data, headers=h, timeout=(10, 30))
        jr = _parse(r)
    except Exception as e:
        return "异常", "请求异常: " + str(e), extra_lines
    ok, msg, already = _ok(jr)
    if already:
        status = "已签"
    elif ok:
        status = "成功"
    else:
        status = "失败"
    if isinstance(jr, dict) and isinstance(jr.get("data"), dict):
        d = jr["data"]
        for k in ["signDay", "totalDays", "continueSignDays",
                  "points", "score", "balance", "prize", "giftName"]:
            if d.get(k) not in (None, "", []):
                extra_lines.append(("奖励" if k in ("points","score","prize","giftName","balance") else "进度")
                                   + " · " + k + ": " + str(d[k]))
    return status, msg, extra_lines

def get_today_done(token):
    """返回 (today_done, signText 或 None)；拿不到返回 (None, None)"""
    h = _common_headers(token, page="packages/game/signin/signin")
    h["content-type"] = "application/x-www-form-urlencoded"
    try:
        r = requests.post(SIGN_RECORD_URL, data="next=0", headers=h, timeout=(10, 30))
    except Exception:
        return None, None
    jr = _parse(r)
    ok, _, _ = _ok(jr)
    if not ok or not isinstance(jr, dict) or not isinstance(jr.get("data"), dict):
        return None, None
    forms = today_str()
    for rec in (jr["data"].get("signRecords") or []):
        if str(rec.get("date")) in forms:
            return rec.get("signIn"), rec.get("signText")
    return None, None

def ensure_token(server, ref):
    """返回 token 字符串；失败返回 None"""
    key = server.rsplit("://", 1)[-1] + "@" + str(ref)
    cache = load_tokens()
    old = cache.get(key, {}) if isinstance(cache.get(key), dict) else {}
    token = old.get("token") if old else None
    if token and token_still_good(token):
        return token, "缓存"
    code, err = YYBClient(APP_ID).get_code(server, ref)
    if not code:
        return None, "YYB取码失败: " + str(err)
    token, err = login_with_code(code)
    if not token:
        return None, "登录失败: " + str(err)
    cache.setdefault(key, {})
    cache[key]["token"] = token
    cache[key]["ref"] = ref
    cache[key]["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    save_tokens(cache)
    return token, "新登录"

def _status_emoji_and_tag(status):
    return {
        "成功":   ("✅", "签到成功"),
        "已签":   ("🟡", "今日已签"),
        "失败":   ("❌", "签到失败"),
        "异常":   ("⚠️", "请求异常"),
    }.get(status, ("❔", status))

def run_account(server, ref):
    _box("账号  ref = " + str(ref))
    token, status_msg = ensure_token(server, ref)
    if not token:
        # 整个账号失败的情况，这里单独美化一行大字 + 错误详情
        print("│ ❌ 账号不可用")
        print("│ · 原因: " + str(status_msg))
        print()
        return False
    # 登录状态一行简注（一般是缓存/新登录，不展开避免啰嗦）
    print("├ 登录态 · " + status_msg)
    t_short, t_long, _ = today_str()
    before, txt = get_today_done(token)
    sym1 = "✅" if before else "⭕"
    log("签到前 · 今日(" + t_short + ") " + sym1
        + ("  signText=" + str(txt) if txt is not None else ""))
    status, msg, extra = do_sign(token)
    emoji, tag = _status_emoji_and_tag(status)
    print("│ " + emoji + " " + tag + "  ──  服务端 msg: " + (msg or ""))
    for line in extra:
        print("│     🎁 " + line)
    after, txt2 = get_today_done(token)
    sym2 = "✅" if after else "⭕"
    log("签到后 · 今日(" + t_short + ") " + sym2
        + ("  signText=" + str(txt2) if txt2 is not None else ""))
    # 如果之前没签 / 之后签了，加一行 🎉 高亮
    if before is False and after is True:
        print("│ 🎉 本账号首次签到成功！")
    print()
    return True

def main():
    entries = list(YYBClient(APP_ID).entries())
    total = len(entries)
    tday, _, _ = today_str()

    # ══════════ 顶部标题盒 ══════════
    print()
    print("╔" + ("═" * 42) + "╗")
    title = "中免会员小程序 · 每日签到"
    pad = 42 - 2 - len(title)
    print("║" + (" " * (pad // 2)) + title + (" " * (pad - pad // 2)) + "║")
    sub = "日期 " + tday + "   共 " + str(total) + " 个账号"
    pad2 = 42 - 2 - len(sub)
    print("║" + (" " * (pad2 // 2)) + sub + (" " * (pad2 - pad2 // 2)) + "║")
    print("╚" + ("═" * 42) + "╝")
    print()

    if not entries:
        print("⚠️ 没有可执行账号（青龙环境变量 YYB_SERVER 空，或被 YYB_ONLY_REFS 过滤空）")
        return

    success = 0
    for i, (server, ref, _) in enumerate(entries, start=1):
        try:
            if run_account(server, ref):
                success += 1
        except Exception as e:
            print("│ ❌ 账号异常: " + str(e))
            print(traceback.format_exc())
            print()
        if i < total:
            wait = random.randint(3, 8)
            # 不打印休息，避免啰嗦；真卡住了用户能从执行计时看在等
            time.sleep(wait)

    # ══════════ 底部结果盒 ══════════
    print("╔" + ("═" * 42) + "╗")
    ok_rate = str(success) + " / " + str(total)
    if success == total:
        tail_title = "🎉 全部完成  " + ok_rate
    else:
        tail_title = "📋 执行结束  " + ok_rate
    pad3 = 42 - 2 - len(tail_title)
    print("║" + (" " * (pad3 // 2)) + tail_title + (" " * (pad3 - pad3 // 2)) + "║")
    print("╚" + ("═" * 42) + "╝")
    print()

if __name__ == "__main__":
    main()