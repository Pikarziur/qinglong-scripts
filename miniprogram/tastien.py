#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# name: 塔斯汀会员
# cron: 20 6,16 * * *
"""
青龙环境变量：
  YYB_SERVER   必填。YYB-Go-Enhanced 地址@账号标识[#备注]，多账号换行 / 空格 / & 分隔
               例：yyb-go:8000@1#塔斯汀A
  TST_NOTIFY   可选。1 推送错误（默认），0 关闭推送

内部配置（直接改下面常量，省得去面板设环境变量）：
  YYB_ONLY_REFS      账号序号白名单（1 起），仅运行指定序号；留空 [] 运行全部，例如 [1, 3]
  FORCE_ACTIVITY_ID  签到活动 ID 写死（0 = 自动：缓存 → 区间探测）
  SHOP_ID            门店 ID（抓包里的 gray-shop-id，默认 3487）

任务流程：
  1. 解析 YYB_SERVER 账号基座，按 YYB_ONLY_REFS 序号白名单筛选（1 起）
  2. 调 YYB-Go 的 /wxapp/getCode 取每个账号的 wx.login code
  3. 用 jsCode 调 /api/intelligence/member/login 换 user-token（每次运行重新登录，不落盘）
  4. 解析签到活动 ID（写死 → 缓存 → 区间探测，命中即缓存到 tst_sign_activity.json）
  5. 调 /api/sign/member/signInfoV2 取签到状态；今日未签则调 /api/sign/member/signV2/sign 签到
  6. 汇总今日奖励、连续天数、积分、等级成长值，并打印距下一个奖励还差几天
  7. 尾部输出 [执行汇总]，出现 ERROR 才推送通知

日志规范：[LEVEL] [TASTIEN] message   （LEVEL: INFO / WARN / ERROR）

作者：lcmovie https://github.com/lcmovie
接口来源：塔斯汀+ 小程序（appid wx557473f23153a429，客户端版本 3.88.3）手机抓包还原
依赖：requests（青龙「依赖管理」中安装 Python3 依赖 requests）
通知：直接调用青龙自带的通知模块（容器内 /ql/data/scripts/notify.py，仓库内同目录 / 上一级 notify.py）
      口径：只推错误 —— 本次运行出现 ERROR 日志才推送一次；无错误完全静默。
"""

# ———————————— 内部配置：直接改这里，省去去面板设环境变量 ————————————
# 账号序号白名单（1 起），仅运行指定序号，留空 [] 运行全部；例如 [1, 3]
YYB_ONLY_REFS = []
# 签到活动 ID 写死（0 = 自动）；活动每月换一次 ID，自动模式下会缓存探测结果
FORCE_ACTIVITY_ID = 0
# 门店 ID（抓包请求头里的 gray-shop-id）
SHOP_ID = "3487"

import json
import os
import random
import re
import sys
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from pathlib import Path

import requests

APP_NAME = "塔斯汀"
HOST = "https://sss-web.tastientech.com"
MINI_APPID = "wx557473f23153a429"
MINI_VERSION = "3.88.3"
MINI_VERSION_PATH = "558"
LOGIN_PATH = "/api/intelligence/member/login"
TIMEOUT = 20
UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_1_2 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 "
    "MicroMessenger/8.0.75(0x18004b66) NetType/WIFI Language/zh_CN"
)
# 活动 ID 探测区间：先从「已知 ID 往后 N 个」找（每月 +1 左右），再兜底倒扫一段
SCAN_FORWARD = 30
SCAN_FALLBACK_HIGH = 130
SCAN_FALLBACK_LOW = 60
SCAN_LIMIT = 45

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

_QN_SITE = "塔斯汀"
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
    # 本脚本自带开关：TST_NOTIFY=0/false/no/off 时整体不推送
    if _os.getenv("TST_NOTIFY", "1").strip().lower() in {"0", "false", "no", "off"}:
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
    line = "[%s] [TASTIEN] %s" % (level, msg)
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


def mask_token(token: str) -> str:
    token = str(token)
    if len(token) <= 14:
        return token[:4] + "***"
    return token[:11] + "***" + token[-4:]


def _split_env(raw: str) -> List[str]:
    """按换行 / 空格 / & 切分环境变量（与仓库其它脚本一致）。"""
    return [x for x in raw.replace("&", " ").replace("\r", "\n").split() if x.strip()]


# ────────────────────────────────────────────
# YYB-Go 账号：取码 / 登录
# ────────────────────────────────────────────
def parse_yyb_entries() -> List[Tuple[str, str, str]]:
    """解析 YYB_SERVER → [(server, ref, remark), ...]，格式「地址@ref#备注」。"""
    entries: List[Tuple[str, str, str]] = []
    for line_no, item in enumerate(_split_env(os.getenv("YYB_SERVER", "")), 1):
        if "#" in item:
            entry_part, remark = item.split("#", 1)
            entry_part, remark = entry_part.strip(), remark.strip()
        else:
            entry_part, remark = item.strip(), ""
        if "@" not in entry_part:
            err("❌ YYB_SERVER 第 %d 项缺少 @，已跳过：%s" % (line_no, item))
            continue
        server, ref = entry_part.rsplit("@", 1)
        server, ref = server.strip().rstrip("/"), ref.strip()
        if not server or not ref:
            err("❌ YYB_SERVER 第 %d 项地址或账号标识为空，已跳过：%s" % (line_no, item))
            continue
        if not server.startswith("http"):
            server = "http://" + server
        entries.append((server, ref, remark or ref))
    return entries


def yyb_get_code(server: str, ref: str) -> str:
    """调 YYB-Go 的 /wxapp/getCode 取 wx.login code。"""
    try:
        sess = requests.Session()
        sess.trust_env = False
        response = sess.post(server + "/wxapp/getCode",
                             json={"ref": ref, "app_id": MINI_APPID}, timeout=(10, 90))
        response.raise_for_status()
        body = response.json()
    except Exception as exc:
        raise RuntimeError("YYB-Go 取码失败：%s" % exc)
    code_field = body.get("code")
    if isinstance(code_field, int) and code_field not in (0, None):
        raise RuntimeError("YYB-Go 取码失败：%s" % (body.get("msg") or body))
    data = body.get("data") or {}
    result = body.get("result") or (isinstance(data, dict) and data.get("result")) or {}
    code = ((isinstance(result, dict) and result.get("code"))
            or (isinstance(data, dict) and data.get("code")) or None)
    if not code:
        raise RuntimeError("YYB-Go 未返回有效微信 code：%s" % str(body)[:200])
    return str(code)


def pick_token(payload: Any, raw_text: str) -> Tuple[str, str]:
    """从登录响应里挑出 user-token，返回 (token, 来源说明)。

    登录接口的返回字段未在抓包里出现，这里做三级兜底：
      ① 键名含 token 的字符串值；② 形如 sssxxxx-uuid 的值；③ 整段响应文本正则。
    """
    strings: List[Tuple[str, str]] = []

    def walk(node, path=""):
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, path + "/" + str(k))
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, path + "[%d]" % i)
        elif isinstance(node, str) and node.strip():
            strings.append((path, node.strip()))

    walk(payload)
    for path, val in strings:
        if "token" in path.lower() and not path.lower().endswith("/type") and 16 <= len(val) <= 120:
            return val, "字段 %s" % path
    for path, val in strings:
        if re.match(r"^sss[0-9A-Za-z]{4,}-", val) or re.match(r"^[0-9A-Za-z]{8}-[0-9A-Za-z-]{8,}$", val):
            return val, "格式匹配 %s" % path
    match = re.search(r"sss[0-9A-Za-z]{4,}-[0-9A-Za-z-]{8,}", raw_text or "")
    if match:
        return match.group(0), "整段响应正则"
    return "", ""


def pick_phone(result: Dict[str, Any]) -> str:
    """响应里可能带手机号（登录响应 / 会员详情都能出），能拿到就用，省一次查询。

    来源说明：抓包里的 memberPhone 是**客户端**加密态，算法在小程序包内、抓不到；
    但服务端 getMemberDetail 的 phone 字段就是明文，直接拿来当 memberPhone 即可
    （实测签到接口给明文也认）。本函数按常见键名逐个尝试，取不到返回空串。
    """
    if not isinstance(result, dict):
        return ""
    for key in ("memberPhone", "phoneEncrypted", "phone", "member_phone", "encryptedPhone",
                "phoneNumber", "mobile"):
        value = result.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def wx_login(js_code: str) -> Tuple[str, Dict[str, Any]]:
    """用 wx.login 的 jsCode 换 user-token（每次运行重新登录，不落盘）。"""
    sess = requests.Session()
    sess.trust_env = False
    try:
        response = sess.post(HOST + LOGIN_PATH, json={"jsCode": js_code},
                             headers=base_headers(), timeout=TIMEOUT)
        response.raise_for_status()
    except Exception as exc:
        raise RuntimeError("登录请求失败：%s" % exc)
    raw = response.text or ""
    try:
        payload = response.json()
    except Exception:
        raise RuntimeError("登录响应不是 JSON：%s" % raw[:200])
    if payload.get("code") != 200:
        raise RuntimeError("登录失败（code=%s msg=%s）" % (payload.get("code"), payload.get("msg")))
    result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
    token, source = pick_token(payload, raw)
    if not token:
        raise RuntimeError("登录成功但未识别到 user-token，原始响应：%s" % raw[:400])
    if source and source != "字段 /result/token":
        log("ℹ️ user-token 取自 %s" % source)
    return token, result


def login_account(server: str, ref: str) -> Tuple[str, Dict[str, Any]]:
    """取码 + 登录，返回 (user-token, 登录响应 result)。"""
    code = yyb_get_code(server, ref)
    log("🔐 取到微信 code（%s…），正在登录" % code[:6])
    return wx_login(code)


# ────────────────────────────────────────────
# HTTP 层
# ────────────────────────────────────────────
def base_headers(token: Optional[str] = None) -> Dict[str, str]:
    headers = {
        "Host": "sss-web.tastientech.com",
        "gray-shop-id": SHOP_ID,
        "channel": "1",
        "content-type": "application/json",
        "version": MINI_VERSION,
        "Accept-Encoding": "gzip,compress,br,deflate",
        "User-Agent": UA,
        "Referer": "https://servicewechat.com/%s/%s/page-frame.html" % (MINI_APPID, MINI_VERSION_PATH),
    }
    if token:
        headers["user-token"] = token
    return headers


def make_session(token: str) -> requests.Session:
    sess = requests.Session()
    sess.trust_env = False  # 不走系统代理环境变量，避免青龙里 http_proxy 干扰
    sess.headers.update(base_headers(token))
    return sess


def api_call(sess: requests.Session, path: str, body: Optional[Dict[str, Any]] = None,
             method: str = "POST", retries: int = 1) -> Any:
    """请求封装：code != 200 抛错；命中限流（HTTP 429 / code=500 含「频繁」）时退避重试一次。"""
    payload: Dict[str, Any] = {}
    for attempt in range(retries + 1):
        if method.upper() == "GET":
            response = sess.get(HOST + path, timeout=TIMEOUT)
        else:
            response = sess.post(HOST + path, json=body if body is not None else {}, timeout=TIMEOUT)
        if response.status_code == 429 and attempt < retries:
            warn("⚠️ 接口 %s 被限流（HTTP 429），1.5s 后重试" % path)
            time.sleep(1.5)
            continue
        response.raise_for_status()
        payload = response.json()
        code = payload.get("code")
        if code == 200:
            return payload.get("result")
        msg = str(payload.get("msg") or "")
        if attempt < retries and any(k in msg for k in ("频繁", "限流", "稍后", "请重试")):
            warn("⚠️ 接口 %s 被限流（%s），1.5s 后重试" % (path, msg))
            time.sleep(1.5)
            continue
        raise RuntimeError("接口 %s 返回 code=%s msg=%s" % (path, code, msg))
    raise RuntimeError("接口 %s 返回 code=%s msg=%s" % (path, payload.get("code"), payload.get("msg")))


def auth_error(exc: Exception) -> bool:
    """登录态失效（HTTP 401/403）——继续扫描活动 ID 没有意义，直接失败退出。"""
    response = getattr(exc, "response", None)
    return getattr(response, "status_code", 0) in (401, 403)


_TOKEN_INVALID = "user-token 无效或已过期（HTTP 401），请检查 YYB-Go 账号状态"


# ────────────────────────────────────────────
# 活动 ID：写死 → 缓存 → 区间探测
# ────────────────────────────────────────────
def cache_path() -> Path:
    ql_dir = Path("/ql/data/config")
    try:
        if ql_dir.is_dir():
            return ql_dir / "tst_sign_activity.json"
    except OSError:
        pass
    return Path(__file__).resolve().parent / "tst_sign_activity.json"


def read_cache() -> Dict[str, Any]:
    try:
        data = json.loads(cache_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def write_cache(activity_id: int, info: Dict[str, Any]) -> None:
    try:
        path = cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "activityId": activity_id,
            "name": info.get("name"),
            "startTime": info.get("startTime"),
            "endTime": info.get("endTime"),
            "updated_at": int(time.time()),
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:
        warn("⚠️ 活动缓存写入失败（不影响签到）：%s" % exc)


def activity_valid(info: Dict[str, Any], now_ms: int) -> bool:
    if not info or "签到" not in str(info.get("name") or ""):
        return False
    start = int(info.get("startTime") or 0)
    end = int(info.get("endTime") or 0)
    return bool(start and end and start <= now_ms <= end)


def fmt_date(ts: Any) -> str:
    try:
        return datetime.fromtimestamp(int(ts) / 1000).strftime("%Y-%m-%d")
    except Exception:
        return "?"


def candidate_ids(cached_id: int) -> List[int]:
    ids: List[int] = []

    def add(value: Any) -> None:
        try:
            value = int(value)
        except (TypeError, ValueError):
            return
        if value > 0 and value not in ids:
            ids.append(value)

    add(FORCE_ACTIVITY_ID)
    add(cached_id)
    base = ids[-1] if ids else 77
    for x in range(base, base + SCAN_FORWARD):
        add(x)
    for x in range(SCAN_FALLBACK_HIGH, SCAN_FALLBACK_LOW - 1, -1):
        add(x)
    return ids[:SCAN_LIMIT]


def resolve_activity(sess: requests.Session) -> int:
    """返回当前进行中的签到活动 ID（命中即写缓存）；找不到返回 0。"""
    now_ms = int(time.time() * 1000)
    cached = read_cache()
    cached_id = int(cached.get("activityId") or 0)

    # 缓存仍在有效期内就直接用（省一次探测；真失效了下面还有兜底重探）
    if cached_id and int(cached.get("endTime") or 0) >= now_ms:
        try:
            result = api_call(sess, "/api/sign/member/signInfoV2", {"activityId": cached_id})
            info = (result or {}).get("activityInfo") or {}
            if activity_valid(info, now_ms):
                log("🎯 命中缓存活动：%s（ID %s）" % (info.get("name"), cached_id))
                return cached_id
        except Exception as exc:
            if auth_error(exc):
                raise RuntimeError(_TOKEN_INVALID)
            warn("⚠️ 缓存活动 ID %s 不可用（%s），重新探测" % (cached_id, exc))

    ids = candidate_ids(cached_id)
    log("🔎 开始探测签到活动 ID（共 %d 个候选，命中即停）" % len(ids))
    for aid in ids:
        try:
            result = api_call(sess, "/api/sign/member/signInfoV2", {"activityId": aid})
        except Exception as exc:
            if auth_error(exc):
                raise RuntimeError(_TOKEN_INVALID)
            continue
        info = (result or {}).get("activityInfo") or {}
        if activity_valid(info, now_ms):
            log("✅ 命中活动：%s（ID %s，%s ~ %s）"
                % (info.get("name"), aid, fmt_date(info.get("startTime")), fmt_date(info.get("endTime"))))
            write_cache(aid, info)
            return aid
        time.sleep(0.2)
    return 0


# ────────────────────────────────────────────
# 业务接口
# ────────────────────────────────────────────
def get_sign_info(sess: requests.Session, activity_id: int) -> Dict[str, Any]:
    return api_call(sess, "/api/sign/member/signInfoV2", {"activityId": activity_id}) or {}


def do_sign(sess: requests.Session, activity_id: int, phone: str) -> Dict[str, Any]:
    return api_call(sess, "/api/sign/member/signV2/sign", {
        "activityId": activity_id,
        "memberName": "",
        "memberPhone": phone or "",
    }) or {}


def get_member_phone(sess: requests.Session) -> str:
    """从会员详情里取手机号（签到接口的 memberPhone）。"""
    result = api_call(sess, "/api/intelligence/member/getMemberDetail", method="GET") or {}
    return pick_phone(result)


def sign_already(msg: str) -> bool:
    """服务端其实是「今日已签到」而不是真失败（并发 / 判定漂移时会出现）。"""
    return any(k in msg for k in ("已经签到", "已签到", "已签", "明天再来", "重复签"))


def get_point(sess: requests.Session) -> Optional[int]:
    result = api_call(sess, "/api/wx/point/myPoint", {}) or {}
    point = result.get("point")
    return int(point) if isinstance(point, (int, float)) else None


def get_level(sess: requests.Session) -> str:
    result = api_call(sess, "/api/minic/shop/c/memberLevel/getLevelInfo", {}) or {}
    name = result.get("levelName") or result.get("nextLevelName")
    if not name:
        return ""
    growth = result.get("growthNum")
    need = result.get("nextGrowthNum")
    label = str(name)
    if isinstance(growth, (int, float)) and isinstance(need, (int, float)) and need:
        label += "（成长值 %d/%d）" % (int(growth), int(need))
    return label


# ────────────────────────────────────────────
# 奖励文案
# ────────────────────────────────────────────
def reward_text(reward: Dict[str, Any]) -> str:
    """把 rewardInfoList 里的一条转成一行文案。"""
    name = str(reward.get("rewardName") or "奖励")
    coupons = reward.get("couponInfo") or []
    point = reward.get("point") or 0
    if coupons:
        num = sum(int(c.get("num") or 1) for c in coupons if isinstance(c, dict))
        content = next((c.get("couponContent") for c in coupons
                        if isinstance(c, dict) and c.get("couponContent")), "")
        return "%s ×%d%s" % (name, num, ("（%s）" % content) if content else "")
    if point:
        return "%s（+%d）" % (name, int(point))
    return name


def rewards_text(result: Dict[str, Any]) -> str:
    items = result.get("rewardInfoList") or []
    if not items and isinstance(result.get("rewardInfo"), dict):
        items = [result["rewardInfo"]]
    texts = [reward_text(x) for x in items if isinstance(x, dict)]
    return "、".join(texts) if texts else ""


def today_reward_from_calendar(info: Dict[str, Any], member: Dict[str, Any]) -> str:
    """已签到时，从活动奖励表反查今天的奖励名。"""
    today = datetime.now().strftime("%Y%m%d")
    for item in info.get("rewardList") or []:
        if isinstance(item, dict) and str(item.get("dateStr") or "") == today:
            return reward_text(item)
    day_no = member.get("barNum") or member.get("continuousNum")
    for item in info.get("rewardList") or []:
        if isinstance(item, dict) and item.get("dayNum") == day_no:
            return reward_text(item)
    return ""


# ────────────────────────────────────────────
# 单账号
# ────────────────────────────────────────────
def run_account(index: int, token: str, login_result: Dict[str, Any], activity_id: int) -> Dict[str, Any]:
    sess = make_session(token)
    info = get_sign_info(sess, activity_id)
    activity = info.get("activityInfo") or {}
    member = info.get("signMemberInfo") or {}

    continuous = int(member.get("continuousNum") or 0)
    total_period = int(activity.get("totalPeriod") or 7)

    # 签到接口要带手机号：优先用登录响应里的，取不到再查会员详情
    phone = pick_phone(login_result)
    if not phone:
        try:
            phone = get_member_phone(sess)
        except Exception as exc:
            warn("⚠️ 手机号查询失败（不影响已签到判定）：%s" % exc)

    status = ""
    reward = ""
    if member.get("todaySign"):
        status = "今日已签到"
    else:
        last_exc: Optional[Exception] = None
        for attempt_phone in ([phone, ""] if phone else [""]):
            try:
                result = do_sign(sess, activity_id, attempt_phone)
            except Exception as exc:
                last_exc = exc
                if sign_already(str(exc)):
                    status = "今日已签到"
                    break
                if attempt_phone:
                    warn("⚠️ 带手机号签到失败（%s），改用空 memberPhone 重试一次" % exc)
                    time.sleep(1.5)
                    continue
                raise
            status = "签到成功"
            continuous = int(result.get("continuousNum") or continuous)
            reward = rewards_text(result) or "接口未返回奖励明细"
            break
        if not status:
            raise last_exc if last_exc else RuntimeError("签到失败")

    if status == "今日已签到":
        reward = reward or today_reward_from_calendar(activity, member) or "已签到（接口不回溯当日奖励详情）"
        log("🟡 %s ｜ 连续签到 %d 天" % (status, continuous))
    else:
        log("✅ %s ｜ 连续签到 %d 天 ｜ 今日奖励：%s" % (status, continuous, reward))

    # 积分 / 等级：任一失败都不影响签到结论；两次查询之间错开一下，避开限流
    point = None
    level = ""
    try:
        point = get_point(sess)
    except Exception as exc:
        warn("⚠️ 积分查询失败：%s" % exc)
    time.sleep(0.4)
    try:
        level = get_level(sess)
    except Exception as exc:
        warn("⚠️ 等级查询失败：%s" % exc)

    parts = [("💰 积分：%d" % point) if point is not None else "💰 积分：?"]
    if level:
        parts.append("🏅 等级：%s" % level)
    log(" ｜ ".join(parts))

    need_days = member.get("rewardNeedDays")
    next_reward = member.get("rewardName")
    if isinstance(need_days, (int, float)) and need_days and next_reward:
        log("🎁 再连续签到 %d 天可得：%s（本轮进度 %d/%d）"
            % (int(need_days), next_reward, int(member.get("barNum") or continuous), total_period))

    return {
        "index": index,
        "success": True,
        "status": status,
        "continuous": continuous,
        "reward": reward,
        "point": point,
    }


# ────────────────────────────────────────────
# 主流程
# ────────────────────────────────────────────
def main() -> int:
    entries = parse_yyb_entries()
    if not entries:
        err("❌ 未配置有效的 YYB_SERVER（格式：yyb-go:8000@账号标识，多账号换行 / 空格 / & 分隔）")
        return 1

    # 按 YYB_ONLY_REFS 序号白名单筛选（1 起）；留空 [] 则运行全部账号
    if YYB_ONLY_REFS:
        wanted = set(int(x) for x in YYB_ONLY_REFS if str(x).strip().isdigit() and int(x) > 0)
        if wanted:
            entries = [e for i, e in enumerate(entries, 1) if i in wanted]
            log("ℹ️ 按 YYB_ONLY_REFS 筛选：请求序号 %s，命中 %d 个账号" % (sorted(wanted), len(entries)))
            if not entries:
                err("❌ YYB_ONLY_REFS 指定的序号均超出账号范围")
                return 1

    total = len(entries)
    log("🚀 塔斯汀会员签到开始 · 共 %d 个账号" % total)

    activity_id = 0  # 全局同一活动，第一个账号登录成功后探测一次
    results: List[Dict[str, Any]] = []
    for index, (server, ref, remark) in enumerate(entries, 1):
        acc_banner(index, total, "标识：" + remark)
        try:
            token, login_result = login_account(server, ref)
            log("🔑 登录成功 · user-token %s" % mask_token(token))
            if not activity_id:
                activity_id = resolve_activity(make_session(token))
                if not activity_id:
                    raise RuntimeError("未找到进行中的签到活动（可写死 FORCE_ACTIVITY_ID 试试）")
            results.append(run_account(index, token, login_result, activity_id))
        except Exception as exc:
            message = _TOKEN_INVALID if auth_error(exc) else str(exc)
            err("❌ 账号 %d 失败：%s" % (index, message))
            results.append({"index": index, "success": False, "error": message})
        acc_footer(index, total)
        if index < total:
            time.sleep(random.randint(2, 5))  # 随机间隔，降低被风控的概率

    ok = sum(1 for r in results if r.get("success"))
    already = sum(1 for r in results if r.get("status") == "今日已签到")
    tail = "📊 [执行汇总] %s · 账号 %d ｜ 成功 %d ｜ 失败 %d" % (APP_NAME, total, ok, total - ok)
    if already:
        tail += " ｜ 🟡 已签 %d" % already
    log(_ACC_RULE)
    log(tail)
    log(_ACC_RULE)
    # 错误日志推送：有错误才发；未配置通知渠道则只提示一行
    flush_notify(
        APP_NAME,
        "账号 %d ｜ 成功 %d ｜ 失败 %d" % (total, ok, total - ok),
        logger=log,
    )
    return 0 if ok == total else 1


if __name__ == "__main__":
    sys.exit(main())
