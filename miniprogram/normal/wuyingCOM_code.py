#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# =========================================================
# name:  无影云电脑
# version: 2026-10-29a
# cron: 0 7 * * *
# 积分有效期：
# =========================================================

# ========== 企业微信推送配置（可选） ==========
QYWX_TOKEN = __import__("os").getenv("QYWX_TOKEN", "")  # 企业微信机器人 Webhook key（机器人地址 ?key= 后面的值，留空不推送）

# ==========================================================
# 功能说明：code 换 token（含缓存与自动刷新）
# 机制：本地 code 服务获取微信 code → 换取 token → 缓存到本地 JSON；
#       下次运行先读取缓存 token，并调用用户信息接口验证是否仍有效；
#       有效则直接复用（无需再获取 code）；失效或过期则重新获取 code 自动刷新。
# ==========================================================


"""
无影云电脑小程序动态 code 版

功能：
  1. 本地 code 服务获取微信 code
  2. 复刻小程序的阿里云账号 OAuth 静默登录
     （/weixin/authLogin.html → GetLoginTokenByAuthCode 拿 LoginToken/SessionId）
  3. RefreshLoginToken 自动续期缓存会话
  4. 签到活动配置与签到日历（DescribeCheckInActivity / DescribeUserCheckInInfo，Activity=check-in）
  5. 每日签到（CreateUserCheckIn，按日历状态幂等：已签则不再提交）
  6. PushPlus 推送
  7. 品赞代理，业务请求优先代理，失败直连兜底

说明：该小程序登录依赖「已绑定手机号的阿里云账号」。若 authLogin 返回
      state=register，需先在小程序内完成手机号/阿里云账号授权注册；
      返回 state=identityVerify 表示阿里云对本次静默登录下发了安全验证挑战，
      脚本不会代过风控验证。可用环境变量 wuying_token（格式 LoginToken#SessionId，
      从小程序已登录会话抓取）作为逃生口，脚本会用 RefreshLoginToken 自动续期。

环境变量：
  PLUSPLUS_TOKEN    PushPlus token，可选
  QYWX_TOKEN        企业微信机器人 Webhook key，可选（机器人地址 ?key= 后面的值）
  PROXY_API         品赞代理提取 API，可选
  PROXY_TYPE        http / socks5，默认 http
  CODE_SERVER       本地 code 服务地址，默认 127.0.0.1:8088
  YYB_SERVER        YYB 账号基座，格式「地址@账号ID」，每行一个
  YYB_ONLY_REFS     账号序号白名单（1 起，按 YYB_SERVER 行序），见脚本顶部硬编码常量，留空跑全部
  wuying_token      手动会话逃生口，多账号按账号顺序用换行或 & 分割

依赖：
  pip install requests
  socks5 代理需：
  pip install requests[socks]
"""

import sys
import json
import os
import random
import time
import traceback
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Tuple
from urllib.parse import urlencode

import requests
# ========== 账号分隔标识 ==========
# 多账号同跑时, 用醒目横线把各账号日志隔开, 便于阅读与定位。
# ========== 启动信息 ==========
def log_startup(total: int = 0) -> None:
    log(ACC_RULE)
    log("📌 脚本版本: %s ｜ %s" % (SCRIPT_VER, now_text()))
    if total:
        log("🔢 账号数量: %d" % total)
    log(ACC_RULE)
SCRIPT_VER = "2026-10-29a"
ACC_RULE = "=" * 70

# ========== 统一日志：[LEVEL] [WY] message ==========
def _emit(level, msg):
    line = "[%s] [WY] %s" % (level, msg)
    print(line, flush=True)


def log(msg):
    _emit("INFO", msg)


def warn(msg):
    _emit("WARN", msg)


def err(msg):
    _emit("ERROR", msg)



APP_NAME = "无影云电脑小程序"
APPID = "wx66f97ce0a56f08c7"

SERVERS = [
    "127.0.0.1:8088",
]

if os.getenv("CODE_SERVER"):
    SERVERS = [os.getenv("CODE_SERVER")]

PLUSPLUS_TOKEN = os.getenv("PLUSPLUS_TOKEN", "")
PROXY_API = os.getenv("PROXY_API", "")
PROXY_TYPE = os.getenv("PROXY_TYPE", "http").lower()

# 账号序号白名单：按 YYB_SERVER 的书写顺序从 1 开始编号，留空 [] 表示全部
YYB_ONLY_REFS = [2,3]

PROXY_RETRY_TIMES = 3
PROXY_VALIDATE_URL = "http://httpbin.org/ip"
PROXY_FETCH_INTERVAL = 3
ENABLE_DIRECT_FALLBACK = True
REQUEST_TIMEOUT = 30

# 阿里云账号 OAuth (SDK env=prod -> account.aliyun.com), 静默登录接口
OAUTH_BASE = "https://account.aliyun.com"
AUTHLOGIN_URL = f"{OAUTH_BASE}/weixin/authLogin.html"
# 账号服务 (换取 LoginToken/SessionId)
ACCOUNT_EP = "https://appstream-center.cn-shanghai.aliyuncs.com"
ACCOUNT_VER = "2022-11-22"
# 桌面服务 (用户/活动/签到)
DESKTOP_EP = "https://wuying-personal-pc.cn-hangzhou.aliyuncs.com"
DESKTOP_VER = "2022-10-01"
APP_VERSION_INFO = "20260112"          # genOpenApiUrl 内置 AppVersionInfo
FROM_CLIENT = "miniapp_weixin"
# 阿里云 OpenAPI 业务成功标识 (desktop/account 业务接口: Code==="success")
BIZ_OK = "success"
# 会话失效 -> 需重新登录
SESSION_INVALID = ("User.LoginInvalid", "InvalidLoginToken.Missing", "NOT_LOGIN")
# 签到活动标识 (HAR 实测: DescribeCheckInActivity / DescribeUserCheckInInfo / CreateUserCheckIn 均用 Activity=check-in)
CHECKIN_ACTIVITY = "check-in"

# 手动会话逃生口: wuying_token = "LoginToken#SessionId", 按账号顺序换行/& 分割
_manual_session_lines = [
    x.strip()
    for x in (os.getenv("wuying_token") or os.getenv("WUYING_TOKEN") or "").replace("&", "\n").splitlines()
    if x.strip()
]
MANUAL_SESSIONS: Dict[str, str] = {server: value for server, value in zip(SERVERS, _manual_session_lines)}

COOKIE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wuyingcookie.json")

USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 13; SM-G9910 Build/TP1A.220624.014) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Mobile Safari/537.36 "
    "MicroMessenger/8.0.49.2600(0x28003137) NetType/WIFI Language/zh_CN "
    "miniProgram/" + APPID
)


def now_text() -> str:
    _dt = datetime.now() if hasattr(datetime, "now") else datetime.datetime.now()



def sleep(seconds: float) -> None:
    time.sleep(seconds)


def mask(value: Any) -> str:
    value = str(value or "")
    if len(value) <= 12:
        return value
    return f"{value[:6]}...{value[-6:]}"


def json_preview(data: Any, limit: int = 800) -> str:
    try:
        return json.dumps(data, ensure_ascii=False)[:limit]
    except Exception:
        return str(data)[:limit]


def to_float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def safe_data(resp: Dict[str, Any]) -> Dict[str, Any]:
    """Safely extract 'data' from an API response, handling null/missing."""
    return resp.get("data") or {}


def log_title() -> None:
    print()
    log("╔" + "═" * 50 + "╗")
    log("║ ☁️ 无影云电脑动态 code 版                    ║")
    log(f"║ 🕒 启动时间: {now_text():<32}║")
    log(f"║ 🔢 账号数量: {len(SERVERS):<34}║")
    log("╚" + "═" * 50 + "╝")


def log_account_header(index: int, total: int, server: str) -> None:
    print()
    log("┌" + "─" * 50 + "┐")
    log(f"│ 🧩 账号 {index} / {total:<37}│")
    log(f"│ 🌍 来源 {server:<40}│")
    log("└" + "─" * 50 + "┘")


def direct_session() -> requests.Session:
    session = requests.Session()
    session.trust_env = False
    return session


def parse_proxy_response(text: Any) -> Dict[str, Any] | None:
    if not isinstance(text, str):
        text = json.dumps(text, ensure_ascii=False)

    text = text.strip()
    if not text:
        return None

    try:
        data = json.loads(text)
        proxy_obj = None

        if isinstance(data.get("data"), list) and data["data"]:
            proxy_obj = data["data"][0]
        elif isinstance(data.get("data"), dict):
            proxy_obj = data["data"]
        elif data.get("ip") and data.get("port"):
            proxy_obj = data
        elif isinstance(data.get("result"), dict):
            proxy_obj = data["result"]

        if proxy_obj:
            host = proxy_obj.get("ip") or proxy_obj.get("host")
            port = proxy_obj.get("port")
            if host and port:
                return {
                    "host": str(host),
                    "port": int(port),
                    "username": proxy_obj.get("user") or proxy_obj.get("username") or "",
                    "password": proxy_obj.get("pass") or proxy_obj.get("password") or "",
                }
    except Exception:
        pass

    if ":" in text:
        parts = text.split(":")
        if len(parts) >= 2:
            return {
                "host": parts[0],
                "port": int(parts[1]),
                "username": parts[2] if len(parts) > 2 else "",
                "password": parts[3] if len(parts) > 3 else "",
            }

    return None


def build_proxy_dict(proxy_info: Dict[str, Any] | None) -> Dict[str, str] | None:
    if not proxy_info:
        return None

    host = proxy_info["host"]
    port = proxy_info["port"]
    username = proxy_info.get("username", "")
    password = proxy_info.get("password", "")

    auth = ""
    if username and password:
        auth = f"{quote(username)}:{quote(password)}@"

    scheme = "socks5" if PROXY_TYPE == "socks5" else "http"
    proxy_url = f"{scheme}://{auth}{host}:{port}"

    log(f"🛠️ [代理] 生成 {scheme.upper()} 代理 {host}:{port}")

    return {
        "http": proxy_url,
        "https": proxy_url,
    }


def validate_proxy(proxies: Dict[str, str] | None) -> Tuple[bool, str]:
    if not proxies:
        return False, ""

    try:
        response = requests.get(PROXY_VALIDATE_URL, proxies=proxies, timeout=15)
        if response.status_code == 200:
            try:
                ip = response.json().get("origin", "未知")
            except Exception:
                ip = "未知"
            log(f"✅ [代理] 验证通过，出口 IP: {ip}")
            return True, ip
    except Exception as exc:
        warn(f"⚠️ [代理] 验证失败: {exc}")

    return False, ""


def get_valid_proxy(account_name: str) -> Tuple[Dict[str, str] | None, str]:
    if not PROXY_API:
        warn(f"⚠️ [代理] {account_name} 未配置 PROXY_API，使用直连")
        return None, ""

    log(f"🌐 [代理] {account_name} 正在获取品赞代理...")

    for index in range(1, PROXY_RETRY_TIMES + 1):
        try:
            response = direct_session().get(PROXY_API, timeout=15)
            proxy_info = parse_proxy_response(response.text)

            if not proxy_info:
                warn(f"⚠️ [代理] 第 {index} 次代理解析失败")
                continue

            log(f"✅ [代理] 提取到 {proxy_info['host']}:{proxy_info['port']}")
            proxies = build_proxy_dict(proxy_info)

            ok, ip = validate_proxy(proxies)
            if ok:
                return proxies, ip

            warn(f"⚠️ [代理] 第 {index} 次代理不可用")
        except Exception as exc:
            warn(f"⚠️ [代理] 第 {index} 次获取代理异常: {exc}")

        if index < PROXY_RETRY_TIMES:
            sleep(2)

    warn("⚠️ [代理] 获取失败，使用直连")
    return None, ""


def request_with_proxy(
    method: str,
    url: str,
    *,
    proxies: Dict[str, str] | None = None,
    server: str = "",
    **kwargs,
) -> requests.Response:
    kwargs.setdefault("timeout", REQUEST_TIMEOUT)

    if proxies:
        try:
            return requests.request(method, url, proxies=proxies, **kwargs)
        except Exception as exc:
            warn(f"⚠️ [代理] {server} 代理请求失败: {exc}")
            if not ENABLE_DIRECT_FALLBACK:
                raise
            warn("🔁 [兜底] 切换直连重试")

    session = direct_session()
    return session.request(method, url, **kwargs)



def send_qywx(title, content):
    """企业微信机器人推送（Webhook）。未配置 QYWX_TOKEN 时自动跳过。"""
    if not QYWX_TOKEN:
        warn("[企业微信] 未配置 QYWX_TOKEN，跳过推送")
        return False
    key = QYWX_TOKEN.split("key=")[-1].strip()
    import json as _qywx_json, urllib.request as _qywx_urllib
    try:
        text = "%s\n%s" % (title, content)
        if len(text.encode("utf-8")) > 2000:
            text = text.encode("utf-8")[:2000].decode("utf-8", "ignore")
        payload = _qywx_json.dumps({"msgtype": "text", "text": {"content": text}}).encode("utf-8")
        req = _qywx_urllib.Request("https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=" + key,
                                   data=payload, headers={"Content-Type": "application/json"})
        res = _qywx_json.loads(_qywx_urllib.urlopen(req, timeout=10).read().decode("utf-8"))
        ok = res.get("errcode") == 0
        err("[企业微信] 推送%s errcode=%s errmsg=%s" % ("成功 ✓" if ok else "失败 ✗", res.get("errcode"), res.get("errmsg", "")))
        return ok
    except Exception as _exc:
        err("[企业微信] 推送异常:", _exc)
        return False
def send_pushplus(title: str, content: str) -> None:
    send_qywx(title, content)  # 企业微信推送（QYWX_TOKEN 未配置时自动跳过）
    if not PLUSPLUS_TOKEN:
        warn("⚠️ [PushPlus] 未配置 PLUSPLUS_TOKEN，跳过推送")
        return

    try:
        requests.post(
            "https://www.pushplus.plus/send",
            json={
                "token": PLUSPLUS_TOKEN,
                "title": title,
                "content": content,
                "template": "txt",
            },
            timeout=10,
        )
        log("✅ [PushPlus] 推送成功")
    except Exception as exc:
        err(f"❌ [PushPlus] 推送失败: {exc}")


def get_code(server: str) -> str | None:
    url = f"http://{server}/login"
    log(f"🔐 [授权] 请求本地 code 服务: {url}")

    try:
        response = direct_session().get(
            url,
            params={"appId": APPID},
            timeout=20,
        )
        data = response.json()

        if data.get("err") != 0 or not data.get("code"):
            err(f"❌ [授权] code 获取失败: {json_preview(data)}")
            return None

        log("✅ [授权] code 获取成功")
        return data["code"]
    except Exception as exc:
        err(f"❌ [授权] code 获取异常: {exc}")
        return None


def common_headers() -> Dict[str, str]:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
        "xweb_xhr": "1",
        "Referer": f"https://servicewechat.com/{APPID}/0/page-frame.html",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    return headers


# ====================== 阿里云 OpenAPI（复刻 genOpenApiUrl，客户端签名已禁用故无需签名） ======================
def open_api(
    server: str,
    endpoint: str,
    action: str,
    version: str,
    params: Dict[str, Any],
    proxies: Dict[str, str] | None,
    method: str = "GET",
) -> Dict[str, Any]:
    q = {
        "Action": action,
        "Format": "JSON",
        "SignatureMethod": "HMAC-SHA1",
        "SignatureNonce": uuid.uuid4().hex,
        "SignatureVersion": "1.0",
        "Timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "Version": version,
        "From": FROM_CLIENT,
        "AppVersionInfo": APP_VERSION_INFO,
    }
    q.update({k: v for k, v in params.items() if v is not None})
    url = f"{endpoint}?{urlencode(sorted(q.items()))}"
    headers = common_headers()
    if method.upper() == "POST":
        # 小程序真实 POST 会把同一组参数再放进 JSON body (HAR 实测), 这里 query + body 双写
        post_headers = dict(headers)
        post_headers["Content-Type"] = "application/json"
        response = request_with_proxy("POST", url, headers=post_headers, json=q, proxies=proxies, server=server)
    else:
        response = request_with_proxy("GET", url, headers=headers, proxies=proxies, server=server)
    try:
        return response.json()
    except Exception:
        return {"Code": "JSON_PARSE_FAIL", "Message": f"JSON解析失败: {response.text[:300]}"}


def biz_ok(resp: Dict[str, Any]) -> bool:
    """业务接口 Code==="success" 视为成功"""
    node = resp if isinstance(resp, dict) else {}
    return str(node.get("Code")) == BIZ_OK


def biz_code(resp: Dict[str, Any]) -> Any:
    return (resp or {}).get("Code")


def is_session_invalid(resp: Dict[str, Any]) -> bool:
    return str((resp or {}).get("Code")) in SESSION_INVALID


def inner_node(resp: Dict[str, Any]) -> Dict[str, Any]:
    node = resp.get("data") if isinstance(resp.get("data"), dict) else resp
    return node if isinstance(node, dict) else {}


def aliyun_authlogin(server: str, code: str, proxies: Dict[str, str] | None) -> Dict[str, Any]:
    """POST {OAUTH_BASE}/weixin/authLogin.html?code=..&appId=.. -> 响应 data 节点。

    state: loginSuccess=已绑定(有 st) / register=未绑定阿里云账号(需注册)
           identityVerify=安全验证挑战 / 其它=异常。
    """
    qs = urlencode({"code": code, "appId": APPID})
    headers = {**common_headers(), "Content-Type": "application/x-www-form-urlencoded"}
    response = request_with_proxy(
        "POST",
        f"{AUTHLOGIN_URL}?{qs}",
        headers=headers,
        data=b"",
        proxies=proxies,
        server=server,
    )
    try:
        body = response.json()
    except Exception:
        return {"state": None, "message": f"响应解析失败: {response.text[:300]}"}
    node = body.get("data") if isinstance(body.get("data"), dict) else body
    return node or {}


def get_login_token(server: str, st: str, proxies: Dict[str, str] | None) -> Tuple[str, str]:
    """st -> GetLoginTokenByAuthCode -> {LoginToken, SessionId}。成功时 Code 为空。"""
    resp = open_api(server, ACCOUNT_EP, "GetLoginTokenByAuthCode", ACCOUNT_VER, {
        "AuthCode": st,
        "AccountType": "aliyun",
        "Scene": "WEIXIN_MINI_APP_AUTO_LOGIN",
        "ClientType": FROM_CLIENT,
    }, proxies)
    node = inner_node(resp)
    if node.get("Code"):  # 登录类接口: 有 Code 即失败
        raise RuntimeError(f"换取登录凭证失败: {node.get('Code')} {node.get('Message', '')}")
    lt, sid = node.get("LoginToken"), node.get("SessionId")
    if not (lt and sid):
        raise RuntimeError("换取登录凭证响应缺少 LoginToken/SessionId")
    return lt, sid


def refresh_login_token(server: str, login_token: str, session_id: str, proxies: Dict[str, str] | None) -> Tuple[str, str] | None:
    """RefreshLoginToken: 复用缓存会话。失败返回 None。"""
    try:
        resp = open_api(server, ACCOUNT_EP, "RefreshLoginToken", ACCOUNT_VER, {
            "LoginToken": login_token,
            "SessionId": session_id,
            "ClientId": f"{session_id}0000",
            "ClientType": FROM_CLIENT,
        }, proxies)
        node = inner_node(resp)
        if node.get("Code"):
            return None
        lt = node.get("LoginToken") or login_token
        sid = node.get("SessionId") or session_id
        return lt, sid
    except Exception:
        return None


# ====================== 签到（HAR 实测接口） ======================
# 签到页是独立的 check-in 活动，不出现在 DescribeUserBenefitActivities /
# DescribeOperationActivities 的活动列表里 —— 旧版「从活动列表找 ActivityId」的思路
# 永远定位不到，这就是日志里「未能定位每日签到活动」的根因。
# 正确链路：DescribeCheckInActivity(活动配置) → DescribeUserCheckInInfo(签到日历) → CreateUserCheckIn(执行)


def describe_checkin_activity(server: str, lt: str, sid: str, proxies: Dict[str, str] | None) -> Dict[str, Any]:
    """GET DescribeCheckInActivity: 签到活动配置（每日/额外奖励规则）。"""
    return open_api(server, DESKTOP_EP, "DescribeCheckInActivity", DESKTOP_VER, {
        "LoginToken": lt,
        "SessionId": sid,
        "Activity": CHECKIN_ACTIVITY,
    }, proxies)


def describe_user_checkin_info(server: str, lt: str, sid: str, proxies: Dict[str, str] | None,
                              days_before: int = 3, days_after: int = 3) -> Dict[str, Any]:
    """GET DescribeUserCheckInInfo: 签到日历（每天 Available / Finished / Missed / Pending）。"""
    return open_api(server, DESKTOP_EP, "DescribeUserCheckInInfo", DESKTOP_VER, {
        "LoginToken": lt,
        "SessionId": sid,
        "Activity": CHECKIN_ACTIVITY,
        "DaysBefore": days_before,
        "DaysAfter": days_after,
    }, proxies)


def create_user_checkin(server: str, lt: str, sid: str, proxies: Dict[str, str] | None) -> Dict[str, Any]:
    """POST CreateUserCheckIn: 执行一次签到。"""
    return open_api(server, DESKTOP_EP, "CreateUserCheckIn", DESKTOP_VER, {
        "LoginToken": lt,
        "SessionId": sid,
        "Activity": CHECKIN_ACTIVITY,
    }, proxies, method="POST")


def checkin_data(resp: Dict[str, Any]) -> Dict[str, Any]:
    """阿里云 OpenAPI 的业务数据块在 Data 字段。"""
    data = resp.get("Data") if isinstance(resp, dict) else None
    return data if isinstance(data, dict) else {}


def beijing_today() -> str:
    """服务端签到日历按北京时间。"""
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")


def login_by_code(server: str, code: str, proxies: Dict[str, str] | None) -> Tuple[str | None, str | None, str | None]:
    """authLogin → GetLoginTokenByAuthCode。返回 (login_token, session_id, state)。"""
    log("🔐 [登录] 使用 code 静默登录阿里云账号")
    node = aliyun_authlogin(server, code, proxies)
    state = node.get("state")
    st = node.get("st")
    if state == "loginSuccess" and st:
        lt, sid = get_login_token(server, st, proxies)
        log(f"✅ [登录] LoginToken 获取成功: {mask(lt)}")
        return lt, sid, "ok"

    if state in ("register", "NeedOAuth", None):
        err("❌ [登录] 该微信身份尚未绑定阿里云账号")
        return None, None, ("该微信身份尚未绑定阿里云账号, 需先在小程序「无影云电脑→我的→"
                            "签到/登录」完成手机号授权并绑定阿里云账号后才能签到")
    if state in ("identityVerify", "IV"):
        # 这不是「实名认证」——已实名的账号同样会收到。阿里云对本次静默登录下发了
        # 一次性「安全验证/身份核验」挑战, 脚本不代为完成、也不绕过。
        # 实测: 换 scene/loginType/redirectURL 等参数均无效, 该挑战按【微信账号】记录,
        # 与设备无关 —— 只能在那个微信号上完成一次验证。
        err("❌ [登录] 阿里云对本次静默登录下发了安全验证挑战(identityVerify)")
        log("   → 挑战按【微信账号】记录, 与设备无关: 在手机用【同一个微信号】完成一次安全验证,")
        log("     之后本脚本用该号的 code 静默登录即可通过。")
        log("   → 若小程序里点登录没弹验证, 说明本地还留着旧会话: 先「我的→退出登录」或删掉")
        log("     小程序清缓存, 再重新登录就会走授权流程并弹出验证。")
        return None, None, ("阿里云下发『安全验证』挑战(state=identityVerify, 与实名认证无关)。"
                            "解法: 用同一个微信号在手机小程序退出登录后重新登录并完成安全验证"
                            "(挑战按微信账号记录, 完成后脚本可自动登录); "
                            "或临时填 wuying_token(LoginToken#SessionId, 实测约半天即过期)")
    err(f"❌ [登录] 登录状态异常(state={state})")
    return None, None, f"登录状态异常(state={state}), 请在小程序内手动登录一次后重试"


# ====================== Token缓存管理 ======================
def load_token_cache() -> Dict[str, Any]:
    try:
        if os.path.exists(COOKIE_FILE):
            with open(COOKIE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as exc:
        warn(f"⚠️ [缓存] 读取失败: {exc}")
    return {}


def save_token_cache(cache: Dict[str, Any]) -> None:
    try:
        with open(COOKIE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
        log("✅ [缓存] Token保存成功")
    except Exception as exc:
        err(f"❌ [缓存] 保存失败: {exc}")


def get_cached_token(server: str) -> str | None:
    cache = load_token_cache()
    data = cache.get(server)
    if data and data.get("token") and data.get("expireTime"):
        try:
            expire = datetime.fromisoformat(data["expireTime"]).timestamp() * 1000
            if time.time() * 1000 < expire - 3600 * 1000:
                log(f"✅ [缓存] 使用 {server} token")
                return data["token"]
        except Exception as exc:
            warn(f"⚠️ [缓存] 过期时间解析异常: {exc}")
    return None


def get_cached_session_id(server: str) -> str:
    cache = load_token_cache()
    data = cache.get(server)
    if isinstance(data, dict):
        return str(data.get("sessionId") or "")
    return ""


def set_cached_token(server: str, token: str, expire_time: str, session_id: str = "") -> None:
    cache = load_token_cache()
    cache[server] = {
        "token": token,
        "sessionId": session_id,
        "expireTime": expire_time,
        "updateTime": datetime.now().isoformat(),
    }
    save_token_cache(cache)


def remove_cached_token(server: str) -> None:
    cache = load_token_cache()
    if cache.pop(server, None) is not None:
        save_token_cache(cache)


def login_with_cache(server: str, proxies: Dict[str, str] | None, force: bool = False) -> Tuple[str | None, str | None, str | None]:
    """会话获取: 手动会话 -> 缓存(RefreshLoginToken 续期兼验证) -> code 静默登录。
    返回 (login_token, session_id, state)。state 为 'ok' 表示已登录, 其余为说明文案。"""
    # 0) 手动会话(逃生口): 静默登录被阿里云风控拦下时, 从小程序抓 LoginToken/SessionId
    manual = MANUAL_SESSIONS.get(server, "")
    if manual and not force:
        parts = [p.strip() for p in manual.replace("&", "#").split("#") if p.strip()]
        if len(parts) >= 2:
            refreshed = refresh_login_token(server, parts[0], parts[1], proxies)
            if refreshed:
                lt, sid = refreshed
                log(f"✅ [缓存] 使用手动会话(已刷新): {mask(lt)}")
                set_cached_token(server, lt, datetime.fromtimestamp(time.time() + 24 * 3600).isoformat(), sid)
                return lt, sid, "ok"
            warn("⚠️ [缓存] wuying_token 已失效, 回退到静默登录")
        else:
            warn("⚠️ [缓存] wuying_token 格式应为 LoginToken#SessionId, 已忽略")

    # 1) 复用缓存并尝试刷新（RefreshLoginToken 成功即验证会话仍有效）
    if force:
        remove_cached_token(server)
    else:
        cached_token = get_cached_token(server)
        cached_sid = get_cached_session_id(server)
        if cached_token and cached_sid:
            refreshed = refresh_login_token(server, cached_token, cached_sid, proxies)
            if refreshed:
                lt, sid = refreshed
                log(f"✅ [缓存] 会话续期成功: {mask(lt)}")
                set_cached_token(server, lt, datetime.fromtimestamp(time.time() + 24 * 3600).isoformat(), sid)
                return lt, sid, "ok"
            warn("⚠️ [缓存] 会话已失效，重新登录")
            remove_cached_token(server)

    # 2) 静默登录 (wx.login code -> authLogin)
    code = get_code(server)
    if not code:
        return None, None, "code 获取失败"
    return login_by_code(server, code, proxies)


def run_account(index: int, total: int, server: str) -> Dict[str, Any]:
    result = {
        "server": server,
        "success": False,
        "proxyStatus": "未使用代理",
        "proxyIp": "-",
        "token": "-",
        "signMsg": "-",
        "error": "",
    }

    log_account_header(index, total, server)

    proxies, proxy_ip = get_valid_proxy(server)
    result["proxyStatus"] = "使用专属代理" if proxies else "使用直连"
    result["proxyIp"] = proxy_ip or "-"

    sleep(PROXY_FETCH_INTERVAL)

    delay = random.randint(2, 6)
    log(f"⏳ [延迟] 启动延迟 {delay}s")
    sleep(delay)

    lt, sid, state = login_with_cache(server, proxies)
    if not lt:
        result["error"] = state or "登录失败"
        warn(f"⚠️ [账号] {result['error']}")
        return result

    result["token"] = mask(lt)

    try:
        # 签到: DescribeCheckInActivity(规则) → DescribeUserCheckInInfo(日历) → CreateUserCheckIn(执行)
        today = beijing_today()
        info = describe_user_checkin_info(server, lt, sid, proxies)
        if is_session_invalid(info):  # 会话失效 -> 强制重登重试一次
            warn("⚠️ [签到] 会话失效, 重新登录...")
            lt, sid, state = login_with_cache(server, proxies, force=True)
            if not lt:
                result["error"] = state or "重新登录失败"
                warn(f"⚠️ [账号] {result['error']}")
                return result
            result["token"] = mask(lt)
            info = describe_user_checkin_info(server, lt, sid, proxies)

        if not biz_ok(info):
            msg = f"签到状态查询失败: {biz_code(info)}"
            result["signMsg"] = msg
            warn(f"⚠️ [签到] {msg}")
            return result

        act = checkin_data(describe_checkin_activity(server, lt, sid, proxies))
        if act.get("Title"):
            log(f"ℹ️ [签到] 活动: {act.get('Title')} · {act.get('SubTitle')}")

        node = checkin_data(info)
        records = node.get("CheckinRecord") or []
        today_rec = next((r for r in records if str(r.get("Date")) == today), None)
        status = str((today_rec or {}).get("Status") or "")
        summary = node.get("CheckinSummary") or {}

        # 幂等: 今天已签过就不再提交
        if status == "Finished":
            msg = (f"今日已签到 (累计 {summary.get('Duration', 0)} 天, "
                   f"共得 {summary.get('TotalRewardCoreHour', 0)} 核时)")
            result["signMsg"] = msg
            log(f"✅ [签到] {msg}")
            result["success"] = True
            return result

        if status != "Available":
            msg = f"今日签到状态为 {status or '未知'}, 未执行签到"
            result["signMsg"] = msg
            warn(f"⚠️ [签到] {msg}")
            return result

        wait_time = random.randint(2, 5)
        log(f"⏳ [签到] 提交前等待 {wait_time}s")
        sleep(wait_time)

        resp = create_user_checkin(server, lt, sid, proxies)
        data = checkin_data(resp)
        code = str(resp.get("Code"))
        err_msg = resp.get("Message") or ""
        if code == BIZ_OK:
            hours = data.get("RewardCoreHour")
            title = data.get("Title") or "签到成功"
            result["signMsg"] = f"{title}" + (f"，获得 {hours} 核时" if hours is not None else "")
            log(f"🎉 [签到] {result['signMsg']}")
            result["success"] = True
        elif any(k in (code + err_msg).lower() for k in ("already", "repeat", "duplicate", "已签", "已参与", "重复")):
            result["signMsg"] = "今日已签到"
            log(f"✅ [签到] {result['signMsg']}")
            result["success"] = True
        else:
            result["signMsg"] = f"签到失败: {code} {err_msg}".strip()
            err(f"❌ [签到] {result['signMsg']}")

        return result

    except Exception as exc:
        result["error"] = traceback.format_exc().strip()
        err(f"❌ [账号] 执行失败: {exc}")
        return result


def build_notify(results: List[Dict[str, Any]]) -> str:
    success_count = sum(1 for item in results if item["success"])
    fail_count = len(results) - success_count

    content = f"""☁️ 无影云电脑签到任务结果

━━━━━━━━━━━━━━━━━━━━
🏁 总结：{success_count} 成功 / {fail_count} 失败
🕒 时间：{now_text()}
━━━━━━━━━━━━━━━━━━━━
"""

    for idx, res in enumerate(results, 1):
        icon = "✅" if res["success"] else "❌"

        content += f"""
🧩 账号 {idx}
📝 签到：{res["signMsg"]}
{icon} 结果：{"成功" if res["success"] else "失败"}
"""

        if not res["success"]:
            content += f"❌ 原因：{res['error']}\n"

        content += "━━━━━━━━━━━━━━━━━━━━\n"

    return content


def main() -> None:
    log_startup()
    log_title()

    if not SERVERS:
        log("🚫 [账号] 没有可执行账号（YYB_SERVER 为空或已被 YYB_ONLY_REFS 过滤空）")
        return 1

    results: List[Dict[str, Any]] = []

    for index, server in enumerate(SERVERS, 1):
        try:
            result = run_account(index, len(SERVERS), server)
            results.append(result)
        except Exception as exc:
            err(f"❌ [主程序] {server} 执行异常: {exc}")
            results.append({
                "server": server,
                "success": False,
                "proxyStatus": "-",
                "proxyIp": "-",
                "token": "-",
                "signMsg": "-",
                "error": traceback.format_exc().strip(),
            })

        if index < len(SERVERS):
            log("⏳ [间隔] 等待 2s 后处理下一个账号")
            sleep(2)

    success_count = sum(1 for item in results if item["success"])
    fail_count = len(results) - success_count
    log("📊 [执行汇总] 无影云电脑 · 账号 %d ｜ 成功 %d ｜ 失败 %d" % (len(results), success_count, fail_count))

    print()
    log("╔" + "═" * 50 + "╗")
    log("║ 🏁 无影云电脑任务执行完成                    ║")
    log(f"║ ✅ 成功: {success_count:<39}║")
    err(f"║ ❌ 失败: {fail_count:<39}║")
    log(f"║ 🕒 结束时间: {now_text():<32}║")
    log("╚" + "═" * 50 + "╝")

    send_pushplus("☁️ 无影云电脑任务完成", build_notify(results))
    return fail_count


# --- YYB compatibility layer (managed) ---
import os as _yyb_os
import json as _yyb_json

def _yyb_accounts():
    result = []
    for line in _yyb_os.getenv("YYB_SERVER", "").splitlines():
        line = line.strip()
        if not line or "@" not in line or line == "[object Object]":
            continue
        endpoint, ref = (part.strip() for part in line.split("@", 1))
        if endpoint and ref:
            if not endpoint.startswith(("http://", "https://")):
                endpoint = "http://" + endpoint
            result.append(endpoint.rstrip("/") + "@" + ref)
    return result


def _yyb_parts(server):
    value = str(server).strip()
    if "@" not in value:
        return value.rstrip("/"), ""
    return value.rsplit("@", 1)[0].rstrip("/"), value.rsplit("@", 1)[1]


def _yyb_appid(args, kwargs):
    appid = kwargs.get("appid") or kwargs.get("app_id")
    if not appid and args and isinstance(args[0], str):
        appid = args[0]
    if not appid:
        appid = globals().get("APPID") or globals().get("APP_ID") or ""
    if isinstance(appid, (list, tuple)):
        appid = appid[0] if appid else ""
    return str(appid)


def _yyb_json_request(server, path, appid, payload=None):
    import requests
    endpoint, ref = _yyb_parts(server)
    if not endpoint or not ref or not appid:
        raise RuntimeError("YYB 参数不完整：需要 地址@账号ID 和 app_id")
    headers = {}
    api_key = _yyb_os.getenv("YYB_API_KEY", "").strip()
    if api_key:
        headers["Authorization"] = "Bearer " + api_key
    response = requests.post(
        endpoint + path,
        json={"ref": ref, "app_id": str(appid), **(payload or {})},
        headers=headers,
        timeout=30,
    )
    try:
        body = response.json()
    except ValueError as exc:
        raise RuntimeError("YYB 返回非 JSON") from exc
    if response.status_code >= 400:
        raise RuntimeError(str(body.get("message") or body.get("msg") or body))
    return body


def _yyb_find_code(value):
    if isinstance(value, dict):
        if value.get("code") not in (None, "", "null", "invalid") and isinstance(value.get("code"), str):
            return value["code"]
        for child in value.values():
            found = _yyb_find_code(child)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _yyb_find_code(child)
            if found:
                return found
    return None


def _yyb_code(server, *args, **kwargs):
    body = _yyb_json_request(server, "/wxapp/getCode", _yyb_appid(args, kwargs))
    code = _yyb_find_code(body)
    if not code:
        raise RuntimeError(str(body.get("msg") or body.get("message") or "YYB 未返回 wx.login code"))
    return str(code)


def _yyb_phone(server, *args, **kwargs):
    body = _yyb_json_request(server, "/wxapp/getPhoneNumber", _yyb_appid(args, kwargs))
    return body.get("result") or body.get("data") or body


if _yyb_accounts():
    SERVERS = _yyb_accounts()
    # 手动会话是按账号顺序绑定的, 必须等 SERVERS 换成 YYB 账号之后再重建映射,
    # 否则 MANUAL_SESSIONS 的 key 仍是默认的本地 code 地址, 逃生口永远匹配不上。
    MANUAL_SESSIONS = {server: value for server, value in zip(SERVERS, _manual_session_lines)}

# 账号序号白名单：按 YYB_SERVER 的书写顺序从 1 开始编号，留空 [] 表示全部
if YYB_ONLY_REFS:
    _before = len(SERVERS)
    _wanted = {int(r) for r in YYB_ONLY_REFS if str(r).strip().isdigit() and int(str(r).strip()) > 0}
    if _wanted:
        SERVERS = [s for i, s in enumerate(SERVERS, 1) if i in _wanted]
        log(f"ℹ️ [账号] 按 YYB_ONLY_REFS 筛选：请求序号 {sorted(_wanted)}，命中 {len(SERVERS)}/{_before} 个账号")
    else:
        warn(f"⚠️ [账号] YYB_ONLY_REFS={YYB_ONLY_REFS} 无效（需为正整数序号），将运行全部账号")
    if not SERVERS:
        log(f"🚫 [账号] YYB_ONLY_REFS 指定的序号均超出账号范围（可选账号共 {_before} 个），无任务可执行")


_yyb_original_get_code = get_code


def get_code(server, *args, **kwargs):
    if "@" in str(server):
        phone_code = kwargs.get("phone_code") is True or (args and args[0] is True)
        if phone_code:
            body = _yyb_phone(server)
            code = _yyb_find_code(body)
            if not code:
                raise RuntimeError("YYB 未返回手机号授权 code")
            return str(code)
        return _yyb_code(server, *args, **kwargs)
    return _yyb_original_get_code(server, *args, **kwargs)


if "get_code_for" in globals():
    _yyb_original_get_code_for = get_code_for

    def get_code_for(server, appid):
        if "@" in str(server):
            return _yyb_code(server, appid)
        return _yyb_original_get_code_for(server, appid)


def get_phone_payload(server, *args, **kwargs):
    if "@" in str(server):
        return _yyb_phone(server, *args, **kwargs)
    raise RuntimeError("当前账号不是 YYB_SERVER 格式，无法获取手机号授权包")


# Adapt the two common source-specific phone helpers when present.
if "get_phone_number_payload" in globals():
    get_phone_number_payload = get_phone_payload
if "get_phone_authorize" in globals():
    get_phone_authorize = get_phone_payload
if "get_phone_data" in globals():
    get_phone_data = get_phone_payload
if "get_phone_package" in globals():
    _yyb_original_get_phone_package = get_phone_package

    def get_phone_package(server, *args, **kwargs):
        if "@" not in str(server):
            return _yyb_original_get_phone_package(server, *args, **kwargs)
        body = _yyb_phone(server, *args, **kwargs)
        # YYB keeps the upstream response shape. Do not invent encryptedData/iv.
        if isinstance(body, dict):
            result = body.get("result") if isinstance(body.get("result"), dict) else body
            data = result.get("data") if isinstance(result, dict) and isinstance(result.get("data"), dict) else result
            raw = data.get("raw") if isinstance(data, dict) and isinstance(data.get("raw"), dict) else data
            if isinstance(raw, dict):
                return {
                    "authCode": str(raw.get("code") or data.get("code") or ""),
                    "encryptPhoneNumber": str(raw.get("encryptedData") or ""),
                    "initVector": str(raw.get("iv") or ""),
                }
        return {"authCode": "", "encryptPhoneNumber": "", "initVector": ""}
if "get_userinfo_blob" in globals():
    _yyb_original_get_userinfo_blob = get_userinfo_blob

    def get_userinfo_blob(server):
        if "@" not in str(server):
            return _yyb_original_get_userinfo_blob(server)
        body = _yyb_json_request(server, "/wx/getuserinfo", _yyb_appid((), {}))
        info = body.get("user_info")
        # /wx/getuserinfo is a profile endpoint; it is not a source of
        # encryptedData/iv/signature. Never fabricate those fields.
        if not isinstance(info, dict):
            return None
        return {"userInfo": info, "rawData": _yyb_json.dumps(info, ensure_ascii=False), "errMsg": "getUserInfo:ok"}
# --- end YYB compatibility layer ---

if __name__ == "__main__":
    sys.exit(main())
