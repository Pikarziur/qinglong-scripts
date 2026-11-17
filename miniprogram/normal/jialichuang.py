#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# =========================================================
# name:  嘉立创
# version: 2026-11-17b
# cron: 12 5,15 * * *
# 积分有效期：次年12月31日【1+1】
# 过期提醒：每年 12/25~12/31 检查即将过期豆豆，汇总打印 + 强推通知（详见下方 EXPIRE_* 配置）
# =========================================================

# 任务流程：
#   1. 读取账号：优先 JLC_AUTH（token#secret）；否则 YYB_SERVER + YYB_ONLY_REFS 经 YYBGO
#   2. 调用 YYBGO 的 /wxapp/getCode 获取 wx.login code，完成 CAS 登录
#   3. 查询当日签到状态，执行签到 signIn
#   4. 领券 receiveVoucher（第七天自动领 8 豆豆），并查询豆豆总额
#   5. 输出汇总
# 可控参数：
#   JLC_AUTH        可选。格式 token#secret，多账号用 & / 换行分隔，免 YYB
#   YYB_SERVER      必填（无 JLC_AUTH 时）。格式「地址@ref#备注」，空格/换行/& 分隔
#   YYB_ONLY_REFS   账号序号白名单（1 起）。留空 [] 跑全部；填 [1,2] 只跑第 1、2 个账号（环境变量同名可覆盖）
#   JLC_CAS_APP_ID  可选。CAS 应用 ID，默认 JLC_MOBILE_APP
#   JLC_PLATFORM_TYPE 可选。平台类型，默认 MP-WEIXIN
#   JLC_SOURCE      可选。来源标识，默认 2
#   JLC_MINI_APPID  可选。小程序 AppID，默认 wx6c7b851c877dba42
#   JLC_MP_ENV / JLC_MP_VERSION  可选。环境/版本，默认 release / 1.117.4
#
# 日志规范：[LEVEL] [JLC] message   （LEVEL: INFO / WARN / ERROR）
# =========================================================

"""
【第七天逻辑说明】
- 状态接口返回 data.day == 7 且 haveSignIn==True 且 haveReceive==False 时，
  自动调用 receiveVoucher 领取"8 豆豆"。
"""
# 账号配置：YYB_SERVER 走青龙环境变量（无脚本内常量兜底）；YYB_ONLY_REFS 为顶部常量，可被同名环境变量覆盖
#   YYB_SERVER     账号基座（青龙环境变量），每项 "地址@ref#备注"，多账号用空格 / 换行 / & 分隔
#                  例：yyb-go:8000@1 yyb-go:8000@2
YYB_ONLY_REFS = []  # 账号序号白名单（1 起），留空 [] 跑全部；例如 [1,3] 只跑第 1、3 个账号

import os
import calendar
from datetime import date, datetime, timedelta, timezone
import sys
import json
import time
import traceback
import hashlib
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Tuple

import requests

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

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

_QN_SITE = "JLC 嘉立创"
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
    line = f"[{level}] [JLC] {msg}"
    print(line, flush=True)
    if level == "ERROR":
        collect_error(line)

def log(msg):   _emit("INFO", msg)
def warn(msg):  _emit("WARN", msg)
def err(msg):   _emit("ERROR", msg)


# ———————————— 账号分隔标识 ————————————
# 多账号同跑时，用醒目的横线 + 账号信息把各账号的日志隔开，便于阅读与定位。
def now_text() -> str:
    # 兼容两种导入: from datetime import datetime / import datetime
    _dt = datetime.now() if hasattr(datetime, "now") else datetime.datetime.now()
    return _dt.strftime("%Y-%m-%d %H:%M:%S")
# ========== 启动信息 ==========
def log_startup(total: int = 0) -> None:
    log(_ACC_RULE)
    log("📌 脚本版本: %s ｜ %s" % (SCRIPT_VER, JLC_MINI_APPID))
    if total:
        log("🔢 账号数量: %d" % total)
    log(_ACC_RULE)
SCRIPT_VER = "2026-11-17b"
_ACC_RULE = "=" * 70

def acc_banner(idx, total, ident=""):
    """打印账号分隔标识（开始）。"""
    log(_ACC_RULE)
    log("👤 账号 %d/%d%s" % (idx, total, (" ｜ " + str(ident)) if ident else ""))
    log(_ACC_RULE)

def acc_footer(idx, total):
    """打印账号分隔标识（结束）。"""
    log("🔚 账号 %d/%d 处理结束" % (idx, total))


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


def mark_done_today(server: str, note: str = "", deadline_expire=None, expire_detail=None,
                    total=None) -> None:
    """给该账号打上「今日已完成」标记（同时缓存当天过期数据与豆豆总数，供跳过时读回）。"""
    cache = load_daily_done()
    today = _today()
    # 只保留当天的记录，跨天自动清理，避免文件无限增长
    cache = {k: v for k, v in cache.items()
             if isinstance(v, dict) and str(v.get("date") or "") == today}
    cache[server] = {
        "date": today,
        "doneTime": now_text(),
        "note": str(note or ""),
        "deadline_expire": str(deadline_expire) if deadline_expire is not None else None,
        "expire_detail": str(expire_detail) if expire_detail is not None else None,
        "totalPoints": str(total) if total is not None else None,
    }
    save_daily_done(cache)
    log(f"✅ [打标] 今日任务已完成，已记录（{today}）")


def expire_from_done(server: str) -> tuple:
    """从当天打标记录读回 (deadline_expire, expire_detail)，供本次被跳过的账号提醒用。"""
    entry = load_daily_done().get(server)
    if not isinstance(entry, dict) or str(entry.get("date") or "") != _today():
        return None, None
    de = amount(entry.get("deadline_expire")) if entry.get("deadline_expire") is not None else None
    dt = amount(entry.get("expire_detail")) if entry.get("expire_detail") is not None else None
    return de, dt


def total_points_from_done(server: str):
    """从当天打标记录读回豆豆总数（总积分），供被跳过的账号在汇总里显示。"""
    entry = load_daily_done().get(server)
    if not isinstance(entry, dict) or str(entry.get("date") or "") != _today():
        return None
    raw = entry.get("totalPoints")
    if raw is None:
        return None
    try:
        return float(str(raw))
    except (TypeError, ValueError):
        return None


def clear_daily_done(server: str = "") -> None:
    """清除指定账号（或全部）的日打标。"""
    cache = load_daily_done()
    if server:
        cache.pop(server, None)
    else:
        cache = {}
    save_daily_done(cache)


def done_key(acc: Dict[str, Any]) -> str:
    """账号唯一标识：YYB 模式用「地址@账号标识」，手动 token 模式用 token 摘要。"""
    if acc.get("mode") == "yyb":
        return "%s@%s" % (acc.get("server", ""), acc.get("ref", ""))
    token = str(acc.get("token") or "")
    return "token:%s" % hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


SCRIPT_NAME = "JLC 嘉立创签到"

# ===================== 日打标（今日完成标记） =====================
# 今日已完成的账号当天再次运行时直接跳过，避免浪费 YYB 取码/取号配额
DAILY_DONE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "jialichuang_daily_done.json")

# ===================== 手动调试开关 =====================
DEBUG = False
DEBUG_ENV = {
    "YYB_ONLY_REFS": "http://127.0.0.1:8080@test#测试号",
}
if DEBUG:
    for _k, _v in DEBUG_ENV.items():
        os.environ.setdefault(_k, _v)
    warn("⚠️ 调试模式已开启（DEBUG=True），使用脚本内置 DEBUG_ENV 配置")
# =======================================================

BASE_URL = "https://m.jlc.com"
CAS_BASE_URL = "https://passport.jlc.com"

# ── 豆豆过期提醒（2026-11-17 新增）────────────────────────────────
# 规则：豆豆"获得起次年 12/31"过期（脚本头部已注明）。年尾前 7 天（12/25~12/31）
# 检查即将过期的豆豆，汇总打印 + 独立强推通知。
# 数值口径（用户定「两者结合」）：
#   主口径 = 服务端 getCustomerIntegral.deadlineExpireNum（官方算好的即将到期数）；
#   对照值 = 明细差值 = 前一年获取的积分 −（前一年已兑换 + 今年已兑换）；
#   两者不一致时只把对照值打印出来供参考，以主口径为准、不阻断。
# 明细接口（均 POST {"pageNum":n,"pageSize":10}，需翻页累加；HAR 142030 验证）：
#   getIntegralVoucherChangeRecord  → data.data[] 每条 integralChangeTypeName/integralChangeNum/createTime
#   selectIntegralVoucherChangeRecord → 已兑换明细（当前账号 totalRows=0，无样本，字段容错解析）
ENABLE_EXPIRE_REMIND = True
EXPIRE_REMIND_DAYS = 7            # 年尾前 N 天开始检查（12/25~12/31，含年末当天）
INTEGRAL_RECORD_PATH = "/api/activity/front/getIntegralVoucherChangeRecord"
EXCHANGE_RECORD_PATH = "/api/activity/front/selectIntegralVoucherChangeRecord"
RECORD_PAGE_SIZE = 50             # 明细翻页大小（HAR 用 10，这里放大减少请求；服务端如截断按实际 total 翻）
RECORD_MAX_PAGES = 30             # 翻页上限保护，避免异常时无限翻
CAS_APP_ID = os.getenv("JLC_CAS_APP_ID", "JLC_MOBILE_APP").strip()
PLATFORM_TYPE = os.getenv("JLC_PLATFORM_TYPE", "MP-WEIXIN").strip()
SOURCE = os.getenv("JLC_SOURCE", "2").strip()

# 嘉立创小程序 appid
JLC_MINI_APPID = os.getenv("JLC_MINI_APPID", "wx6c7b851c877dba42").strip()
# 旧版固定 key 仅用于兼容已有缓存；当前 key 必须从官方更新接口动态获取。
LEGACY_SECRET_KEY = "34343232636134362d646135612d346335662d386464382d356237633937623132413437"
SECRET_UPDATE_URL = BASE_URL + "/api/integrated/secret/update"
CAS_CHECK_APPLET_LOGIN_URL = CAS_BASE_URL + "/api/cas/sso/login/check-applet-login"
CAS_APPLET_LOGIN_URL = CAS_BASE_URL + "/api/cas/sso/login/applet-silent-login"
SECRET_EXPIRED_CODES = {29001, 29003}

# ── YYB 取码重试（2026-11-09 新增）────────────────────────────────
#
# 现象：本 appid 上 YYB-GO 偶发返回 HTTP 502，响应 msg 为
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
# 实测（ref=1 连打 8 次单次 12%），重试 5 次累计 49%。
#
# 只对「上游连接超时」重试：
#   - 429（本地限速/熔断）：服务端已在冷却，重试只会加剧风控 → 不重试
#   - 404 / 409（账号不存在/失效）：不可恢复 → 不重试
#   - 502 且带上游错误码（-14 等授权拒绝）：重试无用 → 不重试
# 间隔需大于 YYB 的取码最小间隔（YYB_CODE_MIN_GAP=3s），否则会被限速。
YYB_RETRY_TIMES = int(os.getenv("JLC_YYB_RETRY", "5") or 5)      # 额外重试次数（不含首次）
YYB_RETRY_DELAY = int(os.getenv("JLC_YYB_RETRY_DELAY", "4") or 4)  # 重试间隔秒数


# 默认 UA
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36 "
    "MicroMessenger/7.0.20.1781(0x6700143B) NetType/WIFI MiniProgramEnv/Windows WindowsWechat/WMPF"
)

session = requests.Session()
_CURRENT_SECRET_KEY = ""


# 已取消 token 本地缓存：每次运行强制重新取码 + 登录，不落盘。

# ===================== 登录相关 =====================
def _parse_yyb_line(item: str) -> Optional[Tuple[str, str, str]]:
    """解析一条账号配置：地址@ref#备注（备注可省，且不能含空格，因空格是分隔符）。无效返回 None。"""
    item = (item or "").strip()
    if not item:
        return None
    if "#" in item:
        entry_part, remark = item.split("#", 1)
        entry_part, remark = entry_part.strip(), remark.strip()
    else:
        entry_part, remark = item.strip(), ""
    if "@" not in entry_part:
        warn(f"⚠️ 跳过无效账号配置（缺 @）：{item!r}")
        return None
    server, ref = entry_part.rsplit("@", 1)
    server, ref = server.strip().rstrip("/"), ref.strip()
    if not server or not ref:
        return None
    if not server.startswith(("http://", "https://")):
        server = "http://" + server
    return server, ref, remark


def _yyb_entries() -> List[Tuple[str, str, str]]:
    """从青龙环境变量 YYB_SERVER 读全部账号，再用 YYB_ONLY_REFS 按 ref 过滤。

    - YYB_SERVER：账号基座，每项 "地址@ref#备注"（多账号空格 / 换行 / & 分隔）
    - YYB_ONLY_REFS：ref 过滤器，留空 = 跑 YYB_SERVER 全部账号；
      填 ["1","2"] = 只跑这些 ref。为顶部常量，可被同名环境变量覆盖。
    """
    raw = _env("YYB_SERVER")
    if not raw:
        raise RuntimeError(
            "缺少账号配置：请在青龙面板设置环境变量 YYB_SERVER"
            "（地址@ref，多账号空格分隔，如 yyb-go:8000@1 yyb-go:8000@2）。"
        )
    base_items = _split_accounts(raw)
    entries: List[Tuple[str, str, str]] = []
    for item in base_items:
        parsed = _parse_yyb_line(item)
        if parsed:
            entries.append(parsed)

    # 按 YYB_ONLY_REFS 序号白名单筛选（1 起）；留空 [] 则运行全部账号
    raw_filter = _env("YYB_ONLY_REFS")
    seqs = list(YYB_ONLY_REFS)
    if raw_filter:
        seqs = []
        for tok in str(raw_filter).replace(",", " ").replace("[", " ").replace("]", " ").split():
            seqs.append(tok)
    if seqs:
        wanted = set(int(x) for x in seqs if str(x).strip().isdigit() and int(x) > 0)
        if wanted:
            entries = [e for i, e in enumerate(entries, 1) if i in wanted]
            log("ℹ️ 按 YYB_ONLY_REFS 筛选：请求序号 %s，命中 %d 个账号" % (sorted(wanted), len(entries)))
    return entries


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


def get_wx_code(server, ref):
    """调用 YYB-Go-Enhanced 获取嘉立创小程序 wx.login code。

    仅「上游连接超时」才重试：这类失败是纯连接层的（上游长连接节点偶发
    不响应，8 秒 deadline 后返回 502，响应体无上游错误码）。YYB-GO 的
    编队转移清单不含连接层超时，不会自动换成员；但它每次新请求会把请求
    轮转到编队的下一个成员，重试正好能命中可用成员。

    熔断（429）、账号失效（404）、上游拒绝（-14 等）一律不重试。
    """
    for attempt in range(YYB_RETRY_TIMES + 1):
        try:
            yyb_session = requests.Session()
            yyb_session.trust_env = False
            response = yyb_session.post(
                f"{server}/wxapp/getCode",
                json={"ref": ref, "app_id": JLC_MINI_APPID},
                timeout=(10, 90),
            )
            try:
                data = response.json()
            except ValueError:
                raise RuntimeError(
                    f"YYB响应不是JSON（HTTP {response.status_code}）") from None

            if not response.ok:
                detail = _yyb_error_detail(data)
                if detail != "上游连接超时" or attempt >= YYB_RETRY_TIMES:
                    raise RuntimeError(
                        f"YYB请求失败（HTTP {response.status_code}）"
                        + (f"（{detail}）" if detail else ""))
                err(f"⏳ [重试] getCode 第{attempt + 1}/{YYB_RETRY_TIMES + 1}次失败"
                    f"（{detail}），{YYB_RETRY_DELAY}秒后重试")
                time.sleep(YYB_RETRY_DELAY)
                continue

            if "code" in data and data.get("code") not in (0, "0", None):
                raise RuntimeError(data.get("error") or data.get("msg") or "YYB请求失败")
            result = data.get("result")
            if result is None:
                result = (data.get("data") or {}).get("result")
            if not isinstance(result, dict) or not result.get("code"):
                raise RuntimeError("YYB未返回有效微信code")
            return str(result["code"])
        except requests.RequestException as exc:
            # 网络层异常（连接被拒 / 读取超时等）同属连接层故障，可重试
            if attempt >= YYB_RETRY_TIMES:
                raise RuntimeError(f"YYB请求异常：{exc}") from None
            err(f"⏳ [重试] getCode 第{attempt + 1}/{YYB_RETRY_TIMES + 1}次失败"
                f"（上游连接超时），{YYB_RETRY_DELAY}秒后重试")
            time.sleep(YYB_RETRY_DELAY)
        except RuntimeError as exc:
            err(f"❌ [YYB] 获取code失败：{exc}")
            return None
    err(f"❌ [YYB] 获取code失败：重试次数已用尽")
    return None


def _extract_login_auth(resp, fallback_secret):
    """兼容从响应头或 JSON 响应体读取登录凭据。"""
    token = resp.headers.get("X-Jlc-Accesstoken") or resp.headers.get("x-jlc-accesstoken")
    secret = resp.headers.get("secretkey") or resp.headers.get("Secretkey") or fallback_secret
    try:
        payload = resp.json()
    except (ValueError, TypeError):
        payload = {}

    candidates = [payload]
    if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
        candidates.insert(0, payload["data"])
    for item in candidates:
        if not isinstance(item, dict):
            continue
        token = token or item.get("accessToken") or item.get("token") or item.get("access_token")
        secret = item.get("secretkey") or item.get("secretKey") or secret

    if token and str(token).upper() != "NONE":
        return str(token), str(secret)
    return None, None


def refresh_secret_key(token="NONE", previous_key=""):
    """从嘉立创官方接口获取当前 keyId，不输出密钥内容。"""
    global _CURRENT_SECRET_KEY
    headers = {
        "accept": "application/json, text/plain, */*",
        "content-type": "application/json;charset=UTF-8",
        "X-JLC-AccessToken": token or "NONE",
        "X-JLC-ClientType": "MP-WEIXIN",
        "X-JLC-MP-AppId": JLC_MINI_APPID,
        "X-JLC-MP-Env": os.getenv("JLC_MP_ENV", "release"),
        "X-JLC-MP-Version": os.getenv("JLC_MP_VERSION", "1.117.4"),
        "origin": BASE_URL,
        "referer": BASE_URL + "/",
        "user-agent": DEFAULT_UA,
    }
    payload = {"keyId": previous_key} if previous_key else {}
    resp = session.post(SECRET_UPDATE_URL, json=payload, headers=headers, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    key_info = data.get("data") or {}
    key_id = key_info.get("keyId") if isinstance(key_info, dict) else None
    if data.get("code") != 200 or not key_id:
        raise RuntimeError(f"更新secretkey失败: {str(data.get('message') or data)[:200]}")
    _CURRENT_SECRET_KEY = str(key_id)
    log("🔑 [JLC] secretkey 已动态更新")
    return _CURRENT_SECRET_KEY


def _extract_cas_auth_code(payload):
    """从 CAS 响应中递归提取真正供 login-by-code 使用的 AC- 授权码。"""
    if isinstance(payload, str):
        return payload if payload.startswith("AC-") else None
    if isinstance(payload, dict):
        for value in payload.values():
            code = _extract_cas_auth_code(value)
            if code:
                return code
    if isinstance(payload, list):
        for value in payload:
            code = _extract_cas_auth_code(value)
            if code:
                return code
    return None


def get_cas_auth_code(server, ref):
    """按嘉立创小程序 154 的官方链路把 wx.login code 换成 CAS AC- code。"""
    wx_code = get_wx_code(server, ref)
    if not wx_code:
        raise RuntimeError("YYB未返回有效微信code")

    headers = {
        "accept": "application/json, text/plain, */*",
        "accept-encoding": "gzip, deflate",
        "content-type": "application/json",
        "referer": f"https://servicewechat.com/{JLC_MINI_APPID}/154/page-frame.html",
        "user-agent": DEFAULT_UA,
    }
    cas_session = requests.Session()
    check_resp = cas_session.post(
        CAS_CHECK_APPLET_LOGIN_URL,
        json={"appId": CAS_APP_ID, "appletAuthCode": wx_code},
        headers=headers,
        timeout=30,
    )
    check_resp.raise_for_status()
    check_data = check_resp.json()
    check_info = check_data.get("data") or {}
    applet_login_token = check_info.get("token") if isinstance(check_info, dict) else None
    if check_data.get("code") != 200 or not applet_login_token:
        message = check_data.get("message") or check_data.get("msg") or check_data
        raise RuntimeError(f"CAS检查小程序登录失败: {str(message)[:200]}")
    if check_info.get("isLoginBind") is False or check_info.get("bind") is False:
        raise RuntimeError("嘉立创账号尚未绑定当前微信，请先在官方小程序完成绑定")

    login_headers = dict(headers)
    login_headers["referer"] = f"{CAS_BASE_URL}/m/login/mp-login?appId={CAS_APP_ID}"
    login_resp = cas_session.post(
        CAS_APPLET_LOGIN_URL,
        json={"token": applet_login_token, "appId": CAS_APP_ID},
        headers=login_headers,
        timeout=30,
    )
    login_resp.raise_for_status()
    login_data = login_resp.json()
    cas_code = _extract_cas_auth_code(login_data)
    if login_data.get("code") != 200 or not cas_code:
        message = login_data.get("message") or login_data.get("msg") or login_data
        raise RuntimeError(f"CAS小程序静默登录失败: {str(message)[:200]}")
    log(f"🔑 [{ref}] CAS授权码获取成功")
    return cas_code


def login_with_code(server, ref):
    """使用 CAS AC- code 登录，并在密钥过期时自动更新后重试。"""
    url = BASE_URL + "/api/login/login-by-code"
    secret = refresh_secret_key()
    errors = []
    for attempt in range(2):
        headers = {
            "accept": "application/json, text/plain, */*",
            "referer": f"https://servicewechat.com/{JLC_MINI_APPID}/154/page-frame.html",
            "X-JLC-AccessToken": "NONE",
            "X-JLC-ClientType": "MP-WEIXIN",
            "X-JLC-MP-AppId": JLC_MINI_APPID,
            "X-JLC-MP-Env": os.getenv("JLC_MP_ENV", "release"),
            "X-JLC-MP-Version": os.getenv("JLC_MP_VERSION", "1.117.4"),
            "secretkey": secret,
            "xweb_xhr": "1",
            "user-agent": DEFAULT_UA,
        }
        try:
            code = get_cas_auth_code(server, ref)
            resp = session.post(url, files={"code": (None, code)}, headers=headers, timeout=30)
            token, response_secret = _extract_login_auth(resp, secret)
            if token:
                log(f"🔑 [{ref}] login-by-code 登录成功")
                return {
                    "token": token,
                    "secret": response_secret,
                    "updatedAt": int(time.time()),
                }
            body = resp.text.replace("\r", " ").replace("\n", " ")[:300]
            detail = f"第{attempt + 1}次: status={resp.status_code}, body={body}"
            errors.append(detail)
            try:
                response_code = resp.json().get("code")
            except (ValueError, AttributeError):
                response_code = None
            if attempt == 0 and (response_code in SECRET_EXPIRED_CODES or resp.status_code == 460):
                secret = refresh_secret_key(previous_key=secret)
                log(f"🔁 [{ref}] 登录未通过，已刷新secretkey并使用新CAS授权码重试")
                continue
            err(f"❌ [{ref}] login-by-code 未通过（{detail}）")
            break
        except Exception as e:
            detail = f"第{attempt + 1}次: 请求异常={e}"
            errors.append(detail)
            err(f"💥 [{ref}] login-by-code {detail}")
            break

    raise RuntimeError("CAS登录失败；" + "；".join(errors))


def get_token_for_account(server, ref, remark):
    """每次运行强制重新取码 + 登录，不落盘。"""
    auth_info = login_with_code(server, ref)
    log(f"🔑 [{remark}] 登录成功")
    return auth_info["token"], auth_info.get("secret", _CURRENT_SECRET_KEY or LEGACY_SECRET_KEY)


# ===================== 工具函数 =====================


def _env(key: str, default: str = "") -> str:
    v = os.environ.get(key, "")
    v = v.strip()
    return v if v else default


def _split_accounts(s: str) -> List[str]:
    """支持 空格 / 换行 / & 分隔。

    青龙面板 YYB_SERVER 多账号用空格分隔，如 "yyb-go:8000@1 yyb-go:8000@2"。
    """
    if not s:
        return []
    s = s.replace("\r\n", "\n").replace("\r", "\n").replace("&", "\n")
    return s.split()


def parse_accounts() -> List[Dict[str, str]]:
    """
    账号解析（账号源：YYB_SERVER 基座 + YYB_ONLY_REFS 过滤）：
    - JLC_AUTH（可选，手动凭据）：token#secret，免 YYB，优先于自动登录
    - YYB_SERVER（推荐）：地址@ref#备注，账号基座；YYB_ONLY_REFS 按 ref 过滤（留空 = 全部）
    """
    accounts: List[Dict[str, str]] = []

    # 1) 手动 token 模式（可选）
    auth_raw = _env("JLC_AUTH")
    if auth_raw:
        items = _split_accounts(auth_raw)
        for idx, item in enumerate(items):
            sep = "#" if "#" in item else ("|" if "|" in item else ("," if "," in item else None))
            if not sep:
                raise RuntimeError("JLC_AUTH 格式错误，应为 token#secret（多账号用 & 或换行分隔）。")
            token, secret = item.split(sep, 1)
            token, secret = token.strip(), secret.strip()
            if not token or not secret:
                raise RuntimeError("JLC_AUTH 中存在空的 token 或 secret。")
            accounts.append({
                "mode": "token",
                "token": token,
                "secret": secret,
                "remark": f"账号{idx+1}",
            })
        return accounts

    # 2) YYB_ONLY_REFS 自动登录模式（推荐）
    for server, ref, remark in _yyb_entries():
        accounts.append({
            "mode": "yyb",
            "server": server,
            "ref": ref,
            "remark": remark or ref,
        })
    if accounts:
        return accounts

    raise RuntimeError("缺少账号配置：请设置 YYB_SERVER（地址@ref，如 yyb-go:8000@1）或 JLC_AUTH。")


def build_headers(token: str, secret: str) -> Dict[str, str]:
    headers = {
        "accept": "application/json, text/plain, */*",
        "content-type": "application/json;charset=UTF-8",
        "x-jlc-accesstoken": token,
        "secretkey": _CURRENT_SECRET_KEY or secret,
        "origin": BASE_URL,
        "referer": BASE_URL + "/",
        "user-agent": DEFAULT_UA,
    }
    return {k: v for k, v in headers.items() if v}


def api_get(session: requests.Session, path: str, params: Optional[Dict[str, Any]] = None,
            token: str = "", secret: str = "") -> Dict[str, Any]:
    url = BASE_URL + path
    for attempt in range(2):
        headers = build_headers(token, secret) if token else {}
        r = session.get(url, params=params, headers=headers, timeout=20)
        r.raise_for_status()
        try:
            data = r.json()
        except Exception:
            raise RuntimeError(f"接口返回非 JSON：{url}\n{r.text[:300]}")
        if attempt == 0 and data.get("code") in SECRET_EXPIRED_CODES:
            refresh_secret_key(token=token, previous_key=headers.get("secretkey", ""))
            continue
        return data
    return data


def api_post(session: requests.Session, path: str, body: Optional[Dict[str, Any]] = None,
             token: str = "", secret: str = "") -> Dict[str, Any]:
    """POST JSON 接口（明细查询用），与 api_get 共用鉴权头与密钥过期重试。"""
    url = BASE_URL + path
    for attempt in range(2):
        headers = build_headers(token, secret) if token else {}
        r = session.post(url, json=body or {}, headers=headers, timeout=20)
        r.raise_for_status()
        try:
            data = r.json()
        except Exception:
            raise RuntimeError(f"接口返回非 JSON：{url}\n{r.text[:300]}")
        if attempt == 0 and data.get("code") in SECRET_EXPIRED_CODES:
            refresh_secret_key(token=token, previous_key=headers.get("secretkey", ""))
            continue
        return data
    return data


def get_sign_status(s, token, secret) -> Dict[str, Any]:
    return api_get(s, "/api/activity/sign/getCurrentUserSignInConfig",
                   params={"platformType": PLATFORM_TYPE}, token=token, secret=secret)


def do_signin(s, token, secret) -> Dict[str, Any]:
    return api_get(s, "/api/activity/sign/signIn",
                   params={"platformType": PLATFORM_TYPE, "source": SOURCE}, token=token, secret=secret)


def receive_voucher(s, token, secret) -> Dict[str, Any]:
    return api_get(s, "/api/activity/sign/receiveVoucher",
                   params={"platformType": PLATFORM_TYPE}, token=token, secret=secret)


def get_doudou_total(s, token, secret) -> Dict[str, Any]:
    return api_get(s, "/api/activity/front/getCustomerIntegral", token=token, secret=secret)


# ===================== 豆豆过期提醒（年尾前 7 天） =====================
def amount(value: Any) -> Decimal:
    """把接口数值安全转成 Decimal，失败按 0。"""
    try:
        return Decimal(str(value or "0"))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal("0")


def in_expire_window(today: date = None) -> bool:
    """是否落在过期提醒窗口：每年 12 月最后 N 天（12/25~12/31，含 12/31 当天）。"""
    today = today or date.today()
    if today.month != 12:
        return False
    return today.day > calendar.monthrange(today.year, 12)[1] - EXPIRE_REMIND_DAYS


def _record_year(rec: Dict[str, Any]) -> Optional[int]:
    """从明细 createTime 取年份。HAR 为 UTC ISO（...Z / +08:00），按北京时间归年更贴合签到口径。"""
    ct = str(rec.get("createTime") or "")
    if not ct:
        return None
    try:
        dt = datetime.fromisoformat(ct.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone(timedelta(hours=8))).year
    except Exception:
        return None


def _record_amount(rec: Dict[str, Any]) -> Decimal:
    """明细积分值，宽松取字段名（不同接口字段可能为 integralChangeNum / integralNum 等）。"""
    for k in ("integralChangeNum", "integralNum", "integralVoucher", "num", "amount", "value"):
        if k in rec and rec.get(k) is not None:
            return amount(rec.get(k))
    return Decimal("0")


def _fetch_all_records(s, token, secret, path) -> Optional[List[Dict[str, Any]]]:
    """翻页拉取全部明细；失败返回 None（与空列表区分：None=查询异常，[]=确实无记录）。"""
    rows: List[Dict[str, Any]] = []
    for page in range(1, RECORD_MAX_PAGES + 1):
        resp = api_post(s, path, {"pageNum": page, "pageSize": RECORD_PAGE_SIZE},
                        token=token, secret=secret)
        if not resp.get("success", False):
            if not rows:
                return None
            break
        data = resp.get("data") or {}
        page_rows = data.get("data") or []
        if isinstance(page_rows, list):
            rows.extend(r for r in page_rows if isinstance(r, dict))
        total_pages = data.get("totalPages")
        try:
            total_pages = int(total_pages)
        except (TypeError, ValueError):
            total_pages = page
        if page >= total_pages or not page_rows:
            break
    return rows


def calc_expire_by_detail(s, token, secret, check_year: int) -> Optional[Decimal]:
    """对照口径：前一年获取 −（前一年已兑换 + 今年已兑换）。

    check_year=检查当年（如 2026）；前一年=2025。豆豆"次年 12/31 过期"，
    故 2025 年获取、且未在 2025/2026 兑换掉的部分，将于 2026-12-31 过期。
    任一明细查询异常返回 None（只影响对照值，不影响主口径 deadlineExpireNum）。
    """
    prev_year = check_year - 1
    gain_rows = _fetch_all_records(s, token, secret, INTEGRAL_RECORD_PATH)
    exchange_rows = _fetch_all_records(s, token, secret, EXCHANGE_RECORD_PATH)
    if gain_rows is None or exchange_rows is None:
        return None
    prev_gain = sum((_record_amount(r) for r in gain_rows
                     if _record_year(r) == prev_year and _record_amount(r) > 0), Decimal("0"))
    exchanged = sum((abs(_record_amount(r)) for r in exchange_rows
                     if _record_year(r) in (prev_year, check_year)), Decimal("0"))
    result = prev_gain - exchanged
    log(f"    🧮 [过期对照] {prev_year}年获取 {prev_gain:.2f} − "
        f"{prev_year}/{check_year}年已兑换 {exchanged:.2f} = {result:.2f} 豆豆")
    return result if result > 0 else Decimal("0")


def notify_expire_alert(lines: list) -> bool:
    """过期提醒强推：独立于「只推 ERROR」的错误通知，命中即推。"""
    if not lines or _qn_send is None:
        return False
    if not any(os.getenv(_k) for _k in _QN_ENVS):
        print("[QL] 未配置通知渠道，过期提醒未推送（可在青龙「通知设置」或环境变量中配置）",
              flush=True)
        return False
    title = "【%s】豆豆即将过期提醒" % _QN_SITE
    body = "\n".join(lines)
    try:
        _qn_send(title, body)
        return True
    except Exception as _e:
        print("[QL] 过期提醒推送失败：%s" % _e, flush=True)
        return False


def check_expire_remind(results: List[Dict[str, Any]]) -> None:
    """年尾窗口内：以 deadlineExpireNum 为主口径，明细差值为对照，命中则汇总打印 + 强推。"""
    if not ENABLE_EXPIRE_REMIND or not in_expire_window():
        return
    hits = []
    for r in results:
        primary = r.get("deadline_expire")
        detail = r.get("expire_detail")
        val = primary if isinstance(primary, Decimal) and primary > 0 else None
        if val is None and isinstance(detail, Decimal) and detail > 0:
            val = detail  # 主口径缺失/为 0 时，用对照值兜底提醒
        if val is None or val <= 0:
            continue
        note = ""
        if isinstance(primary, Decimal) and isinstance(detail, Decimal) and primary != detail:
            note = f"（明细对照 {detail:.2f}，口径不一致仅供参考）"
        hits.append((r.get("label") or "账号", val, note))
    if not hits:
        return
    log(_ACC_RULE)
    log("⏳ [豆豆过期提醒] 检测到即将过期的豆豆（获得起次年 12/31 到期，请尽快使用）：")
    for label, val, note in hits:
        log(f"   · {label}：{val:.2f} 豆豆即将过期{note}")
    log(_ACC_RULE)
    push_lines = ["检测到即将过期的豆豆（获得起次年 12/31 到期，请尽快使用）："]
    push_lines += [f"{label}：{val:.2f} 豆豆即将过期{note}" for label, val, note in hits]
    if notify_expire_alert(push_lines):
        log("📣 [豆豆过期提醒] 已推送通知")


def _extract_streak_day(st_data: Dict[str, Any]) -> Optional[int]:
    for k in ("day", "continuousDay", "continueDay", "signDay"):
        v = st_data.get(k)
        if isinstance(v, int):
            return v
        if isinstance(v, str) and v.isdigit():
            return int(v)
    return None


def run_one_account(idx: int, remark: str, token: str, secret: str) -> Tuple[bool, Dict[str, Any]]:
    result: Dict[str, Any] = {
        "remark": remark,
        "signed": None,
        "gain_signin": None,
        "gain_day7": None,
        "total": None,
        "expireTime": None,
        "streak_day": None,
        "deadline_expire": None,
        "expire_detail": None,
    }

    ok_all = True

    with requests.Session() as s:
        # 1) 查状态
        st = get_sign_status(s, token, secret)
        if not st.get("success", False):
            err(f"❌ [{remark}] 查询签到状态失败：{json.dumps(st, ensure_ascii=False)}")
            return False, result

        st_data = st.get("data") or {}
        have_signin = st_data.get("haveSignIn") is True
        have_receive = st_data.get("haveReceive") is True
        streak_day = _extract_streak_day(st_data)
        result["streak_day"] = streak_day

        if streak_day is not None:
            log(f"📊 [{remark}] 当前连续签到天数：{streak_day} 天")
        else:
            log(f"📊 [{remark}] 当前连续签到天数：未知")

        # 2) 签到（若未签）
        if have_signin:
            result["signed"] = True
            log(f"🟡 [{remark}] 今日已签到")
        else:
            si = do_signin(s, token, secret)
            if not si.get("success", False):
                ok_all = False
                result["signed"] = False
                err(f"❌ [{remark}] 签到失败：{json.dumps(si, ensure_ascii=False)}")
            else:
                si_data = si.get("data") or {}
                gain = si_data.get("gainNum") or 0
                result["signed"] = True
                result["gain_signin"] = gain
                log(f"✅ [{remark}] 签到成功，本次获得：{gain} 豆豆")

                # 签到后刷新状态
                st2 = get_sign_status(s, token, secret)
                if st2.get("success", False):
                    st_data = st2.get("data") or {}
                    have_signin = st_data.get("haveSignIn") is True
                    have_receive = st_data.get("haveReceive") is True
                    streak_day = _extract_streak_day(st_data)
                    result["streak_day"] = streak_day

        # 3) 第七天领取 8 豆豆
        if streak_day == 7 and have_signin and (not have_receive):
            log(f"🎁 [{remark}] 检测到连续签到第 7 天且未领取，开始领取...")
            rv = receive_voucher(s, token, secret)
            if not rv.get("success", False):
                ok_all = False
                err(f"❌ [{remark}] 第七天领取失败：{json.dumps(rv, ensure_ascii=False)}")
            else:
                got = rv.get("data")
                result["gain_day7"] = got
                log(f"🎁 [{remark}] 第七天领取成功：+{got} 豆豆")
        elif streak_day == 7 and have_signin and have_receive:
            log(f"🟡 [{remark}] 连续签到第 7 天奖励已领取")

        # 4) 查豆豆总数
        ct = get_doudou_total(s, token, secret)
        if not ct.get("success", False):
            ok_all = False
            err(f"❌ [{remark}] 查询豆豆总数失败：{json.dumps(ct, ensure_ascii=False)}")
        else:
            data = ct.get("data") or {}
            total = data.get("integralVoucher")
            expire_time = data.get("expireTime")
            result["total"] = total
            result["expireTime"] = expire_time
            # 主口径：服务端算好的即将到期积分（deadlineExpireNum）
            if data.get("deadlineExpireNum") is not None:
                result["deadline_expire"] = amount(data.get("deadlineExpireNum"))
            extra = f"，有效期至：{expire_time}" if expire_time else ""
            log(f"💰 [{remark}] 当前豆豆总数：{total}{extra}")
            if result["deadline_expire"] is not None:
                log(f"    ⏳ 即将过期豆豆（服务端）：{result['deadline_expire']:.2f}")

        # 年尾窗口内才查明细对照值，平时不发这些请求
        if ENABLE_EXPIRE_REMIND and in_expire_window():
            try:
                result["expire_detail"] = calc_expire_by_detail(s, token, secret, date.today().year)
            except Exception as _e:
                warn(f"⚠️ [{remark}] 过期明细对照查询失败：{_e}")

    return ok_all, result


def main() -> int:
    log_startup()
    accounts = parse_accounts()
    results: List[Dict[str, Any]] = []
    fail_count = 0
    skip_count = 0

    log(f"🚀 {SCRIPT_NAME} 开始 · 共 {len(accounts)} 个账号")

    for idx, acc in enumerate(accounts, start=1):
        # 今天已经跑完的账号直接跳过，不再消耗 YYB 取码/取号配额
        key = done_key(acc)
        if is_done_today(key):
            log(f"⏭️ [打标] 账号 {idx}/{len(accounts)} 今日任务已完成，跳过")
            skip_count += 1
            # 从打标缓存读回当天过期数据，保证下午 cron 跳过时汇总仍能提醒
            de, dt = expire_from_done(key)
            results.append({"label": f"账号{idx}({acc.get('remark') or idx})",
                            "deadline_expire": de, "expire_detail": dt,
                            "total": total_points_from_done(key)})
            continue

        remark = acc["remark"]
        acc_banner(idx, len(accounts), "备注：" + (remark or f"账号{idx}"))

        # yyb 模式：每次运行强制重新取码 + 登录换取 token（不落盘）
        if acc.get("mode") == "yyb":
            try:
                token, secret = get_token_for_account(acc["server"], acc["ref"], remark)
            except Exception as e:
                fail_count += 1
                err(f"❌ 登录失败: {e}")
                acc_footer(idx, len(accounts))
                if idx < len(accounts):
                    time.sleep(2)
                continue
        else:
            token, secret = acc["token"], acc["secret"]
            log("📌 手动模式，跳过登录")

        ok, res = run_one_account(idx, remark, token, secret)
        res["ok"] = ok
        res["label"] = f"账号{idx}({remark or idx})"
        results.append(res)

        if not ok:
            fail_count += 1
        else:
            # 账号流程完整走完（ok=True）才算今日完成，同时缓存当天过期数据与豆豆总数
            mark_done_today(key, "签到完成", res.get("deadline_expire"),
                            res.get("expire_detail"), res.get("total"))

        acc_footer(idx, len(accounts))
        # 账号间延迟
        if idx < len(accounts):
            time.sleep(2)

    success_count = len(accounts) - fail_count - skip_count
    log(_ACC_RULE)
    log(f"📊 [执行汇总] {SCRIPT_NAME} · 账号 {len(accounts)} ｜ 成功 {success_count} ｜ 失败 {fail_count} ｜ 跳过 {skip_count}")
    # 豆豆总数（总积分）汇总：无论是否跳过、任务是否已完成，都在这里显示
    for idx, item in enumerate(results, 1):
        total = item.get("total")
        if total is None:
            continue
        log(f"💎 总积分({total})（{item.get('label') or '账号%d' % idx}）")
    log(_ACC_RULE)
    # 豆豆过期提醒：年尾前 7 天，主口径 deadlineExpireNum + 明细对照，汇总打印 + 强推
    try:
        check_expire_remind(results)
    except Exception as _e:
        warn(f"⚠️ [豆豆过期提醒] 检查异常：{_e}")
    # 错误日志推送：有错误才发；未配置通知渠道则只提示一行
    _notify_tail = f"账号 {len(accounts)} ｜ 成功 {success_count} ｜ 失败 {fail_count} ｜ 跳过 {skip_count}"
    _point_lines = [f"{item.get('label') or '账号'} 总积分 {item.get('total')}"
                    for item in results if item.get("total") is not None]
    if _point_lines:
        _notify_tail += "\n" + " ｜ ".join(_point_lines)
    flush_notify(
        "JLC 嘉立创",
        _notify_tail,
        logger=log,
    )
    return 1 if fail_count else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        err("💥 脚本异常：" + str(e))
        err(traceback.format_exc())
        sys.exit(1)
