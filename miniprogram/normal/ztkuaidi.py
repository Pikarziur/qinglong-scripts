#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# =========================================================
# name:  中通快递
# version: 2026-11-17a
# cron: 30 6,16 * * *
# 积分有效期：次年6月30日【1+0.5】
# 检测过期积分：每年6月30日前七天检测并提醒（即将过期积分=上年已获取-上年已使用）
# =========================================================
#
# 任务流程：
#   1. 读取 YYB_SERVER 账号基座，按 YYB_ONLY_REFS 序号白名单筛选（1 起）
#   2. 调用 YYBGO 的 /wxapp/getCode 获取 wx.login code
#   3. 完成微信登录（wxlogin）
#   4. 执行快递签到任务并查询前后积分变化，输出汇总
# 可控参数：
#   YYB_SERVER      必填。格式「地址@ref#备注」，多账号换行分隔
#   YYB_ONLY_REFS   账号序号白名单（1 起）。留空 [] 跑全部；填 [1,2] 只跑第 1、2 个账号
#   PROXY_API_URL    可选。代理 API，返回「ip:端口」文本，填写后请求走代理
#
# 日志规范：[LEVEL] [ZTKD] message   （LEVEL: INFO / WARN / ERROR）
# =========================================================

YYB_ONLY_REFS = []  # 账号序号白名单（1 起），留空 [] 跑全部；例如 [1,3] 只跑第 1、3 个账号

# 502 = YYB-GO 内部把请求转给微信协议层（多渠道轮转），8 秒内全没打通 → 连接层超时。
# 实测（2026-11-13，累计 43 次调用）：成功率约 37%，失败固定 8.0~8.1s、成功 0.6~3.4s，
# 失败随机散布（不是从第 N 次起系统性失败）→ 是随机超时，不是限速。
# 按成功率 p=0.6 估算：重试 3 次 ≈ 78% 成功，5 次 ≈ 90% 成功，故取 5。
# 只有「连接层故障」才重试；上游明确拒绝（rejected / code=-NN）重试无用。
# ⚠️ 排查陷阱：YYB-GO 自身有 pacing 限速（db/tuning.json：login_budget 3 次/10 分钟，
#    code_min_gap 3 秒）。密集压测会自己烧光预算制造 502 假象，别把自己的测试当证据。
YYB_RETRY = 5      # 取码尝试次数（含首次）
YYB_RETRY_WAIT = 5 # 重试间隔基数（秒），实际取 5~8 秒随机


def _yyb_clean_ref(value):
    """去除 ref 的备注(# 后缀)与协议前缀(wx:/yyb:/wmpf:/syzs:)，用于白名单比对。"""
    text = str(value or "").split("#", 1)[0].strip()
    for prefix in ("wx:", "yyb:", "wmpf:", "syzs:"):
        if text.lower().startswith(prefix):
            text = text[len(prefix):].strip()
    return text


import random
import time
import requests
import os
import json
import traceback
from datetime import datetime, timedelta
from typing import Any, Dict

# ———————————— 错误通知（可选项，想用则用）————————————
# 只推错误：本次运行出现 ERROR 日志才推送一次；正常跑完不打扰。
# 直接调用青龙自带的通知模块（容器内为 /ql/data/scripts/notify.py，仓库内为同目录/上一级的 notify.py）——
#   在青龙面板「通知设置」里配一次即可全站通用（该文件由青龙官方维护，支持其全部推送渠道）。
# 找不到该文件、或未配置任何通知渠道时，只在日志末尾提示一行，不报错、不中断。
import os as _os
import sys as _sys

# ———————————— Token 缓存（仓库根 ql_cache.py）————————————
# 缓存登录 token，避免每次都调 YYBGO 取 code（取码会撞 YYBGO 的 5 分钟恢复冷却）。
# 缓存文件放在 /ql/data/config/ql_cache/<app_id>.json（青龙容器内，仓库外，不受脚本更新影响）。
_qc_cache = None
_qc_cache_err = ""
try:
    _qc_here = os.path.dirname(os.path.abspath(__file__))
    _qc_dirs = [_qc_here, os.path.dirname(_qc_here), os.path.dirname(os.path.dirname(_qc_here))]
    for _qc_p in _qc_dirs:
        if _qc_p and os.path.isfile(os.path.join(_qc_p, "ql_cache.py")):
            if _qc_p not in _sys.path:
                _sys.path.insert(0, _qc_p)
            from ql_cache import TokenCache as _qc_TokenCache
            _qc_cache = _qc_TokenCache
            break
except Exception as _qc_e:
    _qc_cache = None
    _qc_cache_err = str(_qc_e)
# ========== 账号分隔标识 ==========
# 多账号同跑时, 用醒目横线把各账号日志隔开, 便于阅读与定位。
def now_text() -> str:
    # 兼容两种导入: from datetime import datetime / import datetime
    _dt = datetime.now() if hasattr(datetime, "now") else datetime.datetime.now()
    return _dt.strftime("%Y-%m-%d %H:%M:%S")
SCRIPT_VER = "2026-11-17a"
ACC_RULE = "=" * 70

# 签到前状态预检开关：查 queryRecentSign 判断今日是否已签，已签跳过签到写请求（防二次写请求）。
# 查询异常/无今日记录一律照常签到，防漏签。与本地日打标互补。想关闭改成 False。
ENABLE_SIGN_STATE_CHECK = True


_QN_SITE = "中通快递"
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

MULTI_ACCOUNT_PROXY = False # 是否使用多账号代理，默认不使用，True则使用多账号代理

# 「日打标」：记录今天已完成的账号，当天再次运行时直接跳过，避免重复消耗取码配额。
# 结构：{ "<server>@<ref>": {"date": "2026-11-07", "doneTime": "...", "note": "...", "expirePoints": null} }
# 跨天自动失效；文件在脚本同目录、非隐藏，面板文件管理里可见/可删。
DAILY_DONE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ztkuaidi_daily_done.json")

# ===================== 积分过期提醒 =====================
# 每年 6 月 30 日前七天（6/24~6/30，含 6/30 当天）检测并连续提醒；
# 即将过期积分 = 上年获取 - 上年已使用。
ENABLE_EXPIRE_REMIND = True
EXPIRE_REMIND_DAYS = 7


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
        print(f"⚠️ [打标] 读取失败: {exc}")
    return {}


def save_daily_done(cache: Dict[str, Any]) -> None:
    """写打标文件（临时文件 + rename 原子替换，避免写坏）。"""
    try:
        tmp = DAILY_DONE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
        os.replace(tmp, DAILY_DONE_FILE)
    except Exception as exc:
        print(f"❌ [打标] 保存失败: {exc}")


def done_key(server: str, ref: str) -> str:
    """账号唯一标识：server@ref（server 已不含 ref 后缀，需自行拼接）。"""
    return f"{server}@{ref}"


def in_expire_window() -> bool:
    """是否处于积分过期提醒窗口（每年 6 月 28~30 日，含 30 号）。"""
    if not ENABLE_EXPIRE_REMIND:
        return False
    now = datetime.now()
    if now.month != 6:
        return False
    last_day = 30
    return now.day > last_day - EXPIRE_REMIND_DAYS


def expire_deadline() -> str:
    """本次窗口对应的积分过期日。"""
    now = datetime.now()
    return f"{now.year}-06-30"


def is_done_today(key: str) -> bool:
    """该账号今天是否已打过标（是则本次直接跳过）。"""
    entry = load_daily_done().get(key)
    if not isinstance(entry, dict):
        return False
    return str(entry.get("date") or "") == _today()


def expire_points_from_done(key: str):
    """读取打标缓存里的即将过期积分（当天第二轮被跳过时用于继续提醒）。"""
    entry = load_daily_done().get(key)
    if not isinstance(entry, dict):
        return None
    val = entry.get("expirePoints")
    try:
        return float(val) if val is not None else None
    except Exception:
        return None


def mark_done_today(key: str, note: str = "", expirePoints=None) -> None:
    """给该账号打上「今日已完成」标记。"""
    cache = load_daily_done()
    today = _today()
    # 只保留当天的记录，跨天自动清理，避免文件无限增长
    cache = {k: v for k, v in cache.items()
             if isinstance(v, dict) and str(v.get("date") or "") == today}
    cache[key] = {
        "date": today,
        "doneTime": now_text(),
        "note": str(note or ""),
        "expirePoints": expirePoints,
    }
    save_daily_done(cache)


def clear_daily_done(key: str = "") -> None:
    """清除指定账号（或全部）的日打标。"""
    cache = load_daily_done()
    if key:
        cache.pop(key, None)
    else:
        cache = {}
    save_daily_done(cache)


class AutoTask:
    # ========== 启动信息 ==========
    def log_startup(self, total: int = 0) -> None:
        self.log(ACC_RULE)
        self.log("📌 脚本版本: %s ｜ %s" % (SCRIPT_VER, self.wx_appid))
        if total:
            self.log("🔢 账号数量: %d" % total)
        self.log(ACC_RULE)


    def __init__(self, site_name):
        """
        初始化自动任务类
        :param site_name: 站点名称，用于日志显示
        """
        self.site_name = site_name
        self.proxy_url = os.getenv("PROXY_API_URL") # 代理api，返回一条txt文本，内容为代理ip:端口
        self.wx_appid = "wx7ddec43d9d27276a" # 微信小程序id
        self.account_results = []
        self.exit_code = 0  # 0=全部成功，1=有账号失败（供 __main__ 决定退出码）
        self.host = "hdgateway.zto.com"
        self._cache_obj = None
        self._last_token = ""
        self.user_agent = "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 MicroMessenger/8.0.75(0x18004b21) NetType/WIFI Language/zh_CN"


    # ========== Token 缓存 ==========
    def _cache(self):
        """按 app_id 隔离的 token 缓存对象（懒加载）。"""
        if self._cache_obj is None and _qc_cache is not None:
            try:
                self._cache_obj = _qc_cache(self.wx_appid, self.site_name)
            except Exception as exc:
                self.log(f"⚠️ [缓存] 初始化失败: {exc}", level="warning")
                self._cache_obj = False
        return self._cache_obj or None

    def cache_get_token(self, server, ref):
        """取缓存 token；无缓存/已过期/模块不可用时返回 None。"""
        cache = self._cache()
        if not cache:
            return None
        try:
            entry = cache.get(server, ref)
        except Exception as exc:
            self.log(f"⚠️ [缓存] 读取失败: {exc}", level="warning")
            return None
        if entry and entry.token:
            self.log(f"✅ [缓存] 命中 token（{ref}）")
            return entry.token
        return None

    def cache_set_token(self, server, ref, token, expire_time=""):
        """写入 token 到缓存。"""
        cache = self._cache()
        if not cache or not token:
            return False
        try:
            ok = cache.set(server, ref, token, expire_time)
            if ok:
                self.log(f"💾 [缓存] token 已保存（{ref}）")
            return ok
        except Exception as exc:
            self.log(f"⚠️ [缓存] 保存失败: {exc}", level="warning")
            return False

    def cache_del_token(self, server, ref):
        """删除缓存 token（登录失效时调用）。"""
        cache = self._cache()
        if not cache:
            return False
        try:
            cache.delete(server, ref)
            self.log(f"🗑️ [缓存] token 已清除（{ref}）")
            return True
        except Exception as exc:
            self.log(f"⚠️ [缓存] 清除失败: {exc}", level="warning")
            return False
    def log(self, msg, level="info"):
        prefix = {"error": "ERROR", "warning": "WARN"}.get(level, "INFO")
        line = f"[{prefix}] [ZTKD] {msg}"
        print(line, flush=True)
        if prefix == "ERROR":
            collect_error(line)

    def acc_banner(self, idx, total, ident=""):
        """打印账号分隔标识（开始）。多账号同跑时把各账号日志隔开，便于阅读与定位。"""
        self.log("=" * 70)
        self.log("👤 账号 %d/%d%s" % (idx, total, (" ｜ " + str(ident)) if ident else ""))
        self.log("=" * 70)

    def acc_footer(self, idx, total):
        """打印账号分隔标识（结束）。"""
        self.log("🔚 账号 %d/%d 处理结束" % (idx, total))

    def get_wx_code(self, server, ref):
        """取微信 code。

        2026-11-13 修正：原先只请求一次，YYB-Go 返回 502 就直接判失败，
        导致 3 个账号全军覆没且脚本仍以「成功」退出。
        现在区分两类错误：
          可重试 —— HTTP 5xx 且内层 msg 不是 rejected（连接层故障/节点失效）
          不可重试 —— 上游明确拒绝（rejected / code=-NN）或业务码非 0
        """
        try:
            host = server.rstrip("/")
            if not host.startswith(("http://", "https://")):
                host = f"http://{host}"
        except Exception as e:
            self.log(f"❌ 取码参数异常: {e}", level="error")
            return None

        last_err = None
        for attempt in range(1, max(1, YYB_RETRY) + 1):
            try:
                response = requests.post(
                    f"{host}/wxapp/getCode",
                    json={"ref": ref, "app_id": self.wx_appid},
                    timeout=60,
                )
                body = {}
                try:
                    body = response.json()
                except Exception:
                    body = {}
                msg = str(body.get("msg") or body.get("message") or "")

                if response.status_code >= 500:
                    # 5xx：区分「上游拒绝」与「连接层故障」
                    if "reject" in msg.lower() or "code=-" in msg:
                        self.log(f"❌ [获取 code] 上游拒绝: {msg[:120]}", level="error")
                        return None
                    last_err = f"HTTP {response.status_code}: {msg[:120] or 'Server Error'}"
                else:
                    response.raise_for_status()
                    code = ((body.get("data") or {}).get("result") or {}).get("code")
                    if body.get("code") != 0 or not code:
                        self.log(f"❌ [获取 code] 失败，YYB-Go 响应码: {body.get('code')} {msg[:80]}",
                                 level="error")
                        return None
                    self.log("🔑 [获取 code] 成功")
                    return code

            except requests.exceptions.RequestException as e:
                last_err = str(e)[:160]
            except Exception as e:
                self.log(f"❌ 获取 code 失败: {e}", level="error")
                return None

            if attempt < YYB_RETRY:
                wait = YYB_RETRY_WAIT + random.randint(0, 3)
                self.log(f"⚠️ [获取 code] 第 {attempt}/{YYB_RETRY} 次失败：{last_err}，"
                         f"{wait}s 后重试...", level="warning")
                time.sleep(wait)

        self.log(f"❌ 获取 code 失败（重试 {YYB_RETRY} 次）: {last_err}", level="error")
        return None

    def get_proxy(self):
        """
        获取代理
        :return: 代理
        """
        if not self.proxy_url:
            self.log("ℹ️ [获取代理] 没有找到环境变量PROXY_API_URL，不使用代理", level="warning")
            return None
        url = self.proxy_url
        response = requests.get(url)
        proxy = response.text
        self.log(f"🛡️ [获取代理]: {proxy}")
        return proxy

    def check_proxy(self, proxy, session):
        """
        检查代理
        :param proxy: 代理
        :param session: session
        :return: 是否可用
        """
        try:
            url = f"http://{self.host}/getApolloConfig"
            session.headers["X-Token"] = ""
            payload = {"keys":["serverTime"]}
            response = session.post(url, json=payload, timeout=5)
            if response.status_code == 200:
                self.log(f"🛡️ [检查代理]: {proxy} 应该可用")
                return True
            else:
                self.log(f"⚠️ [检查代理]: {response.text}")
                return False
        except Exception as e:
            return False


    def check_env(self):
        """
        检查环境变量
        :return: 环境变量字符串
        """
        try:
            yyb_server = os.getenv("YYB_SERVER", "")
            if not yyb_server.strip():
                self.log("🚫 [检查环境变量] 没有找到 YYB_SERVER，请按 地址@微信账号标识 配置", level="error")
                return

            for line_no, raw in enumerate(yyb_server.replace("&", " ").split(), 1):
                raw = raw.strip()
                if not raw:
                    continue
                if "@" not in raw:
                    self.log(f"⚠️ [检查环境变量] YYB_SERVER 第{line_no}行格式错误，已跳过", level="error")
                    continue
                server, ref = raw.rsplit("@", 1)
                ref = ref.strip()
                if not server.strip() or not ref:
                    self.log(f"⚠️ [检查环境变量] YYB_SERVER 第{line_no}行地址或账号标识为空，已跳过", level="error")
                    continue
                yield server.strip(), ref
        except Exception as e:
            self.log(f"💥 [检查环境变量] 发生错误: {str(e)}\n{traceback.format_exc()}", level="error")
            raise

    def wxlogin(self, session, code):
        """
        登录
        :param session: session
        :param code: 微信code
        :return: 登录结果
        """
        try:
            url = f"https://{self.host}/auth_wechatMini_authByCode"
            payload = {
                "code": code
            }
            response = session.post(url, json=payload, timeout=20)
            response.raise_for_status()
            response_json = response.json()
            if response_json['status'] == True:
                self.log(f"🔑 [登录]: {response_json['message']}")
                token = (response_json.get('result') or {}).get('token')
                if not token:
                    self.log("❌ [登录] 响应缺少 token", level="error")
                    return False
                session.headers["X-Token"] = token
                self._last_token = token
                return True
            else:
                self.log(f"❌ [登录] 发生错误: {response_json['message']}", level="error")
                return False
        except requests.RequestException as e:
            self.log(f"💥 [登录] 发生网络错误: {str(e)}\n{traceback.format_exc()}", level="error")
            return False
        except Exception as e:
            self.log(f"💥 [登录] 发生错误: {str(e)}\n{traceback.format_exc()}", level="error")
            return False


    def sign_in(self, session):
        """
        签到
        :param session: session
        :return: 签到结果
        """
        try:
            url = f"https://membergateway.zto.com/member/activity/signIn"
            payload = {
                "signType": "TODAY_SIGN",
                "signDate": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "supplementaryScene": "null"
            }
            response = session.post(url, json=payload, timeout=20)
            response.raise_for_status()
            response_json = response.json()
            if response_json['status'] == True:
                self.log("✅ [签到]: 成功")
                return True, "签到成功"
            else:
                message = response_json.get("message") or "签到失败"
                self.log(f"🟡 [签到]: {message}", level="warning")
                return False, message
        except Exception as e:
            self.log(f"💥 [签到] 发生错误: {str(e)}\n{traceback.format_exc()}", level="error")
            return False, "请求异常"

    def get_points(self, session, quiet=False):
        """查询当前会员积分，失败时返回 None。

        quiet=True 时不打印告警（用于缓存 token 探活 —— 探活失败是正常分支，
        不该刷 WARN 日志、更不该触发错误推送）。
        """
        try:
            response = session.post(
                "https://membergateway.zto.com/member/getMemberPoints",
                json={},
                timeout=20,
            )
            response.raise_for_status()
            response_json = response.json()
            points = (response_json.get("data") or {}).get("totalPoint")
            if response_json.get("success") is True and isinstance(points, (int, float)):
                return int(points)
            if not quiet:
                self.log("⚠️ [积分] 查询失败：响应中没有有效积分", level="warning")
        except Exception as e:
            if not quiet:
                self.log(f"⚠️ [积分] 查询失败: {e}", level="warning")
        return None

    def get_year_points_summary(self, session, target_year):
        """上年获取 - 上年已使用。失败返回 None。

        接口 POST /member/getMemberPointsDetail（application/x-www-form-urlencoded）:
            recordType: 1=获取，2=消耗
            pageNum / pageSize: 翻页参数
        每条记录: quantity（正数=积分绝对值）、recordType（1/2）、pointDate（YYYY-MM-DD HH:MM:SS）。
        """
        try:
            income = 0
            expense = 0
            # 先算获取（recordType=1），再算消耗（recordType=2）。接口 total 独立，互不影响。
            for rt in ("1", "2"):
                page = 1
                scanned = 0
                # 翻页上限 30 * 50 = 1500 条，理论上足够覆盖一年流水
                while page <= 30:
                    response = session.post(
                        "https://membergateway.zto.com/member/getMemberPointsDetail",
                        data={"recordType": rt, "pageNum": page, "pageSize": 50},
                        headers={"Content-Type": "application/x-www-form-urlencoded"},
                        timeout=20,
                    )
                    response.raise_for_status()
                    resp_json = response.json()
                    if not resp_json.get("success"):
                        break
                    data = resp_json.get("data") or {}
                    items = data.get("list") or []
                    if not items:
                        break
                    for item in items:
                        if not isinstance(item, dict):
                            continue
                        date_str = str(item.get("pointDate") or "")
                        try:
                            y = int(date_str[:4])
                        except Exception:
                            continue
                        if y != target_year:
                            continue
                        q = item.get("quantity")
                        try:
                            q = float(q)
                        except Exception:
                            continue
                        # 明细里的 quantity 已是绝对值（正号），直接用
                        if rt == "1":
                            income += q
                        else:
                            expense += q
                    scanned += len(items)
                    total = int(data.get("total") or 0)
                    if scanned >= total:
                        break
                    page += 1
            result = income - expense
            self.log(f"📅 [积分] {target_year} 年 已获取 {income:g} - 已使用 {expense:g} = {result:g}")
            return result
        except Exception as e:
            self.log(f"⚠️ [积分] 查询年度明细失败: {e}", level="warning")
            return None

    def query_signed_today(self, session):
        """签到前状态预检：查 queryRecentSign 判断今日是否已签（只读，不触发任何写请求）。

        HAR 2026-11-17 证实：POST /member/activity/queryRecentSign
        返回 result.dailyList，每天一条含 date / isSigned / isToday。
        今日那条 isSigned=true 即服务端确认今日已签。

        返回：True=今日已签 / False=今日未签 / None=无法判断（异常、无今日记录、业务码异常）。
        调用方仅在 True 时跳过签到写请求，其余情况照常签到防漏签。
        """
        today = datetime.now().strftime("%Y-%m-%d")
        start = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d 00:00:00")
        end = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d 23:59:59")
        try:
            response = session.post(
                "https://membergateway.zto.com/member/activity/queryRecentSign",
                json={"startDate": start, "endDate": end},
                timeout=20,
            )
            response.raise_for_status()
            resp_json = response.json()
            daily = (resp_json.get("result") or {}).get("dailyList")
            if not isinstance(daily, list):
                self.log("⚠️ [状态预检] 响应无 dailyList，无法判断今日签到状态", level="warning")
                return None
            # 优先用服务端标记的 isToday，回退按 date == 今天
            entry = None
            for item in daily:
                if isinstance(item, dict) and item.get("isToday") is True:
                    entry = item
                    break
            if entry is None:
                for item in daily:
                    if isinstance(item, dict) and str(item.get("date") or "") == today:
                        entry = item
                        break
            if entry is None:
                self.log(f"⚠️ [状态预检] 未找到今日({today})签到记录，无法判断", level="warning")
                return None
            signed = entry.get("isSigned")
            if not isinstance(signed, bool):
                self.log("⚠️ [状态预检] isSigned 字段异常，无法判断今日签到状态", level="warning")
                return None
            self.log(f"🔍 [状态预检] 今日({today})已签到: {signed}")
            return signed
        except Exception as e:
            self.log(f"⚠️ [状态预检] 查询失败: {e}，照常签到（防漏签）", level="warning")
            return None

    def log_points_change(self, before, after):
        """用一条清晰日志显示签到前后的积分。"""
        if before is None or after is None:
            before_text = "查询失败" if before is None else str(before)
            after_text = "查询失败" if after is None else str(after)
            self.log(f"📊 [积分]: 初始积分 {before_text} → 完成后积分 {after_text}", level="warning")
            return
        self.log(f"📊 [积分]: 初始积分 {before} → 完成后积分 {after}（变化 {after - before:+d}）")

    def run(self):
        """
        运行任务
        """
        try:
            self.log(f"🚀 【{self.site_name}】开始执行任务")

            # 检查环境变量
            all_entries = list(self.check_env())
            # 按 YYB_ONLY_REFS 序号白名单筛选（1 起）；留空 [] 则运行全部账号
            if YYB_ONLY_REFS:
                wanted = set(int(x) for x in YYB_ONLY_REFS if str(x).strip().isdigit() and int(x) > 0)
                if wanted:
                    all_entries = [e for i, e in enumerate(all_entries, 1) if i in wanted]
                    self.log(f"ℹ️ 按 YYB_ONLY_REFS 筛选：请求序号 {sorted(wanted)}，命中 {len(all_entries)} 个账号")
            for index, (server, ref) in enumerate(all_entries, 1):
                _done_key = done_key(server, ref)
                # 今天已打标的账号直接跳过，不再消耗取码/取号配额
                if is_done_today(_done_key):
                    self.log(f"⏭️ [打标] 账号 {index}/{len(all_entries)} 今日任务已完成，跳过")
                    self.account_results.append({
                        "index": index,
                        "login": "今日已完成，跳过",
                        "sign": "已跳过",
                        "initial_points": None,
                        "final_points": None,
                        "normal": False,
                        "skipped": True,
                        "expirePoints": expire_points_from_done(_done_key),
                    })
                    self.acc_footer(index, len(all_entries))
                    continue

                self.acc_banner(index, len(all_entries), "标识：" + str(ref))

                if MULTI_ACCOUNT_PROXY:
                    proxy = self.get_proxy()
                    if proxy:
                        session = requests.Session()
                        session.proxies.update({"http": f"http://{proxy}", "https": f"http://{proxy}"})
                        # 检查代理，不可用重新获取
                        while not self.check_proxy(proxy, session):
                            proxy = self.get_proxy()
                            session.proxies.update({"http": f"http://{proxy}", "https": f"http://{proxy}"})
                    else:
                        session = requests.Session()
                else:
                    session = requests.Session()

                session.headers.update({
                    "Content-Type": "application/json",
                    "User-Agent": self.user_agent,
                    "Referer": f"https://servicewechat.com/{self.wx_appid}/693/page-frame.html",
                    "x-sv-v": "0.22.0",
                    "x-version": "V8.160.1",
                    "x-clientCode": "wechatMiniZtoHelper",
                })

                # 优先用缓存 token（避免每次取 code 撞 YYBGO 的 5 分钟恢复冷却）
                cached_token = self.cache_get_token(server, ref)
                login_result = False
                if cached_token:
                    session.headers["X-Token"] = cached_token
                    # 用一次只读接口探活：能查到积分说明 token 仍有效
                    probe = self.get_points(session, quiet=True)
                    if probe is not None:
                        self.log("✅ [缓存] token 有效，跳过微信授权")
                        login_result = True
                    else:
                        self.log("⚠️ [缓存] token 已失效，重新登录")
                        self.cache_del_token(server, ref)
                        session.headers.pop("X-Token", None)

                if not login_result:
                    # 执行微信授权
                    code = self.get_wx_code(server, ref)
                    if not code:
                        self.account_results.append({
                            "index": index,
                            "login": "获取微信 code 失败",
                            "sign": "未执行",
                            "initial_points": None,
                            "final_points": None,
                            "normal": False,
                        })
                        self.acc_footer(index, len(all_entries))
                        continue

                    login_result = self.wxlogin(session, code)
                    time.sleep(random.randint(1, 3))
                    if not login_result:
                        self.account_results.append({
                            "index": index,
                            "login": "认证失败",
                            "sign": "未执行",
                            "initial_points": None,
                            "final_points": None,
                            "normal": False,
                        })
                        self.acc_footer(index, len(all_entries))
                        continue
                    # 登录成功 → 写入缓存（下次直接复用）
                    if self._last_token:
                        self.cache_set_token(server, ref, self._last_token)

                initial_points = self.get_points(session)

                # 签到前状态预检：今日已签则跳过签到写请求（防二次写请求/黑号）
                if ENABLE_SIGN_STATE_CHECK:
                    signed_today = self.query_signed_today(session)
                else:
                    signed_today = None

                if signed_today is True:
                    sign_success = True
                    sign_message = "今日已签到（状态预检）"
                    self.log("⏭️ [状态预检] 今日已签到，跳过签到写请求")
                else:
                    # 未签 / 无法判断（None）→ 照常签到，防漏签
                    if signed_today is None:
                        self.log("ℹ️ [状态预检] 无法确认今日签到状态，照常签到（防漏签）")
                    sign_success, sign_message = self.sign_in(session)
                    time.sleep(random.randint(1, 3))

                final_points = self.get_points(session)
                self.log_points_change(initial_points, final_points)

                # 积分过期提醒：窗口内查上年获取-上年消耗；平时不请求
                expire_pts = None
                if in_expire_window():
                    last_year = datetime.now().year - 1
                    expire_pts = self.get_year_points_summary(session, last_year)
                    if expire_pts is not None and expire_pts > 0:
                        self.log(f"⏳ [积分] 即将过期积分: {expire_pts:g}")
                    elif expire_pts is not None:
                        # 上年收支相抵或为负：不提醒、不打标缓存
                        expire_pts = None

                _done = sign_success or sign_message == "今日已签到"
                self.account_results.append({
                    "index": index,
                    "login": "认证成功",
                    "sign": sign_message,
                    "initial_points": initial_points,
                    "final_points": final_points,
                    "normal": _done,
                    "expirePoints": expire_pts,
                })
                # 签到成功或今日已签 → 打上「今日已完成」标记
                if _done:
                    mark_done_today(_done_key, sign_message, expire_pts)
                    self.log(f"✅ [打标] 今日任务已完成，已记录（{_today()}）")

                self.acc_footer(index, len(all_entries))

        except Exception as e:
            self.log(f"💥 【{self.site_name}】执行过程中发生错误: {str(e)}\n{traceback.format_exc()}", level="error")
        finally:
            _n = len(self.account_results)
            _ok = sum(1 for it in self.account_results if it.get("normal"))
            _sk = sum(1 for it in self.account_results if it.get("skipped"))
            _fail = _n - _ok - _sk
            self.log("=" * 70)
            self.log(f"📊 [执行汇总] {self.site_name} · 账号 {_n} ｜ 成功 {_ok} ｜ 失败 {_fail} ｜ 跳过 {_sk}")
            # 汇总类别里打印每个账号的总积分
            for _i, _r in enumerate(self.account_results, 1):
                _pt = _r.get("final_points")
                if _pt is None:
                    _pt = _r.get("initial_points")
                if _pt is None:
                    _pt = _r.get("point")
                if _pt is not None and _pt != "-":
                    self.log(f"💰 [总积分] 账号{_i}：{_pt}")
            # 积分过期提醒：窗口内汇总打印 + 独立强推（与错误推送分开，命中即推）
            try:
                check_expire_remind(self.account_results)
            except Exception as exc:
                self.log(f"⚠️ [积分过期提醒] 检查异常: {exc}", level="warning")
            self.log("=" * 70)
            # 错误日志推送：有错误才发；未配置通知渠道则只提示一行
            flush_notify(
                "中通快递",
                f"账号 {_n} ｜ 成功 {_ok} ｜ 失败 {_fail} ｜ 跳过 {_sk}",
                logger=self.log,
            )
            # ── 退出码 ──────────────────────────────────────────────
            # 2026-11-13 修正：此前 run() 不返回退出码、__main__ 也不检查，
            # 即使 3 个账号全部失败，青龙仍显示「完成 ✅」。
            self.exit_code = 1 if _fail else 0
            if _fail:
                self.log(f"❌ 有 {_fail} 个账号未完成，本次任务以失败退出"
                         f"（成功 {_ok} / 跳过 {_sk} / 共 {_n}）", level="error")
            else:
                self.log(f"✅ 全部 {_n} 个账号执行成功")

def check_expire_remind(rows):
    """汇总区打印积分过期提醒；窗口内独立强推，连续多天提醒。"""
    if not in_expire_window():
        return
    hits = [r for r in (rows or [])
            if r and r.get("expirePoints") is not None and float(r.get("expirePoints")) > 0]
    if not hits:
        return
    deadline = expire_deadline()
    print("⏳ [积分过期提醒]", flush=True)
    for r in hits:
        print(f"   · 账号{r.get('index', '?')}：{float(r['expirePoints']):g} 积分（{deadline} 过期）", flush=True)
    if _qn_send is None:
        print(_QN_NOMOD, flush=True)
        return
    if not any(_os.getenv(_k) for _k in _QN_ENVS):
        print("[QL] 未配置通知渠道，积分过期提醒未推送（可在青龙「通知设置」或环境变量中配置）", flush=True)
        return
    try:
        body = "\n".join(
            f"- 账号{r.get('index', '?')}：{float(r['expirePoints']):g} 积分（{deadline} 过期）" for r in hits
        )
        _qn_send(f"【{_QN_SITE}】积分即将过期提醒", body)
    except Exception as _e:
        print(f"[QL] 积分过期提醒推送失败：{_e}", flush=True)


if __name__ == "__main__":
    auto_task = AutoTask("中通快递")
    auto_task.log_startup()
    auto_task.run()
    # 2026-11-13 修正：此前无条件以 0 退出，账号全失败时青龙仍显示「完成 ✅」
    _sys.exit(auto_task.exit_code)
