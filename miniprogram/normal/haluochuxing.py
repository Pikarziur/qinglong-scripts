#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# =========================================================
# name:  哈啰出行
# version: 2026-11-20a
# cron: 48 4,14 * * *
# 积分有效期：获得起3个月内
# 检测过期积分：每3个月月底前七天 检查并连续提醒；过期积分=现有积分
# =========================================================

"""
哈啰 | 单车助力车打车顺风车租车 微信小程序每日签到与奖励金查询，基于 YYB-Go-Enhanced 自动取码登录。

环境变量：
  YYB_SERVER     必填，格式：YYB-Go-Enhanced 地址@微信账号标识；多账号每行一条
  YYB_API_KEY    可选，YYB 协议令牌
  HL_DRY_RUN     可选，=1 时仅查询，不执行签到
  HL_ENABLE_SIGN 可选，默认 1；=0 时仅查询奖励金

账号白名单：脚本顶部硬编码常量 YYB_ONLY_REFS（1 起序号，按 YYB_SERVER 行序）；留空 [] 跑全部账号

功能包含：静默登录、每日签到、签到前后奖励金对比、钱包余额查询，
执行汇总与错误通知，以及每月月底前 7 天的积分过期提醒（expiring>0 时连续提醒并强推）。

待定任务：视频浏览任务（尚未实现）。此类任务通常需要真实观看行为，脚本无法可靠模拟，
不确保是否会被服务端判定异常或造成黑号；在确认安全性之前暂不接入。

日志规范：[LEVEL] [HLCX] message   （LEVEL: INFO / WARN / ERROR）
错误通知：本次运行出现 ERROR 日志才推送一次（接入青龙 notify.py）；正常跑完不打扰。
"""
# 作者：lcmovie https://github.com/lcmovie

from __future__ import annotations

import calendar
import json
import os
import random
import re
import sys
import time
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    import requests
except ImportError:
    print("❌ 缺少依赖：pip install requests")
    sys.exit(1)

# ────────────────────────────────────────────
# 统一日志
# ────────────────────────────────────────────
# ———————————— 错误通知（可选项，想用则用）————————————
# 只推错误：本次运行出现 ERROR 日志才推送一次；正常跑完不打扰。
# 直接调用青龙自带的通知模块（容器内为 /ql/data/scripts/notify.py，仓库内为同目录/上一级的 notify.py）——
#   在青龙面板「通知设置」里配一次即可全站通用（该文件由青龙官方维护，支持其全部推送渠道）。
# 找不到该文件、或未配置任何通知渠道时，只在日志末尾提示一行，不报错、不中断。
_QN_SITE = "HLCX"
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
    _qn_here = os.path.dirname(os.path.abspath(__file__))
    _qn_dirs += [_qn_here, os.path.dirname(_qn_here)]
except NameError:
    pass
try:
    for _p in _qn_dirs:
        if _p and os.path.isfile(os.path.join(_p, "notify.py")):
            if _p not in sys.path:
                sys.path.insert(0, _p)
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
    _qn_flushed = True
    if _qn_send is None:
        print(_QN_NOMOD, flush=True)
        return False
    if not any(os.getenv(_k) for _k in _QN_ENVS):
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
    if not any(os.getenv(_k) for _k in _QN_ENVS):
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


_qn_sys_excepthook = sys.excepthook
sys.excepthook = _qn_excepthook


def _emit(level, msg):
    # 一律左对齐：剥离消息前导的半角/全角空格，杜绝用缩进做层级排版
    msg = str(msg).lstrip(" \u3000\t")
    line = "[%s] %s" % (level, msg)
    print(line, flush=True)
    if level == "ERROR":
        collect_error(line)


def log(msg):  _emit("INFO", msg)
def warn(msg): _emit("WARN", msg)
def err(msg):  _emit("ERROR", msg)


# ────────────────────────────────────────────
# 账号分隔标识 + 账号白名单
# ────────────────────────────────────────────
# ========== 启动信息 ==========
def log_startup(total: int = 0) -> None:
    log(_ACC_RULE)
    _title = "📃 脚本名称：哈啰出行"
    if APP_NO:
        _title += " ｜" + APP_NO
    log(_title)
    log("📌 脚本版本: %s" % SCRIPT_VER)
    if total:
        log("🔢 账号数量: %d" % total)
    log(_ACC_RULE)
SCRIPT_VER = "2026-11-20a"
_ACC_RULE = "=" * 70
SUMMARY_SEP = "▶︎ " + "━" * 50 + " ◀︎"

# 积分每日快照：按账号存每天最后一次的总积分，用于次日对比涨跌。
POINTS_HISTORY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "haluochuxing_points_history.json")


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


def record_points_snapshot(account: str, total_points: Any) -> None:
    """记录该账号今天的总积分（覆盖同日旧值＝保留当天最后一次运行数据）。"""
    if total_points is None:
        return
    try:
        val = float(total_points)
    except (TypeError, ValueError):
        return
    hist = load_points_history()
    days = hist.get(account)
    if not isinstance(days, dict):
        days = {}
    days[_today()] = val
    hist[account] = days
    save_points_history(hist)


def points_delta(account: str, today_val: float) -> Optional[float]:
    """严格取昨天最后一次记录的总积分，返回 今天-昨天；昨天无记录或异常都返回 None。"""
    try:
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        hist = load_points_history()
        days = hist.get(account)
        if not isinstance(days, dict) or yesterday not in days:
            return None
        return today_val - float(days.get(yesterday))
    except Exception:
        return None


# 积分过期提醒：按脚本头部注释规则（每3个月月底前七天 检查并连续提醒；
# 过期积分=现有积分）。命中窗口时，任一账号现有积分>0 则在汇总日志连续打印并强推。
# 窗口计算：last_day - 7 < today.day <= last_day（月底前 7 天，含最后一天当天）。
ENABLE_EXPIRE_REMIND = True      # 总开关，False 关闭过期提醒
EXPIRE_REMIND_DAYS = 7           # 提前天数（窗口 = 月底前 7 天）
EXPIRE_REMIND_MONTHS = (3, 6, 9, 12)  # 启用提醒的月份（每 3 个月检测一次）

# 账号白名单：按 YYB_SERVER 的行序（1 起）筛选本次要跑的账号。
# 留空 [] 表示运行全部账号；例：[1, 3] 只跑第 1、3 个。
YYB_ONLY_REFS = []

APP_NAME = "哈啰出行签到"
HERE = Path(__file__).resolve().parent
# 日打标文件：今日已完成的账号当天再次运行时直接跳过（不消耗 YYB 取码配额）
DAILY_DONE_FILE = str(HERE / "haluocx_daily_done.json")

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #
APP_NO = "wxb937e3d0b3ca117e"                 # 哈啰出行小程序 AppID
MP_VERSION = "358"                            # 小程序版本号（仅用于 Referer）
API_BASE = "https://api.hellobike.com/api"    # 小程序接口域
MKT_BASE = "https://marketingapi.hellobike.com/api"

ACT_LOGIN = "user.account.weixinEasyLogin"
ACT_SIGN = "common.welfare.signAndRecommend"
ACT_SIGN_INFO = "common.welfare.signInfo"
ACT_POINT = "user.taurus.pointInfo"
ACT_WALLET = "user.wallet.account"
ACT_USER = "user.account.getInfo"

SYS_CODE = 64                                 # 微信小程序渠道
H5_SYSTEM_CODE = 62                           # 奖励金/福利中心 H5 渠道
H5_VERSION = "6.46.0"                         # H5 版本号（服务端只做弱校验）
# --------------------------------------------------------------------------- #
# 开关
# --------------------------------------------------------------------------- #
DRY_RUN = os.getenv("HL_DRY_RUN", "").strip() == "1"
ENABLE_SIGN = os.getenv("HL_ENABLE_SIGN", "1").strip() != "0"
DEBUG = os.getenv("HL_DEBUG", "").strip() == "1"
RANDOM_HEADERS = os.getenv("HL_RANDOM_HEADERS", "1").strip() != "0"
UA_MODE = (os.getenv("HL_UA_MODE", "run").strip() or "run").lower()
LOGIN_RETRY = max(1, int(os.getenv("HL_LOGIN_RETRY", "3") or 3))
# YYB-GO 请求超时（与业务接口分开）：YYB-GO 内部会轮询多个上游长连接节点
# （实测候选 6 个），失败路径约 40 秒才返回。超时太紧会把真实错误码
# （如 manual_auth -10）吞成 "Read timed out"，看不到根因。
# 取码/取号给 90 秒读超时，业务接口仍用各自的短超时。
YYB_TIMEOUT = float(os.getenv("YYB_REQUEST_TIMEOUT", "90") or 90)
HTTP_TIMEOUT = float(os.getenv("HL_HTTP_TIMEOUT", "25") or 25)

# --------------------------------------------------------------------------- #
# 随机请求头
# --------------------------------------------------------------------------- #
UA_POOL = [
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5_1 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Mobile/15E148 MicroMessenger/8.0.53(0x18003528) NetType/WIFI Language/zh_CN",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Mobile/15E148 MicroMessenger/8.0.55(0x18003728) NetType/WIFI Language/zh_CN",
    "Mozilla/5.0 (Linux; Android 14; V2309A Build/UP1A.231005.007; wv) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Version/4.0 Chrome/122.0.6261.120 Mobile Safari/537.36 XWEB/1220093 "
    "MMWEBSDK/20240301 MicroMessenger/8.0.50.2701(0x2800323D) WeChat/arm64 Weixin NetType/WIFI "
    "Language/zh_CN ABI/arm64",
    "Mozilla/5.0 (Linux; Android 13; PGT110 Build/TKQ1.220829.002; wv) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Version/4.0 Chrome/116.0.0.0 Mobile Safari/537.36 XWEB/1160065 "
    "MMWEBSDK/20231202 MicroMessenger/8.0.49.2600(0x28003133) WeChat/arm64 Weixin NetType/WIFI "
    "Language/zh_CN ABI/arm64",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36 MicroMessenger/7.0.20.1781(0x6700143B) "
    "WindowsWechat(0x63090b19) XWEB/11581",
]
_RUN_UA = ""


def pick_user_agent() -> str:
    global _RUN_UA
    if UA_MODE == "request" or not _RUN_UA:
        _RUN_UA = random.choice(UA_POOL)
    return _RUN_UA


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def now_text() -> str:
    # 兼容两种导入: from datetime import datetime / import datetime
    _dt = datetime.now() if hasattr(datetime, "now") else datetime.datetime.now()
    return _dt.strftime("%Y-%m-%d %H:%M:%S")



def mask_phone(value: Any) -> str:
    text = str(value or "").strip()
    return f"{text[:3]}****{text[-4:]}" if len(text) == 11 else (text or "-")


def preview(data: Any, limit: int = 300) -> str:
    try:
        text = json.dumps(data, ensure_ascii=False)
    except Exception:
        text = str(data)
    return text[:limit]


def b32(value: Any) -> str:
    """清洗文本：去掉引号/换行等，避免日志与通知里出现脏名字。"""
    text = str(value or "").strip()
    text = text.strip("\"'` \t\r\n\u200b")
    return text


def day_index(bonus_list: Any) -> Optional[int]:
    """从 bonusList 里找出「今天」是第几天（hasDaySign=True 的那一格）。"""
    if not isinstance(bonus_list, list):
        return None
    for idx, item in enumerate(bonus_list):
        if isinstance(item, dict) and item.get("hasDaySign"):
            return idx + 1
    return None


def as_int(value: Any) -> Optional[int]:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def as_num(value: Any) -> Optional[float]:
    """宽容取数（接口里奖励金偶尔是 "20.0" 这种字符串）。"""
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def fmt_num(value: Any) -> str:
    """整数不带小数点，非整数保留原样。"""
    num = as_num(value)
    if num is None:
        return "-"
    return str(int(num)) if float(num).is_integer() else ("%g" % num)


def deep_first(node: Any, key: str) -> Any:
    if isinstance(node, dict):
        if node.get(key) not in (None, ""):
            return node[key]
        for value in node.values():
            found = deep_first(value, key)
            if found not in (None, ""):
                return found
    elif isinstance(node, list):
        for item in node:
            found = deep_first(item, key)
            if found not in (None, ""):
                return found
    return None


# --------------------------------------------------------------------------- #
# 日打标（今日完成标记）
# --------------------------------------------------------------------------- #
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


def mark_done_today(server: str, note: str = "", expiring: Optional[int] = None,
                    total_points: Any = None) -> None:
    """给该账号打上「今日已完成」标记（同时缓存当天的 expiring 与总积分，供跳过时读回）。"""
    cache = load_daily_done()
    today = _today()
    # 只保留当天的记录，跨天自动清理，避免文件无限增长
    cache = {k: v for k, v in cache.items()
             if isinstance(v, dict) and str(v.get("date") or "") == today}
    cache[server] = {
        "date": today,
        "doneTime": now_text(),
        "note": str(note or ""),
        "expiring": expiring,
        "totalPoints": total_points,
    }
    save_daily_done(cache)
    log(f"✅ [打标] 今日任务已完成，已记录（{today}）")


def expiring_from_done(server: str) -> Optional[int]:
    """从当天打标记录里读回 expiring（供本次被跳过、未走 run_account 的账号使用）。"""
    entry = load_daily_done().get(server)
    if not isinstance(entry, dict) or str(entry.get("date") or "") != _today():
        return None
    if "expiring" not in entry:
        return None
    return as_int(entry.get("expiring"))


def total_points_from_done(server: str) -> Optional[int]:
    """从当天打标记录里读回总积分（供被跳过的账号在汇总里显示）。"""
    entry = load_daily_done().get(server)
    if not isinstance(entry, dict) or str(entry.get("date") or "") != _today():
        return None
    val = entry.get("totalPoints")
    if val is None:
        return None
    return as_int(val)


def clear_daily_done(server: str = "") -> None:
    """清除指定账号（或全部）的日打标。"""
    cache = load_daily_done()
    if server:
        cache.pop(server, None)
    else:
        cache = {}
    save_daily_done(cache)


# --------------------------------------------------------------------------- #
# 账号配置
# --------------------------------------------------------------------------- #
class AccountTarget:
    def __init__(self, endpoint: str = "", ref: str = "", index: int = 0):
        self.endpoint = normalize_endpoint(endpoint) if endpoint else ""
        self.ref = str(ref or "")
        self.index = index
        self.label_suffix = ""

    @property
    def yyb_base(self) -> str:
        return self.endpoint

    @property
    def label(self) -> str:
        number = self.ref if self.ref else str(self.index or "?")
        suffix = f"（{self.label_suffix}）" if self.label_suffix else ""
        return f"账号 {number}{suffix}"

    @property
    def server(self) -> str:
        return f"{self.endpoint}@{self.ref}" if self.endpoint and self.ref else "未配置 YYB_SERVER"


def normalize_endpoint(value: str) -> str:
    text = (value or "").strip().rstrip("/")
    if not text:
        return ""
    if not re.match(r"^https?://", text, re.I):
        text = "http://" + text
    return text


def load_accounts() -> List[AccountTarget]:
    raw = os.getenv("YYB_SERVER", "").strip()
    accounts: List[AccountTarget] = []
    if raw:
        for line in raw.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "@" not in line:
                warn(f"⚠️ [配置] 忽略无账号标识的行：{line}")
                continue
            endpoint, ref = (part.strip() for part in line.rsplit("@", 1))
            if not endpoint or not ref:
                warn(f"⚠️ [配置] 忽略不完整行：{line}")
                continue
            accounts.append(AccountTarget(endpoint=endpoint, ref=ref, index=len(accounts) + 1))
    if not accounts:
        raise RuntimeError(
            "未读取到有效账号。请配置环境变量 YYB_SERVER，每行一个账号，格式：\n"
            "  YYB地址@账号ID      例：yyb-go:8000@1\n"
            "  多账号就是多行（第 2 个账号写 @2，以此类推）"
        )
    return accounts


# --------------------------------------------------------------------------- #
# YYB 调用
# --------------------------------------------------------------------------- #
def yyb_post(account: AccountTarget, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    url = account.yyb_base + path
    headers = {"Content-Type": "application/json", "Accept": "application/json",
               "User-Agent": "okhttp/3.12.13"}
    api_key = os.getenv("YYB_API_KEY", "").strip() or os.getenv("YYB_PROTOCOL_TOKEN", "").strip()
    if api_key:
        headers["Authorization"] = api_key if " " in api_key else "Bearer " + api_key
    resp = requests.post(url, json=payload, headers=headers, timeout=YYB_TIMEOUT)
    resp.raise_for_status()
    body = resp.json()
    if DEBUG:
        log(f"· [YYB] {path} → {preview(body, 400)}")
    if isinstance(body, dict) and body.get("code") not in (0, None):
        raise RuntimeError(f"YYB {path} 返回错误：{body.get('msg') or preview(body)}")
    return body


def yyb_get_wx_code(account: AccountTarget) -> Dict[str, str]:
    """取 wx.login code（一次性，拿到后需尽快使用）+ 微信 openid。"""
    body = yyb_post(account, "/wxapp/getCode", {"ref": account.ref, "app_id": APP_NO})
    data = body.get("data") or {}
    result = data.get("result") if isinstance(data.get("result"), dict) else {}
    code = ""
    for candidate in (result.get("code"), data.get("code"), deep_first(body, "code")):
        if isinstance(candidate, str) and len(candidate) >= 16:
            code = candidate
            break
    if not code:
        raise RuntimeError(f"YYB 未返回有效 code：{preview(body, 200)}")
    nickname = b32((data.get("account") or {}).get("nickname")) or b32(
        (data.get("account") or {}).get("alias"))
    return {"code": code, "openid": b32(data.get("openid")), "nickname": nickname}


# --------------------------------------------------------------------------- #
# 哈啰客户端
# --------------------------------------------------------------------------- #
class NotBoundError(RuntimeError):
    """微信账号尚未绑定哈啰账号 —— 重试无意义。"""


class HlClient:
    """哈啰接口客户端：静默登录 + 福利中心/奖励金业务请求。"""

    def __init__(self, account: AccountTarget):
        self.account = account
        self.session = requests.Session()
        self.token = ""
        self.user: Dict[str, Any] = {}
        self.openid = ""

    # ---------------- 底层请求 ----------------
    def headers(self) -> Dict[str, str]:
        head = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
            "User-Agent": pick_user_agent(),
            "Origin": "https://servicewechat.com",
            "Referer": f"https://servicewechat.com/{APP_NO}/{MP_VERSION}/page-frame.html",
        }
        if RANDOM_HEADERS:
            head["Accept-Language"] = random.choice(
                ["zh-CN,zh;q=0.9", "zh-CN,zh-Hans;q=0.9", "zh-Hans-CN;q=1, zh-Hans;q=0.9"])
        return head

    def post(self, action: str, payload: Optional[Dict[str, Any]] = None,
             host: str = API_BASE) -> Dict[str, Any]:
        body: Dict[str, Any] = {"action": action}
        if payload:
            body.update(payload)
        url = f"{host}?{action}"
        resp = self.session.post(url, data=json.dumps(body, ensure_ascii=False),
                                 headers=self.headers(), timeout=HTTP_TIMEOUT)
        try:
            data = resp.json()
        except Exception:
            raise RuntimeError(f"{action} 返回非 JSON（HTTP {resp.status_code}）：{resp.text[:200]}")
        if DEBUG:
            log(f"· [{action}] {preview(data, 500)}")
        return data

    # ---------------- 登录 ----------------
    def _silent_login_once(self) -> Dict[str, Any]:
        info = yyb_get_wx_code(self.account)
        self.openid = info["openid"]
        if info.get("nickname"):
            self.account.label_suffix = info["nickname"][:12]
        payload = {
            "iv": None,
            "encryptedData": None,
            "wechatLoginCode": info["code"],
            "pageName": "pages/index/index",
            "city": "",
            "adCode": "",
            "cityCode": "",
            "channel": 0,
            "flagType": "WECHAT_SEAMLESS",
            "systemCode": SYS_CODE,
            "extendValue": json.dumps({"openId": info["openid"]}, separators=(",", ":")),
            "ssid": "",
        }
        res = self.post(ACT_LOGIN, payload)
        code = res.get("code")
        if code == 0:
            data = res.get("data") or {}
            token = b32(data.get("token"))
            if not token:
                raise RuntimeError(f"登录成功但未返回 token：{preview(res, 200)}")
            self.token = token
            self.user = data
            return data
        if code == 71000:
            raise NotBoundError("该微信尚未注册/绑定哈啰账号（登录失败！）")
        raise RuntimeError(f"登录失败：{res.get('code')} {res.get('msg') or preview(res, 160)}")

    def login(self) -> Dict[str, Any]:
        last: Optional[Exception] = None
        for attempt in range(1, LOGIN_RETRY + 1):
            try:
                return self._silent_login_once()
            except NotBoundError:
                raise
            except Exception as exc:
                last = exc
                warn(f"⚠️ 第 {attempt}/{LOGIN_RETRY} 次登录失败：{preview(exc, 160)}")
                if attempt < LOGIN_RETRY:
                    time.sleep(random.uniform(1.5, 3.0))
        raise last if last else RuntimeError("登录失败")

    # ---------------- 业务 ----------------
    def _h5_payload(self) -> Dict[str, Any]:
        return {"from": "h5", "platform": 4, "version": H5_VERSION, "token": self.token}

    def point_info(self) -> Dict[str, Any]:
        body = self._h5_payload()
        body.update({"action": ACT_POINT, "systemCode": 61, "pointType": 1})
        return self.post(ACT_POINT, body)

    def sign_info(self) -> Dict[str, Any]:
        body = self._h5_payload()
        body.update({"action": ACT_SIGN_INFO, "systemCode": H5_SYSTEM_CODE, "pointType": 1})
        return self.post(ACT_SIGN_INFO, body)

    def do_sign(self) -> Dict[str, Any]:
        body = self._h5_payload()
        body.update({"action": ACT_SIGN, "systemCode": H5_SYSTEM_CODE, "pointType": 1})
        return self.post(ACT_SIGN, body)

    def wallet(self) -> Dict[str, Any]:
        return self.post(ACT_WALLET, {"token": self.token})

    def user_info(self) -> Dict[str, Any]:
        return self.post(ACT_USER, {"token": self.token})



# --------------------------------------------------------------------------- #
# 单账号执行
# --------------------------------------------------------------------------- #
def points_of(node: Dict[str, Any]) -> Optional[int]:
    data = node.get("data") if isinstance(node, dict) else None
    if not isinstance(data, dict):
        return None
    if "points" in data:
        return as_int(data.get("points"))
    if "accountBalance" in data:
        return None
    return None


def expiring_of(node: Dict[str, Any]) -> Optional[int]:
    """从 pointInfo 返回里取「即将过期」奖励金数（expiring）。取不到返回 None。"""
    data = node.get("data") if isinstance(node, dict) else None
    if not isinstance(data, dict) or "expiring" not in data:
        return None
    return as_int(data.get("expiring"))


def amount_of(node: Dict[str, Any]) -> str:
    data = node.get("data") if isinstance(node, dict) else None
    if not isinstance(data, dict):
        return ""
    if data.get("amount") not in (None, ""):
        return str(data.get("amount"))
    return ""


# 「看视频」类任务的市场计划 / 任务类型（都走微信激励视频，脚本无法代做）
VIDEO_MARKET_PLANS = {"incentive_video_scheme"}
VIDEO_TASK_TYPES = {"meal_subsidy", "sleeping_subsidy"}


def run_account(index: int, total: int, account: AccountTarget) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "label": account.label, "server": account.server, "success": False, "skipped": False,
        "user": "-", "sign": "-", "bonus": "-", "title": "", "extra": "",
        "points_before": None, "points_after": None, "points_delta": None,
        "totalPoints": None,
        "amount_before": "", "amount_after": "", "expiring": None,
        "wallet": "", "continuous": "-",
        "sign_source": "", "error": "",
    }
    log(_ACC_RULE)
    log("▶️ [%d/%d] %s ← %s" % (index, total, account.label, account.server))
    log(_ACC_RULE)

    client = HlClient(account)
    try:
        user = client.login()
        result["label"] = account.label
        mobile = b32(user.get("mobile"))
        result["user"] = f"{mask_phone(mobile)}"
        log(f"✅ 登录成功：{account.label}　手机号 {mask_phone(mobile)}　"
              f"userNewId={b32(user.get('userNewId'))}")
    except NotBoundError as exc:
        result["label"] = account.label
        result["skipped"] = True
        result["sign"] = f"该微信未绑定哈啰账号，已跳过（{exc}）"
        log(f"⏭️ {account.label}：{exc}")
        return result
    except Exception as exc:
        result["error"] = f"登录异常：{exc}"
        err(f"❌ {account.label} 登录异常：{exc}")
        return result

    # ---- 初始奖励金 ----
    try:
        before = client.point_info()
        result["points_before"] = points_of(before)
        result["amount_before"] = amount_of(before)
        result["expiring"] = expiring_of(before)
        log(f"💰 签到前奖励金：{result['points_before']}　"
              f"≈{result['amount_before'] or '-'} 元")
        if result["expiring"] is not None:
            log(f"⏳ 即将过期奖励金：{result['expiring']}")
    except Exception as exc:
        warn(f"⚠️ 查询奖励金失败：{preview(exc, 160)}")

    # ---- 签到状态 ----
    sign_state: Dict[str, Any] = {}
    try:
        info = client.sign_info()
        sign_state = info.get("data") or {}
        result["title"] = b32(sign_state.get("title"))
        result["extra"] = b32(sign_state.get("extraRewardBtnText"))
        day = day_index(sign_state.get("bonusList"))
        result["continuous"] = f"第 {day}/7 天" if day else "-"
        log(f"📋 签到状态：{'今日已签到' if sign_state.get('didSignToday') else '今日未签到'}"
              f"　今日奖励金 {sign_state.get('bountyCountToday') or '-'}"
              f"　{result['continuous']}　{result['title']}")
    except Exception as exc:
        warn(f"⚠️ 查询签到状态失败：{preview(exc, 160)}")

    # ---- 签到 ----
    if DRY_RUN:
        result["sign"] = "DRY_RUN：未执行签到"
        result["success"] = True
    elif not ENABLE_SIGN:
        result["sign"] = "已关闭签到（HL_ENABLE_SIGN=0）"
        result["success"] = True
    elif sign_state.get("didSignToday"):
        # 2026-11-17a：签到前状态接口明确今日已签 → 跳过 do_sign 写请求，避免重复提交。
        # 查询失败时 sign_state 为空、didSignToday 为假，不会走到这里，仍走下面的原签到逻辑
        # （宁可服务端返回"已签"，也不漏签）。
        result["sign"] = "今日已签到（签到前状态确认，跳过签到请求，不重复提交）"
        result["success"] = True
        log(f"🟡 {result['sign']}")
    else:
        try:
            res = client.do_sign()
            data = res.get("data") or {}
            if res.get("code") != 0:
                result["sign"] = f"签到接口返回异常：{res.get('code')} {res.get('msg') or ''}".strip()
                err(f"❌ {result['sign']}")
            else:
                did = bool(data.get("didSignToday"))
                this_time = bool(data.get("doSignThisTime"))
                today_bonus = b32(data.get("bountyCountToday"))
                result["title"] = b32(data.get("title")) or result["title"]
                result["extra"] = b32(data.get("extraRewardBtnText")) or result["extra"]
                bonus_list = data.get("bonusList") or []
                if isinstance(bonus_list, list) and bonus_list:
                    src = b32((bonus_list[0] or {}).get("signSource"))
                    result["sign_source"] = src
                    result["bonus"] = f"{today_bonus} 奖励金"
                day = day_index(bonus_list)
                result["continuous"] = f"第 {day}/7 天" if day else "-"
                if this_time:
                    result["sign"] = f"签到成功，+{today_bonus} 奖励金（{result['continuous']}）"
                    result["success"] = True
                elif did:
                    result["sign"] = (f"今日已签到（本次不重复发放，+0 奖励金，"
                                      f"{result['continuous']}）")
                    result["success"] = True
                else:
                    result["sign"] = "签到未生效，请检查账号状态"
                    warn(f"⚠️ 上游返回：{preview(data, 400)}")
                log(f"✍️ {result['sign']}　{result['title']}")
        except Exception as exc:
            result["sign"] = f"签到异常：{exc}"
            err(f"❌ {result['sign']}")

    # ---- 最终奖励金 ----
    try:
        time.sleep(random.uniform(1.0, 2.0))
        after = client.point_info()
        result["points_after"] = points_of(after)
        result["amount_after"] = amount_of(after)
        if result["points_before"] is not None and result["points_after"] is not None:
            result["points_delta"] = result["points_after"] - result["points_before"]
        # 总积分 = 当前奖励金余额（签到后优先，签到后查不到则退回签到前）
        result["totalPoints"] = (result["points_after"]
                                 if result["points_after"] is not None
                                 else result["points_before"])
        log(f"💰 签到后奖励金：{result['points_after']}　"
              f"≈{result['amount_after'] or '-'} 元")
    except Exception as exc:
        warn(f"⚠️ 查询最终奖励金失败：{preview(exc, 160)}")

    # ---- 钱包余额（附赠信息）----
    try:
        w = client.wallet()
        wd = w.get("data") or {}
        if wd.get("accountBalance") not in (None, ""):
            result["wallet"] = f"{wd.get('accountBalance')} 元"
            log(f"👛 钱包余额：{result['wallet']}")
    except Exception:
        pass

    if not result["success"] and not result["error"] and result["sign"] != "-":
        result["success"] = True if result["points_delta"] is not None else result["success"]
    return result


# --------------------------------------------------------------------------- #
# 汇总与通知
# --------------------------------------------------------------------------- #
def in_expire_window(today: Optional[date] = None) -> bool:
    """今天是否落在「每 3 个月的月份里，月底前 7 天」提醒窗口内。

    窗口 = (last_day - N, last_day]，共 N 天；仅启用月份（EXPIRE_REMIND_MONTHS）生效。
    例：31 号月末且 N=7 → 25~31 号提醒；30 号月末 → 24~30 号提醒。
    """
    if today is None:
        today = date.today()
    if today.month not in EXPIRE_REMIND_MONTHS:
        return False
    last_day = calendar.monthrange(today.year, today.month)[1]
    return today.day > last_day - EXPIRE_REMIND_DAYS


def notify_expire_alert(lines: list) -> bool:
    """积分过期提醒强推：独立于「只推 ERROR」的错误通知，命中即推。"""
    if not lines or _qn_send is None:
        return False
    if not any(os.getenv(_k) for _k in _QN_ENVS):
        print("[QL] 未配置通知渠道，积分过期提醒未推送（可在青龙「通知设置」或环境变量中配置）",
              flush=True)
        return False
    title = "【%s】积分即将过期提醒" % _QN_SITE
    body = "\n".join(lines)
    try:
        _qn_send(title, body)
        return True
    except Exception as _e:
        print("[QL] 积分过期提醒推送失败：%s" % _e, flush=True)
        return False


def check_expire_remind(results: List[Dict[str, Any]]) -> None:
    """窗口内，任一账号现有积分>0 则在汇总日志连续打印并强推（过期积分=现有积分）。"""
    if not ENABLE_EXPIRE_REMIND or not in_expire_window():
        return
    hits = []
    for r in results:
        tp = r.get("totalPoints")
        if isinstance(tp, (int, float)) and tp > 0:
            label = b32(r.get("label")) or "账号"
            hits.append((label, tp))
    if not hits:
        return
    # 汇总日志连续打印（窗口内每天都会打印）
    log(_ACC_RULE)
    log("⏳ [积分过期提醒] 检测到现有积分将于本月底过期（过期积分=现有积分）：")
    for label, exp in hits:
        log(f"· {label}：{exp} 积分即将过期")
    log(_ACC_RULE)
    # 独立强推
    push_lines = ["检测到现有积分将于本月底过期（过期积分=现有积分）："]
    push_lines += [f"{label}：{exp} 积分即将过期" for label, exp in hits]
    if notify_expire_alert(push_lines):
        log("📣 [积分过期提醒] 已推送通知")


def _finish(total: int, ok_count: int, skipped: int = 0,
            results: Optional[List[Dict[str, Any]]] = None) -> int:
    """打印尾部 [执行汇总] 并推送错误日志，返回退出码。"""
    fail_count = total - ok_count - skipped
    tail = "账号 %d ｜ 成功 %d ｜ 失败 %d ｜ 跳过 %d" % (total, ok_count, fail_count, skipped)
    _now = datetime.now()
    _week = "一二三四五六日"[_now.weekday()]
    log(SUMMARY_SEP)
    log("📂 汇总日志")
    log("📃 脚本名称：哈啰出行")
    log("📅 运行日期：%s  周%s" % (_now.strftime("%Y-%m-%d"), _week))
    log("📊 运行结果：✔️ 成功：%d 个；❌ 失败：%d 个；⏭️ 跳过：%d 个"
        % (ok_count, fail_count, skipped))
    _cells = []
    if results:
        for idx, r in enumerate(results, 1):
            total_points = r.get("totalPoints")
            if total_points is None:
                continue
            _akey = r.get("server") or str(idx)
            _delta = points_delta(_akey, total_points)
            _dt = ("(%+.2f)" % _delta) if _delta is not None else ""
            _cells.append("账号%d：%s%s" % (idx, total_points, _dt))
    if _cells:
        log("💰 总积分：" + "  |  ".join(_cells))
    log(SUMMARY_SEP)
    # 积分过期提醒：每 3 个月的月底前 7 天窗口内、现有积分>0 时连续打印 + 强推
    if results is not None:
        try:
            check_expire_remind(results)
        except Exception as _e:
            warn(f"⚠️ [积分过期提醒] 检查异常：{_e}")
    # 错误日志推送：有错误才发；未配置通知渠道则只提示一行
    _notify_tail = tail
    if results:
        _point_lines = [f"{b32(r.get('label')) or '账号'} 总积分 {r.get('totalPoints')}"
                        for r in results if r.get("totalPoints") is not None]
        if _point_lines:
            _notify_tail += "\n" + " ｜ ".join(_point_lines)
    flush_notify(_QN_SITE, _notify_tail, logger=log)
    return 0 if fail_count == 0 else 1


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def main() -> int:
    log_startup()
    log(_ACC_RULE)
    log("🚲 %s" % APP_NAME)
    log("🕒 启动时间: %s" % now_text())
    log("🆔 小程序  : %s" % APP_NO)
    log("🧪 DRY_RUN : %s" % ("开启（只查询）" if DRY_RUN else "关闭"))
    log("🔐 登录    : YYB 取码 → weixinEasyLogin 静默登录（无需抓包）")
    log("✍️ 签到    : " + ("开启" if ENABLE_SIGN else "关闭（HL_ENABLE_SIGN=0）"))
    log(_ACC_RULE)

    try:
        accounts = load_accounts()
    except Exception as exc:
        err("❌ [配置] %s" % exc)
        _finish(0, 0)
        return 1

    # 按 YYB_ONLY_REFS 序号白名单筛选（1 起）；留空 [] 则运行全部账号
    if YYB_ONLY_REFS:
        wanted = set(int(x) for x in YYB_ONLY_REFS if str(x).strip().isdigit() and int(x) > 0)
        if wanted:
            accounts = [a for i, a in enumerate(accounts, 1) if i in wanted]
            log("ℹ️ 按 YYB_ONLY_REFS 筛选：请求序号 %s，命中 %d 个账号" % (sorted(wanted), len(accounts)))
            if not accounts:
                err("❌ YYB_ONLY_REFS 指定的序号均超出账号范围")
                _finish(0, 0)
                return 1

    total = len(accounts)

    results: List[Dict[str, Any]] = []
    for index, account in enumerate(accounts, 1):
        # 今天已经完整跑过的账号直接跳过，不再消耗 YYB 取码/取号配额
        key = account.server
        if is_done_today(key):
            log(f"⏭️ [打标] 账号 {index}/{total} 今日任务已完成，跳过")
            # 并入已有的 skipped 统计（不算成功、也不算失败）
            skip_expiring = expiring_from_done(key)
            results.append({
                "label": account.label, "server": account.server, "success": False,
                "skipped": True, "user": "-", "sign": "今日任务已完成，已跳过", "bonus": "-",
                "title": "", "extra": "", "points_before": None, "points_after": None,
                "points_delta": None, "totalPoints": total_points_from_done(key),
                "amount_before": "", "amount_after": "",
                "expiring": skip_expiring,
                "wallet": "", "continuous": "-", "sign_source": "", "error": "",
                "tasks": None,
            })
            continue
        try:
            result = run_account(index, total, account)
            results.append(result)
            if result.get("totalPoints") is not None:
                record_points_snapshot(account.server, result["totalPoints"])
            # 只有整个流程走完（success=True）才算今日完成；
            # 未绑定账号（skipped）、DRY_RUN、关闭签到等不算真正完成，不打标
            if (result.get("success") and not result.get("skipped")
                    and not DRY_RUN and ENABLE_SIGN):
                mark_done_today(key, result.get("sign", ""), result.get("expiring"),
                                result.get("totalPoints"))
        except Exception as exc:
            err("❌ [主程序] %s 执行异常：%s" % (account.label, exc))
            results.append({
                "label": account.label, "server": account.server, "success": False,
                "skipped": False, "user": "-", "sign": "-", "bonus": "-", "title": "",
                "extra": "", "points_before": None, "points_after": None, "points_delta": None,
                "totalPoints": None,
                "amount_before": "", "amount_after": "", "expiring": None,
                "wallet": "", "continuous": "-",
                "sign_source": "", "error": str(exc), "tasks": None,
            })
        if index < total:
            time.sleep(random.uniform(3.0, 6.0))

    ok = sum(1 for i in results if i.get("success") and not i.get("skipped"))
    skipped = sum(1 for i in results if i.get("skipped"))
    return _finish(total, ok, skipped, results)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log("⏹️ 已手动中断")
        sys.exit(130)
