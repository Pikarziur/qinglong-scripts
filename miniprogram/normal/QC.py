#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# =========================================================
# name:  雀巢
# version: 2026-10-29a
# cron: 48 5,15 * * *
# 积分有效期：
# =========================================================
# 雀巢会员俱乐部自动任务脚本（青龙面板专用版）
# 版本: v2.1.0
# 更新日期: 2026-05-13
# 功能: 自动完成每日签到、浏览官网、浏览视频号任务
# 适配: 雀巢小程序  | 品赞代理 | PushPlus推送
# ==============================================
#
# 【使用前必读】
# 1. 必须先部署好code获取服务（本地/远程均可）
# 2. 品赞代理为可选配置，无代理可使用直连模式
# 3. 脚本仅用于学习交流，请勿用于商业用途
#
# ==============================================
# 【一、依赖安装】
# 青龙面板执行以下命令安装依赖：
# pip install httpx[http2] httpx-socks python-dotenv
# ==============================================
#
# 【二、环境变量配置（青龙面板-环境变量）】
# ┌───────────────┬──────────┬─────────────────────────────────────┐
# │ 变量名         │ 必填/可选 │ 说明                                │
# ├───────────────┼──────────┼─────────────────────────────────────┤
# │ PROXY_API     │ 可选     │ 品赞代理提取API链接                  │
# │ PROXY_TYPE    │ 可选     │ 代理类型：http(默认) 或 socks5       │
# │ PLUSPLUS_TOKEN│ 可选     │ PushPlus推送Token，用于接收任务结果  │
# │ YYB_SERVER         │ 必填     │ 内网wxcode服务地址，多个换行分隔     │
# └───────────────┴──────────┴─────────────────────────────────────┘
#
# YYB_SERVER示例值（多行换行）：
# 127.0.0.1:8088
# 192.168.1.21:8088
# 10.30.9.49:8088
#
# ==============================================
#
# 【三、青龙面板部署步骤】
# 1. 上传脚本：进入青龙面板-脚本管理-新建脚本，粘贴全部代码保存
# 2. 配置环境变量：进入环境变量-添加变量，按上面表格配置
# 3. 设置定时任务：进入定时任务-添加任务，命令填写：python3 quechao.py
#    推荐定时：0 8 * * * （每天早上8点执行）
# 4. 测试运行：点击任务右侧的运行按钮，查看日志确认是否正常执行
#
# ==============================================
#
# 【四、自定义配置说明（脚本内修改）】
# 1. 代理开关：
#    ENABLE_PER_ACCOUNT_PROXY = True  # 每个账号独立代理（推荐）
#    ENABLE_DIRECT_FALLBACK = True    # 代理失败自动切换直连
#
# 2. 关闭代理：清空PROXY_API环境变量即可自动使用直连模式
#
# ==============================================
#
# 【五、常见问题排查】
# 1. ❌ 获取code失败
#    - 检查code服务是否正常运行
#    - 确认YYB_SERVER内服务器地址和端口是否正确
#    - 测试配置格式：YYB_SERVER=yyb-go:8000@你的openid
#
# 2. ❌ 获取token失败
#    - 确认code未过期（有效期5分钟）
#    - 检查网络是否能访问 crm.nestlechinese.com
#    - 关闭系统代理，使用脚本内置代理或直连
#
# 3. ❌ 代理相关问题
#    - 确认品赞API链接是否正确
#    - 检查代理IP是否有剩余
#    - 尝试切换代理类型为socks5
#
# ==============================================
#
# 【六、更新日志】
# v2.1.0 (2026-05-13)
# - 优化青龙面板日志格式，code参数专业打印
# - 恢复品赞代理系统，支持单账号独立代理
# - 移除失效的2026年签到接口，解决多余错误提示
# - 精简代码，移除所有失效的抽奖功能
#
# v2.0.0 (2026-05-12)
# - 修复displayVersion大小写错误
# - 新增token有效性验证
# - 同步最新抓包的接口和请求头
#
# ==============================================
#
# 【七、免责声明】
# 本脚本仅供个人学习和研究使用，请勿用于商业用途
# 使用本脚本产生的任何风险由使用者自行承担
# 如有侵权，请联系作者删除
#
# ==============================================

import os
import sys
import asyncio
import json
import random
from datetime import datetime
from typing import List, Dict, Any, Optional, Tuple
# ========== 账号分隔标识 ==========
# 多账号同跑时, 用醒目横线把各账号日志隔开, 便于阅读与定位。
def now_text() -> str:
    # 兼容两种导入: from datetime import datetime / import datetime
    _dt = datetime.now() if hasattr(datetime, "now") else datetime.datetime.now()
    return _dt.strftime("%Y-%m-%d %H:%M:%S")
# ========== 启动信息 ==========
def log_startup(total: int = 0) -> None:
    log(ACC_RULE)
    log("📌 脚本版本: %s ｜ %s" % (SCRIPT_VER, APPID))
    if total:
        log("🔢 账号数量: %d" % total)
    log(ACC_RULE)
SCRIPT_VER = "2026-10-29a"
ACC_RULE = "=" * 70

# ========== 统一日志：[LEVEL] [QC] message ==========
def _emit(level, msg):
    line = "[%s] [QC] %s" % (level, msg)
    print(line, flush=True)


def log(msg):
    _emit("INFO", msg)


def warn(msg):
    _emit("WARN", msg)


def err(msg):
    _emit("ERROR", msg)


# 强制全局禁用所有系统代理环境变量
for env_var in ['HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy',
                'ALL_PROXY', 'all_proxy', 'NO_PROXY', 'no_proxy']:
    os.environ.pop(env_var, None)

try:
    import httpx
    from httpx import AsyncHTTPTransport
    from httpx_socks import AsyncProxyTransport
except ImportError:
    err("❌ 缺少依赖库，请执行：pip install httpx[http2] httpx-socks python-dotenv")
    sys.exit(1)

# ===================== 配置项 =====================
# 内部配置：直接改这里，省得去面板设环境变量
# 账号序号白名单（1 起，按 YYB_SERVER 里的书写顺序编号）；留空 [] = 全部运行
# 例：[1] 只跑第 1 个账号；[1, 3] 跑第 1 和第 3 个
YYB_ONLY_REFS = []

# 从环境变量 YYB_SERVER 读取内网wxcode服务地址，多条换行分隔
SERVERS = []
env_YYB_SERVER = os.getenv("YYB_SERVER", "")
if env_YYB_SERVER:
    raw_lines = env_YYB_SERVER.splitlines()
    SERVERS = [line.strip() for line in raw_lines if line.strip()]
_all_servers = list(SERVERS)

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
# 连接层超时**不触发 fleet.failover**，所以一个成员超时就整账号失败。
# 而 YYB-GO 每次新请求会按「最近最少使用」把请求轮转到编队的下一个成员，
# 重试正好能命中可用成员。
#
# 只对「上游连接超时」重试：
#   - 429（本地限速/熔断）：服务端已在冷却，重试只会加剧风控 → 不重试
#   - 404 / 409（账号不存在/失效）：不可恢复 → 不重试
#   - 502 且带上游错误码（-14 等授权拒绝）：重试无用 → 不重试
# 间隔需大于 YYB 的取码最小间隔（YYB_CODE_MIN_GAP=3s），否则会被限速。
YYB_RETRY_TIMES = int(os.getenv("QC_YYB_RETRY", "5") or 5)      # 额外重试次数（不含首次）
YYB_RETRY_DELAY = int(os.getenv("QC_YYB_RETRY_DELAY", "4") or 4)  # 重试间隔秒数

# 小程序 AppID —— 版本行要打印它，必须在 log_startup() 调用之前定义
APPID = "wxc5db704249c9bb31"

# 无有效地址直接退出并提示
log_startup()

if len(SERVERS) == 0:

    err("❌ 未配置环境变量 YYB_SERVER")
    log("格式：地址@微信账号标识，多账号换行分隔")
    log("192.168.1.21:8088")
    log("10.30.9.49:8088")
    sys.exit(1)

log(f"✅ 读取到 {len(SERVERS)} 个 YYB Go 账号")

# 按 YYB_ONLY_REFS 序号白名单筛选（1 起）；留空 [] 则运行全部账号
if YYB_ONLY_REFS:
    _wanted = {int(r) for r in YYB_ONLY_REFS}
    SERVERS = [s for i, s in enumerate(SERVERS, 1) if i in _wanted]
    if not SERVERS:
        err(f"❌ YYB_ONLY_REFS={YYB_ONLY_REFS} 未匹配到任何账号（YYB_SERVER 共 {len(_all_servers)} 个）")
        sys.exit(1)
    log(f"✅ 按 YYB_ONLY_REFS={YYB_ONLY_REFS} 筛选后剩 {len(SERVERS)} 个账号")

log("-" * 60 + "\n")

def parse_yyb_go_entry(raw_value: str) -> Tuple[str, str]:
    value = str(raw_value or "").strip()
    if not value:
        return "", ""
    at_index = value.rfind("@")
    if at_index == -1:
        return value, ""
    server = value[:at_index].strip()
    ref = value[at_index + 1:].strip()
    server = server.removeprefix("http://").removeprefix("https://").rstrip("/")
    return server, ref


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


async def get_code_via_yyb(server_entry: str, appid: str) -> Optional[str]:
    server, ref = parse_yyb_go_entry(server_entry)
    if not server:
        err(f"❌ [{server_entry}] 获取code失败 | 服务地址为空")
        return None
    if not ref:
        err(f"❌ [{server_entry}] 获取code失败 | 缺少openid/ref")
        return None

    url = f"http://{server}/wxapp/getCode"
    for attempt in range(YYB_RETRY_TIMES + 1):
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(90.0, connect=10.0), trust_env=False) as client:
                response = await client.post(url, json={"ref": ref, "app_id": appid})
                try:
                    res = response.json()
                except ValueError:
                    res = None
        except httpx.RequestError as exc:
            # 网络层异常（连接被拒 / 读取超时等）同属连接层故障，可重试
            if attempt >= YYB_RETRY_TIMES:
                err(f"❌ [{server_entry}] 获取code异常 | 原因: {str(exc)}")
                return None
            log(f"⏳ [重试] getCode 第{attempt + 1}/{YYB_RETRY_TIMES + 1}次失败（上游连接超时），{YYB_RETRY_DELAY}秒后重试")
            await asyncio.sleep(YYB_RETRY_DELAY)
            continue

        except Exception as e:
            err(f"❌ [{server_entry}] 获取code异常 | 原因: {str(e)}")
            return None

        if response.status_code >= 400:
            detail = _yyb_error_detail(res)
            if detail != "上游连接超时" or attempt >= YYB_RETRY_TIMES:
                err(f"❌ [{server_entry}] 获取code失败 | HTTP {response.status_code}"
                    + (f"（{detail}）" if detail else ""))
                return None
            log(f"⏳ [重试] getCode 第{attempt + 1}/{YYB_RETRY_TIMES + 1}次失败（{detail}），{YYB_RETRY_DELAY}秒后重试")
            await asyncio.sleep(YYB_RETRY_DELAY)
            continue

        if not isinstance(res, dict):
            err(f"❌ [{server_entry}] 获取code失败 | 响应不是JSON格式")
            return None

        code = (((res.get("data") or {}).get("result") or {}).get("code"))
        if res.get("code") != 0 or not code:
            err(f"❌ [{server_entry}] 获取code失败 | 返回异常: {str(res)[:200]}")
            return None

        log(f"✅ [{server}] 获取code成功")
        return code

    err(f"❌ [{server_entry}] 获取code失败 | 重试次数已用尽")
    return None


PLUSPLUS_TOKEN = os.getenv("PLUSPLUS_TOKEN", "")

# 品赞代理配置（环境变量，可选）
PROXY_API = os.getenv("PROXY_API", "")
PROXY_TYPE = os.getenv("PROXY_TYPE", "http")
PROXY_RETRY_TIMES = 3
PROXY_VALIDATE_URL = "http://httpbin.org/ip"

# 核心代理开关
ENABLE_PER_ACCOUNT_PROXY = True
PROXY_FETCH_INTERVAL = 3000
ENABLE_DIRECT_FALLBACK = True

# 固定配置
APP_VERSION = "491"
XWEB_VERSION = "19823"
TOKEN_CLIENT_ID = "wechatMini"
TOKEN_CLIENT_SECRET = "secret"
TOKEN_GRANT_TYPE = "wechat_auth_code"
TOKEN_URL = "https://crm.nestlechinese.com/openapi/identityservice/connect/token"

# UA池
USER_AGENT_LIST = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36 MicroMessenger/7.0.20.1781(0x6700143B) NetType/WIFI MiniProgramEnv/Windows WindowsWechat/WMPF WindowsWechat(0x63090a13) UnifiedPCWindowsWechat(0xf2541923) XWEB/19823",
    f"Mozilla/5.0 (Linux; Android 14; 2512BPNDAC Build/UKQ1.230917.001; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/146.0.7680.153 Mobile Safari/537.36 XWEB/{XWEB_VERSION} MMWEBSDK/20251006 MiniProgramEnv/android"
]

# 跳过的任务GUID
SKIP_TASK_GUIDS = {"38C8BBDA3DAE4CD685B270D939E5063D", "36EFECD2AD8C44278317ED567EB24DD9"}

# ===================== 日打标（今日完成标记） =====================
# 今日已完成的账号当天再次运行时直接跳过，避免浪费 YYB 取码/取号配额
DAILY_DONE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "qc_daily_done.json")

# ===================== 工具函数 =====================
def sleep(ms: int) -> asyncio.Future:
    return asyncio.sleep(ms / 1000)

def random_int(min_val: int, max_val: int) -> int:
    return random.randint(min_val, max_val)

def get_ua() -> str:
    return random.choice(USER_AGENT_LIST)

def build_direct_transport() -> AsyncHTTPTransport:
    return AsyncHTTPTransport()

# ====================== 日打标（今日完成标记） ======================
def _today() -> str:
    """当天日期，用于跨天判定。"""
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
    """该账号今天是否已打过标（是则本次直接跳过）。"""
    entry = load_daily_done().get(server)
    if not isinstance(entry, dict):
        return False
    return str(entry.get("date") or "") == _today()


def mark_done_today(server: str, note: str = "") -> None:
    """给该账号打上「今日已完成」标记。"""
    cache = load_daily_done()
    today = _today()
    # 只保留当天的记录，跨天自动清理，避免文件无限增长
    cache = {k: v for k, v in cache.items()
             if isinstance(v, dict) and str(v.get("date") or "") == today}
    cache[server] = {
        "date": today,
        "doneTime": now_text(),
        "note": str(note or ""),
    }
    save_daily_done(cache)
    log(f"✅ [打标] 今日任务已完成，已记录（{today}）")


def clear_daily_done(server: str = "") -> None:
    """清除指定账号（或全部）的日打标。"""
    cache = load_daily_done()
    if server:
        cache.pop(server, None)
    else:
        cache = {}
    save_daily_done(cache)

# ===================== 品赞代理系统 =====================
def parse_proxy_response(text: str) -> Optional[Dict[str, Any]]:
    text = text.strip()
    if not text:
        return None

    try:
        data = json.loads(text)
        proxy_obj = None
        if data.get("data") and isinstance(data["data"], list) and len(data["data"]) > 0:
            proxy_obj = data["data"][0]
        elif data.get("ip") and data.get("port"):
            proxy_obj = data
        elif data.get("result") and data["result"].get("ip") and data["result"].get("port"):
            proxy_obj = data["result"]

        if proxy_obj:
            return {
                "host": proxy_obj["ip"],
                "port": int(proxy_obj["port"]),
                "username": proxy_obj.get("user") or proxy_obj.get("username") or "",
                "password": proxy_obj.get("pass") or proxy_obj.get("password") or ""
            }
    except json.JSONDecodeError:
        if ":" in text:
            parts = text.split(":")
            if len(parts) >= 2:
                return {
                    "host": parts[0],
                    "port": int(parts[1]),
                    "username": parts[2] if len(parts) >= 3 else "",
                    "password": parts[3] if len(parts) >= 4 else ""
                }
    return None

async def validate_proxy(proxy_info: Dict[str, Any]) -> bool:
    if not proxy_info:
        return False

    try:
        transport = build_proxy_transport(proxy_info)
        async with httpx.AsyncClient(transport=transport, timeout=15.0) as client:
            response = await client.get(PROXY_VALIDATE_URL)
            if response.status_code == 200:
                ip = response.json().get("origin", "未知")
                log(f"✅ 代理验证通过 | 出口IP: {ip}")
                return True
    except Exception as e:
        warn(f"⚠️ 代理验证失败 | 原因: {str(e)}")
    return False

def build_proxy_transport(proxy_info: Dict[str, Any]) -> Optional[AsyncProxyTransport]:
    if not proxy_info:
        return None

    host = proxy_info["host"]
    port = proxy_info["port"]
    username = proxy_info["username"]
    password = proxy_info["password"]

    try:
        if PROXY_TYPE == "socks5":
            proxy_url = f"socks5://{username}:{password}@{host}:{port}" if username and password else f"socks5://{host}:{port}"
        else:
            proxy_url = f"http://{username}:{password}@{host}:{port}" if username and password else f"http://{host}:{port}"

        return AsyncProxyTransport.from_url(proxy_url)
    except Exception as e:
        err(f"❌ 代理生成失败 | 原因: {str(e)}")
        return None

async def get_valid_proxy(account_name: str) -> Optional[Dict[str, Any]]:
    if not PROXY_API:
        log(f"ℹ️ [{account_name}] 未配置代理 | 使用直连模式")
        return None

    log(f"🔌 [{account_name}] 正在获取专属代理...")

    for i in range(PROXY_RETRY_TIMES):
        try:
            async with httpx.AsyncClient(timeout=15.0, transport=build_direct_transport()) as client:
                response = await client.get(PROXY_API)
            proxy_info = parse_proxy_response(response.text)

            if not proxy_info:
                warn(f"⚠️ [{account_name}] 第{i+1}次获取代理失败 | 响应格式错误")
                continue

            if await validate_proxy(proxy_info):
                return proxy_info
            else:
                warn(f"⚠️ [{account_name}] 第{i+1}次代理不可用 | 重试中...")

        except Exception as e:
            warn(f"⚠️ [{account_name}] 第{i+1}次获取代理异常 | 原因: {str(e)}")

        if i < PROXY_RETRY_TIMES - 1:
            await sleep(2000)

    err(f"❌ [{account_name}] 代理获取失败 | 切换直连模式")
    return None

# ===================== PushPlus推送函数 =====================
async def send_plusplus_notification(title: str, content: str) -> None:
    if not PLUSPLUS_TOKEN:
        return

    try:
        async with httpx.AsyncClient(timeout=5.0, transport=build_direct_transport()) as client:
            response = await client.post(
                "https://www.pushplus.plus/send",
                json={
                    "token": PLUSPLUS_TOKEN,
                    "title": title,
                    "content": content,
                    "template": "txt"
                }
            )
        if response.status_code == 200:
            log("✅ 通知推送成功")
    except Exception as e:
        err(f"❌ 通知推送失败 | 原因: {str(e)}")

# ===================== 核心业务类 =====================
class QueChaoBot:
    def __init__(self, server: str, proxy_info: Optional[Dict[str, Any]] = None):
        self.server = server
        self.proxy_info = proxy_info
        self.base_url = "https://crm.nestlechinese.com"
        self.token = None
        self.ua = get_ua()
        self.client = None

    async def get_code(self) -> Optional[str]:
        """从本地服务获取code（青龙优化版日志）"""
        return await get_code_via_yyb(self.server, APPID)

    async def get_token_by_code(self, code: str) -> Optional[str]:
        """通过code换取token（简洁日志）"""
        log(f"🔑 [{self.server}] 正在换取token...")

        headers = {
            "Host": "crm.nestlechinese.com",
            "Connection": "keep-alive",
            "User-Agent": self.ua,
            "xweb_xhr": "1",
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "*/*",
            "Sec-Fetch-Site": "cross-site",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Dest": "empty",
            "Referer": f"https://servicewechat.com/{APPID}/{APP_VERSION}/page-frame.html",
            "Accept-Encoding": "gzip, deflate, br",
            "Accept-Language": "zh-CN,zh;q=0.9"
        }

        form_data = {
            "client_id": TOKEN_CLIENT_ID,
            "client_secret": TOKEN_CLIENT_SECRET,
            "grant_type": TOKEN_GRANT_TYPE,
            "auth_code": code
        }

        try:
            transport = build_proxy_transport(self.proxy_info) if self.proxy_info else build_direct_transport()
            mode = "代理" if self.proxy_info else "直连"

            async with httpx.AsyncClient(
                transport=transport,
                headers=headers,
                timeout=20.0,
                http2=True
            ) as client:
                response = await client.post(TOKEN_URL, data=form_data)

            if response.status_code != 200:
                raise Exception(f"HTTP错误: {response.status_code}")

            res = response.json()
            if res.get("access_token") and res.get("token_type", "Bearer").lower() == "bearer":
                self.token = res["access_token"]
                log(f"✅ [{self.server}] 获取token成功 | 模式: {mode}")
                return self.token
            else:
                raise Exception(f"业务错误: {res.get('error', '未知错误')}")
        except Exception as e:
            warn(f"⚠️ [{self.server}] {mode}获取token失败 | 原因: {str(e)}")

            if self.proxy_info and ENABLE_DIRECT_FALLBACK:
                warn(f"🌐 [{self.server}] 切换直连重试...")
                try:
                    async with httpx.AsyncClient(
                        headers=headers,
                        timeout=20.0,
                        http2=True,
                        transport=build_direct_transport()
                    ) as client:
                        response = await client.post(TOKEN_URL, data=form_data)

                    res = response.json()
                    if res.get("access_token"):
                        self.token = res["access_token"]
                        log(f"✅ [{self.server}] 直连获取token成功")
                        return self.token
                    else:
                        raise Exception(f"直连业务错误: {res.get('error', '未知错误')}")
                except Exception as e2:
                    err(f"❌ [{self.server}] 直连获取token失败 | 原因: {str(e2)}")

        return None

    async def __aenter__(self):
        transport = build_proxy_transport(self.proxy_info) if self.proxy_info else build_direct_transport()
        self.client = httpx.AsyncClient(
            base_url=self.base_url,
            headers=self._get_base_headers(),
            transport=transport,
            http2=True,
            timeout=30.0
        )
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.client:
            await self.client.aclose()

    def _get_base_headers(self) -> Dict[str, str]:
        headers = {
            "Host": "crm.nestlechinese.com",
            "displayVersion": "0",
            "User-Agent": self.ua,
            "xweb_xhr": "1",
            "Content-Type": "application/json",
            "Accept": "*/*",
            "Referer": f"https://servicewechat.com/{APPID}/{APP_VERSION}/page-frame.html",
            "Accept-Encoding": "gzip, deflate, br",
            "Accept-Language": "zh-CN,zh;q=0.9"
        }

        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        return headers

    def check_response(self, response_data: Dict[str, Any]) -> bool:
        if response_data.get("errcode") != 200:
            err(f"❌ [{self.server}] 请求失败 | 原因: {response_data.get('errmsg', '未知错误')}")
            return False
        return True

    async def get_user_balance(self) -> Optional[int]:
        try:
            response = await self.client.post(
                "/openapi/pointsservice/api/Points/getuserbalance",
                content="{}"
            )
            response_data = response.json()

            if self.check_response(response_data):
                return response_data.get("data")
            return None
        except Exception as e:
            err(f"❌ [{self.server}] 获取积分失败 | 原因: {str(e)}")
            return None

    async def daily_sign(self) -> Tuple[bool, str]:
        try:
            response = await self.client.post(
                "/openapi/activityservice/api/sign2025/sign",
                content='{"rule_id":1,"goods_rule_id":1}'
            )
            response_data = response.json()

            if response_data.get("errcode") == 201:
                sign_msg = "今日已签到"
                log(f"ℹ️ [{self.server}] {sign_msg}")
                return True, sign_msg

            if self.check_response(response_data):
                data = response_data.get("data", {})
                sign_day = data.get("sign_day", 0)
                sign_points = data.get("sign_points", 0)
                sign_msg = f"签到成功 | 连续{sign_day}天 | +{sign_points}积分"
                log(f"✅ [{self.server}] {sign_msg}")
                return True, sign_msg
            else:
                sign_msg = f"签到失败: {response_data.get('errmsg', '未知错误')}"
                err(f"❌ [{self.server}] {sign_msg}")
                return False, sign_msg

        except Exception as e:
            sign_msg = f"签到异常: {str(e)}"
            err(f"❌ [{self.server}] {sign_msg}")
            return False, sign_msg

    async def get_task_list(self) -> List[Dict[str, Any]]:
        try:
            response = await self.client.post(
                "/openapi/activityservice/api/task/getlist",
                content="{}"
            )
            response_data = response.json()

            if self.check_response(response_data):
                tasks = response_data.get("data", [])
                uncompleted_tasks = [
                    task for task in tasks
                    if task.get("task_status") == 0
                    and task.get("task_guid") not in SKIP_TASK_GUIDS
                ]
                log(f"📋 [{self.server}] 待完成任务: {len(uncompleted_tasks)}个")
                return uncompleted_tasks
            return []
        except Exception as e:
            err(f"❌ [{self.server}] 获取任务列表失败 | 原因: {str(e)}")
            return []

    async def complete_task(self, task_guid: str, task_desc: str) -> Tuple[bool, str]:
        try:
            response = await self.client.post(
                "/openapi/activityservice/api/task/add",
                content=f'{{"task_guid":"{task_guid}"}}'
            )
            response_data = response.json()

            if self.check_response(response_data):
                msg = f"完成【{task_desc}】 | +2积分"
                log(f"✅ [{self.server}] {msg}")
                return True, msg
            else:
                msg = f"【{task_desc}】失败: {response_data.get('errmsg', '未知错误')}"
                err(f"❌ [{self.server}] {msg}")
                return False, msg

        except Exception as e:
            msg = f"【{task_desc}】异常: {str(e)}"
            err(f"❌ [{self.server}] {msg}")
            return False, msg

    async def run(self) -> Dict[str, Any]:
        result = {
            "server": self.server,
            "success": False,
            "proxy_status": "直连" if not self.proxy_info else "专属代理",
            "sign_msg": "",
            "task_msgs": [],
            "initial_score": 0,
            "final_score": 0,
            "gained_score": 0,
            "error": ""
        }

        log(f"\n{'='*40}")
        log(f"[{self.server}] 开始执行任务")
        log(f"{'='*40}")

        try:
            await sleep(random_int(2000, 5000))

            # 1. 获取code（关键日志已优化）
            code = await self.get_code()
            if not code:
                result["error"] = "获取code失败"
                return result

            # 2. 获取token
            token = await self.get_token_by_code(code)
            if not token:
                result["error"] = "获取token失败"
                return result

            # 3. 执行业务
            async with self:
                initial_balance = await self.get_user_balance()
                if initial_balance is None:
                    result["error"] = "获取初始积分失败"
                    return result

                result["initial_score"] = initial_balance
                log(f"💰 [{self.server}] 初始积分: {initial_balance}")

                # 每日签到
                sign_success, sign_msg = await self.daily_sign()
                result["sign_msg"] = sign_msg
                await sleep(1000)

                # 完成日常任务
                tasks = await self.get_task_list()
                task_msgs = []
                for task in tasks:
                    task_guid = task.get("task_guid", "")
                    task_desc = task.get("task_sub_desc", task.get("task_title", "未知任务"))
                    if task_guid:
                        success, msg = await self.complete_task(task_guid, task_desc)
                        task_msgs.append(msg)
                        await sleep(1000)
                result["task_msgs"] = task_msgs

                # 获取最终积分
                final_balance = await self.get_user_balance()
                if final_balance is not None:
                    result["final_score"] = final_balance
                    result["gained_score"] = final_balance - initial_balance
                    log(f"📊 [{self.server}] 今日新增: {result['gained_score']}积分 | 当前: {final_balance}")

                result["success"] = True
                log(f"✅ [{self.server}] 任务执行完成")

        except Exception as e:
            result["error"] = str(e)
            err(f"❌ [{self.server}] 执行异常 | 原因: {str(e)}")

        return result

# ===================== 主程序 =====================
async def main():
    log('===== 雀巢会员俱乐部每日任务 =====\n')
    log(f"📅 执行时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    log(f"🔌 待执行账号: {len(SERVERS)} 个")
    log(f"🌐 代理模式: {'单账号独立代理' if ENABLE_PER_ACCOUNT_PROXY else '全局共用代理'}\n")

    global_proxy_info = None
    if not ENABLE_PER_ACCOUNT_PROXY and PROXY_API:
        global_proxy_info = await get_valid_proxy("全局共用")

    results = []
    skipped = []
    for index, server in enumerate(SERVERS):
        # 今天已完整跑过的账号直接跳过，不再消耗 YYB 取码/取号配额
        if is_done_today(server):
            log(f"⏭️ [打标] 账号 {index + 1}/{len(SERVERS)} 今日任务已完成，跳过")
            skipped.append(server)
            continue

        proxy_info = global_proxy_info
        if ENABLE_PER_ACCOUNT_PROXY:
            proxy_info = await get_valid_proxy(server)
            await sleep(PROXY_FETCH_INTERVAL)

        bot = QueChaoBot(server, proxy_info)
        result = await bot.run()
        results.append(result)
        # 只有整个流程走完（success=True）才算今日完成
        if result.get("success"):
            mark_done_today(server, result.get("sign_msg", ""))

        if index < len(SERVERS) - 1:
            log(f"\n⏳ 等待2秒后执行下一个账号...")
            await sleep(2000)

    # 汇总结果
    notify_content = "### 雀巢每日任务执行结果\n"
    for res in results:
        notify_content += f"\n#### {res['server']}\n"
        notify_content += f"- 代理状态：{res['proxy_status']}\n"
        notify_content += f"- 执行状态：{'成功' if res['success'] else '失败'}\n"
        if res['success']:
            notify_content += f"- 签到结果：{res['sign_msg']}\n"
            notify_content += f"- 任务完成：{'; '.join(res['task_msgs']) if res['task_msgs'] else '无未完成任务'}\n"
            notify_content += f"- 初始积分：{res['initial_score']}\n"
            notify_content += f"- 最终积分：{res['final_score']}\n"
            notify_content += f"- 今日新增：{res['gained_score']} 积分\n"
        else:
            notify_content += f"- 失败原因：{res['error']}\n"

    await send_plusplus_notification("雀巢每日任务完成", notify_content)

    log('\n' + '='*40)
    log('🎉 所有账号执行完成')
    log(f"📊 成功: {sum(1 for r in results if r['success'])}/{len(results)} 个")
    log(f"💰 今日总新增: {sum(r['gained_score'] for r in results if r['success'])} 积分")
    log('='*40)
    log(ACC_RULE)
    log("📊 [执行汇总] 雀巢 · 账号 %d ｜ 成功 %d ｜ 失败 %d ｜ 跳过 %d" % (
        len(SERVERS),
        sum(1 for r in results if r["success"]),
        sum(1 for r in results if not r["success"]),
        len(skipped)))
    log(ACC_RULE)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log("\n⏹️ 用户中断执行")
    except Exception as e:
        err(f"\n❌ 程序异常: {str(e)}")
