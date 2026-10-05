#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# =========================================================
# name: 小米社区
# cron: 5 6,16 * * *
# 积分有效期：无
# =========================================================

"""小米社区微信小程序签到（YYB-Go-Enhanced）
作者：lcmovie https://github.com/lcmovie
环境变量：
  YYB_SERVER：每行一个 server@微信账号标识，例如 yyb-go:8000@1
  MI_COMMUNITY_APPID：可选，默认使用小米社区小程序 AppID
  MI_COMMUNITY_REF：可选，仅运行指定微信账号标识，便于单账号测试
  YYB_ONLY_REFS：可选，账号序号白名单（1 起），仅运行指定序号；留空 [] 运行全部，
                  例如 [1,3] 只跑第 1、3 个账号

日志规范：[LEVEL] [MI] message   （LEVEL: INFO / WARN / ERROR）
依赖：requests
"""

# 内部配置：直接改这里，省去去面板设环境变量
# 账号序号白名单（1 起），仅运行指定序号，留空 [] 运行全部；例如 [1, 3]
YYB_ONLY_REFS = [1,2]

import os
import sys
import json
import uuid
import re
import base64
from pathlib import Path
from urllib.parse import quote, urljoin, urlparse
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

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

_QN_SITE = "小米社区"
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
    line = f"[{level}] [MI] {msg}"
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


APPID = os.getenv("MI_COMMUNITY_APPID", "wx240a4a764023c444")
BASE = "https://api.vip.miui.com"
ACCOUNT = "https://account.xiaomi.com"
SCRIPT_NAME = "小米社区签到"
UA = "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 MicroMessenger/8.0.73(0x18004939) NetType/WIFI Language/zh_CN"
FLOW_STEPS = 8
CACHE_PATH = Path(os.getenv("MI_COMMUNITY_CACHE", "/ql/data/config/mi_community_sessions.json"))
if not CACHE_PATH.parent.exists():
    CACHE_PATH = Path(__file__).resolve().with_name(".mi_community_sessions.json")


def flow_start(step, message):
    log(f"⏳ [{step}/{FLOW_STEPS}] {message}")


def flow_ok(message):
    log(f"✅ {message}")


def load_cached_session(ref):
    try:
        data = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError, OSError):
        return {}
    account = data.get(str(ref), {}) if isinstance(data, dict) else {}
    if not isinstance(account, dict):
        return {}
    return {
        name: str(account[name])
        for name in ("passToken", "userId", "cUserId")
        if account.get(name)
    }


def save_cached_session(ref, source):
    values = {
        name: str(source[name])
        for name in ("passToken", "userId", "cUserId")
        if source.get(name)
    }
    if not values.get("passToken") or not values.get("userId"):
        return
    try:
        try:
            data = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                data = {}
        except (FileNotFoundError, ValueError, OSError):
            data = {}
        data[str(ref)] = values
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        temp_path = CACHE_PATH.with_suffix(CACHE_PATH.suffix + ".tmp")
        temp_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, CACHE_PATH)
        os.chmod(CACHE_PATH, 0o600)
    except OSError as exc:
        raise RuntimeError(f"小米会话票据保存失败：{exc}") from exc


def entries():
    result = []
    selected_ref = os.getenv("MI_COMMUNITY_REF", "").strip()
    for raw in os.getenv("YYB_SERVER", "").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        if "@" not in raw:
            err("❌ YYB_SERVER 格式应为 地址@微信账号标识，已跳过一行")
            continue
        server, ref = raw.rsplit("@", 1)
        if server and ref and (not selected_ref or ref == selected_ref):
            result.append((server.rstrip("/"), ref))
    return result


def get_code(server, ref):
    host = server if server.startswith(("http://", "https://")) else f"http://{server}"
    # 用户信息操作可能较慢，必须先完成，再最后获取短时有效的一次性 code。
    flow_start(1, "从 YYB-Go 获取微信授权信息")
    user_info = requests.post(
        f"{host}/wxapp/operateWxData",
        json={
            "ref": ref,
            "app_id": APPID,
            "payload": {
                "api_name": "webapi_getuserinfo",
                "data": {"lang": "zh_CN", "version": "3.10.3"},
                "from_component": True,
                "operate_directly": False,
                "with_credentials": True,
            },
        },
        timeout=20,
    )
    user_info.raise_for_status()
    user_body = user_info.json()
    user_result = user_body.get("data", {}).get("result")
    if user_body.get("code") != 0 or not isinstance(user_result, dict):
        raise RuntimeError(f"YYB 获取微信用户信息失败（响应码：{user_body.get('code')}）")
    if os.getenv("MI_COMMUNITY_DEBUG") == "1":
        warn("调试：YYB用户信息字段=" + ",".join(
            f"{key}:{type(value).__name__}:{len(value) if isinstance(value, (str, list, dict)) else '-'}"
            for key, value in sorted(user_result.items())
        ))
    if not user_result.get("rawData") and isinstance(user_result.get("data"), str):
        try:
            try:
                decoded = json.loads(user_result["data"])
            except json.JSONDecodeError:
                decoded = json.loads(base64.b64decode(user_result["data"]).decode("utf-8"))
            profile = decoded.get("data", decoded)
            if isinstance(profile, str):
                profile = json.loads(profile)
            if isinstance(profile, dict):
                user_result["userInfo"] = profile
                user_result["rawData"] = json.dumps(profile, ensure_ascii=False, separators=(",", ":"))
                user_result.setdefault("errMsg", "getUserInfo:ok")
        except (ValueError, TypeError, UnicodeDecodeError):
            pass
    if user_result.get("cloud_id") and not user_result.get("cloudID"):
        user_result["cloudID"] = user_result["cloud_id"]
    if not user_result.get("rawData"):
        profile = user_result.get("userInfo") if isinstance(user_result.get("userInfo"), dict) else {}
        user_result["userInfo"] = profile
        user_result["rawData"] = json.dumps(profile, ensure_ascii=False, separators=(",", ":"))
        user_result.setdefault("errMsg", "getUserInfo:ok")
    required_user_fields = {"encryptedData", "iv", "rawData", "signature"}
    missing_user_fields = required_user_fields.difference(user_result)
    if missing_user_fields:
        raise RuntimeError(f"YYB 微信用户信息缺少字段：{','.join(sorted(missing_user_fields))}")
    user_result = {
        key: user_result[key]
        for key in ("cloudID", "encryptedData", "iv", "signature", "userInfo", "rawData", "errMsg")
        if key in user_result
    }
    flow_ok("微信授权信息获取成功")
    flow_start(2, "从 YYB-Go 获取一次性微信 code")
    response = requests.post(
        f"{host}/wxapp/getCode",
        json={"ref": ref, "app_id": APPID},
        timeout=20,
    )
    response.raise_for_status()
    body = response.json()
    code = body.get("data", {}).get("result", {}).get("code")
    if body.get("code") != 0 or not code:
        raise RuntimeError(f"YYB 获取 code 失败（响应码：{body.get('code')}）")
    flow_ok("一次性微信 code 获取成功")
    return code, user_result


def login_and_sign(code, wx_user_info, ref):
    session = requests.Session()
    session.mount(
        "https://",
        HTTPAdapter(max_retries=Retry(total=3, connect=3, read=3, backoff_factor=1, allowed_methods=None)),
    )
    session.headers.update({"User-Agent": UA, "Referer": f"https://servicewechat.com/{APPID}/73/page-frame.html"})
    session.cookies.set("deviceId", f"wp_{uuid.uuid4()}", domain="account.xiaomi.com", path="/")
    flow_start(3, "使用微信 code 登录小米账号")
    login = session.post(
        f"{ACCOUNT}/pass/sns/wxapp/v2/code",
        data={"code": code, "appid": APPID, "sid": "wx_vip", "userInfo": "true", "_locale": "zh_CN"},
        timeout=20,
    )
    login.raise_for_status()
    body = login.json()
    if body.get("code") != 0:
        raise RuntimeError(f"小米登录失败（响应码：{body.get('code')}）")
    wx_token = body.get("data", {}).get("wxSToken")
    if not wx_token:
        raise RuntimeError("小米登录响应缺少 wxSToken")
    flow_ok("微信 code 校验成功，已取得临时登录凭据")
    session.cookies.set("wxSToken", wx_token, domain="account.xiaomi.com", path="/")
    session.cookies.set(
        "userInfo",
        quote(json.dumps(wx_user_info, ensure_ascii=False, separators=(",", ":")), safe=""),
        domain="account.xiaomi.com",
        path="/",
    )
    flow_start(4, "建立小米账号会话")
    token_login = session.post(
        f"{ACCOUNT}/pass/sns/wxapp/v3/tokenLogin",
        data={"sid": "wx_vip", "appid": APPID, "callback": "", "authType": "1", "wxSToken": wx_token, "_locale": "zh_CN"},
        timeout=20,
        allow_redirects=False,
    )
    token_login.raise_for_status()
    token_body = {}
    session_tokens = {}
    if token_login.is_redirect:
        # 已绑定且此前登录过的账号，tokenLogin 可能通过 302 直接进入
        # serviceLogin。响应中的 Set-Cookie 已由 Session 自动保存，因此
        # 这里只校验重定向目标，不能把这个正常分支一律判为失败。
        redirect_url = urljoin(token_login.url, token_login.headers.get("Location", ""))
        redirect_target = urlparse(redirect_url)
        if (
            redirect_target.scheme != "https"
            or redirect_target.hostname != "account.xiaomi.com"
            or redirect_target.path.rstrip("/") != "/pass/serviceLogin"
        ):
            raise RuntimeError(f"小米会话接口发生未知重定向（HTTP {token_login.status_code}）")
        session_tokens = load_cached_session(ref)
        if not session_tokens.get("passToken") or not session_tokens.get("userId"):
            raise RuntimeError("小米账号已有登录状态，但本地缺少首次登录票据，请重新授权后再执行")
        flow_ok("小米账号已有登录状态，已加载本地会话票据")
    else:
        try:
            token_text = token_login.text.removeprefix("&&&START&&&")
            token_body = requests.models.complexjson.loads(token_text)
        except ValueError as exc:
            title_match = re.search(r"<title[^>]*>(.*?)</title>", token_login.text, re.I | re.S)
            html_title = re.sub(r"\s+", " ", title_match.group(1)).strip() if title_match else "无标题"
            raise RuntimeError(
                f"小米会话接口返回非 JSON（HTTP {token_login.status_code}，"
                f"类型 {token_login.headers.get('Content-Type', '未知')}，长度 {len(token_login.content)}，"
                f"页面 {html_title[:40]}）"
            ) from exc
        if not isinstance(token_body, dict) or not token_body.get("passToken"):
            raise RuntimeError("小米会话建立失败")
        session_tokens = token_body
        save_cached_session(ref, token_body)
        flow_ok("小米账号会话建立成功")

    # 小程序会把 tokenLogin 返回或本地缓存的账号票据写入 Cookie 后再取 STS。
    for name in ("passToken", "userId", "cUserId"):
        if session_tokens.get(name):
            session.cookies.set(name, str(session_tokens[name]), domain="account.xiaomi.com", path="/")
    flow_start(5, "获取小米社区 serviceLogin 跳转地址")
    service_login = session.get(
        f"{ACCOUNT}/pass/serviceLogin",
        params={"sid": "wx_vip", "_json": "true", "_locale": "zh_CN"},
        timeout=20,
    )
    service_login.raise_for_status()
    service_text = service_login.text.removeprefix("&&&START&&&")
    try:
        service_body = requests.models.complexjson.loads(service_text)
    except ValueError as exc:
        raise RuntimeError("小米 serviceLogin 返回非 JSON") from exc
    sts_url = service_body.get("location")
    if service_body.get("code") != 0 or not sts_url:
        raise RuntimeError(f"小米 serviceLogin 失败（响应码：{service_body.get('code')}）")
    save_cached_session(ref, service_body)
    flow_ok("serviceLogin 校验成功")
    flow_start(6, "执行 STS 登录并建立小米社区会话")
    sts = session.get(sts_url, timeout=20)
    sts.raise_for_status()
    if sts.json().get("S") != "OK":
        raise RuntimeError("小米 STS 登录失败")

    ph = next((cookie.value for cookie in session.cookies if cookie.name == "wx_vip_ph"), None)
    if not ph:
        raise RuntimeError("小米 STS 未返回 wx_vip_ph")
    flow_ok("STS 登录成功，社区会话已建立")

    headers = {"Origin": "https://servicewechat.com", "Content-Type": "application/x-www-form-urlencoded"}
    params = {"wx_vip_ph": ph}
    flow_start(7, "查询今日签到状态")
    status = session.get(f"{BASE}/mtop/planet/wechat/checkin/mypagedata", headers=headers, params=params, timeout=20)
    status.raise_for_status()
    status_body = status.json()
    if status_body.get("code") == 401:
        raise RuntimeError("小米登录态无效")
    buttons = status_body.get("entity", {}).get("data", [])
    already = any(item.get("title") == "每日签到" and item.get("buttons", [{}])[0].get("button") == "已签到" for item in buttons)
    if already:
        flow_ok("查询成功：今日已经签到，无需重复执行")
        flow_start(8, "检查是否需要提交签到任务")
        flow_ok("今日已签到，已跳过重复提交")
        return "今日已签到"

    flow_ok("查询成功：今日尚未签到")
    flow_start(8, "提交 WECHAT_CHECKIN_TASK 签到任务")
    sign = session.post(
        f"{BASE}/mtop/planet/wechat/member/addCommunityGrowUpPointByActionV2",
        headers=headers,
        params=params,
        data={"action": "WECHAT_CHECKIN_TASK"},
        timeout=20,
    )
    sign.raise_for_status()
    sign_body = sign.json()
    if sign_body.get("message") != "success":
        raise RuntimeError(f"签到失败（响应码：{sign_body.get('code')}）")
    entity = sign_body.get("entity", {})
    result = f"签到成功，{entity.get('title', '获得成长值')}"
    flow_ok(result)
    return result


def main():
    accounts = entries()
    if not accounts:
        err("🚫 未配置 YYB_SERVER")
        return 1

    # 按 YYB_ONLY_REFS 序号白名单筛选（1 起）；留空 [] 则运行全部账号
    if YYB_ONLY_REFS:
        wanted = set(int(x) for x in YYB_ONLY_REFS if str(x).strip().isdigit() and int(x) > 0)
        if wanted:
            accounts = [a for i, a in enumerate(accounts, 1) if i in wanted]
            log(f"ℹ️ 按 YYB_ONLY_REFS 筛选：请求序号 {sorted(wanted)}，命中 {len(accounts)} 个账号")
            if not accounts:
                err("❌ YYB_ONLY_REFS 指定的序号均超出账号范围")
                return 1

    failed = 0
    log(f"🚀 {SCRIPT_NAME} 开始 · 共读取到 {len(accounts)} 个 YYB 账号")
    for index, (server, ref) in enumerate(accounts, 1):
        acc_banner(index, len(accounts), "标识：" + str(ref))
        try:
            code, wx_user_info = get_code(server, ref)
            result = login_and_sign(code, wx_user_info, ref)
            log(f"🏁 账号 {ref} 流程完成：{result}")
        except Exception as exc:  # 单账号失败不影响其他账号
            failed += 1
            err(f"🛑 账号 {ref} 流程终止：{exc}")
        acc_footer(index, len(accounts))
    log(_ACC_RULE)
    log(f"📊 [执行汇总] {SCRIPT_NAME} · 账号 {len(accounts)} ｜ 成功 {len(accounts) - failed} ｜ 失败 {failed}")
    log(_ACC_RULE)    
    # 错误日志推送：有错误才发；未配置通知渠道则只提示一行
    flush_notify(
        "小米社区",
        f"账号 {len(accounts)} ｜ 成功 {len(accounts) - failed} ｜ 失败 {failed}",
        logger=log,
    )
    return 1 if failed == len(accounts) else 0


if __name__ == "__main__":
    sys.exit(main())
