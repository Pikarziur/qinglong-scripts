#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# =========================================================
# name:  漓泉啤酒生态营地
# version: 2026-11-20b
# cron: 24 5,15 * * *
# 积分有效期：未知
# 每月最后一天统一检测「即将过期积分」(getExpirePoints)，有数据则连续提醒 3 天
# =========================================================

# 任务流程：
#   1. 读取 YYB_SERVER 账号基座，按 YYB_ONLY_REFS 序号白名单筛选（1 起）
#   2. 调用 YYBGO 的 /wxapp/getCode 获取 wx.login code
#   3. 登录并进入小程序，完成每日签到 / 领券等任务
#   4. 输出汇总
#   5. 积分过期提醒：每月最后一天检测 getExpirePoints，有即将过期积分则连续提醒 3 天
# 可控参数：
#   YYB_SERVER      必填。格式「地址@ref#备注」，空格/换行/& 分隔
#   YYB_ONLY_REFS   账号序号白名单（1 起）。留空 [] 跑全部；填 [1,2] 只跑第 1、2 个账号
#   QL_DIR          可选。token 缓存目录，默认 /ql/data/config/yyb_token_caches
#
# 日志规范：[LEVEL] [LQPIJIU] message   （LEVEL: INFO / WARN / ERROR）
# =========================================================

YYB_ONLY_REFS = []  # 账号序号白名单（1 起），留空 [] 跑全部；例如 [1,3] 只跑第 1、3 个账号


import json
import os
import calendar
import random
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

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

_QN_SITE = "漓泉啤酒"
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
    _title = "📃 脚本名称：漓泉啤酒"
    if MINI_APP_ID:
        _title += " ｜" + MINI_APP_ID
    log(_title)
    log("📌 脚本版本: %s" % SCRIPT_VER)
    if total:
        log("🔢 账号数量: %d" % total)
    log(_ACC_RULE)
SCRIPT_VER = "2026-11-20b"
_ACC_RULE = "=" * 70
SUMMARY_SEP = "▶︎ " + "━" * 50 + " ◀︎"

# 积分每日快照：按账号存每天最后一次的总积分，用于次日对比涨跌。
POINTS_HISTORY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "liquanpijiu_points_history.json")


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


def acc_banner(idx, total, ident=""):
    """打印账号分隔标识（开始）。"""
    log(_ACC_RULE)
    log("👤 账号 %d/%d%s" % (idx, total, (" ｜ " + str(ident)) if ident else ""))
    log(_ACC_RULE)

def acc_footer(idx, total):
    """打印账号分隔标识（结束）。"""
    log("🔚 账号 %d/%d 处理结束" % (idx, total))

# 将脚本所在目录加入搜索路径（确保能找到 yyb.py 等同目录模块）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# ===== YYB-Go-Enhanced + QingLong standalone adapter =====
def _yyb_routes():
    routes = []
    for number, line in enumerate(os.getenv("YYB_SERVER", "").replace("&", " ").split(), 1):
        line = line.strip()
        if not line:
            continue
        if "@" not in line:
            raise RuntimeError(f"YYB_SERVER 第 {number} 行格式错误，应为 地址@账号标识")
        server, ref = line.rsplit("@", 1)
        if not server.strip() or not ref.strip():
            raise RuntimeError(f"YYB_SERVER 第 {number} 行格式错误，应为 地址@账号标识")
        server = server.strip().rstrip("/")
        if not server.lower().startswith(("http://", "https://")):
            server = "http://" + server
        routes.append({"server": server, "ref": ref.strip()})
    if not routes:
        raise RuntimeError("未配置 YYB_SERVER（地址@账号标识，支持换行/空格/& 分隔）")
    # 按 YYB_ONLY_REFS 序号白名单筛选（1 起）；留空 [] 则运行全部账号
    if YYB_ONLY_REFS:
        wanted = set(int(x) for x in YYB_ONLY_REFS if str(x).strip().isdigit() and int(x) > 0)
        if wanted:
            filtered = [r for i, r in enumerate(routes, 1) if i in wanted]
            log(f"ℹ️ [账号过滤] YYB_ONLY_REFS={YYB_ONLY_REFS} 命中 {len(filtered)}/{len(routes)} 个账号")
            return filtered
    return routes


def _yyb_clean_ref(value):
    text = str(value or "").split("#", 1)[0].strip()
    for prefix in ("wx:", "yyb:", "wmpf:", "syzs:"):
        if text.lower().startswith(prefix):
            return text[len(prefix):].strip()
    return text


def _yyb_route(identifier):
    routes = _yyb_routes()
    wanted = _yyb_clean_ref(identifier)
    for route in routes:
        if _yyb_clean_ref(route["ref"]) == wanted:
            return route
    if wanted.isdigit() and 0 < int(wanted) <= len(routes):
        return routes[int(wanted) - 1]
    if len(routes) == 1:
        return routes[0]
    raise RuntimeError(f"YYB_SERVER 中找不到账号标识：{wanted or '(空)'}")


# ===== YYB 取码重试（2026-11-15a）=====
# 502 多为 YYB-GO → 微信链路的上游 8 秒超时，不是本地限速；仅瞬时失败重试。
# 总尝试 4 次（含首次），间隔 5~7 秒，满足 YYB-GO 的 code_min_gap_ms=3000。
YYB_CODE_RETRY = 4
YYB_CODE_RETRY_DELAY = 5


def _yyb_retryable(exc: Exception) -> bool:
    """只重试瞬时失败；业务拒绝（403/400/409 等）立即失败，不浪费取码额度。"""
    import requests as _requests
    if isinstance(exc, _requests.HTTPError) and exc.response is not None:
        status = exc.response.status_code
        return status >= 500 or status == 429
    if isinstance(exc, (_requests.RequestException, ValueError)):
        return True
    text = str(exc).lower()
    if "未返回 data.result" in text:
        return True
    if "返回失败" in text:
        # body.code != 0：仅上游超时类文案重试，纯业务拒绝不重试
        return any(k in text for k in ("call failed", "deadline exceeded", "timeout",
                                       "shortlink", "bad gateway"))
    return False


def _yyb_call(endpoint, app_id, identifier):
    import requests as _requests
    route = _yyb_route(identifier)
    response = _requests.post(route["server"] + endpoint,
                              json={"ref": route["ref"], "app_id": app_id}, timeout=60)
    response.raise_for_status()
    body = response.json()
    if not isinstance(body, dict) or int(body.get("code", -1)) != 0:
        raise RuntimeError(f"{endpoint} 返回失败：{body.get('msg') or body.get('message') or body}")
    result = (body.get("data") or {}).get("result")
    if result is None:
        raise RuntimeError(f"{endpoint} 未返回 data.result")
    return result


class _YybClient:
    def get_online_accounts(self):
        return [{"id": x["ref"], "openid": x["ref"], "wxid": x["ref"],
                 "remark": f"YYB账号{i}", "status": "online", "login_type": "WX"}
                for i, x in enumerate(_yyb_routes(), 1)]

    def get_accounts(self, force_refresh=False):
        return self.get_online_accounts()


class _YybCompat:
    YYBClient = _YybClient

    @staticmethod
    def load_accounts(*_args, **_kwargs):
        return _YybClient().get_online_accounts()

    @staticmethod
    def resolve_accounts(env_name=""):
        return [x["ref"] for x in _yyb_routes()]

    @staticmethod
    def get_global_server_url():
        return _yyb_routes()[0]["server"]

    @staticmethod
    def get_single_code(app_id, identifier):
        total = max(1, min(YYB_CODE_RETRY, 6))
        for attempt in range(1, total + 1):
            try:
                result = _yyb_call("/wxapp/getCode", app_id, identifier)
                code = result if isinstance(result, str) else result.get("code")
                if not code:
                    raise RuntimeError("/wxapp/getCode 未返回 data.result.code")
                return str(code)
            except Exception as exc:
                if attempt >= total or not _yyb_retryable(exc):
                    raise
                delay = max(3, YYB_CODE_RETRY_DELAY) + random.uniform(0, 2)
                warn("⚠️ YYB-Go 取码失败（第 %d/%d 次）：%s，%.0fs 后重试"
                     % (attempt, total, str(exc)[:120], delay))
                time.sleep(delay)
        raise RuntimeError("/wxapp/getCode 取码失败：重试次数已用尽")

    @staticmethod
    def get_single_phone_code(app_id, identifier, login_type=None):
        result = _yyb_call("/wxapp/getPhoneNumber", app_id, identifier)
        return str(result if isinstance(result, str) else result.get("code") or "")

    @staticmethod
    def normalize_login_type(value):
        return str(value or "WX").upper()

    @staticmethod
    def login_type_label(value):
        return {"WX": "应用宝", "WMPF": "微信小程序", "SYZS": "手游助手"}.get(
            _YybCompat.normalize_login_type(value), "应用宝")

    @staticmethod
    def _cache_path(name):
        import pathlib
        safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in name)
        return pathlib.Path(os.getenv("QL_DIR", "/ql/data")) / "config" / "yyb_token_caches" / f"{safe}.json"

    @classmethod
    def _read_cache(cls, name):
        try:
            return json.loads(cls._cache_path(name).read_text(encoding="utf-8"))
        except Exception:
            return {}

    @classmethod
    def get_cached_token(cls, name, identifier, options=None):
        item = cls._read_cache(name).get(_yyb_clean_ref(identifier))
        if not item:
            return None
        max_age = int((options or {}).get("max_age_ms", 0))
        if max_age and int(time.time() * 1000) - int(item.get("_saved_at", 0)) > max_age:
            return None
        return item

    @classmethod
    def save_cached_token(cls, name, identifier, value):
        data = cls._read_cache(name)
        data[_yyb_clean_ref(identifier)] = dict(value, _saved_at=int(time.time() * 1000))
        path = cls._cache_path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            path.chmod(0o600)
        except OSError:
            pass

    @classmethod
    def remove_cached_token(cls, name, identifier):
        data = cls._read_cache(name)
        data.pop(_yyb_clean_ref(identifier), None)
        path = cls._cache_path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


yyb = _YybCompat()
# ===== adapter end =====


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
# 小程序 AppID —— 版本行要打印它，必须在 log_startup() 调用之前定义
MINI_APP_ID = "wx08dabaec2783f4e9"

log_startup()

if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ---------------------------------------------------------------------------
# 常量 (均来自小程序反编译源码, 非机密)
# ---------------------------------------------------------------------------
BASE_URL = "https://api.mbr.liquan.com/api"          # vendor.js api_HOST
SUCCESS_CODE = 200
SESSION_INVALID_CODE = 401                            # 拦截器: 清 token 并重新登录
SIGNED = "sign"                                       # taskSignDtoList[].signStatus

# 账号列表：自动从 yyb_go 同步存活账号（可用 lqpj / WX_ID 指定白名单）
ACCOUNT_REFS = yyb.resolve_accounts("lqpj")

TOKEN_CACHE_PATH = Path(__file__).with_name("liquanpijiu_token_cache.json")
# 日打标文件：今日已完成的账号当天再次运行时直接跳过（不消耗 YYB 取码配额）
DAILY_DONE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "liquanpijiu_daily_done.json")
# ===================== 积分过期提醒 =====================
# 每月最后一天检测 getExpirePoints；检测到有即将过期积分，则自检测当天起连续提醒 N 天。
ENABLE_EXPIRE_REMIND = True
EXPIRE_REMIND_DAYS = 3            # 有数据则连续提醒天数（含检测当天）
# 过期提醒状态文件：记录各账号的检测日期/积分值与提醒窗口，供跨天连续提醒
EXPIRE_STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "liquanpijiu_expire_state.json")
DEFAULT_UA = (
    "Mozilla/5.0 (Linux; Android 13; SM-G9910 Build/TP1A.220624.014) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Mobile Safari/537.36 "
    "MicroMessenger/8.0.49.2600(0x28003137) NetType/WIFI Language/zh_CN "
    "miniProgram/" + MINI_APP_ID
)

session = requests.Session()


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------
def bj_now():
    return datetime.now(timezone(timedelta(hours=8)))


def mask(value):
    if not value:
        return ""
    value = str(value)
    if len(value) <= 12:
        return value[:2] + "***"
    return f"{value[:6]}***{value[-4:]}"


def read_token_cache():
    try:
        if TOKEN_CACHE_PATH.exists():
            return json.loads(TOKEN_CACHE_PATH.read_text(encoding="utf-8")) or {}
    except Exception:
        pass
    return {}


def write_token_cache(cache):
    try:
        TOKEN_CACHE_PATH.write_text(
            json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        warn(f"⚠️ 写入token缓存失败: {e}")


def get_cached_session(openid):
    return read_token_cache().get(openid) or {}


def save_cached_session(openid, token, union_id):
    cache = read_token_cache()
    cache[openid] = {"token": token, "unionId": union_id,
                     "updatedAt": int(time.time())}
    write_token_cache(cache)


def remove_cached_session(openid):
    cache = read_token_cache()
    if openid in cache:
        del cache[openid]
        write_token_cache(cache)


# ---------------------------------------------------------------------------
# 日打标（今日完成标记）
# ---------------------------------------------------------------------------
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


def mark_done_today(server: str, note: str = "", total_points: Any = None) -> None:
    """给该账号打上「今日已完成」标记（同时缓存当天总积分，供跳过时读回）。"""
    cache = load_daily_done()
    today = _today()
    # 只保留当天的记录，跨天自动清理，避免文件无限增长
    cache = {k: v for k, v in cache.items()
             if isinstance(v, dict) and str(v.get("date") or "") == today}
    cache[server] = {
        "date": today,
        "doneTime": now_text(),
        "note": str(note or ""),
        "totalPoints": total_points if total_points is not None else None,
    }
    save_daily_done(cache)
    log(f"✅ [打标] 今日任务已完成，已记录（{today}）")


def total_points_from_done(server: str) -> Any:
    """从当天打标记录读回总积分，供本次被跳过的账号在汇总里显示。"""
    entry = load_daily_done().get(server)
    if not isinstance(entry, dict) or str(entry.get("date") or "") != _today():
        return None
    return entry.get("totalPoints")


def clear_daily_done(server: str = "") -> None:
    """清除指定账号（或全部）的日打标。"""
    cache = load_daily_done()
    if server:
        cache.pop(server, None)
    else:
        cache = {}
    save_daily_done(cache)


# ---------------------------------------------------------------------------
# yyb_go: 账号标识 -> wx.login code
# ---------------------------------------------------------------------------
def get_wx_code(openid):
    """从 yyb_go 获取一次性微信 code（openid 为账号标识）"""
    try:
        code = yyb.get_single_code(MINI_APP_ID, str(openid).split("#")[0].strip())
        if code:
            log("✅ [授权] code 获取成功")
            return str(code)
        err("❌ [授权] code 获取失败")
        return None
    except Exception as exc:
        err(f"❌ [授权] code 获取异常: {exc}")
        return None


# ---------------------------------------------------------------------------
# 漓泉会员 API
# ---------------------------------------------------------------------------
def api_headers(token=None):
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json;charset=UTF-8",
        "User-Agent": DEFAULT_UA,
        "Referer": f"https://servicewechat.com/{MINI_APP_ID}/0/page-frame.html",
    }
    if token:
        headers["Token"] = token            # 请求拦截器: headers.Token = 本地 token
    return headers


def api_get(path, token=None, params=None):
    resp = session.get(f"{BASE_URL}{path}", params=params or {},
                       headers=api_headers(token), timeout=30)
    resp.raise_for_status()
    return resp.json()


def api_post(path, body, token=None, extra_headers=None):
    headers = api_headers(token)
    if extra_headers:
        # 同名头按 HTTP 规范大小写不敏感：先移除旧的大小写变体，避免重复头
        for k in list(headers.keys()):
            if k.lower() in {n.lower() for n in extra_headers}:
                headers.pop(k, None)
        headers.update(extra_headers)
    resp = session.post(f"{BASE_URL}{path}",
                        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                        headers=headers, timeout=30)
    resp.raise_for_status()
    return resp.json()


def resp_code(resp):
    """拦截器口径: errcode 优先, 否则 code。"""
    node = resp if isinstance(resp, dict) else {}
    for key in ("errcode", "code"):
        v = node.get(key)
        if v is not None:
            try:
                return int(v)
            except (TypeError, ValueError):
                pass
    return -1


def resp_msg(resp):
    node = resp if isinstance(resp, dict) else {}
    for key in ("errmsg", "message", "msg"):
        v = node.get(key)
        if v:
            return str(v)
    data = node.get("data")
    if isinstance(data, dict) and data.get("error"):
        return str(data["error"])
    return ""


# ===== 风控拦截识别（2026-11-20b）=====
# 服务端要求"安全验证"= 人机/风控拦截。这类响应若伴随 401，旧逻辑会当成会话失效去
# 强制重登重试，反而反复触发风控。命中关键词时：跳过重登重试、直接失败，并在日志
# 明确标注，避免刷风控、也便于一眼区分是风控而非普通接口失败。
_RISK_VERIFY_KEYS = ("安全验证", "安全校验", "人机验证", "滑块验证", "请完成验证")


def is_risk_verify(resp):
    """响应文案含风控验证关键词 → True（此类不重试）。"""
    msg = resp_msg(resp)
    return bool(msg) and any(k in msg for k in _RISK_VERIFY_KEYS)


def risk_tag(resp):
    """命中风控时给失败日志加的醒目标注，否则空串。"""
    return "（风控拦截，已跳过重试避免刷风控）" if is_risk_verify(resp) else ""


# ---------------------------------------------------------------------------
# 登录 / 会话
# ---------------------------------------------------------------------------
def login(openid):
    """wx.login code -> wxLogin -> (token, unionId)。code 放在 URL 路径中。"""
    code = get_wx_code(openid)
    resp = api_get(f"/mbr/members/wxLogin/{code}", params={"appId": MINI_APP_ID})
    if resp_code(resp) != 0:
        raise RuntimeError(f"登录失败: {resp_msg(resp) or resp_code(resp)}")
    data = resp.get("data") or {}
    token = data.get("token")
    union_id = data.get("unionid") or (data.get("wxUserInfo") or {}).get("unionId")
    if not (token and data.get("b2cMemberId")):
        raise RuntimeError("登录响应缺少 token/b2cMemberId, 需在小程序内登录一次后重试")
    return token, union_id


def obtain_session(openid, index, force=False):
    if force:
        remove_cached_session(openid)
    else:
        cached = get_cached_session(openid)
        if cached.get("token"):
            log(f"🔑 账号 {index} 使用缓存 token: {mask(cached['token'])}")
            return cached["token"], cached.get("unionId")
    token, union_id = login(openid)
    save_cached_session(openid, token, union_id)
    log(f"🔑 账号 {index} 静默登录成功: {mask(token)}")
    return token, union_id


# ---------------------------------------------------------------------------
# 签到业务
# ---------------------------------------------------------------------------
def sign_state(token):
    return api_get("/b2c/member/sign/task/list", token=token)


def do_sign(token):
    return api_post("/b2c/member/sign/task", {}, token=token)


def points_balance(token, union_id):
    """仅读取积分余额用于上报, 失败返回 None (不影响签到结论)。"""
    if not union_id:
        return None
    try:
        resp = api_get("/b2c/member/pointsAndCouponCardNumAndShopCardInfo",
                       token=token, params={"unionId": union_id})
        if resp_code(resp) != SUCCESS_CODE:
            return None
        vo = (resp.get("data") or {}).get("MemberCouponShopPointsVo") or {}
        return vo.get("pointsNum")
    except Exception:
        return None


def sign_res(resp):
    data = resp.get("data") if isinstance(resp, dict) else None
    if isinstance(data, dict) and isinstance(data.get("signRes"), dict):
        return data["signRes"]
    return {}


def signed_today(res):
    """今日已签: signRes.sign 为真, 或今日日期行 signStatus == 'sign'。"""
    if res.get("sign") is True:
        return True
    today = bj_now().strftime("%Y-%m-%d")
    for row in res.get("taskSignDtoList") or []:
        if row.get("signTime") == today and row.get("signStatus") == SIGNED:
            return True
    return False


def fmt_num(value):
    """服务端 signNum 为浮点(1.0), 展示成整数更自然。"""
    try:
        f = float(value)
        return str(int(f)) if f == int(f) else str(f)
    except (TypeError, ValueError):
        return str(value)


def run_account(openid, index):
    """执行单账号签到；返回 (ok, expire_val, total_points)。

    expire_val: 仅在「每月最后一天」检测时有效，为即将过期积分数（None=未检测/查询失败）。
    total_points: 账户当前积分余额（总积分），取不到为 None。
    """
    log(f"👤 【账号 {index}】")

    token, union_id = obtain_session(openid, index)

    # 读取签到状态 (首个鉴权调用); 会话失效则强制重登重试一次
    # 风控拦截（安全验证等）即使带 401 也不重登 —— 重试只会刷风控，直接留给下游判失败
    state = sign_state(token)
    if resp_code(state) == SESSION_INVALID_CODE and not is_risk_verify(state):
        log(f"🔁 账号 {index} 会话失效, 重新登录...")
        token, union_id = obtain_session(openid, index, force=True)
        state = sign_state(token)

    # 每月最后一天: 检测即将过期积分 (只读接口, 与签到流程相互独立)
    expire_val = None
    if ENABLE_EXPIRE_REMIND and expire_check_day():
        expire_val = expire_points(token)
        if expire_val is not None:
            log(f"⏳ 账号 {index} 即将过期积分: {expire_val}")

    scode = resp_code(state)
    if scode != SUCCESS_CODE:
        msg = resp_msg(state) or f"获取签到状态失败 code={scode}"
        err(f"❌ 账号 {index} {msg}{risk_tag(state)}")
        return False, expire_val, None

    res = sign_res(state)

    # 幂等预检: 今日已签则不再提交
    if signed_today(res):
        msg = f"今日已签到, 无需重复 (本周已签 {fmt_num(res.get('signNum'))} 天)"
        log(f"🟡 账号 {index} {msg}")
        pts = points_balance(token, union_id)
        if pts is not None:
            log(f"💰 账号 {index} 当前积分: {pts}")
        return True, expire_val, pts

    # 执行一次签到
    # 风控拦截（安全验证等）即使带 401 也不重登重签 —— 重试只会刷风控，直接判失败
    result = do_sign(token)
    rcode = resp_code(result)
    if rcode == SESSION_INVALID_CODE and not is_risk_verify(result):
        log(f"🔁 账号 {index} 会话失效, 重新登录后重试签到...")
        token, union_id = obtain_session(openid, index, force=True)
        result = do_sign(token)
        rcode = resp_code(result)

    if rcode != SUCCESS_CODE:
        msg = resp_msg(result) or f"签到失败 code={rcode}"
        err(f"❌ 账号 {index} {msg}{risk_tag(result)}")
        return False, expire_val, None

    res = sign_res(result)
    if not signed_today(res) and res.get("sign") is not True:
        msg = resp_msg(result) or "签到接口返回成功但未标记已签, 请稍后重试"
        warn(f"⚠️ 账号 {index} {msg}")
        return False, expire_val, None

    msg = f"签到成功, 本周已签 {fmt_num(res.get('signNum'))} 天"
    log(f"🎉 账号 {index} {msg}")

    pts = points_balance(token, union_id)
    if pts is not None:
        log(f"💰 账号 {index} 当前积分: {pts}")
    return True, expire_val, pts


def expire_check_day(today=None) -> bool:
    """今天是否为每月最后一天（检测日）。"""
    d = today or bj_now().date()
    return d.day == calendar.monthrange(d.year, d.month)[1]


def expire_points(token) -> Optional[int]:
    """查询即将过期积分（getExpirePoints）；查询失败返回 None（不影响签到结论）。

    响应形如 {"errcode":0,"errmsg":"ok","data":0}，data 为即将过期的积分数。
    """
    try:
        # 该接口在抓包中使用小写 token 头（与 b2c 系的 Token 头不同），按原样对齐
        resp = api_post("/smhd/member/getExpirePoints", {}, token=token,
                        extra_headers={"token": token})
    except Exception as exc:
        warn(f"⚠️ 即将过期积分查询失败: {exc}")
        return None
    if not isinstance(resp, dict):
        return None
    code = resp.get("errcode")
    if code is not None:
        try:
            if int(code) != 0:
                return None
        except (TypeError, ValueError):
            return None
    value = resp.get("data")
    if value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None


def load_expire_state() -> Dict[str, Any]:
    """读取过期提醒状态文件；不存在或损坏时返回空字典。"""
    try:
        if os.path.exists(EXPIRE_STATE_FILE):
            with open(EXPIRE_STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
    except Exception as exc:
        warn(f"⚠️ [过期提醒] 状态读取失败: {exc}")
    return {}


def save_expire_state(cache: Dict[str, Any]) -> None:
    """写过期提醒状态文件（临时文件 + rename 原子替换，避免写坏）。"""
    try:
        tmp = EXPIRE_STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
        os.replace(tmp, EXPIRE_STATE_FILE)
    except Exception as exc:
        err(f"❌ [过期提醒] 状态保存失败: {exc}")


def update_expire_state(openid: str, expire_val: Optional[int]) -> None:
    """检测日拿到结果时刷新该账号的提醒窗口；查询失败(None)时保持原状不动。"""
    if expire_val is None:
        return
    cache = load_expire_state()
    today = bj_now().date()
    if expire_val <= 0:
        # 本次检测无即将过期积分 → 清掉旧窗口
        cache.pop(openid, None)
    else:
        entry = cache.get(openid)
        if not isinstance(entry, dict):
            entry = {}
        entry["checkDate"] = today.isoformat()
        entry["points"] = int(expire_val)
        entry["remindUntil"] = (today + timedelta(days=EXPIRE_REMIND_DAYS - 1)).isoformat()
        entry.setdefault("lastPushDate", "")
        cache[openid] = entry
    # 清理过期窗口（保留 30 天内的记录，避免文件无限增长）
    limit = (today - timedelta(days=30)).isoformat()
    for k in [k for k, v in cache.items()
              if not isinstance(v, dict) or str(v.get("remindUntil") or "") < limit]:
        cache.pop(k, None)
    save_expire_state(cache)


def expire_remind_hits() -> list:
    """返回当前处于提醒窗口内的账号列表 [(openid, points, lastPushDate)]。"""
    today = bj_now().date().isoformat()
    hits = []
    for openid, entry in (load_expire_state() or {}).items():
        if not isinstance(entry, dict):
            continue
        until = str(entry.get("remindUntil") or "")
        if not until or today > until:
            continue
        try:
            pts = int(entry.get("points") or 0)
        except (TypeError, ValueError):
            continue
        if pts <= 0:
            continue
        hits.append((openid, pts, str(entry.get("lastPushDate") or "")))
    return hits


def mark_expire_pushed(openid: str) -> None:
    """标记该账号今天已推送过过期提醒（保证每天最多一条通知）。"""
    cache = load_expire_state()
    entry = cache.get(openid)
    if isinstance(entry, dict):
        entry["lastPushDate"] = bj_now().date().isoformat()
        cache[openid] = entry
        save_expire_state(cache)


def notify_expire_alert(lines: list) -> bool:
    """过期提醒强推：独立于「只推 ERROR」的错误通知，命中即推。"""
    if not lines or _qn_send is None:
        return False
    if not any(os.getenv(_k) for _k in _QN_ENVS):
        print("[QL] 未配置通知渠道，过期提醒未推送（可在青龙「通知设置」或环境变量中配置）",
              flush=True)
        return False
    title = "【%s】积分即将过期提醒" % _QN_SITE
    body = "\n".join(lines)
    try:
        _qn_send(title, body)
        return True
    except Exception as _e:
        print("[QL] 过期提醒推送失败：%s" % _e, flush=True)
        return False


def check_expire_remind(hits: list, labels: Dict[str, str]) -> None:
    """提醒窗口内：汇总日志连续打印 + 强推（每天最多一条通知）。"""
    if not ENABLE_EXPIRE_REMIND or not hits:
        return
    today = bj_now().date().isoformat()
    # 汇总日志：窗口内每天都打印
    log(_ACC_RULE)
    log("⏳ [积分过期提醒] 检测到即将过期的积分（每月最后一天检测，请尽快使用）：")
    for openid, pts, _ in hits:
        log(f"   · {labels.get(openid) or openid}：{pts} 积分即将过期")
    log(_ACC_RULE)
    # 强推：仅推尚未推送过的账号，保证每天最多一条
    to_push = [h for h in hits if h[2] != today]
    if not to_push:
        return
    push_lines = ["检测到即将过期的积分（每月最后一天检测，请尽快使用）："]
    push_lines += [f"{labels.get(o) or o}：{p} 积分即将过期" for o, p, _ in to_push]
    if notify_expire_alert(push_lines):
        log("📣 [积分过期提醒] 已推送通知")
        for openid, _, _ in to_push:
            mark_expire_pushed(openid)


def main():
    entries = ACCOUNT_REFS

    if not entries:
        err("🚫 未从 yyb_go 获取到存活账号(可配置环境变量 lqpj 指定白名单), 退出。")
        return 1

    log("🚀 漓泉啤酒 签到开始")
    ok_count = 0
    skipped = 0
    # 过期提醒：检测日(每月最后一天)写入状态，后续 3 天内每天在汇总里提醒
    labels: Dict[str, str] = {}
    points_rows: List[tuple] = []   # [(账号序号, 标签, 总积分)]
    for i, entry in enumerate(entries, 1):
        parts = entry.split("#", 1)
        openid = parts[0].strip()
        remark = parts[1].strip() if len(parts) > 1 else ""
        labels[openid] = f"账号{i}({remark or openid})"
        # 今天已经跑完的账号直接跳过，不再消耗 YYB 取码配额
        if is_done_today(openid):
            log(f"⏭️ [打标] 账号 {i}/{len(entries)} 今日任务已完成，跳过")
            skipped += 1
            # 被跳过也要在汇总里显示总积分：从打标缓存读回，不重新登录取码
            cached_total = total_points_from_done(openid)
            if cached_total is not None:
                points_rows.append((i, labels[openid], cached_total, openid))
            continue
        acc_banner(i, len(entries), ("备注：" + remark) if remark else ("标识：" + openid))
        try:
            ok, expire_val, total_points = run_account(openid, i)
            ok_count += 1 if ok else 0
            # 检测日拿到结果 → 写入/清理该账号的过期提醒窗口
            update_expire_state(openid, expire_val)
            # 只有账号流程完整走完（ok=True）才算今日完成；异常路径不打标
            if ok:
                mark_done_today(openid, "签到完成", total_points)
                if total_points is not None:
                    points_rows.append((i, labels[openid], total_points, openid))
                    record_points_snapshot(openid, total_points)
        except Exception as e:
            err(f"❌ 账号 {i} 执行异常: {e}")
        acc_footer(i, len(entries))
        time.sleep(1)

    fail_count = len(entries) - ok_count - skipped
    _now = datetime.now()
    _week = "一二三四五六日"[_now.weekday()]
    log(SUMMARY_SEP)
    log("📂 汇总日志")
    log("📃 脚本名称：漓泉啤酒")
    log("📅 运行日期：%s  周%s" % (_now.strftime("%Y-%m-%d"), _week))
    log("📊 运行结果：✔️ 成功：%d 个；❌ 失败：%d 个；⏭️ 跳过：%d 个"
        % (ok_count, fail_count, skipped))
    _cells = []
    for idx, label, total, akey in points_rows:
        try:
            _dv = float(total)
        except (TypeError, ValueError):
            _dv = None
        if _dv is None:
            _cells.append("账号%d：%s" % (idx, total))
            continue
        _delta = points_delta(akey, _dv)
        _dt = ("(%+.2f)" % _delta) if _delta is not None else ""
        _cells.append("账号%d：%.2f%s" % (idx, _dv, _dt))
    if _cells:
        log("💰 总积分：" + "  |  ".join(_cells))
    log(SUMMARY_SEP)
    # 积分过期提醒：检测日写入的 3 天窗口内，每天汇总打印 + 强推（每天最多一条）
    try:
        check_expire_remind(expire_remind_hits(), labels)
    except Exception as e:
        warn(f"⚠️ 过期提醒处理异常: {e}")
    # 错误日志推送：有错误才发；未配置通知渠道则只提示一行
    _notify_tail = f"账号 {len(entries)} ｜ 成功 {ok_count} ｜ 失败 {fail_count} ｜ 跳过 {skipped}"
    _point_lines = [f"{label} 总积分 {total}" for _idx, label, total, _k in points_rows]
    if _point_lines:
        _notify_tail += "\n" + " ｜ ".join(_point_lines)
    flush_notify(
        "漓泉啤酒",
        _notify_tail,
        logger=log,
    )
    # 退出码语义：有真实失败账号（非跳过）才算失败；全成功/全跳过为 0
    return 0 if fail_count == 0 else 1



if __name__ == "__main__":
    sys.exit(main())
