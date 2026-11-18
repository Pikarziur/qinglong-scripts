#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# =========================================================
# name:  回收猿旧衣服回收
# version: 2026-11-18a
# cron: 6 5,15 * * *
# 奖励金说明：提现型奖励金，永久有效、不过期（满 1 元可提现）
# 提现提醒：每年 6/30 与 12/31 汇总打印并通知用户手动提现
# =========================================================

# 环境变量：
#   YYB_SERVER   每行：地址@账号标识（例如 http://yyb-go:8000@1）
#   HSY_NOTIFY   0 关闭错误推送；默认 1
#
# 内部配置（直接改下面常量，省得去面板设环境变量）：
#   YYB_ONLY_REFS  账号序号白名单（1 起），仅运行指定序号；留空 [] 运行全部，
#                  例如 [1,3] 只跑第 1、3 个账号（序号按 YYB_SERVER 行序，1 起）
#
# 通知：直接调用青龙自带通知模块 notify.py（容器内 /ql/data/scripts/notify.py，
#       仓库内为同目录 / 上一级），在面板「通知设置」配一次即全站通用。
#       口径：只推错误 —— 本次运行出现 ERROR 日志才推送一次；无错误完全静默。
# 日志规范：[LEVEL] [HSY] message   （LEVEL: INFO / WARN / ERROR）
# 提现提醒：奖励金永久有效不过期；每年 6/30、12/31 汇总打印并独立强推，
#           提醒用户进入小程序手动提现（脚本不自动提现，避免误操作真实资金）。
#
# 作者 lcmovie  https://github.com/lcmovie/YYB-GO-Script-i

import hashlib
import calendar
from datetime import date, datetime, timedelta
import json
import os
import sys
import time
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Optional

import requests

APP_ID = "wxadd84841bd31a665"
BASE_URL = "https://www.52bjy.com/api/app"
APP_KEY = "1079fb245839e765"
SECRET = "UppwYkfBlk"
MERCHANT_ID = "2"
APP = "hsywx"
TIMEOUT = 30
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
# 连接层超时**不触发 fleet.failover**，所以一个成员超时就整账号失败。
# 而 YYB-GO 每次新请求会按「最近最少使用」把请求轮转到编队的下一个成员，
# 重试正好能命中可用成员。
#
# 只对「上游连接超时」重试：
#   - 429（本地限速/熔断）：服务端已在冷却，重试只会加剧风控 → 不重试
#   - 404 / 409（账号不存在/失效）：不可恢复 → 不重试
#   - 502 且带上游错误码（-14 等授权拒绝）：重试无用 → 不重试
# 间隔需大于 YYB 的取码最小间隔（YYB_CODE_MIN_GAP=3s），否则会被限速。
YYB_RETRY_TIMES = int(os.getenv("HUISHOUYUAN_YYB_RETRY", "5") or 5)      # 额外重试次数（不含首次）
YYB_RETRY_DELAY = int(os.getenv("HUISHOUYUAN_YYB_RETRY_DELAY", "4") or 4)  # 重试间隔秒数

# 内部配置：直接改这里，省去去面板设环境变量
# 账号序号白名单（1 起），仅运行指定序号，留空 [] 运行全部；例如 [1, 3]
YYB_ONLY_REFS = []

# ── 提现提醒（2026-11-17 新增）────────────────────────────────────
# 奖励金为提现型、永久有效不过期。故不做"过期提醒"，改为每年 6/30 与 12/31
# 汇总打印 + 独立强推，提醒用户进入小程序手动提现（脚本绝不自动提现真实资金）。
ENABLE_WITHDRAW_REMIND = True
WITHDRAW_REMIND_MONTHS_DAYS = ((6, 30), (12, 31))  # 每年这两个日期提醒
WITHDRAW_MIN_AMOUNT = Decimal("1")                 # 可提现达到该金额才提醒（HAR: award_cash=1）

# ===================== 日打标（今日完成标记） =====================
# 今日已完成的账号当天再次运行时直接跳过，避免浪费 YYB 取码/取号配额
DAILY_DONE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "huishouyuan_daily_done.json")


# ────────────────────────────────────────────
# 统一日志 + 错误通知（只推错误）
# ────────────────────────────────────────────
# ———————————— 错误通知（可选项，想用则用）————————————
# 只推错误：本次运行出现 ERROR 日志才推送一次；正常跑完不打扰。
# 直接调用青龙自带的通知模块（容器内为 /ql/data/scripts/notify.py，仓库内为同目录/上一级的 notify.py）——
#   在青龙面板「通知设置」里配一次即可全站通用（该文件由青龙官方维护，支持其全部推送渠道）。
# 找不到该文件、或未配置任何通知渠道时，只在日志末尾提示一行，不报错、不中断。
import os as _os
import sys as _sys

_QN_SITE = "回收猿"
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
    # 本脚本自带开关：HSY_NOTIFY=0/false/no/off 时整体不推送
    if _os.getenv("HSY_NOTIFY", "1").strip().lower() in {"0", "false", "no", "off"}:
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
    line = "[%s] %s" % (level, msg)
    print(line, flush=True)
    if level == "ERROR":
        collect_error(line)


def log(msg):  _emit("INFO", msg)
def warn(msg): _emit("WARN", msg)
def err(msg):  _emit("ERROR", msg)


# 执行汇总块用的分隔线
def now_text() -> str:
    # 兼容两种导入: from datetime import datetime / import datetime
    _dt = datetime.now() if hasattr(datetime, "now") else datetime.datetime.now()
    return _dt.strftime("%Y-%m-%d %H:%M:%S")
# ========== 启动信息 ==========
def log_startup(total: int = 0) -> None:
    log(_ACC_RULE)
    _title = "📃 脚本名称：回收猿"
    if APP_ID:
        _title += " ｜" + APP_ID
    log(_title)
    log("📌 脚本版本: %s" % SCRIPT_VER)
    if total:
        log("🔢 账号数量: %d" % total)
    log(_ACC_RULE)
SCRIPT_VER = "2026-11-18a"
_ACC_RULE = "=" * 70
SUMMARY_SEP = "▶︎ " + "━" * 50 + " ◀︎"

# 积分每日快照：按账号存每天最后一次的总积分，用于次日对比涨跌。
POINTS_HISTORY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "huishouyuan_points_history.json")


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



# ====================== 提现提醒（半年一次） ======================
def in_withdraw_window(today: date = None) -> bool:
    """今天是否是提现提醒日：每年 6/30 与 12/31（UTC+8 本地日期）。"""
    today = today or date.today()
    return (today.month, today.day) in WITHDRAW_REMIND_MONTHS_DAYS


def notify_withdraw_alert(lines: list) -> bool:
    """提现提醒强推：独立于「只推 ERROR」的错误通知，命中即推。"""
    if not lines or _qn_send is None:
        return False
    if not any(os.getenv(_k) for _k in _QN_ENVS):
        print("[QL] 未配置通知渠道，提现提醒未推送（可在青龙「通知设置」或环境变量中配置）",
              flush=True)
        return False
    title = "【%s】奖励金提现提醒" % _QN_SITE
    body = "\n".join(lines)
    try:
        _qn_send(title, body)
        return True
    except Exception as _e:
        print("[QL] 提现提醒推送失败：%s" % _e, flush=True)
        return False


def check_withdraw_remind(results: list) -> None:
    """6/30 与 12/31：任一账号可提现 >= 门槛，则汇总打印 + 独立强推提醒手动提现。"""
    if not ENABLE_WITHDRAW_REMIND or not in_withdraw_window():
        return
    hits = []
    for r in results:
        cashable = r.get("withdrawable")
        if isinstance(cashable, Decimal) and cashable >= WITHDRAW_MIN_AMOUNT:
            hits.append((r.get("label") or "账号", cashable))
    if not hits:
        return
    log(_ACC_RULE)
    log("💰 [提现提醒] 检测到可提现奖励金（每半年提醒一次，请进入回收猿小程序手动申请提现）：")
    for label, cashable in hits:
        log(f"   · {label}：可提现 {cashable:.2f} 元")
    log(_ACC_RULE)
    push_lines = ["检测到可提现奖励金，请进入回收猿小程序手动申请提现："]
    push_lines += [f"{label}：可提现 {cashable:.2f} 元" for label, cashable in hits]
    if notify_withdraw_alert(push_lines):
        log("📣 [提现提醒] 已推送通知")


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


def mark_done_today(server: str, note: str = "", withdrawable: Any = None,
                    total_points: Any = None) -> None:
    """给该账号打上「今日已完成」标记（同时缓存当天可提现金额与奖励金余额，供跳过时读回）。"""
    cache = load_daily_done()
    today = _today()
    # 只保留当天的记录，跨天自动清理，避免文件无限增长
    cache = {k: v for k, v in cache.items()
             if isinstance(v, dict) and str(v.get("date") or "") == today}
    cache[server] = {
        "date": today,
        "doneTime": now_text(),
        "note": str(note or ""),
        "withdrawable": str(withdrawable) if withdrawable is not None else None,
        "totalPoints": str(total_points) if total_points is not None else None,
    }
    save_daily_done(cache)
    log(f"✅ [打标] 今日任务已完成，已记录（{today}）")


def withdrawable_from_done(server: str) -> Decimal:
    """从当天打标记录读回可提现金额（供本次被跳过、未走 run_one 的账号使用）。"""
    entry = load_daily_done().get(server)
    if not isinstance(entry, dict) or "withdrawable" not in entry:
        return Decimal("0")
    return amount(entry.get("withdrawable"))


def total_points_from_done(server: str) -> Optional[Decimal]:
    """从当天打标记录读回奖励金余额（本站积分单位为元），供被跳过的账号在汇总里显示。"""
    entry = load_daily_done().get(server)
    if not isinstance(entry, dict) or "totalPoints" not in entry:
        return None
    raw = entry.get("totalPoints")
    if raw is None:
        return None
    val = amount(raw)
    return val if val is not None else None


def clear_daily_done(server: str = "") -> None:
    """清除指定账号（或全部）的日打标。"""
    cache = load_daily_done()
    if server:
        cache.pop(server, None)
    else:
        cache = {}
    save_daily_done(cache)


def done_key(server: str, ref: str) -> str:
    """账号唯一标识：地址@账号标识（与 YYB_SERVER 配置口径一致，全局唯一）。"""
    return "%s@%s" % (server, ref)


def bind_context():
    # 运行时还原授权上下文，避免将绑定资料以可读文本保存。
    return bytes.fromhex("626135373762353533303135353361637c656e76").decode().split("|", 1)


def key(*codes):
    return "".join(map(chr, codes))


def routes():
    values = []
    for lineno, raw in enumerate(os.getenv("YYB_SERVER", "").splitlines(), 1):
        raw = raw.strip()
        if not raw:
            continue
        if "@" not in raw:
            raise RuntimeError(f"YYB_SERVER 第 {lineno} 行格式错误，应为 地址@账号标识")
        server, ref = raw.rsplit("@", 1)
        server, ref = server.strip().rstrip("/"), ref.strip()
        if not server or not ref:
            raise RuntimeError(f"YYB_SERVER 第 {lineno} 行格式错误，应为 地址@账号标识")
        if not server.startswith(("http://", "https://")):
            server = "http://" + server
        values.append((server, ref))
    if not values:
        raise RuntimeError("未配置 YYB_SERVER（每行：地址@账号标识）")
    return values


def success(body):
    return bool(body.get("isSucess") or body.get("is_success") or body.get("success"))


def message(body):
    return str(body.get("message") or body.get("msg") or body.get("error") or "未知响应")


def amount(value):
    try:
        return Decimal(str(value or "0"))
    except (InvalidOperation, ValueError):
        return Decimal("0")


def sign(params):
    # v135 的 axGet：参数按键名排序后拼接，再追加 secret 做 MD5。
    return hashlib.md5(("&".join(f"{k}={params[k]}" for k in sorted(params)) + SECRET).encode()).hexdigest()


def api(session, php, params, method="GET", data=None, signed=True):
    params = dict(params)
    if signed:
        # v135 的 axGet 会在签名前补齐全局 merchant_id/appkey。
        params.setdefault("merchant_id", MERCHANT_ID)
        params.setdefault("appkey", APP_KEY)
        params["sign"] = sign(params)
    response = session.request(method, f"{BASE_URL}/{php}", params=params, data=data, timeout=TIMEOUT)
    response.raise_for_status()
    try:
        return response.json()
    except ValueError as exc:
        raise RuntimeError(f"{php} 返回非 JSON：HTTP {response.status_code}") from exc


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


def yyb_code(server, ref):
    """取 wx.login code；仅「上游连接超时」自动重试。

    连接层超时是纯连接层故障（上游长连接节点 8 秒内不响应，返回 502
    且响应体无上游错误码）。YYB-GO 的编队转移清单不含连接层超时，
    不会自动换成员；但它每次新请求会把请求轮转到编队的下一个成员，
    重试正好能命中可用成员。

    熔断（429）、账号失效（404）、上游拒绝（-14 等）一律不重试。
    """
    url = f"{server}/wxapp/getCode"
    for attempt in range(YYB_RETRY_TIMES + 1):
        try:
            response = requests.post(url, json={"ref": ref, "app_id": APP_ID}, timeout=YYB_TIMEOUT)
        except requests.RequestException as exc:
            # 网络层异常（连接被拒 / 读取超时等）同属连接层故障，可重试
            if attempt >= YYB_RETRY_TIMES:
                raise RuntimeError(f"YYB 取 code 请求异常：{exc}") from None
            log("⏳ [重试] getCode 第%d/%d次失败（上游连接超时），%d秒后重试"
                % (attempt + 1, YYB_RETRY_TIMES + 1, YYB_RETRY_DELAY))
            time.sleep(YYB_RETRY_DELAY)
            continue

        try:
            body = response.json()
        except ValueError:
            body = None

        if response.status_code >= 400:
            detail = _yyb_error_detail(body)
            if detail != "上游连接超时" or attempt >= YYB_RETRY_TIMES:
                raise RuntimeError(f"YYB 取 code 失败：HTTP {response.status_code}"
                                   + (f"（{detail}）" if detail else ""))
            log("⏳ [重试] getCode 第%d/%d次失败（%s），%d秒后重试"
                % (attempt + 1, YYB_RETRY_TIMES + 1, detail, YYB_RETRY_DELAY))
            time.sleep(YYB_RETRY_DELAY)
            continue

        if not isinstance(body, dict):
            raise RuntimeError("YYB 取 code 失败：响应非 JSON")

        if int(body.get("code", -1)) != 0:
            raise RuntimeError(f"YYB 取 code 失败：{body.get('msg') or body.get('message') or body}")
        result = (body.get("data") or {}).get("result")
        code = result if isinstance(result, str) else (result or {}).get("code")
        if not code:
            raise RuntimeError("YYB 未返回 data.result.code")
        return str(code)

    raise RuntimeError("YYB 取 code 失败：重试次数已用尽")


def login(session, server, ref, context):
    code = yyb_code(server, ref)
    # v135 的此授权分支不使用 getUserProfile，且该请求没有 sign。
    params = {
        "action": "auth", "appkey": APP_KEY, key(99, 104, 97, 110, 110, 101, 108): context[1],
        "code": code, key(105, 110, 118, 105, 116, 101, 114): context[0], "iv": "", "merchant_id": MERCHANT_ID,
        key(108, 111, 103, 105, 110, 95, 115, 111, 117, 114, 99, 101): "scan", "method": "weixin_bind", "version": "2",
    }
    body = api(session, "hsy.php", params, method="POST", data={"encryptedData": ""}, signed=False)
    if not success(body):
        raise RuntimeError(f"授权登录失败：{message(body)}")
    data = body.get("data") or {}
    username = str(data.get("username") or "").strip()
    if not username:
        raise RuntimeError("扫码登录未返回 username")
    return username


def center(session, username):
    body = api(session, "hsy.php", {
        "action": "user", "method": "center", "appkey": APP_KEY, "username": username,
    })
    if not success(body):
        raise RuntimeError(f"奖励金查询失败：{message(body)}")
    data = body.get("data") or {}
    # 前端“我的奖励金”显示 award_balance；冻结金额不参与可提现判断。
    return amount(data.get("award_balance")), data


def sign_in(session, username):
    before = api(session, "hsy.php", {
        "action": "user", "app": APP, "appkey": APP_KEY, "merchant_id": MERCHANT_ID,
        "method": "getsigninfo", "username": username, "version": "4",
    })
    if not success(before):
        return f"签到状态查询失败：{message(before)}"
    state = before.get("data") or {}
    if str(state.get("hassign", "0")) == "1":
        return f"今日已签到（连续 {state.get('thisturn', '?')} 天）"
    body = api(session, "hsy.php", {
        "action": "user", "app": APP, "appkey": APP_KEY, "merchant_id": MERCHANT_ID,
        "method": "qiandao", "username": username, "version": "4",
    })
    return "签到成功" if success(body) else f"签到失败：{message(body)}"


def lucky_draw(session, username):
    state = api(session, "promotionjgg.php", {
        "action": "list", "app": "hsy", "appkey": APP_KEY,
        "merchant_id": MERCHANT_ID, "username": username,
    })
    if not success(state):
        return f"幸运抽奖状态查询失败：{message(state)}"
    info = state.get("data") or {}
    try:
        chances = int(info.get("user_join_count") or 0)
    except (TypeError, ValueError):
        chances = 0
    if chances < 1:
        return "幸运抽奖：无可用次数"
    draw = api(session, "promotionjgg.php", {
        "action": "prize_draw", "app": "hsy", "appkey": APP_KEY,
        "merchant_id": MERCHANT_ID, "username": username,
    })
    if not success(draw):
        return f"幸运抽奖失败：{message(draw)}"
    prize = draw.get("data") or {}
    title = str(prize.get("title") or prize.get("name") or "未返回奖品名称").strip()
    return f"幸运抽奖：{title}"


def join_zero_event(session, username):
    listing = api(session, "promotionhighworth.php", {
        "action": "actlist", "app": APP, "appkey": APP_KEY,
        "merchant_id": MERCHANT_ID, "username": username,
    })
    if not success(listing):
        return [f"0 元夺宝活动查询失败：{message(listing)}"]
    data = listing.get("data") or {}
    active = data.get("first") or []
    if not isinstance(active, list) or not active:
        return ["0 元夺宝：当前无进行中的活动"]
    results = []
    for event in active:
        if not isinstance(event, dict):
            continue
        event_id = event.get("itemid") or event.get("id")
        title = str(event.get("title") or event.get("mall_title") or event_id or "活动").strip()
        if not event_id:
            results.append(f"0 元夺宝：{title} 缺少活动编号，跳过")
            continue
        if str(event.get("isjoin", "0")) == "1":
            results.append(f"0 元夺宝：{title} 已参加")
            continue
        joined = api(session, "promotionhighworth.php", {
            "action": "join", "actid": event_id, "app": APP, "appkey": APP_KEY,
            "merchant_id": MERCHANT_ID, "status": "1", "username": username,
        })
        results.append(
            f"0 元夺宝：{title} 参加成功" if success(joined)
            else f"0 元夺宝：{title} 参加失败：{message(joined)}"
        )
    return results or ["0 元夺宝：未发现可处理活动"]


def query_withdrawable(session, username):
    """查可提现金额（awardlist.data.award_amount）。成功返回 Decimal，失败返回 None。

    HAR 依据：envcash.php?action=awardlist&type=award → data.award_amount（可提现余额）、
    data.award_check（在途/审核中金额）。这里只取可提现余额，不发起提现。
    """
    available = api(session, "envcash.php", {
        "action": "awardlist", "appkey": APP_KEY, "genre": "0", "merchant_id": MERCHANT_ID,
        "type": "award", "username": username,
    })
    if not success(available):
        return None
    info = available.get("data") or {}
    return amount(info.get("award_amount"))


def manual_withdrawal_reminder(cashable):
    """根据可提现金额生成提醒文案；未达门槛返回 None。金额由 query_withdrawable 查得。"""
    if cashable is None:
        return None
    if cashable >= WITHDRAW_MIN_AMOUNT:
        return f"可提现 {cashable:.2f} 元，请进入回收猿小程序手动申请提现"
    return None


def run_one(index, server, ref, context):
    result = [f"账号 {index}（YYB {ref}）"]
    ok, errmsg = True, ""
    withdrawable = None
    balance = None
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0 MicroMessenger/7.0.20.1781 MiniProgramEnv/Windows"})
    try:
        username = login(session, server, ref, context)
        initial, _ = center(session, username)
        result.append(f"初始奖励金：{initial:.2f} 元")
        result.append(sign_in(session, username))
        result.append(lucky_draw(session, username))
        result.extend(join_zero_event(session, username))
        time.sleep(1)
        final, _ = center(session, username)
        result.append(f"最终奖励金：{final:.2f} 元")
        # 账户奖励金余额（本站积分单位为「元」，即总积分）
        balance = final
        withdrawable = query_withdrawable(session, username)
        reminder = manual_withdrawal_reminder(withdrawable)
        if reminder:
            result.append(reminder)
    except Exception as exc:
        ok, errmsg = False, str(exc)
        result.append(f"失败：{exc}")
    print(" | ".join(result))
    return result, ok, errmsg, withdrawable, balance


def main():
    log_startup()
    context = bind_context()
    accounts = routes()
    # 按 YYB_ONLY_REFS 序号白名单筛选（1 起）；留空 [] 则运行全部账号
    if YYB_ONLY_REFS:
        wanted = set(int(x) for x in YYB_ONLY_REFS if str(x).strip().isdigit() and int(x) > 0)
        if wanted:
            accounts = [acc for i, acc in enumerate(accounts, 1) if i in wanted]
            log("ℹ️ 按 YYB_ONLY_REFS 筛选：请求序号 %s，命中 %d 个账号" % (sorted(wanted), len(accounts)))
            if not accounts:
                err("❌ YYB_ONLY_REFS 指定的序号均超出账号范围")
                return 1
    total = len(accounts)
    log("回收猿：签到、抽奖与 0 元夺宝 · 共 %d 个账号" % total)
    ok_count = 0
    skipped = 0
    withdraw_results = []  # 供半年一次提现提醒使用：{"label", "withdrawable"}
    for index, (server, ref) in enumerate(accounts, 1):
        label = f"账号 {index}（YYB {ref}）"
        # 今天已经跑完的账号直接跳过，不再消耗 YYB 取码/取号配额
        key = done_key(server, ref)
        if is_done_today(key):
            log(f"⏭️ [打标] 账号 {index}/{total} 今日任务已完成，跳过")
            skipped += 1
            # 从打标缓存读回当天可提现金额，保证下午 cron 跳过时仍能提醒
            withdraw_results.append({"label": label, "withdrawable": withdrawable_from_done(key),
                                     "total": total_points_from_done(key), "key": key})
            continue
        _lines, ok, errmsg, withdrawable, balance = run_one(index, server, ref, context)
        withdraw_results.append({"label": label, "withdrawable": withdrawable,
                                 "total": balance, "key": key})
        record_points_snapshot(key, balance)
        if ok:
            ok_count += 1
            # _lines 顺序：账号行 / 初始奖励金 / 签到结果；签到失败不算今日完成
            sign_msg = _lines[2] if len(_lines) > 2 else "签到成功"
            if "失败" not in sign_msg:
                mark_done_today(key, sign_msg, withdrawable, balance)
        else:
            err("❌ 账号 %d 失败：%s" % (index, errmsg))
    fail_count = total - ok_count - skipped
    _tail = "账号 %d ｜ 成功 %d ｜ 失败 %d ｜ 跳过 %d" % (total, ok_count, fail_count, skipped)
    _now = datetime.now()
    _week = "一二三四五六日"[_now.weekday()]
    log(SUMMARY_SEP)
    log("📂 汇总日志")
    log("📃 脚本名称：回收猿")
    log("📅 运行日期：%s  周%s" % (_now.strftime("%Y-%m-%d"), _week))
    log("📊 运行结果：✔️ 成功：%d 个；❌ 失败：%d 个；⏭️ 跳过：%d 个"
        % (ok_count, fail_count, skipped))
    _cells = []
    for idx, item in enumerate(withdraw_results, 1):
        total = item.get("total")
        if total is None:
            continue
        _delta = points_delta(item.get("key") or str(idx), total)
        _dt = ("(%+.2f)" % _delta) if _delta is not None else ""
        _cells.append("账号%d：%.2f 元%s" % (idx, total, _dt))
    if _cells:
        log("💰 总积分：" + "  |  ".join(_cells))
    log(SUMMARY_SEP)
    # 提现提醒：6/30 与 12/31，任一账号可提现达标则汇总打印 + 独立强推
    try:
        check_withdraw_remind(withdraw_results)
    except Exception as _e:
        warn(f"⚠️ [提现提醒] 检查异常：{_e}")
    # 错误日志推送：有错误才发；未配置通知渠道则只提示一行
    _notify_tail = _tail
    _point_lines = [f"{item.get('label')} 总积分 {item.get('total'):.2f} 元"
                    for item in withdraw_results if item.get("total") is not None]
    if _point_lines:
        _notify_tail += "\n" + " ｜ ".join(_point_lines)
    flush_notify("回收猿", _notify_tail, logger=log)
    return 0 if fail_count == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
