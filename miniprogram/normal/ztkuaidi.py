#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# =========================================================
# name:  中通快递
# version: 2026-10-29a
# cron: 30 6,16 * * *
# 积分有效期：次年6月30日
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
from datetime import datetime
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
SCRIPT_VER = "2026-10-29a"
ACC_RULE = "=" * 70


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
# 结构：{ "<server>@<ref>": {"date": "2026-11-07", "doneTime": "...", "note": "..."} }
# 跨天自动失效；文件在脚本同目录、非隐藏，面板文件管理里可见/可删。
DAILY_DONE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ztkuaidi_daily_done.json")


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


def is_done_today(key: str) -> bool:
    """该账号今天是否已打过标（是则本次直接跳过）。"""
    entry = load_daily_done().get(key)
    if not isinstance(entry, dict):
        return False
    return str(entry.get("date") or "") == _today()


def mark_done_today(key: str, note: str = "") -> None:
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
        try:
            host = server.rstrip("/")
            if not host.startswith(("http://", "https://")):
                host = f"http://{host}"
            response = requests.post(
                f"{host}/wxapp/getCode",
                json={"ref": ref, "app_id": self.wx_appid},
                timeout=60,
            )
            response.raise_for_status()
            body = response.json()
            code = ((body.get("data") or {}).get("result") or {}).get("code")
            if body.get("code") != 0 or not code:
                self.log(f"❌ [获取 code] 失败，YYB-Go 响应码: {body.get('code')}", level="error")
                return None
            self.log("🔑 [获取 code] 成功")
            return code
        except Exception as e:
            self.log(f"❌ 获取 code 失败: {e}", level="error")
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
                sign_success, sign_message = self.sign_in(session)
                time.sleep(random.randint(1, 3))
                final_points = self.get_points(session)
                self.log_points_change(initial_points, final_points)
                _done = sign_success or sign_message == "今日已签到"
                self.account_results.append({
                    "index": index,
                    "login": "认证成功",
                    "sign": sign_message,
                    "initial_points": initial_points,
                    "final_points": final_points,
                    "normal": _done,
                })
                # 签到成功或今日已签 → 打上「今日已完成」标记
                if _done:
                    mark_done_today(_done_key, sign_message)
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
            self.log(f"📊 [执行汇总] {self.site_name} · 账号 {_n} ｜ 成功 {_ok} ｜ 失败 {_fail}" + (f" ｜ 跳过 {_sk}" if _sk else ""))
            self.log("=" * 70)
            # 错误日志推送：有错误才发；未配置通知渠道则只提示一行
            flush_notify(
                "中通快递",
                f"账号 {_n} ｜ 成功 {_ok} ｜ 失败 {_fail}" + (f" ｜ 跳过 {_sk}" if _sk else ""),
                logger=self.log,
            )

if __name__ == "__main__":
    auto_task = AutoTask("中通快递")
    auto_task.log_startup()
    auto_task.run()
