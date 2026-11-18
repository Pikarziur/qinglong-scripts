#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# =========================================================
# name:  蜜蜂惊喜社
# version: 2026-11-18a
# cron: 30 5,15 * * *
# 积分有效期：当前获取的积分将在后年1月1日过期【1+1】
# 即将过期检查方式：年底前 7 天检查即将过期积分（上年+今年）
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
蜜蜂惊喜社微信小程序动态 code 版

功能：
  1. 本地 code 服务获取微信 code
  2. 有赞 authorize 接口使用 code 换 accessToken（yz_union 授权）
  3. 查询积分（签到前/签到后 user_points.json）
  4. 2026-11-17a：签到前 ISV 状态预检（point/log/query 的 checkin），今日已签则跳过签到写请求
  5. 获取 yz_open_id（getUserOpenInfo.json，用于状态查询与 ISV 签到）；拿不到则降级原签到
  6. 每日签到：优先 ISV /api/sign（与状态查询同源）；拿不到 yz_open_id 时降级 checkinV2.json
  7. PushPlus 推送
  8. 品赞代理，业务请求优先代理，失败直连兜底
  9. 2026-11-17b：浏览直播预告领奖任务（开关默认关）——查 pagesview 状态 + point/log/create 领奖；
     真实浏览文章无法由脚本模拟，故默认关闭，需人工确认可接受风险后再开启。

环境变量：
  PLUSPLUS_TOKEN    PushPlus token，可选
  QYWX_TOKEN        企业微信机器人 Webhook key，可选（机器人地址 ?key= 后面的值）
  PROXY_API         品赞代理提取 API，可选
  PROXY_TYPE        http / socks5，默认 http
  CODE_SERVER       覆盖本地 code 服务地址，可选

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
import calendar
import traceback
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

import requests
# ========== 账号分隔标识 ==========
# 多账号同跑时, 用醒目横线把各账号日志隔开, 便于阅读与定位。
# ========== 启动信息 ==========
def log_startup(total: int = 0) -> None:
    log(ACC_RULE)
    _title = "📃 脚本名称：蜜蜂惊喜社"
    if APPID:
        _title += " ｜" + APPID
    log(_title)
    log("📌 脚本版本: %s" % SCRIPT_VER)
    if total:
        log("🔢 账号数量: %d" % total)
    log(ACC_RULE)
SCRIPT_VER = "2026-11-18a"
ACC_RULE = "=" * 70
SUMMARY_SEP = "▶︎ " + "━" * 50 + " ◀︎"

# ========== 统一日志：[LEVEL] [MFJXS] message ==========
def _emit(level, msg):
    line = "[%s] %s" % (level, msg)
    print(line, flush=True)


def log(msg):
    _emit("INFO", msg)


def warn(msg):
    _emit("WARN", msg)


def err(msg):
    _emit("ERROR", msg)



APP_NAME = "蜜蜂惊喜社小程序"
APPID = "wx3aa848f81dfa0967"

# ========== 内部配置：直接改这里，省去去面板设环境变量 ==========
# 账号序号白名单（1 起，按 YYB_SERVER 里的书写顺序编号）；留空 [] = 全部运行
# 例：[1] 只跑第 1 个账号；[1, 3] 跑第 1 和第 3 个
YYB_ONLY_REFS = []

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
YYB_RETRY_TIMES = int(os.getenv("MIFENGJXS_YYB_RETRY", "5") or 5)      # 额外重试次数（不含首次）
YYB_RETRY_DELAY = int(os.getenv("MIFENGJXS_YYB_RETRY_DELAY", "4") or 4)  # 重试间隔秒数


API_BASE = "https://h5.youzan.com"
LOGIN_URL = f"{API_BASE}/wscshop/weapp/authorize.json"
POINTS_URL = f"{API_BASE}/wscump/integral/user_points.json"
CHECKIN_PAGE_URL = f"{API_BASE}/wscump/checkin/show_checkin_page_v2.json"
CHECKIN_URL = f"{API_BASE}/wscump/checkin/checkinV2.json"
# 2026-11-17a：ISV 体系（与状态查询同源）。yz_open_id 由 getUserOpenInfo.json 返回。
ISV_BASE = "https://mp-isv.youzanyun.com"
ISV_POINT_QUERY_URL = f"{ISV_BASE}/api/point/log/query"   # 任务状态：checkin/pagesview/subscribe
ISV_SIGN_URL = f"{ISV_BASE}/api/sign"                     # ISV 签到写请求
# 2026-11-17b：浏览直播预告领奖（HAR 115030）。真实浏览文章无法由脚本模拟，默认关闭。
ISV_POINT_CREATE_URL = f"{ISV_BASE}/api/point/log/create"  # 预告领奖写请求（type=2, points=10）

# ===== 浏览直播预告领奖任务开关（2026-11-17b）=====
# 默认关：该任务需要真实在微信里打开公众号文章，脚本只能查状态 + 提交领奖，
# 服务端是否校验前置浏览无法从 HAR 确认，存在黑号风险，故默认关闭。
# 想用时改为 True；开后仍会先查 pagesview，已完成任务则跳过写请求（状态检测）。
ENABLE_ARTICLE_VIEW_TASK = False
ARTICLE_VIEW_TASK_POINTS = 10          # 单次领奖积分（HAR 实测 10）
ARTICLE_VIEW_TASK_TYPE = 2             # 任务类型：浏览预告（HAR 实测 type=2）

# ===== 积分过期提醒（2026-11-17c）=====
# 规则：今年获取的积分，次年 12 月 31 日到期。因此每年年底前 7 天检查一次：
#   轮询「上年」获取的积分量；上年为 0 → 无积分会过期，不提醒；
#   上年 > 0 → 即将过期量 = 上年获取 + 今年获取，打印在汇总日志并推送通知。
# 数据源：积分中心 pointstore（HAR 151902）
#   - getPointsRecords.json   积分明细（含 amount/createdAt/expiredAt），按 createdAt 归年汇总
#   - getCustomerPoints.json   服务端口径对照（nearlyExpiredAmount = 即将过期积分）
ENABLE_EXPIRE_REMIND = True
EXPIRE_REMIND_DAYS = 7                 # 年底前 N 天开始检查（含 12/31 当天）
EXPIRE_RECORDS_PAGE_SIZE = 50           # 明细分页大小
EXPIRE_RECORDS_MAX_PAGES = 40           # 明细最大翻页数（40 × 50 = 2000 条，足够覆盖一年签到）
POINTSTORE_RECORDS_PATH = "wscump/pointstore/getPointsRecords.json"
POINTSTORE_CUSTOMER_PATH = "wscump/pointstore/getCustomerPoints.json"
# pointstore 是 h5 页面接口，Referer 需与抓包一致（HAR 151902）
POINTSTORE_REFERER = {
    POINTSTORE_RECORDS_PATH: "https://h5.youzan.com/wscump/pointstore/pointdetails",
    POINTSTORE_CUSTOMER_PATH: "https://h5.youzan.com/wscump/pointstore/pointcenter",
}
# 时间戳按北京时间归年（积分签到口径）
CN_TZ = timezone(timedelta(hours=8))

KDT_ID = "104905610"
USER_VERSION = "2.226.7.201"
CLIENT_ID = "4d65249d377b2c3ed8"
CLIENT_SECRET = "1cdc05151d64f3a4a6ebd0e9de64422a"
GRANT_TYPE = "yz_union"

COOKIE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mfjxscookie.json")

# 每日任务完成标记：记录「今天这个账号已经完整跑完」，避免同一天重复调用。
# 结构：{ "<server>": {"date": "2026-11-07", "doneTime": "...", "note": "签到成功"} }
# 跨天自动失效（读时比对当天日期）；标记文件在脚本同目录，非隐藏，便于面板查看/删除。
DAILY_DONE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mifengjxs_daily_done.json")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) MicroMessenger/3.9.12 MiniProgramEnv/Windows WindowsWechat/WMPF"
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
    return resp.get("data") or {}


def log_title() -> None:
    print()
    log(ACC_RULE)
    log("🐝 蜜蜂惊喜社小程序动态 code 版")
    log(f"🕒 启动时间: {now_text()}")
    log(f"🔢 账号数量: {len(SERVERS)}")
    log(ACC_RULE)


def log_account_header(index: int, total: int, server: str) -> None:
    log(ACC_RULE)
    log(f"👤 账号 {index}/{total} ｜ 来源 {server}")
    log(ACC_RULE)


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
    """企业微信机器人推送（Webhook）。未配置 QYWX_TOKEN 时返回 False，不打印日志。"""
    if not QYWX_TOKEN:
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
        return res.get("errcode") == 0
    except Exception:
        return False


def send_pushplus(title: str, content: str) -> list:
    """依次尝试各通知渠道，返回成功的渠道名列表；不打印逐渠道日志。"""
    ok_channels = []
    if send_qywx(title, content):
        ok_channels.append("企业微信")
    if not PLUSPLUS_TOKEN:
        return ok_channels
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
        ok_channels.append("PushPlus")
    except Exception:
        pass
    return ok_channels

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


def common_headers(token: str | None = None, session_id: str | None = None) -> Dict[str, str]:
    """有赞系请求头：access_token 放 URL 参数，Extra-Data 的 sid 必须是 session_id

    ⚠️ 2026-11-09 修复：此前把 access_token 当作 sid 塞进 Extra-Data，
    有赞校验的其实是会话 ID，导致所有业务接口一律返回
    「invalid session」（登录那步却是成功的，所以极具迷惑性）。
    实测对照（同一 token）：
        sid=access_token  -> code=-1 invalid session
        sid=session_id    -> code=0  ok
        不带 Extra-Data   -> code=0  ok
    取不到 session_id 时退化为不带 Extra-Data（实测同样通过）。
    """
    headers = {
        "User-Agent": USER_AGENT,
        "Referer": f"https://servicewechat.com/{APPID}/32/page-frame.html",
        "Accept": "application/json, text/plain, */*",
    }
    if token and session_id:
        headers["Extra-Data"] = json.dumps({
            "is_weapp": 1,
            "sid": session_id,
            "version": USER_VERSION,
            "client": "weapp",
            "bizEnv": "wsc",
        }, separators=(",", ":"), ensure_ascii=False)
    return headers


def extract_session_id(data: Any) -> str | None:
    """有赞 authorize 响应：data.session_id（Extra-Data 的 sid 就是它）"""
    if not isinstance(data, dict):
        return None

    candidates = [
        data.get("session_id"),
        data.get("sessionId"),
    ]

    inner = data.get("data")
    if isinstance(inner, dict):
        candidates.extend([
            inner.get("session_id"),
            inner.get("sessionId"),
        ])

    for item in candidates:
        if item and item != "null":
            return str(item)

    return None


def extract_token(data: Any) -> str | None:
    """有赞 authorize 响应：data.accessToken（兼存 sessionId 用于 Extra-Data）"""
    if not isinstance(data, dict):
        return None

    candidates = [
        data.get("accessToken"),
        data.get("access_token"),
        data.get("token"),
    ]

    inner = data.get("data")
    if isinstance(inner, dict):
        candidates.extend([
            inner.get("accessToken"),
            inner.get("access_token"),
            inner.get("token"),
        ])

    for item in candidates:
        if item and item != "null":
            return str(item)

    return None


def build_url(api_path: str, token: str, query: Dict[str, Any] | None = None) -> str:
    """照源脚本 buildUrl：store_id/app_id/kdt_id/access_token 拼 URL 参数"""
    pathname = api_path.lstrip("/")
    params = {
        "store_id": "",
        "app_id": APPID,
        "kdt_id": KDT_ID,
        "access_token": token,
    }
    if query:
        params.update(query)
    query_str = "&".join(f"{key}={quote(str(value))}" for key, value in params.items())
    return f"{API_BASE}/{pathname}?{query_str}"


def login_by_code(server: str, code: str, proxies: Dict[str, str] | None) -> Tuple[str | None, str | None, Dict[str, Any] | None]:
    try:
        log("🔐 [登录] 使用 code 换 accessToken")
        response = request_with_proxy(
            "POST",
            build_url("wscshop/weapp/authorize.json", ""),
            headers={"User-Agent": USER_AGENT, "Referer": f"https://servicewechat.com/{APPID}/32/page-frame.html", "Accept": "application/json, text/plain, */*", "Content-Type": "application/json"},
            json={
                "appId": APPID,
                "clientId": CLIENT_ID,
                "clientSecret": CLIENT_SECRET,
                "grantType": GRANT_TYPE,
                "code": code,
            },
            proxies=proxies,
            server=server,
        )

        try:
            data = response.json()
        except Exception:
            data = {"raw": response.text[:800]}

        token = extract_token(data)
        session_id = extract_session_id(data)
        if token and to_float(data.get("code")) == 0:
            log(f"✅ [登录] accessToken 获取成功: {mask(token)}")
            if session_id:
                log(f"✅ [登录] sessionId 获取成功: {mask(session_id)}")
            else:
                warn("⚠️ [登录] 未取到 sessionId，业务请求将不带 Extra-Data")
            return token, session_id, data

        msg = data.get("msg") or data.get("message") or json_preview(data)
        err(f"❌ [登录] 登录失败: {msg}")
        return None, None, data
    except Exception as exc:
        err(f"❌ [登录] 请求异常: {exc}")
        return None, None, None


def api_get(server: str, url: str, token: str, proxies: Dict[str, str] | None,
            session_id: str | None = None) -> Dict[str, Any]:
    response = request_with_proxy(
        "GET",
        url,
        headers=common_headers(token, session_id),
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


# ====================== 日打标（今日完成标记） ======================
def _today() -> str:
    """当天日期（本地时区），用于跨天判定。"""
    return datetime.now().strftime("%Y-%m-%d")


def load_daily_done() -> Dict[str, Any]:
    """读取日打标文件；不存在或损坏时返回空字典。"""
    try:
        if os.path.exists(DAILY_DONE_FILE):
            with open(DAILY_DONE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
    except Exception as exc:
        warn(f"⚠️ [打标] 读取失败: {exc}")
    return {}


def save_daily_done(cache: Dict[str, Any]) -> None:
    """写打标文件（临时文件 + rename 原子替换，避免写坏）。"""
    try:
        tmp = DAILY_DONE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
        os.replace(tmp, DAILY_DONE_FILE)
    except Exception as exc:
        err(f"❌ [打标] 保存失败: {exc}")


def is_done_today(server: str) -> bool:
    """该账号今天是否已经完整跑过（是则本次直接跳过）。"""
    entry = load_daily_done().get(server)
    if not isinstance(entry, dict):
        return False
    return str(entry.get("date") or "") == _today()


def mark_done_today(server: str, note: str = "",
                    expire: Optional[Dict[str, Any]] = None,
                    total_points: Any = None) -> None:
    """记录该账号今天已完成。

    expire：可选的积分过期数据（prev/cur/nearly），缓存进打标文件，
            供下午 cron 全跳过时读回，保证汇总照样能提醒。
    """
    cache = load_daily_done()
    # 只保留当天的记录，跨天自动清理，避免文件无限增长
    today = _today()
    cache = {k: v for k, v in cache.items()
             if isinstance(v, dict) and str(v.get("date") or "") == today}
    entry: Dict[str, Any] = {
        "date": today,
        "doneTime": now_text(),
        "note": str(note or ""),
    }
    if isinstance(expire, dict):
        entry["expire"] = {
            "prev": to_float(expire.get("prev")),
            "cur": to_float(expire.get("cur")),
            "nearly": (to_float(expire.get("nearly"))
                       if expire.get("nearly") is not None else None),
        }
    if total_points is not None:
        entry["totalPoints"] = to_float(total_points)
    cache[server] = entry
    save_daily_done(cache)
    log(f"✅ [打标] 今日任务已完成，已记录（{today}）")


def total_points_from_done(server: str) -> Optional[float]:
    """从当天打标记录读回总积分，供本次被跳过的账号在汇总里显示。"""
    entry = load_daily_done().get(server)
    if not isinstance(entry, dict) or str(entry.get("date") or "") != _today():
        return None
    val = entry.get("totalPoints")
    if val is None:
        return None
    return to_float(val)


def clear_daily_done(server: str = "") -> None:
    """清除指定账号（或全部）的日打标。"""
    cache = load_daily_done()
    if server:
        cache.pop(server, None)
    else:
        cache = {}
    save_daily_done(cache)


# 积分每日快照：按账号存每天最后一次的总积分，用于次日对比涨跌。
# 结构：{ "<server>": { "2026-11-17": 612.5, "2026-11-18": 688.0 } }
POINTS_HISTORY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mifengjingxishe_points_history.json")


def load_points_history() -> Dict[str, Any]:
    try:
        if os.path.exists(POINTS_HISTORY_FILE):
            with open(POINTS_HISTORY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
    except Exception as exc:
        warn(f"⚠️ [积分] 历史读取失败: {exc}")
    return {}


def save_points_history(cache: Dict[str, Any]) -> None:
    try:
        tmp = POINTS_HISTORY_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
        os.replace(tmp, POINTS_HISTORY_FILE)
    except Exception as exc:
        err(f"❌ [积分] 历史保存失败: {exc}")


def record_points_snapshot(server: str, total_points: Any) -> None:
    if total_points is None:
        return
    try:
        val = float(total_points)
    except (TypeError, ValueError):
        return
    hist = load_points_history()
    days = hist.get(server)
    if not isinstance(days, dict):
        days = {}
    days[_today()] = val
    hist[server] = days
    save_points_history(hist)


def points_delta(server: str, today_val: float) -> Optional[float]:
    try:
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        hist = load_points_history()
        days = hist.get(server)
        if not isinstance(days, dict) or yesterday not in days:
            return None
        return today_val - float(days.get(yesterday))
    except Exception:
        return None


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


def set_cached_token(server: str, token: str, expire_time: str, session_id: str | None = None) -> None:
    cache = load_token_cache()
    old = cache.get(server) if isinstance(cache.get(server), dict) else {}
    entry = {
        "token": token,
        "sessionId": session_id or "",
        "expireTime": expire_time,
        "updateTime": datetime.now().isoformat(),
    }
    # 保留已缓存的 ISV 用户标识，避免刷新 token 时丢失
    for field in ("buyerId", "userOpenId"):
        if old.get(field):
            entry[field] = old[field]
    cache[server] = entry
    save_token_cache(cache)


def get_cached_session_id(server: str) -> str | None:
    """取缓存里的 session_id（Extra-Data 的 sid）；旧缓存没有则返回 None"""
    data = load_token_cache().get(server)
    if not isinstance(data, dict):
        return None
    sid = data.get("sessionId") or data.get("session_id")
    return str(sid) if sid else None


def get_cached_field(server: str, field: str) -> str | None:
    """读取缓存里的扩展字段（buyerId / userOpenId）"""
    data = load_token_cache().get(server)
    if not isinstance(data, dict):
        return None
    value = data.get(field)
    return str(value) if value else None


def set_cached_fields(server: str, **fields: Any) -> None:
    """把扩展字段写回缓存（不覆盖 token/sessionId/expireTime）"""
    cache = load_token_cache()
    entry = cache.get(server)
    if not isinstance(entry, dict):
        entry = {}
    for key, value in fields.items():
        if value:
            entry[key] = str(value)
    entry["updateTime"] = datetime.now().isoformat()
    cache[server] = entry
    save_token_cache(cache)


def get_points(server: str, token: str, proxies: Dict[str, str] | None, label: str = "积分",
               session_id: str | None = None) -> tuple:
    """照源脚本 getPoints()：user_points.json 查积分。

    返回 (展示文本, 积分数值)；查询失败时数值为 None。
    """
    resp = api_get(server, build_url("wscump/integral/user_points.json", token), token, proxies, session_id)
    if to_float(resp.get("code")) != 0:
        msg = resp.get("msg") or resp.get("message") or json_preview(resp, 200)
        warn(f"⚠️ [积分] {label}获取失败: {msg}")
        return f"获取失败: {msg}", None
    data = safe_data(resp)
    points = data.get("current_points") if data.get("current_points") is not None else (
        data.get("real_points") if data.get("real_points") is not None else (
            data.get("total_points") if data.get("total_points") is not None else "未知"
        )
    )
    log(f"✅ [积分] {label}: {points}")
    value = None
    try:
        value = float(points)
    except (TypeError, ValueError):
        value = None
    return f"{points} 积分", value


# ===================== 积分过期提醒（年底前 7 天） =====================
# 规则：今年获取的积分，次年 12 月 31 日到期（HAR 151902 明细里 2026 年获取的记录
#       expiredAt=1830268800 → +08 为 2028-01-01 00:00，反证「获取年份 +1 的年底」）。
# 因此每年年底前 7 天检查一次：轮询上年获取量；上年为 0 → 不提醒；
# 上年 > 0 → 即将过期 = 上年获取 + 今年获取。
def in_expire_window(today: date | None = None) -> bool:
    """年底前 N 天（含 12/31 当天）才检查即将过期积分。"""
    today = today or date.today()
    if today.month != 12:
        return False
    last_day = calendar.monthrange(today.year, 12)[1]
    return today.day > last_day - EXPIRE_REMIND_DAYS


def _cn_year(ts: Any) -> int:
    """时间戳按北京时间(+8)归年；解析失败返回 0。"""
    try:
        value = int(float(ts))
    except (TypeError, ValueError):
        return 0
    if value <= 0:
        return 0
    try:
        return datetime.fromtimestamp(value, CN_TZ).year
    except (OSError, OverflowError, ValueError):
        return 0


def pointstore_get(server: str, path: str, token: str, proxies: Dict[str, str] | None,
                   session_id: str | None, query: Dict[str, Any]) -> Dict[str, Any]:
    """积分中心(pointstore)接口。

    HAR 151902 是 h5 页面形态：URL 只带 kdt_id，靠 Cookie 的 KDTSESSIONID / nobody_sign 维持会话；
    本脚本是小程序 code 换 access_token 形态，所以两条鉴权路径都带上：
      - URL 参数 access_token（build_url 自带）
      - Cookie KDTSESSIONID / nobody_sign = session_id（有赞 H5 惯例）
    """
    url = build_url(path, token, query)
    headers = common_headers(token, session_id)
    headers["Referer"] = POINTSTORE_REFERER.get(path, "https://h5.youzan.com/wscump/pointstore/")
    if session_id:
        headers["Cookie"] = f"KDTSESSIONID={session_id}; nobody_sign={session_id}"
    try:
        resp = request_with_proxy("GET", url, headers=headers, proxies=proxies, server=server)
        return resp.json()
    except Exception as exc:
        warn(f"⚠️ [积分过期] {path} 请求异常: {exc}")
        return {"code": -1, "msg": str(exc)}


def fetch_point_records(server: str, token: str, proxies: Dict[str, str] | None,
                        session_id: str | None, ts_start: int, ts_end: int) -> List[Dict[str, Any]]:
    """拉取指定时间范围的积分明细（自动翻页）。"""
    records: List[Dict[str, Any]] = []
    page = 1
    total = 0
    while page <= EXPIRE_RECORDS_MAX_PAGES:
        resp = pointstore_get(server, POINTSTORE_RECORDS_PATH, token, proxies, session_id,
                              {"pageSize": EXPIRE_RECORDS_PAGE_SIZE, "page": page,
                               "timeStart": ts_start, "timeEnd": ts_end,
                               "type": 0, "needGoodsDetail": 1})
        if to_float(resp.get("code")) != 0:
            warn(f"⚠️ [积分过期] 积分明细查询失败: {resp.get('msg') or json_preview(resp, 200)}")
            break
        data = safe_data(resp)
        items = data.get("logContentList") or []
        records.extend([x for x in items if isinstance(x, dict)])
        if not total:
            total = int(to_float(data.get("total")) or 0)
        if not items or (total and len(records) >= total):
            break
        page += 1
    if total and len(records) < total:
        warn(f"⚠️ [积分过期] 积分明细可能被截断：已取 {len(records)}/{total} 条")
    return records


def sum_gained_points(records: List[Dict[str, Any]], year: int) -> float:
    """统计指定年份「获取」的积分总量（amount>0；负数为消耗不计）。"""
    total = 0.0
    for r in records:
        amount = to_float(r.get("amount"))
        if amount <= 0:
            continue
        if _cn_year(r.get("createdAt")) != year:
            continue
        total += amount
    return total


def query_expire_summary(server: str, token: str, proxies: Dict[str, str] | None,
                         session_id: str | None) -> Optional[Dict[str, Any]]:
    """查询即将过期积分数据。

    返回 {"prev": 上年获取量, "cur": 今年获取量, "nearly": 服务端口径(可空)}；失败返回 None。
    """
    today = date.today()
    year = today.year
    prev_year = year - 1
    # 时间分段按北京时间；每段不超过一年，避免服务端时间范围限制
    prev_start = int(datetime(prev_year, 1, 1, 0, 0, 0, tzinfo=CN_TZ).timestamp())
    prev_end = int(datetime(year, 1, 1, 0, 0, 0, tzinfo=CN_TZ).timestamp()) - 1
    cur_start = prev_end + 1
    cur_end = int(datetime(year, 12, 31, 23, 59, 59, tzinfo=CN_TZ).timestamp())

    prev_sum = sum_gained_points(fetch_point_records(server, token, proxies, session_id,
                                                     prev_start, prev_end), prev_year)
    cur_sum = sum_gained_points(fetch_point_records(server, token, proxies, session_id,
                                                    cur_start, cur_end), year)

    # 对照口径：服务端现成的「即将过期积分」
    nearly = None
    resp = pointstore_get(server, POINTSTORE_CUSTOMER_PATH, token, proxies, session_id,
                          {"withNearlyExpired": True})
    if to_float(resp.get("code")) == 0:
        nearly = to_float((safe_data(resp) or {}).get("nearlyExpiredAmount"))
    return {"prev": prev_sum, "cur": cur_sum, "nearly": nearly}


def expire_from_done(server: str) -> Optional[Dict[str, Any]]:
    """从日打标文件读回当天缓存的过期数据（下午 cron 全跳过时仍能提醒）。"""
    entry = load_daily_done().get(server)
    if not isinstance(entry, dict):
        return None
    if str(entry.get("date") or "") != _today():
        return None
    value = entry.get("expire")
    return value if isinstance(value, dict) else None


def check_expire_remind(rows: List[Dict[str, Any]]) -> None:
    """汇总打印 + 推送即将过期积分提醒。

    rows: [{"idx": 1, "server": "...", "prev": 上年获取, "cur": 今年获取, "nearly": 服务端口径}]
    上年获取为 0 → 该账号没有会到期的积分，不提醒。
    """
    hits = [r for r in rows if to_float(r.get("prev")) > 0]
    if not hits:
        return
    lines: List[str] = []
    total_all = 0.0
    for r in hits:
        prev = to_float(r.get("prev"))
        cur = to_float(r.get("cur"))
        total = prev + cur
        total_all += total
        text = f"账号{r.get('idx')}：即将过期 {total:.0f} 分（上年 {prev:.0f} + 今年 {cur:.0f}）"
        nearly = r.get("nearly")
        if nearly is not None:
            nearly_val = to_float(nearly)
            if abs(nearly_val - total) > 0.5:
                text += f"（服务端 nearlyExpiredAmount={nearly_val:.0f}，口径不一致仅供参考）"
        lines.append(text)

    log("⏳ [积分过期提醒] " + " ｜ ".join(lines))
    content = (
        "⏳ 积分即将过期提醒（年底前 7 天检查）\n"
        f"📅 检查日期：{_today()}\n"
        f"💠 合计即将过期：{total_all:.0f} 分\n"
        + "\n".join(f"　• {t}" for t in lines)
        + "\n\n⚠️ 到期规则：今年获取的积分，次年 12 月 31 日到期，请及时使用。"
    )
    try:
        send_pushplus("🐝 蜜蜂惊喜社积分即将过期提醒", content)
    except Exception as exc:
        warn(f"⚠️ [积分过期] 通知推送异常: {exc}")


# ====================== ISV 体系（2026-11-17a） ======================
# 背景：签到状态查询（point/log/query）与签到写请求（/api/sign）都在 mp-isv.youzanyun.com，
#       靠 yz_open_id（= getUserOpenInfo.json 的 userOpenId）标识用户，与 h5 的 access_token 体系不同。
#       目标：签到前先查 checkin 状态，已签则跳过写请求，避免二次提交判黑号。
#       拿不到 yz_open_id 时降级回原 checkinV2 签到，保证不漏签。

def extract_buyer_id(raw_login: Any) -> str | None:
    """从登录响应里宽松提取 buyerId / yzUid（getUserOpenInfo.json 需要）"""
    if not isinstance(raw_login, dict):
        return None
    candidates: List[str] = []
    for key in ("buyerId", "buyer_id", "yzUid", "yz_uid", "openId", "userId", "uid"):
        candidates.append(str(raw_login.get(key) or ""))
    inner = raw_login.get("data")
    if isinstance(inner, dict):
        for key in ("buyerId", "buyer_id", "yzUid", "yz_uid", "openId", "userId", "uid", "buyerid"):
            candidates.append(str(inner.get(key) or ""))
    for value in candidates:
        if value and value != "null":
            return value
    return None


def isv_headers() -> Dict[str, str]:
    """ISV 接口请求头（HAR 实测：仅需 content-type + Referer + UA，无 Extra-Data/access_token）"""
    return {
        "User-Agent": USER_AGENT,
        "Referer": f"https://servicewechat.com/{APPID}/32/page-frame.html",
        "Content-Type": "application/json;charset=UTF-8",
        "Accept": "application/json, text/plain, */*",
    }


def get_yz_open_id(server: str, token: str, session_id: str | None,
                   proxies: Dict[str, str] | None, raw_login: Any) -> str | None:
    """获取 ISV 的 yz_open_id（= userOpenId）。缓存优先，其次 getUserOpenInfo.json；失败返回 None。"""
    cached_open = get_cached_field(server, "userOpenId")
    if cached_open:
        log(f"🔑 [ISV] 使用缓存 userOpenId: {mask(cached_open)}")
        return cached_open

    buyer_id = get_cached_field(server, "buyerId") or extract_buyer_id(raw_login)
    # getUserOpenInfo.json 走 h5 体系（access_token + Extra-Data），buyerId 取不到就先不传，
    # 由服务端按 access_token 识别；仍失败则本次不做 ISV 预检、降级原签到。
    query = {"buyerId": buyer_id} if buyer_id else None
    url = build_url("wscuser/weapp/getUserOpenInfo.json", token, query)
    resp = api_get(server, url, token, proxies, session_id)
    if to_float(resp.get("code")) != 0:
        msg = resp.get("msg") or resp.get("message") or json_preview(resp, 200)
        warn(f"⚠️ [ISV] 获取 userOpenId 失败: {msg}（本次降级原签到）")
        return None
    data = safe_data(resp)
    open_id = data.get("userOpenId") or data.get("yzOpenId") or data.get("openId")
    if not open_id:
        warn("⚠️ [ISV] getUserOpenInfo 未返回 userOpenId（本次降级原签到）")
        return None
    open_id = str(open_id)
    set_cached_fields(server, userOpenId=open_id, buyerId=buyer_id)
    log(f"🔑 [ISV] userOpenId 获取成功: {mask(open_id)}")
    return open_id


def query_isv_task_state(server: str, yz_open_id: str,
                         proxies: Dict[str, str] | None) -> Dict[str, Any] | None:
    """查询 ISV 任务完成状态：{pagesview, checkin, subscribe}；失败返回 None"""
    try:
        resp = request_with_proxy(
            "POST", ISV_POINT_QUERY_URL,
            headers=isv_headers(),
            json={"kdtid": int(KDT_ID), "yz_open_id": yz_open_id},
            proxies=proxies, server=server,
        )
        data = resp.json()
    except Exception as exc:
        warn(f"⚠️ [ISV] 任务状态查询异常: {exc}")
        return None
    if str(data.get("flag")) != "200" and to_float(data.get("flagIntValue")) != 200:
        msg = data.get("message") or json_preview(data, 200)
        warn(f"⚠️ [ISV] 任务状态查询失败: {msg}")
        return None
    state = safe_data(data)
    if not isinstance(state, dict) or not state:
        warn("⚠️ [ISV] 任务状态为空（本次降级原签到）")
        return None
    return state


def isv_sign(server: str, yz_open_id: str, proxies: Dict[str, str] | None) -> str | None:
    """ISV 签到写请求；返回 '签到成功'/'今日已签到'，失败返回 None（降级原签到）"""
    try:
        resp = request_with_proxy(
            "POST", ISV_SIGN_URL,
            headers=isv_headers(),
            json={"kdt_id": int(KDT_ID), "yz_open_id": yz_open_id},
            proxies=proxies, server=server,
        )
        data = resp.json()
    except Exception as exc:
        warn(f"⚠️ [ISV] 签到异常: {exc}")
        return None
    flag = str(data.get("flag"))
    if flag == "200" or to_float(data.get("flagIntValue")) == 200:
        payload = data.get("data")
        msg = str(payload) if payload is not None else str(data.get("message") or "")
        if "已签" in msg:
            log("✅ [ISV] 今日已签到")
            return "今日已签到"
        log(f"✅ [ISV] 签到成功: {msg}")
        return "签到成功"
    msg = data.get("message") or json_preview(data, 200)
    warn(f"⚠️ [ISV] 签到失败: {msg}（本次降级原签到）")
    return None


def get_article_view_live_id(server: str, proxies: Dict[str, str] | None) -> str | None:
    """查询当前直播预告活动，返回 live_id（每天不同，不能写死）。
    HAR 115030 #20：POST /api/show  body={kdt_id} -> data.id（如 2768）、account_link。
    """
    try:
        resp = request_with_proxy(
            "POST", f"{ISV_BASE}/api/show",
            headers=isv_headers(),
            json={"kdt_id": int(KDT_ID)},
            proxies=proxies, server=server,
        )
        data = resp.json()
    except Exception as exc:
        warn(f"⚠️ [预告] 查询直播活动异常: {exc}")
        return None
    if str(data.get("flag")) != "200" and to_float(data.get("flagIntValue")) != 200:
        warn(f"⚠️ [预告] 查询直播活动失败: {data.get('message') or json_preview(data, 200)}")
        return None
    info = safe_data(data)
    live_id = info.get("id") or info.get("live_id")
    if not live_id:
        warn("⚠️ [预告] 活动未返回 live_id")
        return None
    return str(live_id)


def do_article_view_task(server: str, yz_open_id: str,
                         proxies: Dict[str, str] | None,
                         state: Dict[str, Any] | None) -> str:
    """浏览直播预告领奖任务（开关默认关）。
    状态检测：优先复用调用方已查的 state.pagesview；否则重新查询。
      - pagesview=true（今日已完成）→ 跳过写请求，返回"今日已完成"。
      - 状态查询失败 → 不做写请求，返回失败原因（避免盲目提交）。
      - pagesview=false → 提交 point/log/create 领奖。
    注意：真实浏览文章这一步脚本无法模拟；此处仅按状态提交领奖，是否判黑号由服务端决定。
    """
    if not ENABLE_ARTICLE_VIEW_TASK:
        return "-"

    if state is None:
        state = query_isv_task_state(server, yz_open_id, proxies)
    if state is None:
        warn("⚠️ [预告] 任务状态查询失败，跳过领奖写请求")
        return "状态查询失败，已跳过"

    if bool(state.get("pagesview")):
        log("✅ [预告] 今日浏览任务已完成（pagesview=true，未发送领奖请求）")
        return "今日已完成"

    live_id = get_article_view_live_id(server, proxies)
    if not live_id:
        return "未获取到活动 live_id，已跳过"

    wait_time = random.randint(2, 5)
    log(f"⏳ [预告] 领奖前等待 {wait_time}s（live_id={live_id}）")
    sleep(wait_time)

    try:
        resp = request_with_proxy(
            "POST", ISV_POINT_CREATE_URL,
            headers=isv_headers(),
            json={
                "kdtid": int(KDT_ID),
                "yz_open_id": yz_open_id,
                "live_id": live_id,
                "type": ARTICLE_VIEW_TASK_TYPE,
                "points": ARTICLE_VIEW_TASK_POINTS,
            },
            proxies=proxies, server=server,
        )
        data = resp.json()
    except Exception as exc:
        warn(f"⚠️ [预告] 领奖请求异常: {exc}")
        return f"领奖异常: {exc}"

    if str(data.get("flag")) == "200" or to_float(data.get("flagIntValue")) == 200:
        msg = str(data.get("data") or data.get("message") or "")
        log(f"✅ [预告] 领奖成功: {msg}")
        return f"领奖成功: {msg}"
    msg = data.get("message") or json_preview(data, 200)
    if any(kw in str(msg) for kw in ("已领", "重复", "已完成", "已参与")):
        log(f"✅ [预告] 今日已领取: {msg}")
        return "今日已领取"
    warn(f"⚠️ [预告] 领奖失败: {msg}")
    return f"领奖失败: {msg}"


def login_with_cache(server: str, proxies: Dict[str, str] | None) -> Tuple[str | None, str | None, Dict[str, Any] | None]:
    """优先使用缓存 token（积分接口验证），失效自动 code 刷新"""
    cache_token = get_cached_token(server)
    if cache_token:
        cache_sid = get_cached_session_id(server)
        log("🔍 [缓存] 验证 token")
        try:
            resp = api_get(server, build_url("wscump/integral/user_points.json", cache_token), cache_token, proxies, cache_sid)
            if to_float(resp.get("code")) == 0:
                log("✅ [缓存] token 有效")
                return cache_token, cache_sid, None
        except Exception as exc:
            warn(f"⚠️ [缓存] 验证异常: {exc}")
        warn("⚠️ [缓存] token 已失效，重新登录")

    code = get_code(server)
    if not code:
        return None, None, None

    token, session_id, raw_login = login_by_code(server, code, proxies)
    if not token:
        return None, None, raw_login

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
    set_cached_token(server, token, expire_time, session_id)
    # 2026-11-17a：首登时把 buyerId 缓存下来，供后续缓存 token 运行 ISV 状态预检使用
    buyer_id = extract_buyer_id(raw_login)
    if buyer_id:
        set_cached_fields(server, buyerId=buyer_id)
        log(f"🔑 [ISV] 买家标识 buyerId 已缓存: {buyer_id}")
    return token, session_id, raw_login


def do_checkin(server: str, token: str, proxies: Dict[str, str] | None,
               session_id: str | None = None) -> str:
    """照源脚本 getCheckinInfo + doCheckin：查活动 -> checkinV2 签到"""
    page_resp = api_get(server, build_url("wscump/checkin/show_checkin_page_v2.json", token), token, proxies, session_id)
    if to_float(page_resp.get("code")) != 0:
        msg = page_resp.get("msg") or page_resp.get("message") or json_preview(page_resp, 200)
        warn(f"⚠️ [签到] 获取签到活动失败: {msg}")
        return f"获取签到活动失败: {msg}"

    data = safe_data(page_resp)
    checkin_id = data.get("checkinId") or data.get("checkin_id") or ""
    is_show = data.get("isShow") if data.get("isShow") is not None else data.get("is_show")
    log(f"✅ [签到] 签到活动: checkinId={checkin_id or '未获取'} isShow={is_show}")

    if not checkin_id:
        warn("⚠️ [签到] 未获取到签到活动，跳过")
        return "未获取到签到活动，跳过"

    wait_time = random.randint(2, 5)
    log(f"⏳ [签到] 提交前等待 {wait_time}s")
    sleep(wait_time)

    sign_resp = api_get(server, build_url("wscump/checkin/checkinV2.json", token, {"checkinId": checkin_id}), token, proxies, session_id)
    if to_float(sign_resp.get("code")) == 0:
        sign_data = safe_data(sign_resp)
        award_list = []
        for item in sign_data.get("list") or []:
            if isinstance(item, dict):
                infos = item.get("infos") or {}
                title = infos.get("title") or infos.get("desc") or ""
                if title:
                    award_list.append(title)
        award = ", ".join(award_list)
        msg = f"{sign_data.get('desc') or ''}{' ' + award if award else ''}".strip()
        log(f"✅ [签到] 签到成功: {msg}")
        return f"签到成功: {msg}"

    message = str(sign_resp.get("msg") or sign_resp.get("message") or json_preview(sign_resp, 200))
    if any(keyword in message for keyword in ("已签到", "已经签到", "重复", "参与次数")) or "今日" in message and "签" in message:
        log("✅ [签到] 今日已签到")
        return "今日已签到"

    err(f"❌ [签到] 签到失败: {message}")
    return f"签到失败: {message}"


def run_account(index: int, total: int, server: str) -> Dict[str, Any]:
    result = {
        "server": server,
        "success": False,
        "proxyStatus": "未使用代理",
        "proxyIp": "-",
        "token": "-",
        "signMsg": "-",
        "pointsMsg": "-",
        "totalPoints": None,
        "articleMsg": "-",
        "expire": None,
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

    token, session_id, raw_login = login_with_cache(server, proxies)
    if not token:
        result["error"] = f"登录失败: {json_preview(raw_login)}"
        return result

    result["token"] = mask(token)

    try:
        # 签到前积分
        points_before, points_before_val = get_points(server, token, proxies, "签到前积分", session_id)

        # 2026-11-17a：优先走 ISV 体系——先查任务状态，已签则跳过写请求（防重复提交判黑号）。
        # 任一环节拿不到 yz_open_id / 查不到状态 / 签到失败，均降级回原 checkinV2，保证不漏签。
        signed_by_isv = False
        state: Dict[str, Any] | None = None
        yz_open_id = get_yz_open_id(server, token, session_id, proxies, raw_login)
        if yz_open_id:
            state = query_isv_task_state(server, yz_open_id, proxies)
            if state is not None:
                checkin_done = bool(state.get("checkin"))
                log(f"📋 [ISV] 任务状态: checkin={checkin_done} "
                    f"pagesview={bool(state.get('pagesview'))} subscribe={bool(state.get('subscribe'))}")
                if checkin_done:
                    log("✅ [签到] 今日已签到（ISV 状态预检确认，未发送签到请求）")
                    result["signMsg"] = "今日已签到"
                    signed_by_isv = True
                else:
                    isv_msg = isv_sign(server, yz_open_id, proxies)
                    if isv_msg:
                        result["signMsg"] = isv_msg
                        signed_by_isv = True

        if not signed_by_isv:
            log("🔁 [签到] ISV 路径未生效，降级原 checkinV2 签到")
            result["signMsg"] = do_checkin(server, token, proxies, session_id)

        # 签到后积分
        points_after, points_after_val = get_points(server, token, proxies, "签到后积分", session_id)
        result["pointsMsg"] = f"{points_before} → {points_after}"
        # 总积分：签到后余额优先，取不到则退回签到前余额
        result["totalPoints"] = points_after_val if points_after_val is not None else points_before_val

        # 2026-11-17b：浏览直播预告领奖任务（开关默认关，关时直接返回"-"不产生任何请求）
        if ENABLE_ARTICLE_VIEW_TASK:
            if yz_open_id:
                result["articleMsg"] = do_article_view_task(server, yz_open_id, proxies, state)
            else:
                result["articleMsg"] = "无 userOpenId，已跳过"
                warn("⚠️ [预告] 未获取到 userOpenId，跳过领奖任务")

        # 积分过期数据（仅年底前 7 天查询，平时完全不发请求；失败不影响主流程）
        if ENABLE_EXPIRE_REMIND and in_expire_window():
            try:
                result["expire"] = query_expire_summary(server, token, proxies, session_id)
            except Exception as exc:
                warn(f"⚠️ [积分过期] 查询异常: {exc}")
                result["expire"] = None

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

    content = f"""🐝 蜜蜂惊喜社小程序任务结果

━━━━━━━━━━━━━━━━━━━━
🏁 总结：{success_count} 成功 / {fail_count} 失败{' / %d 跳过（今日已完成）' % skip_count if skip_count else ''}
🕒 时间：{now_text()}
━━━━━━━━━━━━━━━━━━━━
"""

    for idx, res in enumerate(results, 1):
        icon = "✅" if res["success"] else "❌"

        # 2026-11-17b：预告任务仅在开关开启时展示
        article_line = f"\n📺 预告：{res.get('articleMsg', '-')}" if ENABLE_ARTICLE_VIEW_TASK else ""

        content += f"""
👤 账号 {idx}
📝 签到：{res["signMsg"]}
💰 积分：{res["pointsMsg"]}{article_line}
{icon} 结果：{"成功" if res["success"] else "失败"}
"""

        if not res["success"]:
            content += f"❌ 原因：{res['error']}\n"

        content += "━━━━━━━━━━━━━━━━━━━━\n"

    # 总积分汇总（跳过账号从打标缓存读回，一并列出）
    points_rows = []
    for item in results:
        if item.get("totalPoints") is not None:
            points_rows.append((int(item.get("idx") or 0), float(item["totalPoints"])))
    for item in (skipped or []):
        val = total_points_from_done(item.get("server"))
        if val is not None:
            points_rows.append((int(item.get("idx") or 0), float(val)))
    if points_rows:
        content += "💎 总积分汇总：\n"
        for idx, val in sorted(points_rows):
            content += "💎 总积分(%.2f)（账号%d）\n" % (val, idx)
        content += "━━━━━━━━━━━━━━━━━━━━\n"

    return content


def main() -> None:
    log_startup(len(SERVERS))

    results: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    # 积分过期数据单独收集，不混进 results，避免影响原有成功/失败/跳过统计口径
    expire_rows: List[Dict[str, Any]] = []
    # 总积分汇总：[(账号序号, 总积分)]
    points_rows: List[tuple] = []

    for index, server in enumerate(SERVERS, 1):
        result: Optional[Dict[str, Any]] = None
        # 今天已经完整跑过的账号直接跳过，不再消耗 YYB 取码/取号配额
        if is_done_today(server):
            log(f"⏭️ [打标] 账号 {index}/{len(SERVERS)} 今日任务已完成，跳过")
            skipped.append({"server": server, "idx": index})
            # 从打标缓存读回当天查到的过期数据，保证下午 cron 跳过时汇总仍能提醒
            cached_expire = expire_from_done(server)
            if cached_expire:
                expire_rows.append({"idx": index, "server": server, **cached_expire})
            # 被跳过也要在汇总里显示总积分：从打标缓存读回，不重新登录取码
            cached_total = total_points_from_done(server)
            if cached_total is not None:
                points_rows.append((index, cached_total))
            continue

        try:
            result = run_account(index, len(SERVERS), server)
            result["idx"] = index
            results.append(result)
            if result.get("totalPoints") is not None:
                record_points_snapshot(server, result["totalPoints"])
            # 本脚本 result["success"] 恒为 True，签到是否真的成功只体现在 signMsg 里，
            # 所以只有「签到成功 / 今日已签到」才打标（异常路径已在上面 except 处理，不会走到这）。
            sign_msg = str(result.get("signMsg", ""))
            should_mark = bool(result.get("success")) and (
                sign_msg.startswith("签到成功") or sign_msg == "今日已签到")
        except Exception as exc:
            err(f"❌ [主程序] {server} 执行异常: {exc}")
            results.append({
                "server": server,
                "success": False,
                "proxyStatus": "-",
                "proxyIp": "-",
                "token": "-",
                "signMsg": "-",
                "pointsMsg": "-",
                "totalPoints": None,
                "articleMsg": "-",
                "expire": None,
                "error": traceback.format_exc().strip(),
            })
        else:
            # 放在 else 里：打标失败不会把已成功的账号误记为失败
            if should_mark:
                mark_done_today(server, sign_msg, expire=result.get("expire"),
                                total_points=result.get("totalPoints"))

        expire_info = result.get("expire") if isinstance(result, dict) else None
        if isinstance(expire_info, dict):
            expire_rows.append({"idx": index, "server": server, **expire_info})
        total_points = result.get("totalPoints") if isinstance(result, dict) else None
        if total_points is not None:
            points_rows.append((index, total_points))

        if index < len(SERVERS):
            log("⏳ [间隔] 等待 2s 后处理下一个账号")
            sleep(2)

    success_count = sum(1 for item in results if item["success"])
    fail_count = len(results) - success_count

    # ===== 统一汇总日志（新风格）=====
    _now = datetime.now()
    _week = "一二三四五六日"[_now.weekday()]
    log(SUMMARY_SEP)
    log("📂 汇总日志")
    log("📃 脚本名称：蜜蜂惊喜社")
    log("📅 运行日期：%s  周%s" % (_now.strftime("%Y-%m-%d"), _week))
    log("📊 运行结果：✔️ 成功：%d 个；❌ 失败：%d 个；⏭️ 跳过：%d 个"
        % (success_count, fail_count, len(skipped)))

    # 总积分：一行并列；括号内为较昨天最后一次记录的涨跌
    _pts = []
    for item in results:
        val = item.get("totalPoints")
        if val is not None and str(val).strip() != "":
            try:
                _pts.append((int(item.get("idx") or 0), float(val), item.get("server", "")))
            except (TypeError, ValueError):
                pass
    for item in skipped:
        srv = item.get("server", "")
        val = total_points_from_done(srv)
        if val is not None:
            try:
                _pts.append((int(item.get("idx") or 0), float(val), srv))
            except (TypeError, ValueError):
                pass
    _pts.sort()
    if _pts:
        _cells = []
        for idx, val, srv in _pts:
            _delta = points_delta(srv, val)
            _dt = "(%+.2f)" % _delta if _delta is not None else ""
            _cells.append("账号%d：%.2f%s" % (idx, val, _dt))
        log("💰 总积分：" + "  |  ".join(_cells))

    if expire_rows:
        try:
            check_expire_remind(expire_rows)
        except Exception as exc:
            warn(f"⚠️ [积分过期] 提醒处理异常: {exc}")
    log(SUMMARY_SEP)

    _channels = send_pushplus("🐝 蜜蜂惊喜社任务完成", build_notify(results, skipped))
    if _channels:
        log("📢 [%s]通知已发送成功！" % "、".join(_channels))
    else:
        warn("⚠️ 通知渠道未配置，跳过推送。")
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

    熔断（429）、账号失效（404）、上游拒绝（-14 等）一律不重试。
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


if _yyb_accounts():
    SERVERS = _yyb_accounts()

# 账号序号白名单：按 YYB_SERVER 的书写顺序从 1 开始编号，留空 [] 表示全部
if YYB_ONLY_REFS:
    _wanted = {int(r) for r in YYB_ONLY_REFS}
    SERVERS = [s for i, s in enumerate(SERVERS, 1) if i in _wanted]
    if not SERVERS:
        warn(f"⚠️ [账号] YYB_ONLY_REFS={YYB_ONLY_REFS} 未匹配到任何账号（YYB_SERVER 共 {len(_yyb_accounts())} 个）")


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
