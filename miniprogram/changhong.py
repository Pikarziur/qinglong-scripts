#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# =========================================================
# name: 长虹智慧家居
# cron: 5 5,15 * * *
# =========================================================
#
# 任务流程：
#   1. 读取 YYB_SERVER 账号基座，按 YYB_ONLY_REFS 序号白名单筛选（1 起）
#   2. 调用 YYBGO 的 /wxapp/getCode 获取每个账号的 wx.login code
#   3. 用 code 完成微信登录，进入长虹小程序会话
#   4. 执行签到任务并查询积分，输出汇总
# 可控参数：
#   YYB_SERVER      必填。格式「地址@ref#备注」，多账号换行分隔
#   YYB_ONLY_REFS   账号序号白名单（1 起）。留空 [] 跑全部；填 [1,2] 只跑第 1、2 个账号
#   CH_AGGR_ID      可选。手动指定签到活动 ID；留空则从首页自动发现
#   CH_IPV4_ONLY     网络模式，默认 1（仅 IPv4）；填 0 恢复双栈解析
#
# 日志规范：[LEVEL] [CHANGHONG] message   （LEVEL: INFO / WARN / ERROR）
# =========================================================

YYB_ONLY_REFS = []  # 账号序号白名单（1 起），留空 [] 跑全部；例如 [1,3] 只跑第 1、3 个账号

import base64
import json
import os
import re
import sys
import socket
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

import requests
import urllib3.util.connection

APP_ID = "wx36c3413e8fe39263"
BASE = "https://hongke.changhong.com/gw/applet"
TITLE = "长虹智慧家居签到"


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

_QN_SITE = "长虹智慧家居"
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
    line = f"[{level}] [CHANGHONG] {msg}"
    print(line, flush=True)
    if level == "ERROR":
        collect_error(line)

def log(msg):   _emit("INFO", msg)
def warn(msg):  _emit("WARN", msg)
def err(msg):   _emit("ERROR", msg)


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


def configure_network():
    # ql2 实测：AF_UNSPEC/AAAA解析EAI_AGAIN，A记录及IPv4 HTTPS正常。
    # 仅影响当前脚本进程；不硬编码IP、不关闭TLS验证。
    if os.getenv("CH_IPV4_ONLY", "1") != "0":
        urllib3.util.connection.allowed_gai_family = lambda: socket.AF_INET


class TaskError(Exception):
    pass


def request(session, method, url, label, **kwargs):
    try:
        response = session.request(method, url, timeout=(10, 35),
                                   allow_redirects=False, **kwargs)
        if response.status_code != 200:
            raise TaskError(f"{label}：HTTP {response.status_code}")
        result = response.json()
        if not isinstance(result, dict):
            raise TaskError(f"{label}：响应格式异常")
        return result
    except (requests.RequestException, ValueError) as exc:
        # 不打印可能含 URL、凭据或个人信息的原始异常及服务端原文。
        detail = str(exc)
        reason = "DNS解析失败" if "NameResolutionError" in detail or "Failed to resolve" in detail else type(exc).__name__
        raise TaskError(f"{label}：网络或JSON异常（{reason}）") from None


def parse_entry(line):
    if "@" not in line:
        raise TaskError("YYB_SERVER 格式应为 服务地址@账号标识")
    server, ref = (part.strip() for part in line.rsplit("@", 1))
    if not server.startswith(("http://", "https://")):
        server = "http://" + server
    u = urlsplit(server)
    if not u.hostname or not ref or u.query or u.fragment or u.username:
        raise TaskError("YYB_SERVER 地址或账号标识无效")
    return server.rstrip("/"), ref


def entry_ref(line):
    """取出 YYB_SERVER 行的 ref；格式错误返回 None，交给 parse_entry 统一报错。"""
    try:
        return parse_entry(line)[1]
    except TaskError:
        return None


class Changhong:
    def __init__(self, server, ref):
        self.server, self.ref = server, ref
        self.yyb = requests.Session()
        self.yyb.trust_env = False
        self.web = requests.Session()
        self.web.headers.update({
            "content-type": "application/json",
            "Referer": f"https://servicewechat.com/{APP_ID}/330/page-frame.html",
            "User-Agent": "Mozilla/5.0 MicroMessenger/8.0.50 MiniProgramEnv/iOS",
        })

    def close(self):
        self.yyb.close()
        self.web.close()

    def yyb_call(self, endpoint, **extra):
        body = request(self.yyb, "POST", self.server + "/wxapp/" + endpoint,
                       "YYB " + endpoint,
                       json={"ref": self.ref, "app_id": APP_ID, **extra})
        result = (body.get("data") or {}).get("result")
        if body.get("code") != 0 or not isinstance(result, dict):
            raise TaskError(f"YYB {endpoint}失败，请检查账号授权及YYB日志")
        return result

    def api(self, method, path, **kwargs):
        body = request(self.web, method, BASE + path, path, **kwargs)
        if str(body.get("code")) != "200":
            raise TaskError(f"{path}：业务请求失败，请检查小程序登录/授权/活动状态")
        return body.get("data")

    def login(self):
        # 用户资料为辅助字段；不把YYB宿主OpenID误当作长虹小程序OpenID。
        profile, info = {}, {}
        try:
            info = self.yyb_call("operateWxData", payload={
                "api_name": "webapi_getuserinfo", "data": {"lang": "zh_CN"},
                "with_credentials": True, "from_component": True,
                "operate_directly": False,
            })
            profile = info.get("userInfo") or {}
            if not profile and info.get("rawData"):
                profile = json.loads(info["rawData"])
            if not profile and info.get("data"):
                raw = info["data"]
                if isinstance(raw, dict):
                    profile = raw
                else:
                    try:
                        profile = json.loads(raw)
                    except (ValueError, TypeError):
                        profile = json.loads(base64.b64decode(raw))
            if not isinstance(profile, dict):
                profile = {}
        except (TaskError, ValueError, TypeError):
            log("ℹ️ 微信资料不可用，将由长虹服务端通过新code识别账号")
        code = self.yyb_call("getCode").get("code")
        if not isinstance(code, str) or not code.strip():
            raise TaskError("YYB未返回有效的wx.login code")
        common = {
            "jsCode": code, "invitation": "", "system": "iOS 16.0",
            "userName": profile.get("nickName", ""),
            "sex": {1: "男", 2: "女"}.get(profile.get("gender"), "未知"),
            "avatarUrl": profile.get("avatarUrl", ""),
        }
        # HAR 先通过此接口换取长虹小程序身份，再提交手机号授权。
        identity = self.api("POST", "/appletUser/getTokenByJsCode", json={
            **common, "iv": info.get("iv", ""),
        }, headers={"token": "", "smarthome": ""})
        if not isinstance(identity, dict):
            raise TaskError("长虹身份响应格式异常")
        data = identity
        if not data.get("token"):
            if not identity.get("openId") or not identity.get("unionId"):
                raise TaskError("长虹未返回小程序OpenID/UnionID，请检查微信授权")
            phone = self.yyb_call("getPhoneNumber")
            encrypted = phone.get("encryptedData") or phone.get("encrypted_data")
            if not all(isinstance(v, str) and v for v in
                       (phone.get("code"), encrypted, phone.get("iv"))):
                raise TaskError("YYB手机号授权数据不完整，请先在小程序授权手机号")
            # HAR 中第二步沿用第一步jsCode，由业务端处理；不再次向微信兑换。
            data = self.api("POST", "/appletUser/getTokenByCode", json={
                **common, "encryptedData": encrypted, "iv": phone["iv"],
                "code": phone["code"], "openId": identity["openId"],
                "unionId": identity["unionId"],
            }, headers={"token": "", "smarthome": ""})
        if not isinstance(data, dict) or not isinstance(data.get("token"), str) or not data["token"]:
            raise TaskError("长虹登录未返回token，需核对授权字段")
        # 实测静默登录返回token时isRegistered=0；手机号登录HAR为1。
        # 不能把该分支标记当作登录成败，必须用token访问业务接口验证。
        self.web.headers.update({"token": data["token"], "smarthome": data["token"]})
        # 实际业务查询验证登录态，不能仅凭拿到code/token判定成功。
        self.api("GET", "/mine/getAppletUser")

    def activity_id(self):
        explicit = os.getenv("CH_AGGR_ID", "").strip()
        if explicit:
            return explicit
        menu = self.api("POST", "/homePage/getShortcutMenuList")
        found = set()

        def walk(value):
            if isinstance(value, list):
                for item in value:
                    walk(item)
            elif isinstance(value, dict):
                if "签到" in str(value.get("name", "")):
                    match = re.search(r"activityData=([A-Za-z0-9_-]+)", str(value.get("webUrl", "")))
                    if match:
                        found.add(match.group(1))
                for item in value.values():
                    if isinstance(item, (list, dict)):
                        walk(item)

        walk(menu)
        if len(found) != 1:
            raise TaskError("首页未发现唯一签到活动，请设置 CH_AGGR_ID")
        return found.pop()

    def state(self, aggr_id):
        data = self.api("GET", "/aggr/aggregationInfo", params={"aggrId": aggr_id})
        if not isinstance(data, dict):
            raise TaskError("活动详情格式异常")
        today = datetime.now(timezone(timedelta(hours=8))).date().isoformat()
        if not (str(data.get("startTime", ""))[:10] <= today <= str(data.get("endTime", ""))[:10]):
            raise TaskError("活动未开始或已结束，请更新活动入口")
        if str(data.get("status")) != "1" or str(data.get("isCan")) != "1":
            raise TaskError("当前账号无法参与该活动")
        signs = [x["aggrAssemblySignin"] for x in data.get("aggrAssemblyList", [])
                 if isinstance(x, dict) and isinstance(x.get("aggrAssemblySignin"), dict)]
        if len(signs) != 1 or signs[0].get("isSignin") not in (0, 1):
            raise TaskError("签到组件或状态无法识别")
        return signs[0]

    def run(self):
        self.login()
        aggr_id = self.activity_id()
        before = self.state(aggr_id)
        if before["isSignin"] == 1:
            status = "今日已签到"
        else:
            error = None
            try:
                self.api("POST", "/aggr/signin", params={"aggrId": aggr_id})
            except TaskError as exc:
                error = exc
            # 签到POST超时也只查询结果，不盲目重复提交。
            if self.state(aggr_id)["isSignin"] != 1:
                raise TaskError(str(error) if error else "签到后状态未变为已签到")
            status = "签到成功（已复查）"
        try:
            points = self.api("GET", "/homePage/getUserPoint")
            # 实测刚签到时可能暂返0，稍后查询恢复实际余额。
            if before["isSignin"] == 0 and points == 0:
                for delay in (2, 4):
                    time.sleep(delay)
                    points = self.api("GET", "/homePage/getUserPoint")
                    if points != 0:
                        break
            if isinstance(points, (int, float)) and not isinstance(points, bool):
                status += f"，当前积分：{points}"
                if before["isSignin"] == 0 and points == 0:
                    status += "（接口可能尚未更新，请稍后查看）"
            else:
                status += "，积分格式无法识别"
        except TaskError:
            status += "，积分查询失败"
        return status


def main():
    configure_network()
    lines = [x.strip() for x in os.getenv("YYB_SERVER", "").splitlines() if x.strip()]
    # 按 YYB_ONLY_REFS 序号白名单筛选（1 起）；留空 [] 则运行全部账号
    if YYB_ONLY_REFS:
        wanted = set(int(x) for x in YYB_ONLY_REFS if str(x).strip().isdigit() and int(x) > 0)
        if wanted:
            lines = [x for i, x in enumerate(lines, 1) if i in wanted]
            log("ℹ️ 按 YYB_ONLY_REFS 筛选：请求序号 %s，命中 %d 个账号" % (sorted(wanted), len(lines)))
    results, fail_count = [], 0
    if not lines:
        results.append("未配置 YYB_SERVER")
        fail_count = 1
    log(f"🚀 {TITLE} 开始 · 共 {len(lines)} 个账号")
    for index, line in enumerate(lines, 1):
        acc_banner(index, len(lines), "标识：" + str(entry_ref(line) or line))
        client = None
        failed = False
        try:
            client = Changhong(*parse_entry(line))
            result = client.run()
        except TaskError as exc:
            result = str(exc); fail_count += 1; failed = True
        except Exception as exc:
            result = f"处理异常（{type(exc).__name__}）"; fail_count += 1; failed = True
        finally:
            if client:
                client.close()
        msg = f"账号{index}：{result}"
        if failed:
            err("❌ " + msg)
        elif "已签到" in result:
            log("🟡 " + msg)
        else:
            log("✅ " + msg)
        results.append(msg)
        acc_footer(index, len(lines))
    if not lines:
        err("🚫 " + results[0])
    # 控制台执行汇总
    _n = len(lines)
    log(_ACC_RULE)
    log(f"📊 [执行汇总] {TITLE} · 账号 {_n} ｜ 成功 {_n - fail_count} ｜ 失败 {fail_count}")
    log(_ACC_RULE)    
    # 错误日志推送：有错误才发；未配置通知渠道则只提示一行
    flush_notify(
        "长虹智慧家居",
        f"账号 {_n} ｜ 成功 {_n - fail_count} ｜ 失败 {fail_count}",
        logger=log,
    )
    return 1 if fail_count else 0


if __name__ == "__main__":
    sys.exit(main())
