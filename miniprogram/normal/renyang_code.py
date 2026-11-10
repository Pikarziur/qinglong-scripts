#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# =========================================================
# name:  认养一头牛官方商城
# version: 2026-11-10a
# cron: 0 6,16 * * *
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
人本（认养一头牛官方商城）code 版

功能：
  1. 本地 code 服务获取微信 code
  2. /mall/xhr/minilogin 使用 code 换 token（token 优先取响应头 X-Auth-Token）
  3. 每日签到（签到并返回等级/手机号/积分）
  4. 自动申请可申请的试用商品（按等级匹配）
  5. 查询中奖记录
  6. 社区每日答题（缓存命中优先；未命中用知识表启发式打分，未覆盖则随机）
  7. 社区发帖种草后自动删帖
  8. PushPlus 推送
  9. 品赞代理，业务请求优先代理，失败直连兜底

环境变量：
  PLUSPLUS_TOKEN    PushPlus token，可选
  QYWX_TOKEN        企业微信机器人 Webhook key，可选（机器人地址 ?key= 后面的值）
  PROXY_API         品赞代理提取 API，可选
  PROXY_TYPE        http / socks5，默认 http
  CODE_SERVER       本地 code 服务地址，默认 127.0.0.1:8088
  YYB_SERVER        YYB 账号基座，格式「地址@账号ID」，每行一个
  YYB_ONLY_REFS     账号序号白名单（1 起，按 YYB_SERVER 行序），见脚本顶部硬编码常量，留空跑全部

⚠️ 登录限制：源脚本为“手机号一键授权登录”，/mall/xhr/minilogin 正常需要
   encryptedData/offset(iv)/phoneCode（手机号授权数据，本地 code 服务无法提供），
   本脚本仅用 wx.login 的 wxCode 尝试登录，未真机验证，失败请抓包核对。

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
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

import requests

# ========== 答题知识表（可选依赖，缺失时自动降级为随机） ==========
# ryytn_quiz_knowledge.py 与本脚本同目录，存放「成分 → 公认生理功能」的静态字典。
# 加载失败不影响主流程：社区答题会自动退化为随机作答（= 无知识表时的行为）。
try:
    import importlib.util as _imp_util

    _KB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ryytn_quiz_knowledge.py")
    _KB = {}
    if os.path.exists(_KB_PATH):
        _spec = _imp_util.spec_from_file_location("ryytn_quiz_knowledge", _KB_PATH)
        if _spec and _spec.loader:
            _mod = _imp_util.module_from_spec(_spec)
            _spec.loader.exec_module(_mod)
            _KB = {
                "knowledge": getattr(_mod, "QUIZ_KNOWLEDGE", {}) or {},
                "qtype_rules": getattr(_mod, "QUIZ_QTYPE_RULES", ()) or (),
                "ratio_range": getattr(_mod, "QUIZ_RATIO_RANGE", (1.0, 3.0)) or (1.0, 3.0),
                "safe_words": getattr(_mod, "QUIZ_SAFE_WORDS", ()) or (),
                "danger_words": getattr(_mod, "QUIZ_DANGER_WORDS", ()) or (),
                "absurd_words": getattr(_mod, "QUIZ_ABSURD_WORDS", ()) or (),
            }
except Exception as _kb_exc:  # noqa: BLE001
    _KB = {}
    print("⚠️ [答题知识表] 加载失败，答题将退化为随机: %s" % _kb_exc)

# ========== 账号分隔标识 ==========
# 多账号同跑时, 用醒目横线把各账号日志隔开, 便于阅读与定位。
# ========== 启动信息 ==========
def log_startup(total: int = 0) -> None:
    log(ACC_RULE)
    log("📌 脚本版本: %s ｜ %s" % (SCRIPT_VER, APPID))
    if total:
        log("🔢 账号数量: %d" % total)
    log(ACC_RULE)
SCRIPT_VER = "2026-11-10a"
ACC_RULE = "=" * 70

# ========== 统一日志：[LEVEL] [RY] message ==========
def _emit(level, msg):
    line = "[%s] [RY] %s" % (level, msg)
    print(line, flush=True)


def log(msg):
    _emit("INFO", msg)


def warn(msg):
    _emit("WARN", msg)


def err(msg):
    _emit("ERROR", msg)



APP_NAME = "人本（认养一头牛官方商城）"
APPID = "wx0408f3f20d769a2f"

SERVERS = [
    "127.0.0.1:8088",
]

if os.getenv("CODE_SERVER"):
    SERVERS = [os.getenv("CODE_SERVER")]

PLUSPLUS_TOKEN = os.getenv("PLUSPLUS_TOKEN", "")
PROXY_API = os.getenv("PROXY_API", "")
PROXY_TYPE = os.getenv("PROXY_TYPE", "http").lower()

PROXY_RETRY_TIMES = 3
PROXY_VALIDATE_URL = "http://httpbin.org/ip"
PROXY_FETCH_INTERVAL = 3
ENABLE_DIRECT_FALLBACK = True
REQUEST_TIMEOUT = 30

# YYB-GO 请求超时（与业务接口分开）：YYB-GO 内部会轮询多个上游长连接节点
# （实测候选 6 个），失败路径约 40 秒才返回。超时太紧会把真实错误码
# （如 manual_auth -10）吞成 "Read timed out"，看不到根因。
# 取码/取号给 90 秒读超时，业务接口仍用各自的短超时。
YYB_TIMEOUT = (10, 90)

# ── YYB-GO 熔断（HTTP 429）处理 ────────────────────────────────────────
# 2026-11-05：YYB-GO 对 (账号, AppID, 操作类型) 做目标层熔断，连续 2 次 -14
# 就暂停该目标 10 分钟；取码限速也会返回 429。此时请求根本没发往上游，
# 继续换账号硬打只会让熔断扩散、放大上游风控。
#
# 实测数据（同账号相邻两次熔断间隔）：最小 6s / 中位 10790s(约3小时) / 最大约2天。
# 中位数远超一轮任务的时长，等待没有实际意义，因此**不做自动退避重试**：
# 遇 429 立即放弃本账号，把 Retry-After 秒数打印出来，由下一次定时任务自然重试。
#
# ── YYB 取码重试（2026-11-09 新增）────────────────────────────────
#
# 现象：YYB-GO 频繁返回 HTTP 502，响应 msg 为
#       "call failed: context deadline exceeded" 或
#       "all ShortLink targets failed ... i/o timeout"
#       （上游长连接节点 8 秒内不响应），属**纯连接层**故障 ——
#       不是账号/授权问题，也拿不到上游错误码。
#
# 为什么必须由脚本自己重试：YYB-GO 的编队转移清单只含「成员被上游推回」
# 类错误（-14 / 凭据签发拒绝 / 预算耗尽 / 会话不可用 / 账号过期），
# 连接层超时**不触发 fleet.failover**，所以一个成员超时就整轮失败。
# 而 YYB-GO 每次新请求会按「最近最少使用」把请求轮转到编队的下一个成员，
# 重试正好能换到可用成员。
#
# 只对「上游连接超时」重试：
#   - 429（本地限速/熔断）：服务端已在冷却，重试只会加剧风控 → 不重试
#   - 404 / 409（账号不存在/失效）：不可恢复 → 不重试
#   - 502 且带上游错误码（-14 等授权拒绝）：重试无用 → 不重试
# 间隔需大于 YYB 的取码最小间隔（YYB_CODE_MIN_GAP=3s），否则会被限速。
YYB_RETRY_TIMES = int(os.getenv("RY_YYB_RETRY", "5") or 5)      # 额外重试次数（不含首次）
YYB_RETRY_DELAY = int(os.getenv("RY_YYB_RETRY_DELAY", "4") or 4)  # 重试间隔秒数

BASE_URL = "https://www.milkcard.mall.ryytngroup.com"
LOGIN_URL = f"{BASE_URL}/mall/xhr/minilogin"

CHECKIN_SAVE_URL = f"{BASE_URL}/mall/xhr/task/checkin/save"
CHECKIN_RULE_URL = f"{BASE_URL}/mall/xhr/task/checkin/getRule"
ADDRESS_LIST_URL = f"{BASE_URL}/mall/xhr/address/receive/list"
TRIAL_LIST_URL = f"{BASE_URL}/mall/xhr/freeTrial/getList"
TRIAL_APPLY_URL = f"{BASE_URL}/mall/xhr/freeTrial/apply"
WINNING_LIST_URL = f"{BASE_URL}/mall/xhr/freeTrialUser/getList"
QUIZ_ACTIVITIES_URL = f"{BASE_URL}/mall/xhr/quizActivity/activities"
QUIZ_SUBMIT_URL = f"{BASE_URL}/mall/xhr/quizActivity/submit"
COMMUNITY_RECOMMEND_URL = f"{BASE_URL}/mall/xhr/community/home/recommend/item"
COMMUNITY_PUSH_URL = f"{BASE_URL}/mall/xhr/community/posts/push"

COOKIE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ryytncookie.json")
QUIZ_CACHE_NAME = "ryytn_quiz_cache.json"
QUIZ_CACHE_MAX = 500

# 每日任务完成标记：记录「今天这个账号已经完整跑完」，避免同一天重复调用。
# 结构：{ "<server>": {"date": "2026-11-05", "doneTime": "...", "point": "231"} }
# 跨天自动失效（读时比对当天日期）；标记文件在脚本同目录，非隐藏，便于面板查看/删除。
DAILY_DONE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ryytn_daily_done.json")

# 账号序号白名单：按 YYB_SERVER 的书写顺序从 1 开始编号，留空 [] 表示全部
YYB_ONLY_REFS = []

USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_3 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Mobile/15E148 MicroMessenger/8.0.61(0x18003d24) "
    "NetType/4G Language/zh_CN"
)


def now_text() -> str:
    # 兼容两种导入: from datetime import datetime / import datetime
    _dt = datetime.now() if hasattr(datetime, "now") else datetime.datetime.now()
    return _dt.strftime("%Y-%m-%d %H:%M:%S")



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
    data = resp.get("data")
    return data if isinstance(data, dict) else {}


def mask_phone(phone: str) -> str:
    phone = str(phone or "")
    if len(phone) >= 11:
        return f"{phone[:3]}****{phone[-4:]}"
    return phone


def beijing_today_0am() -> str:
    now = datetime.now(timezone(timedelta(hours=8)))
    return now.strftime("%Y-%m-%d 00:00:00")


def log_title() -> None:
    print()
    log("╔" + "═" * 50 + "╗")
    log("║ 🥛 人本（认养一头牛）动态 code 版            ║")
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


def common_headers(token: str | None = None) -> Dict[str, str]:
    headers = {
        "User-Agent": USER_AGENT,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "xweb_xhr": "1",
        "Referer": f"https://servicewechat.com/{APPID}/305/page-frame.html",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    if token:
        headers["X-Auth-Token"] = token
    return headers


def extract_token(data: Any) -> str | None:
    if not isinstance(data, dict):
        return None

    candidates = [
        data.get("token"),
        data.get("x-auth-token"),
        data.get("xAuthToken"),
        data.get("accessToken"),
        data.get("access_token"),
    ]

    inner = data.get("data")
    if isinstance(inner, dict):
        candidates.extend([
            inner.get("token"),
            inner.get("x-auth-token"),
            inner.get("xAuthToken"),
            inner.get("accessToken"),
            inner.get("access_token"),
        ])

    for item in candidates:
        if item and item != "null":
            return str(item)

    return None


def login_by_code(server: str, code: str, proxies: Dict[str, str] | None) -> Tuple[str | None, Dict[str, Any] | None]:
    payload = {
        "encryptedData": "",
        "offset": "",
        "wxCode": code,
        "code": "",
    }

    # YYB 账号：minilogin 必须同时带手机号授权三件套（encryptedData / offset=iv / phoneCode），
    # 只给 wxCode 会被服务端静默丢弃（HTTP 200 + 空响应体），换不到 token。
    if "@" in str(server):
        try:
            pkg = _yyb_phone_package(server)
            payload["encryptedData"] = pkg["encryptPhoneNumber"]
            payload["offset"] = pkg["initVector"]
            payload["code"] = pkg["authCode"]
            log("✅ [登录] 已获取手机号授权数据（encryptedData/offset/code）")
        except _YybCircuitOpen:
            # 熔断 = 服务端本地挡住，请求根本没发往上游。此时继续拿空授权数据去请求
            # minilogin 必然白跑一次业务接口，直接放弃本账号，别把熔断扩散到其它账号。
            raise
        except _YybAccountInvalid as exc:
            warn(f"🚫 [登录] {exc}")
            return None, None
        except Exception as exc:
            warn(f"⚠️ [登录] 手机号授权数据获取失败，将以空授权数据尝试: {exc}")

    try:
        log("🔐 [登录] 使用 code 换 token")
        response = request_with_proxy(
            "POST",
            LOGIN_URL,
            headers=common_headers(),
            json=payload,
            proxies=proxies,
            server=server,
        )

        # token 优先取响应头 X-Auth-Token
        token = response.headers.get("X-Auth-Token") or response.headers.get("x-auth-token")

        try:
            data = response.json()
        except Exception:
            data = {"raw": response.text[:800]}

        if not token:
            token = extract_token(data)

        if token:
            log(f"✅ [登录] token 获取成功: {mask(token)}")
            return str(token), data

        # 空响应体 = 服务端在 encryptedData/offset 为空时直接不回内容（实测 200 + Content-Length: 0）
        if not (response.text or "").strip():
            print("❌ [登录] 服务端返回空响应（HTTP %s，Content-Length: %s）"
                  % (response.status_code, response.headers.get("Content-Length")))
            print("   原因：/mall/xhr/minilogin 要求同时带 encryptedData + offset"
                  "（小程序用户信息授权数据），只给 wxCode 会被服务端静默丢弃")
            print("   → 纯 code 换 token 走不通。可改用 /mall/xhr/login（手机号+验证码），"
                  "或抓包补一次授权数据")
            return None, data

        err(f"❌ [登录] 未识别 token 字段: {json_preview(data)}")
        return None, data
    except Exception as exc:
        err(f"❌ [登录] 请求异常: {exc}")
        return None, None


def api_get(server: str, url: str, token: str, proxies: Dict[str, str] | None) -> Dict[str, Any]:
    response = request_with_proxy(
        "GET",
        url,
        headers=common_headers(token),
        proxies=proxies,
        server=server,
    )
    try:
        return response.json()
    except Exception:
        return {
            "code": -1,
            "msg": f"JSON解析失败: {response.text[:300]}",
        }


def api_post(server: str, url: str, token: str, proxies: Dict[str, str] | None, payload: Dict[str, Any]) -> Dict[str, Any]:
    response = request_with_proxy(
        "POST",
        url,
        headers=common_headers(token),
        json=payload,
        proxies=proxies,
        server=server,
    )
    try:
        return response.json()
    except Exception:
        return {
            "code": -1,
            "msg": f"JSON解析失败: {response.text[:300]}",
        }


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


# ====================== 每日完成标记 ======================
def _today() -> str:
    """当天日期（本地时区），用于跨天判定。"""
    return datetime.now().strftime("%Y-%m-%d")


def load_daily_done() -> Dict[str, Any]:
    """读取每日完成标记；文件不存在或损坏时返回空字典。"""
    try:
        if os.path.exists(DAILY_DONE_FILE):
            with open(DAILY_DONE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
    except Exception as exc:
        warn(f"⚠️ [标记] 读取失败: {exc}")
    return {}


def save_daily_done(cache: Dict[str, Any]) -> None:
    """写标记文件（临时文件 + rename 原子替换，避免写坏）。"""
    try:
        tmp = DAILY_DONE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
        os.replace(tmp, DAILY_DONE_FILE)
    except Exception as exc:
        err(f"❌ [标记] 保存失败: {exc}")


def is_done_today(server: str) -> bool:
    """该账号今天是否已经完整跑过（是则本次直接跳过）。"""
    entry = load_daily_done().get(server)
    if not isinstance(entry, dict):
        return False
    return str(entry.get("date") or "") == _today()


def mark_done_today(server: str, point: str = "") -> None:
    """记录该账号今天已完成。"""
    cache = load_daily_done()
    # 只保留当天的记录，跨天自动清理，避免文件无限增长
    today = _today()
    cache = {k: v for k, v in cache.items()
             if isinstance(v, dict) and str(v.get("date") or "") == today}
    cache[server] = {
        "date": today,
        "doneTime": now_text(),
        "point": str(point or ""),
    }
    save_daily_done(cache)
    log(f"✅ [标记] 今日任务已完成，已记录（{today}）")


def clear_daily_done(server: str = "") -> None:
    """清除指定账号（或全部）的今日完成标记。"""
    cache = load_daily_done()
    if server:
        cache.pop(server, None)
    else:
        cache = {}
    save_daily_done(cache)


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


def set_cached_token(server: str, token: str, expire_time: str) -> None:
    cache = load_token_cache()
    cache[server] = {"token": token, "expireTime": expire_time, "updateTime": datetime.now().isoformat()}
    save_token_cache(cache)


def login_with_cache(server: str, proxies: Dict[str, str] | None) -> Tuple[str | None, Dict[str, Any] | None]:
    """优先使用缓存 token（中奖记录接口验证），失效自动 code 刷新"""
    cache_token = get_cached_token(server)
    if cache_token:
        log("🔍 [缓存] 验证 token")
        try:
            resp = api_post(
                server,
                WINNING_LIST_URL,
                cache_token,
                proxies,
                {"parentTabStatus": 1, "pageNum": 1, "pageSize": 10, "subTabStatus": 1},
            )
            if resp.get("code") == 200:
                log("✅ [缓存] token 有效")
                return cache_token, None
        except Exception as exc:
            warn(f"⚠️ [缓存] 验证异常: {exc}")
        warn("⚠️ [缓存] token 已失效，重新登录")

    try:
        code = get_code(server)
    except _YybCircuitOpen as exc:
        # 取码也被熔断/限速挡住：请求没发往上游，换号继续打只会扩大熔断。
        warn(f"🚫 [熔断] 取码被熔断，放弃本账号（{exc}）")
        return None, {"error": str(exc)}
    if not code:
        return None, None

    try:
        token, raw_login = login_by_code(server, code, proxies)
    except _YybCircuitOpen as exc:
        # 同理：熔断期间不再重试，也不去请求 minilogin（必然拿不到 token）。
        wait = exc.retry_after
        tip = f"，约 {wait}s 后解除" if wait else ""
        warn(f"🚫 [熔断] 手机号授权被熔断，放弃本账号（{exc}{tip}）")
        return None, {"error": str(exc)}
    except _YybAccountInvalid as exc:
        warn(f"🚫 [登录] {exc}")
        return None, {"error": str(exc)}

    if not token:
        return None, raw_login

    expire_time = None
    if raw_login and isinstance(raw_login, dict):
        inner = raw_login.get("data")
        if isinstance(inner, dict):
            expire_time = inner.get("expireTime") or inner.get("expire_time")
            expires_in = inner.get("expiresIn")
            if not expire_time and isinstance(expires_in, (int, float)) and expires_in > 0:
                expire_time = datetime.fromtimestamp(time.time() + expires_in).isoformat()
    if not expire_time:
        expire_time = datetime.fromtimestamp(time.time() + 24 * 3600).isoformat()
    elif not isinstance(expire_time, str):
        expire_time = datetime.fromtimestamp(expire_time / 1000).isoformat()
    set_cached_token(server, token, expire_time)
    return token, raw_login


def check_checkin(server: str, token: str, proxies: Dict[str, str] | None) -> Dict[str, Any]:
    """签到并返回账号状态（幂等），data 含 grade/phone/point"""
    return api_post(server, CHECKIN_SAVE_URL, token, proxies, {})


def quiz_cache_path() -> str:
    """答题缓存路径：优先放青龙数据目录，避免污染仓库目录。"""
    for base in ("/ql/data", "/ql/data/scripts"):
        if os.path.isdir(base):
            return os.path.join(base, QUIZ_CACHE_NAME)
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), QUIZ_CACHE_NAME)


def load_quiz_cache() -> Dict[str, Any]:
    try:
        path = quiz_cache_path()
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
    except Exception as exc:
        warn(f"⚠️ [答题缓存] 读取失败: {exc}")
    return {}


def save_quiz_cache(cache: Dict[str, Any]) -> None:
    try:
        items = list(cache.items())
        if len(items) > QUIZ_CACHE_MAX:
            items = items[-QUIZ_CACHE_MAX:]
        with open(quiz_cache_path(), "w", encoding="utf-8") as f:
            json.dump(dict(items), f, ensure_ascii=False, indent=2)
    except Exception as exc:
        warn(f"⚠️ [答题缓存] 保存失败: {exc}")


def quiz_cache_get(title: str) -> str:
    """按题干取缓存的正确答案文本。"""
    if not title:
        return ""
    entry = load_quiz_cache().get(title)
    if isinstance(entry, dict):
        return str(entry.get("correctValue") or "")
    return str(entry or "")


def quiz_cache_put(title: str, correct_value: str, correct_key: str = "") -> None:
    if not title or not correct_value:
        return
    cache = load_quiz_cache()
    cache[title] = {
        "correctValue": correct_value,
        "correctKey": correct_key,
        "updateTime": now_text(),
    }
    save_quiz_cache(cache)
    log(f"💾 [答题缓存] 已记录本题正确答案")


def resolve_correct_value(answer: Any, options: List[Dict[str, Any]]) -> str:
    """把服务端回显的正确答案（可能是选项 key，也可能是选项文本）统一归一成选项文本。"""
    if answer in (None, ""):
        return ""
    text = str(answer).strip()
    for opt in options:
        if str(opt.get("key") or "").strip() == text:
            return str(opt.get("value") or "")
    for opt in options:
        if str(opt.get("value") or "").strip() == text:
            return text
    return ""


# ====================== 答题启发式（纯规则，无 AI） ======================
# 背景（2026-11-10 实测）：
#   - 每题只有 2 个选项，随机作答期望命中率仅 50%
#   - 当天第一个答题的账号是"试验品"，答错拿 0 积分；
#     但服务端在答错时仍回传 correctAnswer → 照样学 → 写缓存 →
#     后续账号命中缓存稳拿积分（所以现状是"1 个号吃亏，2 个号稳赚"）
#   - 目标：把首个账号的命中率从 50% 抬到 90%+
#
# 原理：题目集中在奶粉营养领域，且答案有公认标准（全站答对率 84~90%）。
#       用一张静态知识表（成分 → 公认生理功能）给选项打分即可，
#       纯查表 + 关键词匹配，**不涉及任何 AI / 模型 / 网络调用**。
#
# 安全性：知识表未覆盖时退化为随机（= 改动前行为）；
#         且服务端答错仍回传正确选项 → 最坏情况与现状完全相同，绝不会更差。

def quiz_detect_qtype(title: str) -> str:
    """按题干关键词判断题型。"""
    for keywords, qtype in _KB.get("qtype_rules", ()):
        for kw in keywords:
            if kw in title:
                return qtype
    return "unknown"


def quiz_extract_entity(title: str) -> str:
    """从题干抽取成分/主题词，用于查知识表。"""
    import re

    match = re.search(r"添加(.+?)的", title)
    if match:
        return match.group(1).strip()
    match = re.search(r"为什么.*添加(.+?)[？?]", title)
    if match:
        return match.group(1).strip()
    match = re.search(r"^(.+?)与(.+?)的主要区别", title)
    if match:
        return (match.group(1) + match.group(2)).strip()
    return ""


def quiz_score_options(
    title: str,
    options: List[Dict[str, Any]],
) -> Tuple[Optional[Dict[str, Any]], List[Tuple[str, float, str]]]:
    """给每个选项打分。

    返回 (best_option, [(key, score, reason), ...])；
    best_option 为 None 表示知识表/规则都没命中 → 调用方应随机选。
    """
    if not _KB.get("knowledge"):
        return None, []

    qtype = quiz_detect_qtype(title)
    knowledge = _KB.get("knowledge", {})
    title_lower = title.lower()

    # 命中的知识条目 + 其功能词（去重，避免多个条目贡献同一词导致重复计分）
    matched_words: List[str] = []
    for entity, words in knowledge.items():
        if not entity:
            continue
        ent_lower = entity.lower()
        if ent_lower in title_lower:
            for w in words:
                w = str(w)
                if w and w not in matched_words:
                    matched_words.append(w)

    scored: List[Tuple[str, float, str]] = []
    for opt in options:
        key = str(opt.get("key") or "")
        value = str(opt.get("value") or "")
        if not key:
            # 异常选项（缺 key）→ 不参与打分
            continue
        value_lower = value.lower()
        score = 0.0
        reasons: List[str] = []

        # 规则 1：知识表命中（权重最高，解决"促进骨骼发育"这类词频误判）
        if matched_words:
            hits = []
            for w in matched_words:
                if not w:
                    continue
                wl = w.lower()
                if wl in value_lower and w not in hits:
                    hits.append(w)
            if hits:
                score += 6.0 * len(hits)
                reasons.append("知识命中:" + "/".join(hits[:3]))
            else:
                score -= 3.0
                reasons.append("知识排除")

        # 规则 2：题型相关
        if qtype == "time":
            import re

            nums = [int(x) for x in re.findall(r"\d+", value)]
            if nums:
                score += -min(nums) * 0.1
                reasons.append("时长%d" % min(nums))
        elif qtype == "ratio" and ":" in value:
            try:
                ratio = float(value.split(":")[0].split("-")[0])
            except (ValueError, IndexError):
                ratio = None
            if ratio is not None:
                low, high = _KB.get("ratio_range", (1.0, 3.0))
                if low <= ratio <= high:
                    score += 1.0
                    reasons.append("比值%.1f合理" % ratio)
                else:
                    score -= 3.0
                    reasons.append("比值%.1f异常" % ratio)
        elif qtype == "yesno":
            if any(w in value for w in _KB.get("absurd_words", ())):
                score -= 5.0
                reasons.append("反常识")
        elif qtype == "safe":
            if any(w in value for w in _KB.get("safe_words", ())):
                score += 4.0
                reasons.append("合规表述")
            if any(w in value for w in _KB.get("danger_words", ())):
                score -= 5.0
                reasons.append("危险建议")

        scored.append((key, score, "，".join(reasons)))

    if not scored:
        return None, []

    best_score = max(item[1] for item in scored)
    winners = [item for item in scored if item[1] == best_score]
    if len(winners) != 1:
        # 同分 → 无法判断，交给调用方随机（退化为现状）
        return None, scored
    return {"key": winners[0][0], "score": best_score}, scored


def run_quiz(server: str, token: str, proxies: Dict[str, str] | None) -> str:
    quiz_resp = api_get(server, QUIZ_ACTIVITIES_URL, token, proxies)
    if quiz_resp.get("code") != 200:
        warn(f"⚠️ [答题] 获取社区答题题库失败: {quiz_resp.get('msg') or json_preview(quiz_resp, 300)}")
        return "获取题库失败"

    activities = quiz_resp.get("data") if isinstance(quiz_resp.get("data"), list) else []
    today = beijing_today_0am()
    today_quiz = next((item for item in activities if item.get("relatedDate") == today), None)
    if not today_quiz:
        warn("⚠️ [答题] 今日暂无社区答题题目")
        return "今日无题目"

    log(f"📝 [答题] 获取到社区答题题目: {today_quiz.get('questionTitle')}")
    try:
        options = json.loads(today_quiz.get("options") or "[]")
    except Exception:
        err("❌ [答题] 解析答题选项失败")
        return "解析选项失败"
    if not options:
        warn("⚠️ [答题] 未获取到题目选项，跳过答题")
        return "无选项"

    question_title = str(today_quiz.get("questionTitle") or "")
    selected = None

    cached_value = quiz_cache_get(question_title)
    if cached_value:
        for opt in options:
            if str(opt.get("value") or "").strip() == cached_value:
                selected = opt
                break
        if selected is not None:
            log(f"♻️ [答题] 命中历史答案: {cached_value}")

    if selected is None:
        # 缓存未命中 → 先用启发式打分；知识表未覆盖 / 同分则退回随机
        best, scored = quiz_score_options(question_title, options)
        if best is not None:
            for key, score, reason in scored:
                log("   🧠 [答题启发式] 选项%s「%s」得分 %.1f %s"
                    % (key, str(next((o.get("value") for o in options if str(o.get("key")) == key), "")),
                       score, ("(" + reason + ")") if reason else ""))
            selected = next((o for o in options if str(o.get("key")) == best["key"]), None)
            if selected is not None:
                log("🧠 [答题启发式] 知识表命中，选用选项%s（%.1f 分）" % (best["key"], best["score"]))
        if selected is None:
            selected = random.choice(options)
            log("🎲 [答题] 无历史答案，知识表未覆盖，随机作答")

    selected_key = selected.get("key")
    log(f"📝 [答题] 准备提交答案{selected_key}: {selected.get('value')}")

    wait_time = 6 + random.random() * 2
    log(f"⏳ [答题] 提交前等待 {wait_time:.0f}s")
    sleep(wait_time)

    submit_resp = api_post(
        server,
        QUIZ_SUBMIT_URL,
        token,
        proxies,
        {"quizActivityId": int(today_quiz.get("id")), "userAnswer": selected_key},
    )
    if submit_resp.get("code") != 200:
        err(f"❌ [答题] 提交答题失败: {submit_resp.get('msg') or json_preview(submit_resp, 300)}")
        return "提交失败"

    data = submit_resp.get("data") or {}
    actual_correct_key = data.get("correctAnswer") or (selected_key if data.get("isCorrect") == 1 else "")
    if data.get("isCorrect") == 1:
        quiz_cache_put(question_title, str(selected.get("value") or ""), str(selected_key or ""))
        msg = f"回答正确，获得{data.get('point', 0)}积分"
        log(f"✅ [答题] {msg}")
    else:
        correct_value = resolve_correct_value(actual_correct_key, options)
        if correct_value:
            quiz_cache_put(question_title, correct_value, str(actual_correct_key or ""))
        suffix = f"，正确答案是{actual_correct_key}" if actual_correct_key else ""
        msg = f"答案错误{suffix}，今日未获得积分"
        warn(f"⚠️ [答题] {msg}")
    return msg


def run_community_post(server: str, token: str, proxies: Dict[str, str] | None) -> str:
    recommend_resp = api_post(
        server,
        COMMUNITY_RECOMMEND_URL,
        token,
        proxies,
        {"recommendationId": 7, "sort": "personalized", "direction": "desc", "pageNum": 1, "pageSize": 3},
    )
    if recommend_resp.get("code") != 200:
        warn(f"⚠️ [发帖] 获取推荐内容失败: {recommend_resp.get('msg') or json_preview(recommend_resp, 300)}")
        return "获取推荐内容失败"

    items = safe_data(recommend_resp).get("list") or []
    if not items:
        warn("⚠️ [发帖] 未获取到推荐帖子内容，跳过发帖")
        return "无推荐内容"

    item = random.choice(items)
    content = item.get("content") or ""
    image_urls = item.get("imageUrls") or []
    log(f"🌱 [发帖] 准备发帖，内容: {content[:10]}...")

    wait_time = 6 + random.random() * 2
    log(f"⏳ [发帖] 发帖前等待 {wait_time:.0f}s")
    sleep(wait_time)

    push_resp = api_post(
        server,
        COMMUNITY_PUSH_URL,
        token,
        proxies,
        {
            "postId": None,
            "title": "",
            "content": content,
            "imageUrls": image_urls,
            "topicLabelNames": [],
            "communityTopicActivityId": None,
            "communitPostDraftId": None,
            "freeTrialCommentId": None,
            "productIds": [],
        },
    )
    if push_resp.get("code") != 200:
        err(f"❌ [发帖] 发帖失败: {push_resp.get('msg') or json_preview(push_resp, 300)}")
        return "发帖失败"

    post_id = push_resp.get("data")
    log("✅ [发帖] 发帖成功，准备删帖...")
    if not post_id:
        err("❌ [发帖] 发帖响应中未获取到 postId，取消删帖")
        return "发帖成功，删帖失败（无 postId）"

    delete_resp = api_get(server, f"{BASE_URL}/mall/xhr/community/posts/delete?postId={quote(str(post_id))}", token, proxies)
    if delete_resp.get("code") != 200:
        err(f"❌ [发帖] 删帖失败: {delete_resp.get('msg') or json_preview(delete_resp, 300)}")
        return "发帖成功，删帖失败"

    log("✅ [发帖] 帖子删除成功")
    return "发帖并删帖成功"


def run_account(index: int, total: int, server: str) -> Dict[str, Any]:
    result = {
        "server": server,
        "success": False,
        "proxyStatus": "未使用代理",
        "proxyIp": "-",
        "token": "-",
        "signMsg": "-",
        "point": "-",
        "trialMsg": "-",
        "winningMsg": "-",
        "quizMsg": "-",
        "postMsg": "-",
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

    token, raw_login = login_with_cache(server, proxies)
    if not token:
        result["error"] = f"登录失败: {json_preview(raw_login)}"
        return result

    result["token"] = mask(token)

    try:
        # 1. 签到（签到并返回等级/手机号/积分）
        checkin_resp = check_checkin(server, token, proxies)
        if checkin_resp.get("code") != 200:
            result["signMsg"] = checkin_resp.get("msg") or "签到失败"
            err(f"❌ [签到] {result['signMsg']}")
            result["error"] = f"签到失败: {result['signMsg']}"
            return result

        checkin_data = safe_data(checkin_resp)
        grade = checkin_data.get("grade")
        phone = mask_phone(str(checkin_data.get("phone") or ""))
        point = checkin_data.get("point", 0)
        result["point"] = str(point)
        result["signMsg"] = "签到成功"
        log(f"✅ [签到] {result['signMsg']}（手机 {phone}，等级 {grade}，当前积分 {point}）")

        # 辅助拉一次签到规则（不影响结果）
        api_get(server, CHECKIN_RULE_URL, token, proxies)

        # 2. 试用商品申请
        trial_msg_parts: List[str] = []
        address_resp = api_post(server, ADDRESS_LIST_URL, token, proxies, {})
        if address_resp.get("code") != 200:
            result["trialMsg"] = "获取收货地址失败"
            warn(f"⚠️ [试用] 获取收货地址失败: {address_resp.get('msg') or json_preview(address_resp, 300)}")
        else:
            addresses = address_resp.get("data") if isinstance(address_resp.get("data"), list) else []
            if not addresses:
                result["trialMsg"] = "未填写收货地址，跳过试用申请"
                warn("⚠️ [试用] 需要先在小程序 我的-收货地址 中填写地址")
            else:
                address_id = int(addresses[0].get("id"))
                city_name = str(addresses[0].get("cityName") or "")

                trial_resp = api_post(
                    server,
                    TRIAL_LIST_URL,
                    token,
                    proxies,
                    {"pageNum": 1, "pageSize": 10, "statusList": [1, 2]},
                )
                if trial_resp.get("code") != 200:
                    result["trialMsg"] = "获取试用商品列表失败"
                    warn(f"⚠️ [试用] 获取试用商品列表失败: {trial_resp.get('msg') or json_preview(trial_resp, 300)}")
                else:
                    trial_list = safe_data(trial_resp).get("list") or []
                    for item in trial_list:
                        if item.get("freeTrialButton") != 3:
                            continue
                        grade_list = [str(x) for x in (item.get("gradeList") or [])]
                        if grade is None or str(grade) not in grade_list:
                            continue
                        trial_id = int(item.get("id"))
                        product_name = str(item.get("productName") or "")
                        draw_time = str(item.get("drawTime") or "")
                        log(f"🎁 [试用] 【{product_name}】可申请试用，开奖时间 {draw_time}")
                        apply_resp = api_post(server, TRIAL_APPLY_URL, token, proxies, {"id": trial_id, "addressId": address_id})
                        if apply_resp.get("code") == 200:
                            msg = f"{product_name} 申请成功（收货地址 {city_name}）"
                            log(f"✅ [试用] {msg}")
                        else:
                            msg = f"{product_name} 申请失败: {apply_resp.get('msg') or '未知错误'}"
                            err(f"❌ [试用] {msg}")
                        trial_msg_parts.append(msg)

                    result["trialMsg"] = "；".join(trial_msg_parts) if trial_msg_parts else "无符合条件可申请的试用商品"
                    log(f"🎁 [试用] {result['trialMsg']}")

        # 3. 中奖记录查询
        winning_resp = api_post(
            server,
            WINNING_LIST_URL,
            token,
            proxies,
            {"parentTabStatus": 1, "pageNum": 1, "pageSize": 10, "subTabStatus": 1},
        )
        if winning_resp.get("code") != 200:
            result["winningMsg"] = "查询中奖记录失败"
            warn(f"⚠️ [中奖] 查询中奖记录失败: {winning_resp.get('msg') or json_preview(winning_resp, 300)}")
        else:
            records = safe_data(winning_resp).get("list") or []
            if not records:
                result["winningMsg"] = "暂无中奖记录"
                warn("⚠️ [中奖] 暂无中奖记录")
            else:
                record_names = [str(item.get("productName") or "") for item in records]
                result["winningMsg"] = "、".join(name for name in record_names if name)
                for item in records:
                    log(f"🎉 [中奖] 恭喜中奖【{item.get('productName') or ''}】，完成试用后需要提交试用报告，否则会取消下次中奖资格")

        # 4. 社区每日答题
        try:
            result["quizMsg"] = run_quiz(server, token, proxies)
        except Exception as exc:
            result["quizMsg"] = f"答题异常: {exc}"
            err(f"❌ [答题] 社区答题异常: {exc}")
            result["error"] = traceback.format_exc().strip()

        # 5. 社区发帖种草后删帖
        try:
            result["postMsg"] = run_community_post(server, token, proxies)
        except Exception as exc:
            result["postMsg"] = f"发帖异常: {exc}"
            err(f"❌ [发帖] 发帖种草异常: {exc}")
            result["error"] = traceback.format_exc().strip()

        result["success"] = True
        return result

    except Exception as exc:
        result["error"] = traceback.format_exc().strip()
        err(f"❌ [账号] 执行失败: {exc}")
        return result


def build_notify(results: List[Dict[str, Any]], skipped: Optional[List[Dict[str, Any]]] = None) -> str:
    success_count = sum(1 for item in results if item["success"])
    fail_count = len(results) - success_count
    skip_count = len(skipped or [])

    content = f"""🥛 人本（认养一头牛）任务结果

━━━━━━━━━━━━━━━━━━━━
🏁 总结：{success_count} 成功 / {fail_count} 失败{' / %d 跳过（今日已完成）' % skip_count if skip_count else ''}
🕒 时间：{now_text()}
━━━━━━━━━━━━━━━━━━━━
"""

    for idx, res in enumerate(results, 1):
        icon = "✅" if res["success"] else "❌"

        content += f"""
🧩 账号 {idx}
📝 签到：{res["signMsg"]}
💰 积分：{res["point"]}
🎁 试用：{res["trialMsg"]}
🎉 中奖：{res["winningMsg"]}
📝 答题：{res["quizMsg"]}
🌱 发帖：{res["postMsg"]}
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
    skipped: List[Dict[str, Any]] = []

    for index, server in enumerate(SERVERS, 1):
        # 今天已经完整跑过的账号直接跳过，不再消耗 YYB 取码/取号配额
        if is_done_today(server):
            log(f"⏭️ [标记] 账号 {index}/{len(SERVERS)} 今日任务已完成，跳过")
            skipped.append({"server": server})
            continue

        try:
            result = run_account(index, len(SERVERS), server)
            results.append(result)
            # 只有整个流程走完（success=True）才算今日完成
            if result.get("success"):
                mark_done_today(server, result.get("point", ""))
        except Exception as exc:
            err(f"❌ [主程序] {server} 执行异常: {exc}")
            results.append({
                "server": server,
                "success": False,
                "proxyStatus": "-",
                "proxyIp": "-",
                "token": "-",
                "signMsg": "-",
                "point": "-",
                "trialMsg": "-",
                "winningMsg": "-",
                "quizMsg": "-",
                "postMsg": "-",
                "error": traceback.format_exc().strip(),
            })

        if index < len(SERVERS):
            log("⏳ [间隔] 等待 2s 后处理下一个账号")
            sleep(2)

    success_count = sum(1 for item in results if item["success"])
    fail_count = len(results) - success_count
    log("📊 [执行汇总] 认养一头牛 · 账号 %d ｜ 成功 %d ｜ 失败 %d ｜ 跳过 %d"
        % (len(SERVERS), success_count, fail_count, len(skipped)))

    print()
    log("╔" + "═" * 50 + "╗")
    log("║ 🏁 人本（认养一头牛）任务执行完成            ║")
    log(f"║ ✅ 成功: {success_count:<39}║")
    err(f"║ ❌ 失败: {fail_count:<39}║")
    if skipped:
        log(f"║ ⏭️ 跳过(今日已完成): {len(skipped):<29}║")
    log(f"║ 🕒 结束时间: {now_text():<32}║")
    log("╚" + "═" * 50 + "╝")

    send_pushplus("🥛 人本（认养一头牛）任务完成", build_notify(results, skipped))
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


class _YybCircuitOpen(RuntimeError):
    """YYB-GO 熔断/限速：HTTP 429，请求根本没发往上游。

    2026-11-05 实测响应体：
      {"code":429,"msg":"call deferred: 目标暂时熔断，192s 内不再请求上游；避免重复触发风控",
       "data":{"circuit_open":"target","request_rejected":false,"retry_after":192}}
    响应头同时带 Retry-After: 176。

    这类错误**不是上游拒绝**（上游拒绝是 502 + shortlink_application -14），
    而是服务端本地挡住了重复请求。必须按「立即放弃本账号」处理，不能换号硬打。
    """

    def __init__(self, message: str, retry_after: int = 0, circuit_open: str = ""):
        super().__init__(message)
        self.retry_after = retry_after
        self.circuit_open = circuit_open


class _YybAccountInvalid(RuntimeError):
    """账号已失效需要重新扫码：HTTP 409 / 404，或返回体带 rescan_required。"""


def _yyb_retry_after(response, body) -> int:
    """从响应头 Retry-After 或返回体 data.retry_after 取退避秒数。"""
    header = response.headers.get("Retry-After") or response.headers.get("retry-after")
    for candidate in (header,):
        if candidate:
            try:
                return max(0, int(float(str(candidate).strip())))
            except (TypeError, ValueError):
                pass
    data = body.get("data") if isinstance(body, dict) else None
    if isinstance(data, dict):
        raw = data.get("retry_after")
        if raw is not None:
            try:
                return max(0, int(float(raw)))
            except (TypeError, ValueError):
                pass
    return 0


def _yyb_error_detail(body):
    """从 YYB-GO 错误响应体里提取可安全展示的原因摘要。

    只输出固定措辞，绝不回显服务端原文（可能含 URL、凭据或个人信息）。
    """
    if not isinstance(body, dict):
        return ""
    msg = str(body.get("message") or body.get("msg") or body.get("error") or "")
    low = msg.lower()
    data = body.get("data") if isinstance(body.get("data"), dict) else {}

    # 上游明确拒绝（有结构化错误码）—— 这类重试无用
    upstream = data.get("upstream_errors") or data.get("rejections")
    if upstream:
        codes = []
        for item in upstream:
            if isinstance(item, dict) and item.get("code") is not None:
                codes.append(str(item.get("code")))
        return ("上游拒绝（错误码 %s）" % "/".join(codes)) if codes else "上游拒绝"

    # 本地熔断 / 限速
    if data.get("circuit_open"):
        return "服务端熔断中"
    if "circuit" in low or "熔断" in msg:
        return "服务端熔断中"

    # 连接层超时（无上游错误码 + 8 秒 deadline）
    if ("context deadline exceeded" in low or "shortlink" in low
            or "i/o timeout" in low or "timeout" in low):
        return "上游连接超时"
    if "not found" in low:
        return "账号不存在"
    return ""


class _YybError(RuntimeError):
    """YYB-GO 调用失败。detail 存放可安全展示的原因摘要。"""

    def __init__(self, message, detail=""):
        super().__init__(message)
        self.detail = detail


def _yyb_json_request(server, path, appid, payload=None):
    """调用 YYB-GO 的 /wxapp/<path>，连接层失败时自动重试。

    仅「上游连接超时」才重试：这类失败是纯连接层的（上游长连接节点偶发
    不响应，8 秒 deadline 后返回 502，响应体无上游错误码）。YYB-GO 的
    编队转移清单不含连接层超时，不会自动换成员；但它每次新请求会把请求
    轮转到编队的下一个成员，重试正好能命中可用成员。

    熔断（429）、账号失效（404/409）、上游拒绝（-14 等）一律不重试。
    """
    import requests
    endpoint, ref = _yyb_parts(server)
    if not endpoint or not ref or not appid:
        raise RuntimeError("YYB 参数不完整：需要 地址@账号ID 和 app_id")
    headers = {}
    api_key = _yyb_os.getenv("YYB_API_KEY", "").strip()
    if api_key:
        headers["Authorization"] = "Bearer " + api_key

    for attempt in range(YYB_RETRY_TIMES + 1):
        try:
            response = requests.post(
                endpoint + path,
                json={"ref": ref, "app_id": str(appid), **(payload or {})},
                headers=headers,
                timeout=YYB_TIMEOUT,
            )
        except requests.RequestException as exc:
            # 网络层异常（连接被拒/读取超时等）同样属于可重试的连接层故障
            if attempt >= YYB_RETRY_TIMES:
                raise _YybError("YYB 请求异常：%s" % exc, "上游连接超时") from None
            log("⏳ [重试] %s 第%d/%d次失败（上游连接超时），%d秒后重试"
                % (path, attempt + 1, YYB_RETRY_TIMES + 1, YYB_RETRY_DELAY))
            time.sleep(YYB_RETRY_DELAY)
            continue

        try:
            body = response.json()
        except ValueError:
            body = None

        # ── 429：熔断 / 取码限速，请求没到上游 ──────────────────────────────
        # 2026-11-05：认养一头牛 3 个账号连续被 -14 打爆后，服务端把每个成员的
        # (账号, AppID, 操作类型) 目标熔断 10 分钟。此时继续换账号硬打只会让
        # 熔断扩散，必须按 Retry-After 退避。熔断期间重试只会加剧风控，
        # 因此**不重试**，立即上抛。
        if response.status_code == 429:
            wait = _yyb_retry_after(response, body)
            data = body.get("data") if isinstance(body, dict) else None
            circuit = ""
            if isinstance(data, dict):
                circuit = str(data.get("circuit_open") or "")
            msg = (body.get("msg") or body.get("message") or "请求被限速") \
                if isinstance(body, dict) else "请求被限速"
            raise _YybCircuitOpen(
                "YYB 熔断/限速（HTTP 429, circuit_open=%s）：%s" % (circuit or "-", msg),
                retry_after=wait,
                circuit_open=circuit,
            )

        # ── 409 / 404：账号失效或不存在，需要重新扫码 ──────────────────────
        # 不可恢复，**不重试**，立即上抛。
        if response.status_code in (404, 409):
            msg = (body.get("msg") or body.get("message")
                   or ("HTTP %s" % response.status_code)) \
                if isinstance(body, dict) else ("HTTP %s" % response.status_code)
            raise _YybAccountInvalid("YYB 账号失效需重扫（HTTP %s）：%s" % (response.status_code, msg))

        if response.status_code >= 400:
            detail = _yyb_error_detail(body)
            if detail != "上游连接超时" or attempt >= YYB_RETRY_TIMES:
                raise _YybError(
                    "YYB %s 失败：HTTP %d%s"
                    % (path, response.status_code,
                       ("（%s）" % detail) if detail else ""),
                    detail)
            log("⏳ [重试] %s 第%d/%d次失败（上游连接超时），%d秒后重试"
                % (path, attempt + 1, YYB_RETRY_TIMES + 1, YYB_RETRY_DELAY))
            time.sleep(YYB_RETRY_DELAY)
            continue

        if body is None:
            raise _YybError("YYB 返回非 JSON")
        return body

    raise _YybError("YYB %s 失败：重试次数已用尽" % path)


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


def _yyb_extract_phone_fields(body: Any) -> Dict[str, str]:
    """从 YYB 返回体中提取手机号授权三件套。

    实测 YYB HTTP 接口返回层级为 {"code":0,"data":{"account":..,"openid":..,"result":{..}}}，
    三件套在 data.result 内；WebUI 结构则是顶层 result。这里把两层都兼容掉。
    """
    candidates: List[Dict[str, Any]] = []

    def add(node: Any) -> None:
        if not isinstance(node, dict):
            return
        candidates.append(node)
        for key in ("result", "raw"):
            add(node.get(key))
        raw = node.get("data")
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
            except Exception:
                parsed = None
            if isinstance(parsed, dict):
                candidates.append(parsed)

    add(body.get("data") if isinstance(body, dict) else None)
    add(body.get("result") if isinstance(body, dict) else None)
    add(body)

    encrypted = ""
    iv = ""
    phone_code = ""
    for node in candidates:
        if not encrypted and node.get("encryptedData"):
            encrypted = str(node.get("encryptedData"))
        if not iv and node.get("iv"):
            iv = str(node.get("iv"))
        if not phone_code and node.get("code"):
            phone_code = str(node.get("code"))

    return {"authCode": phone_code, "encryptPhoneNumber": encrypted, "initVector": iv}


def _yyb_phone_package(server, *args, **kwargs) -> Dict[str, str]:
    """取微信手机号授权三件套。

    minilogin 需要：encryptedData（手机号密文）、offset（=iv）、code（phoneCode）。
    实测 /wxapp/getPhoneNumber 与 /wx/getphonenumber 两个能力路径都可用。

    2026-11-05：区分两类失败，避免把「服务端本地熔断」误当成「上游拒绝」
      - _YybCircuitOpen（429）：请求没发往上游，换路径重试也没用，直接上抛放弃本账号
      - 其它异常（502 / -14）：上游真的拒绝了这个 (微信 × 渠道 × 小程序) 组合
    """
    appid = _yyb_appid(args, kwargs)
    errors: List[str] = []

    for path in ("/wxapp/getPhoneNumber", "/wx/getphonenumber"):
        try:
            body = _yyb_json_request(server, path, appid)
        except _YybCircuitOpen:
            # 熔断是本地状态，换另一条能力路径同样会被挡，直接上抛给调用方
            raise
        except Exception as exc:
            errors.append("%s -> %s" % (path, exc))
            continue

        fields = _yyb_extract_phone_fields(body)
        if fields["encryptPhoneNumber"] and fields["initVector"]:
            return fields

        errors.append("%s -> 返回缺少 encryptedData/iv" % path)

    raise RuntimeError("YYB 手机号授权数据获取失败（%s）" % "；".join(errors))


if _yyb_accounts():
    SERVERS = _yyb_accounts()

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
        body = _yyb_json_request(server, "/wxapp/getPhoneNumber", _yyb_appid(args, kwargs))
        # 三件套必须来自服务端真实下发，不做任何伪造或补值。
        return _yyb_extract_phone_fields(body)
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
