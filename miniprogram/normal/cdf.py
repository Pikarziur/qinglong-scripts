#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# =========================================================
# name:  中免会员
# version: 2026-11-20a
# cron: 6 4,14 * * *
# 积分有效期：积分到账后36个自然月有效（到期日 = 到账日 + 3 年同月同日）【1+2】
# 检测过期积分：到期日前 7 天起每天检测、到期日前一天停（当天不提醒），提醒量=账户现有总积分；整池三年清空一次，检测年份按 2028 → 2031 → 2034 → 2037 → 2040 … 递推（每轮窗口固定 12-25 ~ 12-31）
# =========================================================

# 任务流程：
#   1. 读取 YYB_SERVER 账号基座，按 YYB_ONLY_REFS 序号白名单筛选（1 起）
#   2. 调用 YYBGO 的 /wxapp/getCode 获取每个账号的 wx.login code
#   3. 用 code 请求登录接口换取会员 token（每次运行强制重新登录，不落盘）
#   4. 调用签到接口完成每日签到，并查询今日是否已签 / 连续签到天数
#   5. 查询「预过期积分」明细取最近一笔到期日，若距今在 1~EXPIRE_WARN_DAYS 天之间（到期前一天停止），
#      即将过期积分金额直接套用「账户现有总积分」统计（不拆到期日分组），
#      在汇总下方输出 💰 提醒，并强制推送一次汇总通知（无错误也推）；
#      接口只读，不消耗取码配额
#   6. 查询账户积分总览（总积分 / 即将过期 / 累计获得 / 累计消耗），只取总积分写入
#      日打标缓存（当天第二轮被跳过时直接读回，汇总里仍打印 💎 总积分）
# 可控参数：
#   YYB_SERVER      必填。格式「地址@ref#备注」，多账号换行 / 空格 / & 分隔
#   YYB_ONLY_REFS   账号序号白名单（1 起）。留空 [] 跑全部；填 [1,2] 只跑第 1、2 个账号
#
# 日志规范：[LEVEL] message   （LEVEL: INFO / WARN / ERROR）
#
# =========================================================

YYB_ONLY_REFS = []   # 账号序号白名单（1 起），留空 [] 跑全部；例如 [1,3] 只跑第 1、3 个账号

import os, re, time, random, traceback, json
import requests
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

# ============== 新手配置区 ==============
APP_ID = "wxdf26125d1f97992c"
SIGN_LNG = ""
SIGN_LAT = ""
CDF_VERSION = "5.5.98"
APP_VERSION_PATH = "176"
HOST = "cdfmbrapi.cdfg.com.cn"
UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 16_1_2 like Mac OS X) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Mobile/15E148 MicroMessenger/8.0.75(0x18004b66) NetType/WIFI Language/zh_CN")
# ========================================

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
YYB_RETRY_TIMES = int(os.getenv("CDF_YYB_RETRY", "5") or 5)      # 额外重试次数（不含首次）
YYB_RETRY_DELAY = int(os.getenv("CDF_YYB_RETRY_DELAY", "4") or 4)  # 重试间隔秒数

LOGIN_URL       = "https://" + HOST + "/api/session/wxSession/v2"
SIGN_URL        = "https://" + HOST + "/api/user/sign"
SIGN_RECORD_URL = "https://" + HOST + "/api/user/signRecord"

# ——— 预过期积分提醒（2026-11-16 新增）————————————————————
# 真机抓包：POST /api/points/expirePointsList，无请求体，
# 返回 data[] = [{"balanceUnit":"10.00","expireDate":"2029-03-31 00:00:00"}, ...]
# 本接口只用来定「最近一笔到期日」（决定是否进入提醒窗口），
# 提醒金额用账户总积分（balance），见 user/info/v3
EXPIRE_POINTS_URL = "https://" + HOST + "/api/points/expirePointsList"
EXPIRE_PAGE_URL   = "packages/list-view/integral-past/integral-past"
EXPIRE_WARN_DAYS  = 7     # 最近一笔到期日距今在 1~N 天之间才提醒（到期日-7 天起、到期前一天止）；已过期不提醒

# ——— 账户积分总览（2026-11-17 新增）———————————————————————
# 真机抓包：POST /api/user/info/v3，无请求体、无 query 参数，
# x-access-token 鉴权，pageUrl = packages/list-view/integral-detail/integral-detail
# 返回 data.points = {balance, balanceShow, frozenBalance,
#                     expireBalance, totalAdd, totalConsume}
#   balance       账户总积分（可用余额）
#   expireBalance 即将过期积分合计（只有金额，没有到期日）
#   totalAdd      累计获得积分
#   totalConsume 累计消耗积分
USER_INFO_URL      = "https://" + HOST + "/api/user/info/v3"
USER_INFO_PAGE_URL = "packages/list-view/integral-detail/integral-detail"

# 日打标文件：今日已完成的账号当天再次运行时直接跳过（不消耗 YYB 取码配额）
DAILY_DONE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "cdf_daily_done.json")

# ———————————— 错误通知（可选项，想用则用）————————————
# 只推错误：本次运行出现 ERROR 日志才推送一次；正常跑完不打扰。
# 直接调用青龙自带的通知模块（容器内为 /ql/data/scripts/notify.py，仓库内为同目录/上一级的 notify.py）——
#   在青龙面板「通知设置」里配一次即可全站通用（该文件由青龙官方维护，支持其全部推送渠道）。
# 找不到该文件、或未配置任何通知渠道时，只在日志末尾提示一行，不报错、不中断。
import os as _os
import sys as _sys

_QN_SITE = "中免会员"
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


def flush_notify(_site=None, summary="", logger=None, force=False, **_kw):
    """收尾调用：有 ERROR，或 force=True（积分即将过期）才推送，一次运行最多一条。

    force 只决定「该不该推」；能否发出仍取决于青龙 notify.py 与通知渠道配置，
    未配置时只在日志提示一行，不影响脚本结果。站点名以 _QN_SITE 为准。
    """
    global _qn_flushed
    if _qn_flushed or (not _qn_errors and not force):
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
        # 有错误仍沿用原标题；仅「积分即将过期」触发时改用汇总标题
        _title = "执行出错" if _qn_errors else "执行汇总"
        _qn_send("【%s】%s" % (_QN_SITE, _title), _body)
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

# ———————————— 统一日志 ————————————
def _emit(level, msg):
    line = f"[{level}] {msg}"
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
    _title = "📃 脚本名称：中免会员"
    if APP_ID:
        _title += " ｜" + APP_ID
    log(_title)
    log("📌 脚本版本: %s" % SCRIPT_VER)
    if total:
        log("🔢 账号数量: %d" % total)
    log(_ACC_RULE)
SCRIPT_VER = "2026-11-20a"
_ACC_RULE = "=" * 70
SUMMARY_SEP = "▶︎ " + "━" * 50 + " ◀︎"

def acc_banner(idx, total, ident=""):
    """打印账号分隔标识（开始）。"""
    log(_ACC_RULE)
    log("👤 账号 %d/%d%s" % (idx, total, (" ｜ " + str(ident)) if ident else ""))
    log(_ACC_RULE)

def acc_footer(idx, total):
    """打印账号分隔标识（结束）。"""
    log("🔚 账号 %d/%d 处理结束" % (idx, total))

def today_str():
    y, m, d = time.localtime()[:3]
    # 抓包里是 2026-9-2 格式，对齐返回（日期匹配 + 显示）
    return (
        str(y) + "-" + str(m) + "-" + str(d),
        str(y) + "-" + str(m).zfill(2) + "-" + str(d).zfill(2),
        time.strftime("%Y-%m-%d"),
    )

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


def mark_done_today(server: str, note: str = "", expire_info: Dict[str, Any] = None,
                    points_info: Dict[str, Any] = None) -> None:
    """给该账号打上「今日已完成」标记；顺带缓存本轮查到的最近一笔预过期积分。

    缓存 expireInfo 是为了让当天第二轮（账号被日打标跳过、不重新登录）也能
    在汇总下输出提醒，而不额外消耗 YYB 取码配额；跨天会随打标记录一起清理。
    pointsInfo 同理：缓存账户积分总览，跳过时直接读回打印。
    """
    cache = load_daily_done()
    today = _today()
    # 只保留当天的记录，跨天自动清理，避免文件无限增长
    cache = {k: v for k, v in cache.items()
             if isinstance(v, dict) and str(v.get("date") or "") == today}
    cache[server] = {
        "date": today,
        "doneTime": now_text(),
        "note": str(note or ""),
        "expireInfo": expire_info if isinstance(expire_info, dict) else None,
        "pointsInfo": points_info if isinstance(points_info, dict) else None,
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


# 积分每日快照：按账号存每天最后一次的总积分，用于次日对比涨跌。
# 结构：{ "<账号key>": { "2026-11-17": 612.5, "2026-11-18": 688.0 } }
POINTS_HISTORY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "cdf_points_history.json")


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


# ———————————— 基础组件 ————————————
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
                yield server, ref, line
            except ValueError:
                pass
    def get_code(self, server, ref):
        """取 wx.login code；仅「上游连接超时」自动重试。

        连接层超时是纯连接层故障（上游长连接节点 8 秒内不响应，返回 502
        且响应体无上游错误码）。YYB-GO 的编队转移清单不含连接层超时，
        不会自动换成员；但它每次新请求会把请求轮转到编队的下一个成员，
        重试正好能命中可用成员。

        熔断（429）、账号失效（404）、上游拒绝（-14 等）一律不重试。
        """
        url = server + "/wxapp/getCode"
        for attempt in range(YYB_RETRY_TIMES + 1):
            try:
                s = requests.Session(); s.trust_env = False
                r = s.post(url,
                           json={"ref": ref, "app_id": self.appid}, timeout=(10, 90))
            except requests.RequestException as exc:
                # 网络层异常（连接被拒 / 读取超时等）同属连接层故障，可重试
                if attempt >= YYB_RETRY_TIMES:
                    return None, "YYB 请求异常：%s" % exc
                log("⏳ [重试] getCode 第%d/%d次失败（上游连接超时），%d秒后重试"
                    % (attempt + 1, YYB_RETRY_TIMES + 1, YYB_RETRY_DELAY))
                time.sleep(YYB_RETRY_DELAY)
                continue

            try:
                body = r.json()
            except ValueError:
                body = None

            if r.status_code >= 400:
                detail = _yyb_error_detail(body)
                if detail != "上游连接超时" or attempt >= YYB_RETRY_TIMES:
                    return None, ("HTTP %d%s" % (r.status_code,
                                  ("（%s）" % detail) if detail else ""))
                log("⏳ [重试] getCode 第%d/%d次失败（%s），%d秒后重试"
                    % (attempt + 1, YYB_RETRY_TIMES + 1, detail, YYB_RETRY_DELAY))
                time.sleep(YYB_RETRY_DELAY)
                continue

            if not isinstance(body, dict):
                return None, "响应不是 JSON"

            if isinstance(body.get("code"), int) and body["code"] not in (0, None):
                return None, str(body.get("msg") or body)
            data = body.get("data") or {}
            result = body.get("result") or (isinstance(data, dict) and data.get("result")) or {}
            code = (isinstance(result, dict) and result.get("code")) or None
            if not code:
                return None, "返回空 code"
            openid = (isinstance(data, dict) and data.get("openid")) or None
            return code, openid
        return None, "YYB 取码失败：重试次数已用尽"

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

def get_nearest_expire_points(token):
    """查询「最近一笔」预过期积分（纯只读接口）。

    返回 {"date": "YYYY-MM-DD", "amount": float, "days": int}；
    注：返回的 amount 是「该到期日分组」的明细金额，仅供对照；
    正式提醒金额在调用方统一改写为「账户总积分」（balance）。
    接口不可用 / 无明细 / 日期无法解析时返回 None，只记一行 WARN，不影响签到。
    """
    h = _common_headers(token, page=EXPIRE_PAGE_URL)
    h["content-type"] = "application/x-www-form-urlencoded"
    try:
        # 真机抓包：Content-Length: 0，无请求体、无 query 参数
        r = requests.post(EXPIRE_POINTS_URL, data="", headers=h, timeout=(10, 30))
        jr = _parse(r)
    except Exception as exc:
        warn("⚠️ [预过期积分] 查询失败: " + str(exc)[:120])
        return None
    rows = jr.get("data") if isinstance(jr, dict) else None
    if not isinstance(rows, list) or not rows:
        code = jr.get("code") if isinstance(jr, dict) else "?"
        warn("⚠️ [预过期积分] 接口未返回明细（code=%s）" % code)
        return None
    parsed = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        ds = str(row.get("expireDate") or "")[:10]
        try:
            d = datetime.strptime(ds, "%Y-%m-%d").date()
            amount = float(row.get("balanceUnit") or 0)
        except (ValueError, TypeError):
            continue
        parsed.append((d, amount))
    if not parsed:
        warn("⚠️ [预过期积分] 明细日期无法解析")
        return None
    # 「最近一笔」= 到期日最早的那笔；同一到期日若有多条则合并金额
    nearest = min(d for d, _ in parsed)
    amount = sum(a for d, a in parsed if d == nearest)
    return {
        "date": nearest.strftime("%Y-%m-%d"),
        "amount": amount,
        "days": (nearest - datetime.now().date()).days,
    }


def get_points_info(token):
    """查询账户积分总览（纯只读接口）。

    返回 {"balance", "expireBalance", "totalAdd", "totalConsume"}（均为 float）；
    接口不可用 / 响应缺字段时返回 None，只记一行 WARN，不影响签到结果。
    """
    h = _common_headers(token, page=USER_INFO_PAGE_URL)
    h["content-type"] = "application/x-www-form-urlencoded"
    try:
        # 真机抓包：Content-Length: 0，无请求体、无 query 参数
        r = requests.post(USER_INFO_URL, data="", headers=h, timeout=(10, 30))
        jr = _parse(r)
    except Exception as exc:
        warn("⚠️ [积分] 查询失败: " + str(exc)[:120])
        return None
    if not isinstance(jr, dict):
        warn("⚠️ [积分] 响应非 JSON")
        return None
    ok, _, _ = _ok(jr)
    data = jr.get("data") if isinstance(jr.get("data"), dict) else None
    if not ok or not isinstance(data, dict):
        warn("⚠️ [积分] 接口未返回数据（code=%s）" % jr.get("code"))
        return None
    p = data.get("points")
    if not isinstance(p, dict):
        warn("⚠️ [积分] 响应里没有 points 字段")
        return None

    def _num(v):
        try:
            return float(str(v).replace(",", "").strip())
        except (TypeError, ValueError):
            return 0.0

    return {
        "balance": _num(p.get("balance")),
        "expireBalance": _num(p.get("expireBalance")),
        "totalAdd": _num(p.get("totalAdd")),
        "totalConsume": _num(p.get("totalConsume")),
    }


def is_expire_alert(info) -> bool:
    """最近一笔到期日距今 1~EXPIRE_WARN_DAYS 天才提醒。

    提醒窗口整体前移一天：从「到期日-7天」开始，到「到期日-1天」结束，
    到期当天（days=0）不再提醒。这样窗口不会跨年，便于按年算出检测时间。

    days 为 None = 没拿到到期日（服务端 expireBalance > 0 但明细接口无数据），
    这种情况下只要有余额就提醒。
    """
    if not isinstance(info, dict):
        return False
    days = info.get("days")
    if days is None:
        try:
            return float(info.get("amount") or 0) > 0
        except (TypeError, ValueError):
            return False
    try:
        days = int(days)
    except (ValueError, TypeError):
        return False
    # days=0 是到期当天（不提醒）；1~7 天才提醒
    return 1 <= days <= EXPIRE_WARN_DAYS


def expire_alert_line(info: Dict[str, Any], idx: int) -> str:
    """按约定格式生成提醒行：💰 积分(10.00)即将过期： 2029-03-31（账号1）"""
    amount = float(info.get("amount") or 0)
    date = str(info.get("date") or "")
    if date:
        return "💰 积分(%.2f)即将过期： %s（账号%d）" % (amount, date, idx)
    return "💰 积分(%.2f)即将过期： 到期日未知（账号%d）" % (amount, idx)


def ensure_token(server, ref):
    """每次都强制重新取码 + 登录，返回 (token, 状态说明)；失败返回 (None, 原因)。"""
    code, err_msg = YYBClient(APP_ID).get_code(server, ref)
    if not code:
        return None, "YYB取码失败: " + str(err_msg)
    token, err_msg = login_with_code(code)
    if not token:
        return None, "登录失败: " + str(err_msg)
    return token, "新登录"

def _status_emoji_and_tag(status):
    return {
        "成功":   ("✅", "签到成功"),
        "已签":   ("🟡", "今日已签"),
        "失败":   ("❌", "签到失败"),
        "异常":   ("⚠️", "请求异常"),
    }.get(status, ("❔", status))

def run_account(server, ref):
    token, status_msg = ensure_token(server, ref)
    if not token:
        brief = " ".join(str(status_msg).split())[:80]
        err("❌ 账号不可用 · 原因: " + brief)
        return False, "ref " + str(ref) + ": ❌ " + brief, "", None, None
    # 登录状态一行简注（每次都是新登录）
    log("🔑 登录态 · " + status_msg)
    t_short, t_long, _ = today_str()
    before, txt = get_today_done(token)
    sym1 = "✅" if before else "⭕"
    log("📊 签到前 · 今日(" + t_short + ") " + sym1
        + ("  signText=" + str(txt) if txt is not None else ""))
    if before:
        # 2026-11-17a：签到前状态接口明确今日已签 → 跳过 do_sign 写请求，避免重复提交。
        # signIn 字段可能是 True 或 1，统一用 truthy 判断；查询失败返回 None、未签返回 0/False
        # 都不跳过，仍走原签到逻辑（宁可服务端返回"已签"，也不漏签）。
        status, msg = "已签", "签到前状态确认今日已签，跳过签到请求"
        log("🟡 今日已签  ──  " + msg + "，本次不重复提交")
    else:
        status, msg, extra = do_sign(token)
        emoji, tag = _status_emoji_and_tag(status)
        line = emoji + " " + tag + "  ──  服务端 msg: " + (msg or "")
        if status in ("成功", "已签"):
            log(line)
        else:
            err(line)
        for one in extra:
            log("🎁 " + one)
        after, txt2 = get_today_done(token)
        sym2 = "✅" if after else "⭕"
        log("📊 签到后 · 今日(" + t_short + ") " + sym2
            + ("  signText=" + str(txt2) if txt2 is not None else ""))
        # 如果之前没签 / 之后签了，加一行 🎉 高亮
        if before is False and after is True:
            log("🎉 本账号首次签到成功！")
    emoji, tag = _status_emoji_and_tag(status)
    # 预过期积分只在本账号已拿到 token 时查一次；失败只 WARN，不改签到结果
    expire_info = get_nearest_expire_points(token)
    # 账户积分总览（总积分 / 即将过期 / 累计获得 / 累计消耗）；失败只 WARN
    points_info = get_points_info(token)
    if points_info:
        # 统计口径（用户约定）：即将过期积分直接套用账户现有总积分，
        # 不再拆到期日分组统计（整池三年清空、到期前手动消费的模式下，
        # 提醒量就是全部待消费积分）；明细接口只用来定「最近一笔到期日」。
        total_balance = points_info.get("balance", 0)
        if expire_info:
            detail_amount = expire_info.get("amount", 0)
            expire_info["amount"] = total_balance
            expire_info["detailAmount"] = detail_amount
            log("ℹ️ [预过期积分] 最近到期日 %s，统计口径=账户总积分 %.2f"
                "（该批明细 %.2f）"
                % (str(expire_info.get("date") or "未知"), total_balance, detail_amount))
        elif points_info.get("expireBalance", 0) > 0:
            # 明细接口没给出任何条目、但服务端 expireBalance > 0 时，
            # 同样用账户总积分统计（只缺到期日，有余额就提醒）
            expire_info = {"date": "", "amount": total_balance, "days": None,
                           "detailAmount": points_info.get("expireBalance", 0)}
            warn("⚠️ [预过期积分] 明细接口无数据，改用账户总积分 %.2f 提醒"
                 % total_balance)
    return True, "ref " + str(ref) + ": " + emoji + " " + tag, status, expire_info, points_info

def main():
    entries = list(YYBClient(APP_ID).entries())
    # 按 YYB_ONLY_REFS 序号白名单筛选（1 起）；留空 [] 则运行全部账号
    if YYB_ONLY_REFS:
        wanted = set(int(x) for x in YYB_ONLY_REFS if str(x).strip().isdigit() and int(x) > 0)
        if wanted:
            entries = [e for i, e in enumerate(entries, 1) if i in wanted]
            log("ℹ️ 按 YYB_ONLY_REFS 筛选：请求序号 %s，命中 %d 个账号" % (sorted(wanted), len(entries)))
    total = len(entries)
    log_startup(total)
    tday, _, _ = today_str()

    log("🚀 中免会员小程序 每日签到开始 · 日期 " + tday + " · 共 " + str(total) + " 个账号")

    if not entries:
        err("🚫 没有可执行账号（青龙环境变量 YYB_SERVER 空，或被 YYB_ONLY_REFS 过滤空）")
        return 1

    success = 0
    skipped = 0
    expire_alerts = []   # [(账号序号, 最近一笔预过期积分信息)]
    points_rows = []     # [(账号序号, 总积分)]
    for i, (server, ref, _) in enumerate(entries, start=1):
        # 账号标头无条件打印：与影视飓风模板一致，打标跳过的账号也要有标题块
        acc_banner(i, total, "标识：" + str(ref))
        # 今日已成功签到的账号直接跳过，不再消耗 YYB 取码配额
        key = "%s@%s" % (server, ref)
        if is_done_today(key):
            log(f"⏭️ [打标] 账号 {i}/{total} 今日任务已完成，跳过")
            skipped += 1
            # 当天第二轮被跳过时，从打标缓存读回上一轮查到的结果，不重新登录取码
            cached = load_daily_done().get(key)
            info = cached.get("expireInfo") if isinstance(cached, dict) else None
            if is_expire_alert(info):
                expire_alerts.append((i, info))
            pinfo = cached.get("pointsInfo") if isinstance(cached, dict) else None
            if isinstance(pinfo, dict) and pinfo:
                points_rows.append((i, float(pinfo.get("balance") or 0), key))
            continue
        try:
            ok, line, status, expire_info, points_info = run_account(server, ref)
            if ok:
                success += 1
            # 只有服务端明确返回「签到成功 / 今日已签到」才算今日完成
            if status in ("成功", "已签"):
                mark_done_today(key, line, expire_info, points_info)
            if is_expire_alert(expire_info):
                expire_alerts.append((i, expire_info))
            if isinstance(points_info, dict):
                _bal = float(points_info.get("balance") or 0)
                points_rows.append((i, _bal, key))
                # 真实跑完且拿到总积分时写入今日快照（跳过账号不重复写，沿用首次记录）
                record_points_snapshot(key, _bal)
        except Exception as e:
            err("💥 账号异常: " + str(e))
            err(traceback.format_exc())
        acc_footer(i, total)
        if i < total:
            wait = random.randint(3, 8)
            # 不打印休息，避免啰嗦；真卡住了用户能从执行计时看在等
            time.sleep(wait)

    fail = total - success - skipped
    _now = datetime.now()
    _week = "一二三四五六日"[_now.weekday()]
    log(SUMMARY_SEP)
    log("📂 汇总日志")
    log("📃 脚本名称：中免会员")
    log("📅 运行日期：%s  周%s" % (_now.strftime("%Y-%m-%d"), _week))
    log("📊 运行结果：✔️ 成功：%d 个；❌ 失败：%d 个；⏭️ 跳过：%d 个"
        % (success, fail, skipped))
    # 账户总积分：一行并列，账号之间用 "  |  " 分隔；括号内为较昨天最后一次记录的涨跌
    if points_rows:
        _cells = []
        for idx, bal, akey in points_rows:
            _delta = points_delta(akey, bal)
            _delta_text = ("(%+.2f)" % _delta) if _delta is not None else ""
            _cells.append("账号%d：%.2f%s" % (idx, bal, _delta_text))
        log("💰 总积分：" + "  |  ".join(_cells))
    # 预过期积分提醒（树状）：只在最近一笔到期日距今 ≤ EXPIRE_WARN_DAYS 天时输出
    if expire_alerts:
        log("⏳  即将过期积分：")
        for _j, (idx, info) in enumerate(expire_alerts):
            _branch = "└─" if _j == len(expire_alerts) - 1 else "├─"
            _amt = float(info.get("amount") or 0)
            _date = str(info.get("date") or "到期日未知")
            log("%s 账号%d %s %.2f（%s到期）" % (_branch, idx, "·" * 20, _amt, _date))
    log(SUMMARY_SEP)

    # 通知：有 ERROR 照旧推；检测到积分即将过期时，即使全成功也强制推一次汇总
    notify_summary = f"账号 {total} ｜ 成功 {success} ｜ 失败 {fail} ｜ 跳过 {skipped}"
    if points_rows:
        notify_summary += "\n" + " ｜ ".join(
            "账号%d 总积分 %.2f" % (idx, bal) for idx, bal, _k in points_rows)
    if expire_alerts:
        notify_summary += "\n" + "\n".join(
            expire_alert_line(info, idx) for idx, info in expire_alerts)
    flush_notify("中免会员", notify_summary, logger=log, force=bool(expire_alerts))
    # 退出码语义：有真实失败账号才算失败；全成功或全跳过为 0（与同批脚本一致）
    return 0 if fail == 0 else 1

if __name__ == "__main__":
    _sys.exit(main())
