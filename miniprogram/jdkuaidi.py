#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# name: 京东快递
# cron: 45 5,15 * * *
"""
青龙环境变量：
  YYB_SERVER  YYB-Go-Enhanced 地址@账号标识，多账号每行一条
              例：yyb-go:8000@1
  JDEXPRESS_NOTIFY  1 推送错误（默认），0 关闭推送
  JDEXPRESS_ACCOUNT_LIMIT  可选，仅运行前 N 个账号（用于测试）
  JDEXPRESS_REF_FILTER  可选，仅运行指定账号标识（用于测试）

内部配置（直接改下面常量，省得去面板设环境变量）：
  YYB_ONLY_REFS  账号序号白名单（1 起），仅运行指定序号；留空 [] 运行全部，
                 例如 [1,3] 只跑第 1、3 个账号（序号按 YYB_SERVER 行序，1 起）

作者：lcmovie https://github.com/lcmovie

依赖：requests（青龙“依赖管理”中安装 Python3 依赖 requests）
通知：直接调用青龙自带的通知模块（容器内为 /ql/data/scripts/notify.py，
      仓库内为同目录 / 上一级的 notify.py）——在青龙「通知设置」里配一次即全站通用。
      口径：只推错误 —— 本次运行出现 ERROR 日志才推送一次；无错误完全静默。

日志规范：[LEVEL] [JDEXPRESS] message   （LEVEL: INFO / WARN / ERROR）

更新日志：
  2026-09-04 v1.0  依据手机微信真实 HAR 实现登录、签到、奖励核算和通知
  2026-09-04 v1.1  登录改用 YYB-Go + 京东 PT OAuth 跳转链，修复无 pt_key/pt_pin
  2026-09-04 v1.2  增加全参数登录回退，区分未绑定京东账号与风控账号
  2026-10-01 v1.3  接入青龙 notify.py（只推错误）+ atexit / 异常兜底；新增 YYB_ONLY_REFS 白名单；
                   日志改为 [LEVEL] [JDEXPRESS] 规范，补账号分隔标识与尾部 [执行汇总]
"""

import html
import json
import os
import re
import sys
import time
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, parse_qsl, unquote, urlencode, urljoin, urlparse, urlunparse

import requests

# 内部配置：直接改这里，省去去面板设环境变量
# 账号序号白名单（1 起），仅运行指定序号，留空 [] 运行全部；例如 [1, 3]
YYB_ONLY_REFS = [1]


APP_NAME = "京东快递签到"
APP_ID = "wx73247c7819d61796"
JD_PT_APP_ID = "wx2f5d8f9715c59d10"
JD_PT_APP = "300"
JD_PT_RETURN_URL = "https://my.m.jd.com/account/index.html"
JD_LOGIN_APP_ID = "wx91d27dbf599dff74"
API_BASE = "https://lop-proxy.jd.com"
TIMEOUT = 20
UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 "
    "MicroMessenger/8.0.73 NetType/WIFI Language/zh_CN "
    f"miniProgram/{APP_ID}"
)


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

_QN_SITE = "京东快递"
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
    # 本脚本自带开关：JDEXPRESS_NOTIFY=0/false/no/off 时整体不推送
    if _os.getenv("JDEXPRESS_NOTIFY", "1").strip().lower() in {"0", "false", "no", "off"}:
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
    line = "[%s] [JDEXPRESS] %s" % (level, msg)
    print(line, flush=True)
    if level == "ERROR":
        collect_error(line)


def log(msg):  _emit("INFO", msg)
def warn(msg): _emit("WARN", msg)
def err(msg):  _emit("ERROR", msg)


# ———————————— 账号分隔标识 ————————————
# 多账号同跑时，用醒目的横线 + 账号信息把各账号的日志隔开，便于阅读与定位。
_ACC_RULE = "=" * 70


def acc_banner(idx, total, ident=""):
    """打印账号分隔标识（开始）。"""
    log(_ACC_RULE)
    log("👤 账号 %d/%d%s" % (idx, total, (" ｜ " + str(ident)) if ident else ""))
    log(_ACC_RULE)


def acc_footer(idx, total):
    """打印账号分隔标识（结束）。"""
    log("🔚 账号 %d/%d 处理结束" % (idx, total))


def parse_accounts() -> List[Tuple[str, str]]:
    raw = os.getenv("YYB_SERVER", "")
    accounts: List[Tuple[str, str]] = []
    for line_no, line in enumerate(raw.splitlines(), 1):
        value = line.strip()
        if not value:
            continue
        if "@" not in value:
            err(f"❌ YYB_SERVER 第 {line_no} 行缺少 @，已跳过")
            continue
        server, ref = value.rsplit("@", 1)
        server, ref = server.strip().rstrip("/"), ref.strip()
        if not server or not ref:
            err(f"❌ YYB_SERVER 第 {line_no} 行地址或账号标识为空，已跳过")
            continue
        if not server.startswith(("http://", "https://")):
            server = "http://" + server
        accounts.append((server, ref))
    return accounts


def get_wx_code(server: str, ref: str, app_id: str = APP_ID) -> str:
    response = requests.post(
        f"{server}/wxapp/getCode",
        json={"ref": ref, "app_id": app_id},
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    payload = response.json()
    result = ((payload.get("data") or {}).get("result") or {})
    code = result.get("code") if isinstance(result, dict) else None
    if payload.get("code") != 0 or not isinstance(code, str) or len(code) < 8:
        keys = ",".join(sorted(str(key) for key in result.keys())) if isinstance(result, dict) else type(result).__name__
        detail = result.get("errMsg") if isinstance(result, dict) else ""
        raise RuntimeError(f"YYB-Go 未返回有效 code：{detail or payload.get('msg') or payload.get('code')}；result字段={keys}")
    return code


def extract_pt_cookie(session: requests.Session, payload: Any) -> str:
    values = session.cookies.get_dict()
    pt_key, pt_pin = values.get("pt_key"), values.get("pt_pin")
    if isinstance(payload, dict):
        pt_key = pt_key or payload.get("pt_key") or payload.get("ptKey")
        pt_pin = pt_pin or payload.get("pt_pin") or payload.get("ptPin")
        data = payload.get("data") or payload.get("result") or {}
        if isinstance(data, dict):
            pt_key = pt_key or data.get("pt_key") or data.get("ptKey")
            pt_pin = pt_pin or data.get("pt_pin") or data.get("ptPin")
    if not pt_key or not pt_pin:
        return ""
    return f"pt_key={pt_key}; pt_pin={pt_pin};"


def yyb_result(payload: Dict[str, Any]) -> Dict[str, Any]:
    if payload.get("code") != 0:
        raise RuntimeError(payload.get("msg") or f"YYB-Go 返回 code={payload.get('code')}")
    data = payload.get("data") or {}
    result = data.get("result") if isinstance(data, dict) else None
    if not isinstance(result, dict):
        raise RuntimeError("YYB-Go 返回缺少 data.result")
    return result


def nested_text(value: Any, names: Tuple[str, ...]) -> str:
    wanted = {name.lower() for name in names}
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).lower() in wanted and item not in (None, ""):
                return str(item)
        for item in value.values():
            found = nested_text(item, names)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = nested_text(item, names)
            if found:
                return found
    return ""


def follow_acrj(session: requests.Session, payload: Dict[str, Any]) -> str:
    current = nested_text(payload, ("ACRJUrl", "acrjUrl"))
    state = nested_text(payload, ("ACRJState", "acrjState"))
    if not current:
        return ""
    if current.startswith("//"):
        current = "https:" + current
    elif current.startswith("/"):
        current = "https://wq.jd.com" + current
    if state and "ACRJState=" not in current:
        parsed = urlparse(current)
        query = parse_qsl(parsed.query, keep_blank_values=True)
        query.append(("ACRJState", state))
        current = urlunparse(parsed._replace(query=urlencode(query)))
    for _ in range(8):
        if not allowed_jd_url(current):
            raise RuntimeError("ACRJ 刷新地址不是受信任的京东 HTTPS 域名")
        response = session.get(current, allow_redirects=False, timeout=TIMEOUT)
        cookie = extract_pt_cookie(session, {})
        if cookie:
            return cookie
        location = response.headers.get("Location", "")
        if not location or response.status_code not in {301, 302, 303, 307, 308}:
            break
        current = urljoin(current, location)
    return extract_pt_cookie(session, {})


def get_yyb_user_info(server: str, ref: str) -> Dict[str, str]:
    response = requests.post(
        f"{server}/wxapp/operateWxData",
        json={
            "ref": ref,
            "app_id": JD_LOGIN_APP_ID,
            "payload": {"api_name": "getUserInfo", "data": {"withCredentials": True}, "env": 1},
        },
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    result = yyb_result(response.json())
    raw_data = result.get("rawData") or result.get("raw_data") or result.get("data")
    if isinstance(raw_data, (dict, list)):
        raw_data = json.dumps(raw_data, ensure_ascii=False, separators=(",", ":"))
    user_info = result.get("userInfo") or result.get("user_info")
    if not raw_data and isinstance(user_info, dict):
        raw_data = json.dumps(user_info, ensure_ascii=False, separators=(",", ":"))
    if not raw_data:
        direct_info = {
            key: result[key]
            for key in ("nickName", "gender", "language", "city", "province", "country", "avatarUrl")
            if key in result and result[key] is not None
        }
        if direct_info:
            raw_data = json.dumps(direct_info, ensure_ascii=False, separators=(",", ":"))
    encrypted = result.get("encryptedData") or result.get("encrytData") or result.get("encrypted_data")
    info = {
        "rawData": str(raw_data or ""),
        "signature": str(result.get("signature") or ""),
        "encryptedData": str(encrypted or ""),
        "iv": str(result.get("iv") or ""),
        "openid": str(result.get("openid") or ""),
    }
    missing = [key for key in ("rawData", "signature", "encryptedData", "iv") if not info[key]]
    if missing:
        keys = ",".join(sorted(str(key) for key in result.keys()))
        raise RuntimeError("YYB-Go getUserInfo 缺少字段：" + ",".join(missing) + f"；返回字段={keys}")
    return info


def full_jd_login(server: str, ref: str) -> Tuple[requests.Session, str]:
    code = get_wx_code(server, ref, JD_LOGIN_APP_ID)
    info = get_yyb_user_info(server, ref)
    session = requests.Session()
    session.trust_env = False
    session.headers.update({
        "User-Agent": UA,
        "Referer": f"https://servicewechat.com/{JD_LOGIN_APP_ID}/873/page-frame.html",
        "Accept": "application/json, text/plain, */*",
    })
    params = {
        "appid": JD_LOGIN_APP_ID,
        "code": code,
        "type": "silent",
        "isPopup": "false",
        "isIgnoreCookie": "false",
        "isOfficialPin": "false",
        "loginColor": "{}",
        "returnUrl": "pages/my/index/index",
        "deviceName": "iPhone",
        "deviceOS": "iOS",
        "deviceOSVersion": "17.0",
        "deviceVersion": "8.0.49",
        "g_tk": "0",
        "g_ty": "ls",
        "rawData": info["rawData"],
        "signature": info["signature"],
        "encrytData": info["encryptedData"],
        "encryptedData": info["encryptedData"],
        "iv": info["iv"],
        "ou": info["openid"],
    }
    response = session.get(
        "https://wq.jd.com/mlogin/wxapp/login_lt",
        params=params,
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    try:
        payload = response.json()
    except ValueError:
        text = response.text.strip()
        start, end = text.find("{"), text.rfind("}")
        try:
            payload = json.loads(text[start:end + 1]) if start >= 0 and end > start else {}
        except ValueError:
            payload = {}
    cookie = extract_pt_cookie(session, payload)
    if not cookie:
        cookie = follow_acrj(session, payload)
    if not cookie:
        message = nested_text(payload, ("errmsg", "errMsg", "retMsg", "message", "msg"))
        fields = ",".join(sorted(str(key) for key in payload.keys()))
        ret_code = nested_text(payload, ("retCode", "code"))
        if ret_code == "201" or "pin not exist" in message.lower():
            raise RuntimeError("该微信账号未绑定京东账号，请先在京东快递小程序完成登录/绑定（retCode=201）")
        if ret_code == "202" or "risk user" in message.lower():
            raise RuntimeError("京东判定该账号存在风险，请先在京东快递小程序完成安全验证（retCode=202）")
        suffix = f"；retCode={ret_code}" if ret_code else ""
        raise RuntimeError((message or "京东全参数登录未返回 pt_key/pt_pin") + suffix + f"；响应字段={fields}")
    session.headers.update(business_headers(cookie))
    pin = unquote(session.cookies.get("pt_pin") or cookie.split("pt_pin=", 1)[1].split(";", 1)[0])
    return session, pin


def allowed_jd_url(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    return parsed.scheme == "https" and (
        host == "jd.com" or host.endswith(".jd.com")
        or host == "jd.hk" or host.endswith(".jd.hk")
        or host == "3.cn" or host.endswith(".3.cn")
    )


def html_redirect(base_url: str, body: str) -> str:
    text = html.unescape(str(body or ""))
    patterns = (
        r'<meta[^>]+url\s*=\s*["\']?([^"\' >]+)',
        r'(?:window\.)?location(?:\.href)?\s*=\s*["\']([^"\']+)',
        r'location\.(?:replace|assign)\s*\(\s*["\']([^"\']+)',
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return urljoin(base_url, match.group(1).strip())
    return ""


def login(server: str, ref: str) -> Tuple[requests.Session, str]:
    try:
        code = get_wx_code(server, ref, JD_PT_APP_ID)
    except Exception as exc:
        warn(f"⚠️ JD PT 专用 code 不可用（{exc}），改用全参数登录")
        return full_jd_login(server, ref)
    session = requests.Session()
    session.trust_env = False
    session.headers.update({
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9",
    })

    entry = session.get(
        "https://plogin.m.jd.com/user/login.action",
        params={"appid": JD_PT_APP, "returnurl": JD_PT_RETURN_URL},
        allow_redirects=False,
        timeout=TIMEOUT,
    )
    location = entry.headers.get("Location", "")
    if entry.status_code not in range(300, 400) or not location:
        raise RuntimeError(f"JD PT login.action 未跳转：HTTP {entry.status_code}")
    oauth_url = urljoin(entry.url, location)
    query = parse_qs(urlparse(oauth_url).query, keep_blank_values=True)
    if query.get("appid", [""])[0] != JD_PT_APP_ID:
        raise RuntimeError("JD PT OAuth appid 不匹配")
    redirect_uri = query.get("redirect_uri", [""])[0]
    state = query.get("state", [""])[0]
    if not redirect_uri or not state:
        raise RuntimeError("JD PT OAuth 缺少 redirect_uri/state")

    callback = urlparse(redirect_uri)
    callback_query = parse_qsl(callback.query, keep_blank_values=True)
    callback_query.extend([("code", code), ("state", state)])
    current = urlunparse(callback._replace(query=urlencode(callback_query)))
    cookie = ""
    last_status = 0
    for _ in range(8):
        if not allowed_jd_url(current):
            raise RuntimeError("JD PT 跳转超出允许的京东域名")
        response = session.get(current, allow_redirects=False, timeout=TIMEOUT)
        last_status = response.status_code
        cookie = extract_pt_cookie(session, {})
        if cookie:
            break
        location = response.headers.get("Location", "")
        if not location and response.status_code == 200:
            location = html_redirect(current, response.text)
        if not location or response.status_code not in {200, 301, 302, 303, 307, 308}:
            break
        current = urljoin(current, location)
    if not cookie:
        names = ",".join(sorted(session.cookies.get_dict().keys())) or "无"
        warn(f"⚠️ JD PT 跳转链未完成（HTTP {last_status}，Cookie字段={names}），改用全参数登录")
        return full_jd_login(server, ref)
    session.headers.update(business_headers(cookie))
    return session, unquote(session.cookies.get("pt_pin") or cookie.split("pt_pin=", 1)[1].split(";", 1)[0])


def business_headers(cookie: str) -> Dict[str, str]:
    return {
        "Cookie": cookie,
        "User-Agent": UA,
        "Referer": "https://jingcai-h5.jd.com/",
        "Origin": "https://jingcai-h5.jd.com",
        "Content-Type": "application/json;charset=utf-8",
        "Accept": "application/json, text/plain, */*",
        "X-Requested-With": "XMLHttpRequest",
        "app-key": "jexpress",
        "appparams": '{"appid":158,"ticket_type":"m"}',
        "clientinfo": '{"appName":"jingcai","client":"m"}',
        "biz-type": "service-monitor",
        "access": "H5",
        "lop-dn": "jingcai.jd.com",
        "sdkversion": "1.0.7",
        "source-client": "2",
        "forcebot": "0",
        "screen": "428*926",
        "event-id": str(uuid.uuid4()),
    }


def api_post(session: requests.Session, path: str, body: Any) -> Dict[str, Any]:
    session.headers["event-id"] = str(uuid.uuid4())
    response = session.post(API_BASE + path, json=body, timeout=TIMEOUT)
    response.raise_for_status()
    payload = response.json()
    if payload.get("success") is not True or payload.get("code") != 1:
        raise RuntimeError(payload.get("errorMsg") or payload.get("msg") or f"接口返回 code={payload.get('code')}")
    content = payload.get("content")
    return content if isinstance(content, dict) else {"value": content}


def query_balance(session: requests.Session, pin: str) -> Dict[str, int]:
    content = api_post(session, "/JingIntegralApi/userAccount", [{"pin": pin}])
    return {
        "jdBean": int(content.get("jdBean") or 0),
        "integral": int(content.get("integral") or 0),
    }


def query_calendar(session: requests.Session) -> Dict[str, Any]:
    return api_post(session, "/jiFenApi/signInList", [{"userNo": ""}])


def today_item(calendar: Dict[str, Any]) -> Dict[str, Any]:
    for item in calendar.get("dayList") or []:
        if isinstance(item, dict) and item.get("isToday") is True:
            return item
    return {}


def coupon_reward(item: Dict[str, Any]) -> Optional[str]:
    reward = item.get("rewardDto") or {}
    batch = reward.get("batchInfo") or {}
    if not item.get("existReward") or not isinstance(batch, dict):
        return None
    name = batch.get("restrictedCopy") or batch.get("activityName") or batch.get("remark")
    count = int(batch.get("sendNum") or 1)
    return f"{name} ×{count}" if name else f"优惠券 ×{count}"


def reward_summary(before: Dict[str, int], after: Dict[str, int], item: Dict[str, Any]) -> str:
    rewards: List[str] = []
    bean_delta = after["jdBean"] - before["jdBean"]
    integral_delta = after["integral"] - before["integral"]
    if bean_delta > 0:
        rewards.append(f"京豆 +{bean_delta}")
    if integral_delta > 0:
        rewards.append(f"积分 +{integral_delta}")
    coupon = coupon_reward(item)
    if coupon:
        rewards.append(coupon)
    return "、".join(rewards) if rewards else "无（京豆/积分未增加，当日无券奖励）"


def mask_ref(ref: str) -> str:
    return ref if ref.isdigit() else (ref[:3] + "***" + ref[-3:] if len(ref) > 8 else "***")


def run_account(index: int, server: str, ref: str) -> Dict[str, Any]:
    result: Dict[str, Any] = {"index": index, "ref": mask_ref(ref), "success": False}
    session, pin = login(server, ref)
    log("✅ 微信 code 登录成功")
    before = query_balance(session, pin)
    calendar_before = query_calendar(session)
    today_before = today_item(calendar_before)
    already_signed = today_before.get("isCanSignIn") is False and today_before.get("signInType") == 1

    if already_signed:
        after = before
        today_after = today_before
        status = "今日已签到"
        reward = coupon_reward(today_after) or "已签到，接口无法回溯当日普通奖励"
    else:
        api_post(session, "/jiFenApi/signIn", [{
            "signInDate": datetime.now().strftime("%Y-%m-%d"),
            "userNo": "",
        }])
        time.sleep(1)
        after = query_balance(session, pin)
        calendar_after = query_calendar(session)
        today_after = today_item(calendar_after)
        if today_after.get("isCanSignIn") is not False:
            raise RuntimeError("签到接口返回成功，但日历仍显示可签到")
        status = "签到成功"
        reward = reward_summary(before, after, today_after)

    days = calendar_before.get("signedInDay") or 0
    if not already_signed:
        days = (calendar_after.get("signedInDay") or days)
    result.update({
        "success": True,
        "status": status,
        "reward": reward,
        "days": days,
        "balance": after,
    })
    log(f"✅ {status}；今日奖励：{reward}")
    return result


def main() -> int:
    accounts = parse_accounts()
    if not accounts:
        err("❌ 未配置有效 YYB_SERVER，格式：yyb-go:8000@账号ID或OpenID")
        return 1
    # 按 YYB_ONLY_REFS 序号白名单筛选（1 起）；留空 [] 则运行全部账号
    if YYB_ONLY_REFS:
        wanted = set(int(x) for x in YYB_ONLY_REFS if str(x).strip().isdigit() and int(x) > 0)
        if wanted:
            accounts = [acc for i, acc in enumerate(accounts, 1) if i in wanted]
            log("ℹ️ 按 YYB_ONLY_REFS 筛选：请求序号 %s，命中 %d 个账号" % (sorted(wanted), len(accounts)))
            if not accounts:
                err("❌ YYB_ONLY_REFS 指定的序号均超出账号范围")
                return 1
    ref_filter = os.getenv("JDEXPRESS_REF_FILTER", "").strip()
    if ref_filter:
        accounts = [item for item in accounts if item[1] == ref_filter]
        if not accounts:
            err("❌ JDEXPRESS_REF_FILTER 未匹配任何 YYB_SERVER 账号")
            return 1
    limit_raw = os.getenv("JDEXPRESS_ACCOUNT_LIMIT", "").strip()
    if limit_raw:
        try:
            limit = int(limit_raw)
            if limit > 0:
                accounts = accounts[:limit]
        except ValueError:
            warn("⚠️ JDEXPRESS_ACCOUNT_LIMIT 不是有效正整数，已忽略")
    total = len(accounts)
    results: List[Dict[str, Any]] = []
    for index, (server, ref) in enumerate(accounts, 1):
        acc_banner(index, total, "标识：" + mask_ref(ref))
        try:
            results.append(run_account(index, server, ref))
        except Exception as exc:
            err(f"❌ 账号 {index} 失败：{exc}")
            results.append({"index": index, "ref": mask_ref(ref), "success": False, "error": str(exc)})
        acc_footer(index, total)
    ok = sum(1 for r in results if r.get("success"))
    log(_ACC_RULE)
    log(f"📊 [执行汇总] {APP_NAME} · 账号 {total} ｜ 成功 {ok} ｜ 失败 {total - ok}")
    log(_ACC_RULE)
    # 错误日志推送：有错误才发；未配置通知渠道则只提示一行
    flush_notify(
        "京东快递",
        f"账号 {total} ｜ 成功 {ok} ｜ 失败 {total - ok}",
        logger=log,
    )
    return 0 if ok == total else 1


if __name__ == "__main__":
    sys.exit(main())
