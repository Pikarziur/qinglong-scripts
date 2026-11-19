#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# =========================================================
# name:  小紫有约
# version: 2026-11-19c
# cron: 36 6,16 * * *
# 站点：sxkyziqidonglai.cn（ycb SaaS 商城，公众号「小紫有约」渠道 WXjxriol8e8293wezu）
# =========================================================
#
# 任务流程：
#   1. 读取 YYB_SERVER 账号基座，按 YYB_ONLY_REFS 序号白名单筛选（1 起）
#   2. 调 YYBGO 的 /wx/h5code 取公众号网页授权 code（scope=snsapi_base，静默授权）
#   3. 用 code 调 /api/platform/weChatAuthLogin 换已登录 SESSION
#      （SESSION 落盘缓存：48h 内校验有效直接复用，免取码；失效才重新取码换，
#       取码失败时仍先试缓存兜底 —— 参考拼多多果园 pddgygzc 的 Token 复用机制）
#   4. 探测当月签到活动码 actCode（命中后缓存到本地，不必每月手改）
#   5. 查活动页签到状态 → 未签则调 /activity/launchByValidater 完成签到，
#      已签则跳过写请求（幂等，不重复提交）
#   6. 查账户积分总览（余额 / 累计 / 即将过期）与券列表，
#      写入每日积分快照供次日对比涨跌
#
# 可控参数：
#   XZY_SERVER      建议用这个独立变量，只放本脚本要跑的账号。
#                   格式「地址@ref」，多账号换行分隔；不配置时回退到 YYB_SERVER
#   YYB_SERVER      公共账号基座（其他脚本共用），本脚本仅在 XZY_SERVER 缺失时使用
#   YYB_ONLY_REFS   账号序号白名单（1 起）。留空 [] 跑全部；填 [1,2] 只跑第 1、2 个账号
#
# 维护点（两个前置条件，缺一不可）：
#   ① 微信必须已关注「小紫有约」公众号，否则 YYBGO 取码返回 -14
#   ② 微信必须已是商城会员，否则 weChatAuthLogin 返回 4002「客户未绑定手机号」，
#      需要先在微信里手动完成一次手机号绑定（带滑块，无法自动化）
#
# 日志规范：[LEVEL] message   （LEVEL: INFO / WARN / ERROR）
#
# =========================================================

YYB_ONLY_REFS = [3]   # 当前仅排查第 3 个账号；排查完成后恢复 [] 跑全部

# 直填登录态（可选，最高优先级）：环境变量 XZY_SESSION，每行一个账号，
#   格式 `[备注@]SESSION值`。配了就跳过 YYBGO 取码，直接用现成 SESSION 跑。
#   SESSION 有效期 48 小时，适合「取码被风控挡了但手里有 HAR 抓到的 SESSION」时兜底。
#   ⚠️ SESSION 是 base64 明文，base64 解出来就是 userId + channelCode + siteId，
#      等同于账号凭据，别贴到公开渠道。
# 例：
#   XZY_SESSION=我的号@JDE3MTk2Njc0JCRXWGp4cmlvbDhlODI5M3dlenUkJFNJVEVfMzMyNTQyNDI2...
import os, re, time, random, json, traceback
import requests
from datetime import datetime, timedelta
from typing import Any, Dict, Optional, Tuple

# ============== 新手配置区 ==============
HOST = "sxkyziqidonglai.cn"
SITE_ID = "SITE_33254242630091515087"      # 商城站点号
CHANNEL_CODE = "WXjxriol8e8293wezu"        # 公众号渠道码
WX_APP_ID = "wx3db193ecdebd3fea"           # 公众号 AppID（取码时用）
ACT_CODE = "SG26102"                       # 当月签到活动码（2026-11 = 每日签到（11月））
ACT_CODE_FALLBACKS = ["SG26102"]           # 探测用候选（按顺序试，命中即缓存）
OAUTH_SCOPE = "snsapi_base"                # 静默授权；snsapi_userinfo 会取不到 code
UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 16_1_2 like Mac OS X) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) MicroMessenger/8.0.75(0x18004b66) NetType/WIFI Language/zh_CN")
# ========================================

SCRIPT_VER = "2026-11-19c"
SITE_NAME = "小紫有约"

BASE = "https://" + HOST
API_IS_LOGIN     = "/api/platform/isLogin"
API_AUTH_LOGIN   = "/api/platform/weChatAuthLogin"
API_LAUNCH_PAGE  = "/api/mobile/activity-v2/activity/launchPage"
API_SIGN         = "/api/mobile/activity-v2/activity/launchByValidater"
API_BALANCE      = "/api/mobile/eShop/voucher/balance"
API_COUPON_LIST  = "/api/mobile/eShop/cardPacks/getCouponList"
COUPON_ATTR = "KMQ,ZCQ,OTOQ,YYZLKQ,SHYHQ"

# 授权回调地址：必须简洁，只带 channelCode + siteId。
# 多带 param / registSource / wxSnapShot 会让 YYBGO 解析回调失败。
REDIRECT_URI = ("https://" + HOST + "/auth/wxAutomatic"
                "?channelCode=" + CHANNEL_CODE + "&siteId=" + SITE_ID)

# ——— YYB 取码重试（连接层故障可重试，授权拒绝不重试）——————————————————
# 现象：YYB-GO 频繁返回 HTTP 502，msg 为 "call failed: context deadline exceeded"
#       或 "all ShortLink targets failed ... i/o timeout"（上游长连接节点 8 秒内不响应），
#       属纯连接层故障，拿不到上游错误码。
# H5 OAuth 已到上游后的明确拒绝（如 manual_auth -10 / shortlink -14）不重发，
# 避免重复触发授权恢复；只对没有上游业务拒绝码的连接/超时故障重试。
YYB_RETRY_TIMES = int(os.getenv("XZY_YYB_RETRY", "5") or 5)        # 额外重试次数（不含首次）
YYB_RETRY_DELAY = int(os.getenv("XZY_YYB_RETRY_DELAY", "4") or 4)  # 重试间隔秒数
YYB_CODE_TIMEOUT = 180     # h5code 单次超时（微信授权偶发较慢）
HTTP_TIMEOUT = (10, 30)    # 业务接口 (连接, 读取)

# ———————————— 错误通知（可选项，想用则用）————————————
# 只推错误：本次运行出现 ERROR 日志才推送一次；正常跑完不打扰。
# 直接调用青龙自带的通知模块（容器内为 /ql/data/scripts/notify.py，仓库内为同目录/上一级的 notify.py）——
#   在青龙面板「通知设置」里配一次即可全站通用。
# 找不到该文件、或未配置任何通知渠道时，只在日志末尾提示一行，不报错、不中断。
import os as _os
import sys as _sys

_QN_SITE = SITE_NAME
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
_ACC_RULE = "=" * 70
SUMMARY_SEP = "▶︎ " + "━" * 50 + " ◀︎"


def now_text() -> str:
    # 兼容两种导入: from datetime import datetime / import datetime
    _dt = datetime.now() if hasattr(datetime, "now") else datetime.datetime.now()
    return _dt.strftime("%Y-%m-%d %H:%M:%S")


# ========== 启动信息 ==========
def log_startup(total: int = 0) -> None:
    log(_ACC_RULE)
    log("📃 脚本名称：%s ｜%s" % (SITE_NAME, WX_APP_ID))
    log("📌 脚本版本: %s" % SCRIPT_VER)
    if total:
        log("🔢 账号数量: %d" % total)
    log(_ACC_RULE)


def acc_banner(idx, total, ident=""):
    """打印账号分隔标识（开始）。"""
    log(_ACC_RULE)
    log("👤 账号 %d/%d%s" % (idx, total, (" ｜ " + str(ident)) if ident else ""))
    log(_ACC_RULE)


def acc_footer(idx, total):
    """打印账号分隔标识（结束）。"""
    log("🔚 账号 %d/%d 处理结束" % (idx, total))


def today_str() -> str:
    return time.strftime("%Y-%m-%d")


# ====================== 日打标（今日完成标记） ======================
def _today() -> str:
    """当天日期，用于跨天判定。"""
    return datetime.now().strftime("%Y-%m-%d")


DAILY_DONE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "xzyy_daily_done.json")


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


def mark_done_today(server: str, note: str = "",
                    points_info: Optional[Dict[str, Any]] = None) -> None:
    """给该账号打上「今日已完成」标记；顺带缓存本轮查到的积分总览。

    缓存 pointsInfo 是为了让当天第二轮（账号被日打标跳过、不重新取码）也能
    在汇总下输出总积分，而不额外消耗 YYB 取码配额；跨天会随打标记录一起清理。
    """
    cache = load_daily_done()
    today = _today()
    cache = {k: v for k, v in cache.items()
             if isinstance(v, dict) and str(v.get("date") or "") == today}
    cache[server] = {
        "date": today,
        "doneTime": now_text(),
        "note": str(note or ""),
        "pointsInfo": points_info if isinstance(points_info, dict) else None,
    }
    save_daily_done(cache)
    log(f"✅ [打标] 今日任务已完成，已记录（{today}）")


# ====================== SESSION 缓存复用（参考 pddgygzc） ======================
# 换到的 SESSION 落盘保存，48h 内先校验复用，避免每次运行都依赖 YYBGO 取码。
# 结构：{ "<账号key>": { "session": "xxx", "uid": "xxx", "savedAt": "2026-11-19 09:00:00",
#                        "ts": 1763519xxx } }
SESSION_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  "xzyy_session_cache.json")
SESSION_MAX_AGE = 40 * 3600   # 缓存最长复用 40h（SESSION 标称 48h，留 8h 安全余量）


def load_session_cache() -> Dict[str, Any]:
    """读取 SESSION 缓存文件；不存在或损坏时返回空字典。"""
    try:
        if os.path.exists(SESSION_CACHE_FILE):
            with open(SESSION_CACHE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
    except Exception as exc:
        warn(f"⚠️ [会话缓存] 读取失败: {exc}")
    return {}


def save_session_cache(cache: Dict[str, Any]) -> None:
    """写 SESSION 缓存（临时文件 + rename 原子替换）。"""
    try:
        tmp = SESSION_CACHE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
        os.replace(tmp, SESSION_CACHE_FILE)
    except Exception as exc:
        err(f"❌ [会话缓存] 保存失败: {exc}")


def get_cached_session(key: str) -> str:
    """取未过期的缓存 SESSION；无缓存/已过期/损坏则返回空串。"""
    entry = load_session_cache().get(key)
    if not isinstance(entry, dict):
        return ""
    val = str(entry.get("session") or "")
    if not val:
        return ""
    ts = entry.get("ts")
    try:
        if time.time() - float(ts) > SESSION_MAX_AGE:
            log("🗑️ [会话缓存] 缓存已超过 %dh，弃用" % (SESSION_MAX_AGE // 3600))
            return ""
    except (TypeError, ValueError):
        return ""
    return val


def get_cached_session_any(key: str) -> str:
    """取缓存 SESSION，不检查时效（供取码失败时兜底用）。

    缓存标记 40h 只是「主动弃用」阈值；SESSION 标称 48h，超 40h 未必真失效。
    取码失败时仍值得拿它试一次，由服务端 check_login 最终判定。
    """
    entry = load_session_cache().get(key)
    if not isinstance(entry, dict):
        return ""
    return str(entry.get("session") or "")


def put_cached_session(key: str, session_val: str, uid: str = "") -> None:
    """保存新换到的 SESSION 到缓存。"""
    if not session_val:
        return
    cache = load_session_cache()
    cache[key] = {
        "session": session_val,
        "uid": str(uid or ""),
        "savedAt": now_text(),
        "ts": time.time(),
    }
    save_session_cache(cache)
    log("💾 [会话缓存] 已保存（%s）" % now_text())


# 积分每日快照：按账号存每天最后一次的总积分，用于次日对比涨跌。
# 结构：{ "<账号key>": { "2026-11-17": 5.0, "2026-11-18": 5.0 } }
POINTS_HISTORY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "xzyy_points_history.json")


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


# ———————————— 活动码缓存 ————————————
# actCode 按月变（SG26102 = 2026 年 11 月）。探测成功后缓存下来，
# 次月失效时脚本会自动再探一次，不必每月手改硬编码常量。
ACT_CODE_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "xzyy_act_code_cache.json")
ACT_CODE_CACHE_DAYS = 40


def load_act_code_cache() -> Dict[str, Any]:
    try:
        if os.path.exists(ACT_CODE_CACHE_FILE):
            with open(ACT_CODE_CACHE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
    except Exception as exc:
        warn(f"⚠️ [活动码] 缓存读取失败: {exc}")
    return {}


def save_act_code_cache(act_code: str, activity_name: str) -> None:
    try:
        tmp = ACT_CODE_CACHE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"actCode": act_code, "activityName": activity_name,
                       "savedAt": _today(), "savedAtTime": now_text()},
                      f, ensure_ascii=False, indent=2)
        os.replace(tmp, ACT_CODE_CACHE_FILE)
    except Exception as exc:
        warn(f"⚠️ [活动码] 缓存保存失败: {exc}")


def _cache_act_code_valid(cache: Dict[str, Any]) -> bool:
    """缓存的活动码是否还在有效期内（按保存日期粗略算，超过 N 天重新探测）。"""
    saved = str(cache.get("savedAt") or "")
    if not saved:
        return False
    try:
        dt = datetime.strptime(saved, "%Y-%m-%d")
    except ValueError:
        return False
    return (datetime.now() - dt).days < ACT_CODE_CACHE_DAYS


# ———————————— 业务请求封装 ————————————
def _make_session() -> requests.Session:
    sess = requests.Session()
    # 商城站点可能被代理干扰，业务请求不走环境代理；YYBGO 是内网地址更是必须直连。
    sess.trust_env = False
    sess.headers.update({
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
    })
    return sess


def _post_json(sess, path, payload):
    """POST JSON。返回 (status_code, parsed_json 或 None, error_str)"""
    try:
        r = sess.post(BASE + path, json=payload, timeout=HTTP_TIMEOUT)
    except requests.RequestException as exc:
        return None, None, "请求异常: %s" % exc
    try:
        return r.status_code, r.json(), ""
    except ValueError:
        return r.status_code, None, "响应非 JSON（HTTP %s）" % r.status_code


def _post_form(sess, path, payload):
    """POST form-urlencoded。返回 (status_code, parsed_json 或 None, error_str)"""
    try:
        r = sess.post(BASE + path, data=payload, timeout=HTTP_TIMEOUT)
    except requests.RequestException as exc:
        return None, None, "请求异常: %s" % exc
    try:
        return r.status_code, r.json(), ""
    except ValueError:
        return r.status_code, None, "响应非 JSON（HTTP %s）" % r.status_code


def _api_ok(jr) -> bool:
    return isinstance(jr, dict) and jr.get("success") is True


def _api_msg(jr) -> str:
    if not isinstance(jr, dict):
        return ""
    return str(jr.get("msg") or jr.get("message") or "").strip()


def _api_status(jr) -> Any:
    if not isinstance(jr, dict):
        return None
    return jr.get("status")


# ———————————— YYBGO 取码 ————————————
def _yyb_error_detail(body) -> str:
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
        stages = []
        for item in upstream:
            if not isinstance(item, dict):
                continue
            stage = item.get("stage")
            if stage and str(stage) not in stages:
                stages.append(str(stage))
            # ⚠️ 2026-11-18 修正：YYB-GO 的 /wx/h5code 错误体里，
            #    upstream_errors 的元素字段名是 **upstream_code**（不是 code），
            #    例如 {"stage":"shortlink_application","upstream_code":-14}。
            #    原来只读 item["code"] → 取不到码 → 落到兜底的「上游拒绝」，
            #    丢掉了结构化错误码这个可执行信息。
            #    两个字段名都兼容，避免服务端换版本后又失效。
            for key in ("upstream_code", "code"):
                val = item.get(key)
                if val is not None:
                    codes.append(str(val))
                    break
        if codes:
            joined = "/".join(codes)
            stage_text = ("阶段 %s，" % "/".join(stages)) if stages else ""
            # ⚠️ 2026-11-18 再修正：**-14 不是「该微信未关注公众号」**。
            #    实证（YYB-GO system_logs）：20:42:39 acct=4 拒绝(-14)，
            #    20:42:46 acct=5 成功、20:42:51 acct=9 也成功 —— 同一 app_id、
            #    同一时间窗口内失败与成功交替出现，属账号/节点级的临时拒绝
            #    （YYB-GO 有编队 failover，换个成员就能成），与关注状态无关。
            if "-14" in codes or "14" in codes:
                # 实测原文明细：ShortLink response failed (authorization not replayed):
                #   shortlink application rejected: code=-14
                # → 语义是「该微信的授权无法重放」（登录态/授权凭据失效，或被临时风控），
                #   与「是否关注公众号」无关。持续失败需在 YYB-GO 里重新扫码登录该微信号。
                hint = ("；YYB-GO 标记需重新授权" if data.get("rescan_required") else "")
                return ("上游拒绝（%s-14，该微信授权无法重放%s；请核对 YYB-GO 中该 ref "
                        "是否对应刚扫码的账号）" % (stage_text, hint))
            if "10009" in codes:
                return "上游拒绝（%s10009，微信侧授权频率限制）" % stage_text
            hint = ("；YYB-GO 标记需重新授权" if data.get("rescan_required") else "")
            if "manual_auth" in stages and "-10" in codes:
                return ("上游拒绝（%smanual_auth -10，YYB-GO 的 ManualAuth 授权凭据被拒绝%s；"
                        "请核对该 ref 是否为刚扫码的账号，必要时在 YYB-GO 删除旧条目后重新扫码）"
                        % (stage_text, hint))
            return "上游拒绝（%s错误码 %s%s）" % (stage_text, joined, hint)
        return "上游拒绝"

    # 本地熔断 / 限速
    if data.get("circuit_open"):
        return "服务端熔断中"
    if "circuit" in low or "熔断" in msg:
        return "服务端熔断中"

    # 连接层超时（无上游错误码 + 8 秒 deadline）
    if ("context deadline exceeded" in low or "i/o timeout" in low
            or "timeout" in low):
        return "上游连接超时"
    if "not found" in low:
        return "账号不存在"
    return ""


def _yyb_retryable(detail: str) -> bool:
    """只重试没有上游业务拒绝码的连接超时，不重发 OAuth 业务拒绝。"""
    return "上游连接超时" in (detail or "")


def load_direct_sessions():
    """解析环境变量 XZY_SESSION 里直填的登录态。

    格式：每行一个账号，`[备注@]SESSION值`；`@` 前面的部分只作账号标识（缺省用序号）。
    留空 / 只有注释行 → 返回空列表，脚本回退到 YYBGO 取码模式。

    返回 [(标识, SESSION值), ...]
    """
    out = []
    raw = os.getenv("XZY_SESSION") or ""
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        ident, sep, val = line.rpartition("@")
        val = val.strip()
        if not sep or not val:
            # 没有 @ 分隔：整行就是 SESSION，标识用序号
            val, ident = line, ""
        out.append((ident.strip() or ("直填%d" % (len(out) + 1)), val))
    return out


class YYBClient:
    """YYB-Go-Enhanced 客户端：只负责取公众号网页授权 code。"""
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
        """读取账号基座。

        优先用独立变量 XZY_SERVER（只放本脚本要跑的账号），没配时回退到
        公共变量 YYB_SERVER。两者都不配则无账号可跑。
        """
        raw = os.getenv("XZY_SERVER") or os.getenv("YYB_SERVER") or ""
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                server, ref = self.parse_entry(line)
                yield server, ref, line
            except ValueError:
                pass

    @staticmethod
    def env_source() -> str:
        """当前用的是哪个环境变量（仅用于日志展示）。"""
        if os.getenv("XZY_SERVER"):
            return "XZY_SERVER"
        if os.getenv("YYB_SERVER"):
            return "YYB_SERVER"
        return "（未配置）"

    def get_h5_code(self, server: str, ref: str) -> Tuple[Optional[str], str]:
        """取公众号网页授权 code。

        ⚠️ 2026-11-18：可重试范围从「仅连接超时」扩大到「连接超时 + 上游拒绝」
        （-14 / 10009 等临时性拒绝），间隔按 2 的幂递增退避（封顶 30 秒）。
        依据见 _yyb_retryable() 的说明。

        返回 (code, "") 或 (None, 原因)。
        """
        url = server + "/wx/h5code"
        payload = {
            "ref": ref,
            "app_id": WX_APP_ID,
            "payload": {
                "redirect_uri": REDIRECT_URI,
                "scope": OAUTH_SCOPE,
                "state": "code",
            },
        }
        for attempt in range(YYB_RETRY_TIMES + 1):
            try:
                s = requests.Session()
                s.trust_env = False
                r = s.post(url, json=payload, timeout=(10, YYB_CODE_TIMEOUT))
            except requests.RequestException as exc:
                if attempt >= YYB_RETRY_TIMES:
                    return None, "YYB 请求异常：%s" % exc
                delay = min(YYB_RETRY_DELAY * (2 ** attempt), 30)
                log("⏳ [重试] h5code 第%d/%d次失败（连接层），%d秒后重试"
                    % (attempt + 1, YYB_RETRY_TIMES + 1, delay))
                time.sleep(delay)
                continue

            try:
                body = r.json()
            except ValueError:
                body = None

            if r.status_code >= 400:
                detail = _yyb_error_detail(body)
                # ⚠️ 2026-11-18 修正：原来只对「上游连接超时」重试，
                #    「上游拒绝」(-14 / 10009 等) 直接判死 → 一次风控抖动就全灭。
                #    现在按 _yyb_retryable() 判定，并对重试间隔做递增退避，
                #    避免密集重试反过来加重微信侧限流。
                if not _yyb_retryable(detail) or attempt >= YYB_RETRY_TIMES:
                    return None, ("HTTP %d%s" % (r.status_code,
                                  ("（%s）" % detail) if detail else ""))
                delay = min(YYB_RETRY_DELAY * (2 ** attempt), 30)
                log("⏳ [重试] h5code 第%d/%d次失败（%s），%d秒后重试"
                    % (attempt + 1, YYB_RETRY_TIMES + 1, detail, delay))
                time.sleep(delay)
                continue

            if not isinstance(body, dict):
                return None, "响应不是 JSON"

            if isinstance(body.get("code"), int) and body["code"] not in (0, None):
                detail = _yyb_error_detail(body)
                return None, str(body.get("msg") or detail or body)

            data = body.get("data") if isinstance(body.get("data"), dict) else {}
            result = body.get("result") or (data.get("result") if isinstance(data, dict) else {}) or {}
            code = result.get("code") if isinstance(result, dict) else None
            if not code:
                detail = _yyb_error_detail(body)
                return None, detail or "返回空 code"
            return code, ""
        return None, "YYB 取码失败：重试次数已用尽"


# ———————————— 业务接口 ————————————
def exchange_session(sess, code: str) -> Tuple[bool, str]:
    """用公众号 OAuth code 换已登录 SESSION。

    返回 (ok, 说明)。失败时说明里带可执行的处理建议。
    """
    url = (BASE + API_AUTH_LOGIN + "?code=" + requests.utils.quote(code)
           + "&channelCode=" + CHANNEL_CODE + "&siteId=" + SITE_ID + "&envType=WX")
    try:
        r = sess.get(url, timeout=HTTP_TIMEOUT)
    except requests.RequestException as exc:
        return False, "换取登录态失败（请求异常: %s）" % exc
    try:
        jr = r.json()
    except ValueError:
        return False, "换取登录态失败（响应非 JSON，HTTP %s）" % r.status_code

    if _api_ok(jr):
        return True, "已登录"

    status = _api_status(jr)
    msg = _api_msg(jr)
    if status == 4002:
        return False, ("客户未绑定手机号（status 4002）—— 该微信还不是商城会员，"
                       "需先在微信里用手机号完成一次绑定")
    if status in (401, 403):
        return False, "登录态无效（status %s）：%s" % (status, msg)
    return False, "换取登录态失败（status %s）：%s" % (status, msg or "未知原因")


def _cookie_session_value(sess) -> str:
    """安全取 SESSION cookie 值。

    不用 sess.cookies.get("SESSION")：直填/缓存模式下我们手动 set 的 SESSION
    （domain=HOST）与服务器 Set-Cookie 回来的 SESSION（domain=.HOST）同名不同域，
    requests 的 .get() 会因重名抛 CookieConflictError。这里改为遍历 jar，
    优先返回 domain 精确等于 HOST 的那个（即我们手动 set 的值）。
    """
    fallback = ""
    try:
        for c in sess.cookies:
            if getattr(c, "name", "") != "SESSION":
                continue
            val = getattr(c, "value", "") or ""
            if not val:
                continue
            dom = (getattr(c, "domain", "") or "").lstrip(".")
            if dom == HOST:
                return val
            fallback = val
    except Exception:
        pass
    return fallback


def check_login(sess) -> Dict[str, Any]:
    """校验登录态，返回 isLogin / userId / merUserId / session（当前 cookie 值）。"""
    code, jr, err = _post_form(sess, API_IS_LOGIN,
                                {"channelCode": CHANNEL_CODE, "siteId": SITE_ID})
    if err:
        return {"ok": False, "err": err}
    if not _api_ok(jr):
        return {"ok": False, "err": _api_msg(jr) or ("status=%s" % _api_status(jr))}
    data = jr.get("data") if isinstance(jr.get("data"), dict) else {}
    _ck = _cookie_session_value(sess)
    return {
        "ok": bool(data.get("isLogin")),
        "userId": data.get("userId"),
        "merUserId": data.get("merUserId"),
        "session": _ck,
        "err": "" if data.get("isLogin") else "会话未登录",
    }


def fetch_activity(sess, act_code: str) -> Tuple[str, Dict[str, Any]]:
    """拉活动页信息。

    返回 (state, payload)：
      state = "invalid" 活动码无效 / 活动不存在
      state = "ended"   活动码有效但活动已结束
      state = "ok"      活动进行中
    payload 在 ok/ended 时含 activityInfo 与 signInfo。
    """
    code, jr, err = _post_json(sess, API_LAUNCH_PAGE,
                               {"actCode": act_code, "siteId": SITE_ID})
    if err:
        return "invalid", {"err": err}
    if not _api_ok(jr):
        return "invalid", {"err": _api_msg(jr) or ("status=%s" % _api_status(jr))}
    data = jr.get("data") if isinstance(jr.get("data"), dict) else {}
    ai = data.get("activityInfo") if isinstance(data.get("activityInfo"), dict) else {}
    si = data.get("signInfo") if isinstance(data.get("signInfo"), dict) else {}
    status_name = str(ai.get("activityStatusName") or "")
    state = "ok" if ("进行中" in status_name) else "ended"
    return state, {"activityInfo": ai, "signInfo": si}


def resolve_act_code(sess) -> Tuple[Optional[str], str, str, Dict[str, Any]]:
    """探测可用活动码。

    顺序：本地缓存 → ACT_CODE → ACT_CODE_FALLBACKS。
    返回 (act_code, 活动名, 活动状态, payload)；失败时 act_code 为 None。
    """
    tried = []

    cache = load_act_code_cache()
    cached_code = str(cache.get("actCode") or "")
    if cached_code and _cache_act_code_valid(cache):
        state, payload = fetch_activity(sess, cached_code)
        if state in ("ok", "ended"):
            ai = payload.get("activityInfo") or {}
            log("ℹ️ [活动码] 命中缓存 %s（%s）"
                % (cached_code, ai.get("activityName") or "未知活动"))
            return cached_code, str(ai.get("activityName") or ""), state, payload
        tried.append(cached_code)

    for cand in [ACT_CODE] + list(ACT_CODE_FALLBACKS):
        if not cand or cand in tried:
            continue
        tried.append(cand)
        state, payload = fetch_activity(sess, cand)
        if state in ("ok", "ended"):
            ai = payload.get("activityInfo") or {}
            name = str(ai.get("activityName") or "")
            save_act_code_cache(cand, name)
            log("ℹ️ [活动码] 探测命中 %s（%s），已缓存" % (cand, name))
            return cand, name, state, payload
        time.sleep(1)

    return None, "", "invalid", {}


def signed_today(sign_info: Dict[str, Any]) -> Tuple[bool, Optional[Dict[str, Any]]]:
    """判断今天是否已签到：signRecordVoList 里存在今天的记录即为已签。"""
    today = today_str()
    for rec in (sign_info.get("signRecordVoList") or []):
        if isinstance(rec, dict) and str(rec.get("signDate") or "")[:10] == today:
            return True, rec
    return False, None


def reward_for_day(sign_info: Dict[str, Any], sign_day: Any) -> Optional[float]:
    """按连签天数从 signLevelVoList 取当天奖励值。"""
    if sign_day is None:
        return None
    for lv in (sign_info.get("signLevelVoList") or []):
        if isinstance(lv, dict) and str(lv.get("signDay")) == str(sign_day):
            try:
                return float(lv.get("rewardValue"))
            except (TypeError, ValueError):
                return None
    return None


def do_sign(sess, act_code: str) -> Tuple[bool, str]:
    """执行签到。返回 (ok, msg)。"""
    code, jr, err = _post_json(sess, API_SIGN,
                               {"actCode": act_code, "siteId": SITE_ID})
    if err:
        return False, err
    msg = _api_msg(jr)
    if _api_ok(jr):
        return True, msg or "签到成功"
    # 幂等：服务端认为今天已经签过
    if any(k in msg for k in ("今日已完成签到", "已签到", "重复", "明天再来", "请于明日")):
        return True, msg
    return False, msg or ("status=%s" % _api_status(jr))


def get_balance(sess) -> Optional[Dict[str, Any]]:
    """查账户积分总览。失败返回 None（只 WARN，不影响签到结果）。"""
    code, jr, err = _post_form(sess, API_BALANCE, {"siteId": SITE_ID})
    if err:
        warn("⚠️ [积分] 查询失败: " + str(err)[:120])
        return None
    if not _api_ok(jr):
        warn("⚠️ [积分] 接口未返回数据（status=%s）" % _api_status(jr))
        return None
    data = jr.get("data") if isinstance(jr.get("data"), dict) else None
    if not isinstance(data, dict):
        warn("⚠️ [积分] 响应里没有 data 字段")
        return None

    def _num(v):
        try:
            return float(str(v).replace(",", "").strip())
        except (TypeError, ValueError):
            return 0.0

    return {
        "balance": _num(data.get("balance")),
        "totalPoints": _num(data.get("totalPoints")),
        "usedPoints": _num(data.get("usedPoints")),
        "expiredPoints": _num(data.get("expiredPoints")),
        "soonExpirePoints": _num(data.get("soonExpirePoints")),
    }


def get_coupons(sess) -> Optional[list]:
    """查券列表。失败返回 None。"""
    code, jr, err = _post_form(sess, API_COUPON_LIST,
                                {"status": 0, "couponAttr": COUPON_ATTR,
                                 "siteId": SITE_ID})
    if err:
        warn("⚠️ [券] 查询失败: " + str(err)[:120])
        return None
    if not _api_ok(jr):
        warn("⚠️ [券] 接口未返回数据（status=%s）" % _api_status(jr))
        return None
    data = jr.get("data")
    if isinstance(data, dict):
        data = data.get("data")
    return data if isinstance(data, list) else []


def coupon_summary(coupons: list) -> str:
    """把券列表压成一行可读摘要（字段名做多候选兜底，缺字段不影响）。"""
    if not coupons:
        return "无"
    names = []
    for c in coupons[:3]:
        if not isinstance(c, dict):
            continue
        label = ""
        for k in ("couponName", "name", "voucherName", "activityName", "title"):
            v = c.get(k)
            if v:
                label = str(v)
                break
        amount = None
        for k in ("couponAmount", "amount", "faceValue", "value", "discount"):
            v = c.get(k)
            if v not in (None, ""):
                try:
                    amount = float(v)
                except (TypeError, ValueError):
                    amount = None
                if amount is not None:
                    break
        if label and amount is not None:
            names.append("%s(%.2f)" % (label, amount))
        elif label:
            names.append(label)
    if not names:
        return "%d 张" % len(coupons)
    more = " 等%d张" % len(coupons) if len(coupons) > 3 else ""
    return "、".join(names) + more


# ———————————— 单账号流程 ————————————
def run_account(server: str, ref: str, direct_session: str = "",
                session_key: str = ""):
    """跑一个账号。

    direct_session 非空 → 直填登录态模式，跳过 YYB 取码，直接用现成 SESSION。
    否则走「缓存复用」：SESSION 落盘缓存 48h 内校验有效直接复用，
    失效才重新取码；取码失败时再退回缓存兜底一次。

    返回 (ok, 汇总行, 状态, points_info, extra_lines)
      状态：成功 / 已签 / 失败
    """
    extra_lines = []
    cache_key = session_key or (("直填@%s" % ref) if server is None
                                else ("%s@%s" % (server, ref)))

    sess = _make_session()
    li = None

    # 1) 直填模式：用现成 SESSION，跳过取码与缓存
    if direct_session:
        sess.cookies.set("SESSION", direct_session, domain=HOST, path="/")
        log("🔑 登录态 · 直填 SESSION（跳过 YYB 取码）")
        li = check_login(sess)
        if not li.get("ok"):
            brief = str(li.get("err") or "未知")[:90]
            err("❌ 会话校验失败 · " + brief)
            err("   ↳ 直填的 SESSION 已失效（有效期 48 小时）—— "
                "需重新抓 HAR 取新 SESSION，或改用 YYBGO 取码模式")
            return False, "ref %s: ❌ 会话无效" % ref, "失败", None, extra_lines

    else:
        # 2) 缓存复用：未过期缓存先校验，有效就直接跳过取码
        cached = get_cached_session(cache_key)
        if cached:
            log("♻️ [会话缓存] 命中未过期缓存，先验有效性（免取码）")
            sess.cookies.set("SESSION", cached, domain=HOST, path="/")
            li = check_login(sess)
            if li.get("ok"):
                log("✅ [会话缓存] 缓存有效，直接复用（跳过 YYB 取码）")
            else:
                log("⚠️ [会话缓存] 缓存已失效，重新取码")
                sess = _make_session()
                li = None

        # 3) 缓存无效/无缓存 → 取码换 SESSION
        if li is None:
            code, err_msg = YYBClient().get_h5_code(server, ref)
            if not code:
                brief = " ".join(str(err_msg).split())[:90]
                err("❌ 取码失败 · 原因: " + brief)
                # 3b) 取码失败兜底：即便缓存超 40h 也试一次（48h 内可能仍有效）
                any_cached = get_cached_session_any(cache_key)
                if any_cached:
                    log("🆘 [兜底] 取码失败，试缓存 SESSION（由服务端判有效性）")
                    sess = _make_session()
                    sess.cookies.set("SESSION", any_cached, domain=HOST, path="/")
                    li = check_login(sess)
                    if li.get("ok"):
                        log("✅ [兜底] 缓存仍有效，本次照常签到")
                        extra_lines.append("🆘 取码失败，已用缓存登录态兜底")
                    else:
                        li = None
                        err("❌ [兜底] 缓存也已失效，无法签到")
                if li is None:
                    return False, "ref %s: ❌ 取码失败（%s）" % (ref, brief), "失败", None, extra_lines
            else:
                log("🔑 授权码 · 获取成功")
                ok, msg = exchange_session(sess, code)
                if not ok:
                    brief = " ".join(str(msg).split())[:110]
                    err("❌ 登录失败 · 原因: " + brief)
                    return False, "ref %s: ❌ 登录失败（%s）" % (ref, brief), "失败", None, extra_lines
                log("🔑 登录态 · " + msg)
                li = check_login(sess)
                if not li.get("ok"):
                    brief = str(li.get("err") or "未知")[:90]
                    err("❌ 会话校验失败 · " + brief)
                    return False, "ref %s: ❌ 会话无效" % ref, "失败", None, extra_lines
                # 新换的 SESSION 落盘缓存，供后续运行复用
                put_cached_session(cache_key, str(li.get("session") or ""),
                                   uid=str(li.get("userId") or ""))

    # 4) 统一校验登录态（直填/缓存/取码三条路都到这里）
    if li is None or not li.get("ok"):
        brief = str((li or {}).get("err") or "未知")[:90]
        err("❌ 会话校验失败 · " + brief)
        return False, "ref %s: ❌ 会话无效" % ref, "失败", None, extra_lines
    log("🆔 会员ID · %s" % (li.get("userId") or "-"))

    # 4) 探测活动码
    act_code, act_name, act_state, act_payload = resolve_act_code(sess)
    if not act_code:
        err("❌ 未找到可用的签到活动码（可在脚本顶部 ACT_CODE / ACT_CODE_FALLBACKS 中补充当月活动码）")
        return False, "ref %s: ❌ 活动码失效" % ref, "失败", None, extra_lines
    if act_state == "ended":
        _ai0 = act_payload.get("activityInfo") or {}
        err("❌ 活动未在进行中 · %s（%s）" % (act_name, _ai0.get("activityStatusName") or "-"))
        return False, "ref %s: ❌ 活动未在进行中" % ref, "失败", None, extra_lines
    log("🎯 活动 · %s ｜ %s" % (act_name, act_code))
    _ai = act_payload.get("activityInfo") or {}
    if _ai.get("startDate") or _ai.get("endDate"):
        log("🗓️ 活动期 · %s ~ %s" % (_ai.get("startDate") or "-",
                                     _ai.get("endDate") or "-"))

    # 5) 查签到状态
    state, payload = fetch_activity(sess, act_code)
    if state != "ok":
        err("❌ 活动页查询失败 · " + str(payload.get("err") or "")[:90])
        return False, "ref %s: ❌ 活动页查询失败" % ref, "失败", None, extra_lines
    sign_info = payload.get("signInfo") or {}
    already, _rec = signed_today(sign_info)
    cont_days = sign_info.get("signContinuousDays")
    if already:
        status = "已签"
        log("🟡 今日已签  ──  连签 %s 天，本次不重复提交"
            % (cont_days if cont_days is not None else "-"))
    else:
        log("📊 签到前 · 今日(%s) ⭕  未签到" % today_str())
        ok, msg = do_sign(sess, act_code)
        if not ok:
            err("❌ 签到失败  ──  服务端 msg: " + str(msg)[:140])
            return False, "ref %s: ❌ 签到失败" % ref, "失败", None, extra_lines
        status = "成功"
        log("✅ 签到成功  ──  服务端 msg: " + str(msg)[:100])

        # 重新拉一次活动页拿最新连签天数与奖励档位
        state2, payload2 = fetch_activity(sess, act_code)
        if state2 == "ok":
            sign_info = payload2.get("signInfo") or {}
            cont_days = sign_info.get("signContinuousDays")
            done2, _rec2 = signed_today(sign_info)
            log("📊 签到后 · 今日(%s) %s  连签 %s 天"
                % (today_str(), "✅" if done2 else "⭕",
                   cont_days if cont_days is not None else "-"))
            if done2:
                log("🎉 本账号首次签到成功！")
        reward = reward_for_day(sign_info, cont_days)
        if reward is not None:
            extra_lines.append("🎁 本次奖励 · %s 元券（连签第 %s 天）"
                               % (("%g" % reward), cont_days))

    # 6) 积分总览 + 券列表
    points_info = get_balance(sess)
    if points_info:
        log("💰 积分 · 余额 %.2f ｜累计 %.2f ｜已用 %.2f"
            % (points_info["balance"], points_info["totalPoints"],
               points_info["usedPoints"]))
    else:
        warn("⚠️ [积分] 未取到积分总览")
    coupons = get_coupons(sess)
    if coupons is not None:
        log("🎟️ 券 · %s" % coupon_summary(coupons))

    emoji = "✅" if status == "成功" else "🟡"
    tag = "签到成功" if status == "成功" else "今日已签"
    return True, "ref %s: %s %s" % (ref, emoji, tag), status, points_info, extra_lines


def main():
    # 账号来源优先级：XZY_SESSION 直填 > XZY_SERVER / YYB_SERVER 取码
    direct = load_direct_sessions()
    if direct:
        entries = [(None, ident, sval) for ident, sval in direct]
        source = "XZY_SESSION（直填登录态）"
    else:
        entries = list(YYBClient().entries())
        # 按 YYB_ONLY_REFS 序号白名单筛选（1 起）；留空 [] 则运行全部账号
        if YYB_ONLY_REFS:
            wanted = set(int(x) for x in YYB_ONLY_REFS
                         if str(x).strip().isdigit() and int(x) > 0)
            if wanted:
                entries = [e for i, e in enumerate(entries, 1) if i in wanted]
                log("ℹ️ 按 YYB_ONLY_REFS 筛选：请求序号 %s，命中 %d 个账号"
                    % (sorted(wanted), len(entries)))
        source = YYBClient.env_source()

    total = len(entries)
    log_startup(total)
    log("🔌 账号来源 · %s" % source)

    log("🚀 %s 每日签到开始 · 日期 %s · 共 %d 个账号"
        % (SITE_NAME, today_str(), total))

    if not entries:
        err("🚫 没有可执行账号（青龙环境变量 XZY_SESSION / XZY_SERVER / YYB_SERVER 为空，"
            "或被 YYB_ONLY_REFS 过滤空）")
        return

    success = 0
    skipped = 0
    points_rows = []   # [(账号序号, 总积分, 账号key)]
    expire_alerts = []  # [(账号序号, 即将过期积分)]

    for i, (server, ref, sess_val) in enumerate(entries, start=1):
        key = ("直填@%s" % ref) if server is None else ("%s@%s" % (server, ref))
        # 今日已成功签到的账号直接跳过，不再消耗 YYB 取码配额
        if is_done_today(key):
            log(f"⏭️ [打标] 账号 {i}/{total} 今日任务已完成，跳过")
            skipped += 1
            cached = load_daily_done().get(key)
            pinfo = cached.get("pointsInfo") if isinstance(cached, dict) else None
            if isinstance(pinfo, dict) and pinfo:
                points_rows.append((i, float(pinfo.get("balance") or 0), key))
                if float(pinfo.get("soonExpirePoints") or 0) > 0:
                    expire_alerts.append((i, float(pinfo.get("soonExpirePoints") or 0)))
            continue

        acc_banner(i, total, "标识：" + str(ref))
        try:
            # 只有 XZY_SESSION 直填模式下 server 为 None、sess_val 才是 SESSION 值；
            # YYB_SERVER/XZY_SERVER 取码模式下 sess_val 是配置行文本（host:port@ref），
            # 绝不能当 SESSION 传入，否则会误走直填并把配置行塞进 cookie 触发重名崩溃。
            _direct = (sess_val or "") if server is None else ""
            ok, line, status, points_info, extra = run_account(
                server, ref, direct_session=_direct, session_key=key)
            if ok:
                success += 1
            # 只有服务端明确返回「签到成功 / 今日已签到」才算今日完成
            if status in ("成功", "已签"):
                mark_done_today(key, line, points_info)
            if isinstance(points_info, dict):
                _bal = float(points_info.get("balance") or 0)
                points_rows.append((i, _bal, key))
                record_points_snapshot(key, _bal)
                _soon = float(points_info.get("soonExpirePoints") or 0)
                if _soon > 0:
                    expire_alerts.append((i, _soon))
            for one in extra:
                log(one)
        except Exception as e:
            err("💥 账号异常: " + str(e))
            err(traceback.format_exc())
        acc_footer(i, total)
        if i < total:
            time.sleep(random.randint(3, 8))

    fail = total - success - skipped
    _now = datetime.now()
    _week = "一二三四五六日"[_now.weekday()]
    log(SUMMARY_SEP)
    log("📂 汇总日志")
    log("📃 脚本名称：%s" % SITE_NAME)
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
    # 即将过期积分提醒（树状）
    if expire_alerts:
        log("⏳  即将过期积分：")
        for _j, (idx, amt) in enumerate(expire_alerts):
            _branch = "└─" if _j == len(expire_alerts) - 1 else "├─"
            log("%s 账号%d %s %.2f" % (_branch, idx, "·" * 20, amt))
    log(SUMMARY_SEP)

    # 通知：有 ERROR 照旧推；检测到积分即将过期时，即使全成功也强制推一次汇总
    notify_summary = "账号 %d ｜ 成功 %d ｜ 失败 %d ｜ 跳过 %d" % (
        total, success, fail, skipped)
    if points_rows:
        notify_summary += "\n" + " ｜ ".join(
            "账号%d 总积分 %.2f" % (idx, bal) for idx, bal, _k in points_rows)
    if expire_alerts:
        notify_summary += "\n" + "\n".join(
            "⏳ 账号%d 积分(%.2f)即将过期" % (idx, amt) for idx, amt in expire_alerts)
    flush_notify(SITE_NAME, notify_summary, logger=log, force=bool(expire_alerts))


if __name__ == "__main__":
    main()
