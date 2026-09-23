# cron: 16 7,16 * * *
#!/usr/bin/env python3
# name: 嘉立创
# -*- coding: utf-8 -*-

"""
【第七天逻辑说明】
- 状态接口返回 data.day == 7 且 haveSignIn==True 且 haveReceive==False 时，
  自动调用 receiveVoucher 领取"8 豆豆"。
"""
# 账号配置：YYB_SERVER 走青龙环境变量（无脚本内常量兜底）；YYB_ONLY_REFS 为顶部常量，可被同名环境变量覆盖
#   YYB_SERVER     账号基座（青龙环境变量），每项 "地址@ref#备注"，多账号用空格 / 换行 / & 分隔
#                  例：yyb-go:8000@1 yyb-go:8000@2
YYB_ONLY_REFS = []  # 只跑 YYB 里 ref 等于这些的账号，留空 [] = 跑 YYB_SERVER 里的全部账号；填 ["1","2"] = 只跑这些 ref

import os
import sys
import json
import time
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ===================== 通知推送 =====================
try:
    from notify import send as notify_send
except ImportError:
    def notify_send(title, content):
        print(f"--- 通知 ---\n{title}\n{content}\n-------------")


# 通知开关：默认开；填 0/false/off/no 关闭
JLC_NOTIFY = os.getenv("JLC_NOTIFY", "1").strip().lower() not in ("0", "false", "off", "no")


# 日志收集
log_lines = []
SCRIPT_NAME = "JLC 嘉立创签到"
# ===================================================

# ===================== 手动调试开关 =====================
DEBUG = False
DEBUG_ENV = {
    "YYB_ONLY_REFS": "http://127.0.0.1:8080@test#测试号",
}
if DEBUG:
    for _k, _v in DEBUG_ENV.items():
        os.environ.setdefault(_k, _v)
    print("⚠️ 调试模式已开启（DEBUG=True），使用脚本内置 DEBUG_ENV 配置")
# =======================================================

BASE_URL = "https://m.jlc.com"
CAS_BASE_URL = "https://passport.jlc.com"
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
        print(f"⚠️ 跳过无效账号配置（缺 @）：{item!r}")
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

    raw_filter = _env("YYB_ONLY_REFS")
    filters = _split_accounts(raw_filter) if raw_filter else list(YYB_ONLY_REFS)
    if filters:
        want = {f.strip() for f in filters if f.strip()}
        entries = [e for e in entries if e[1] in want]
    return entries


def get_wx_code(server, ref):
    """调用 YYB-Go-Enhanced 获取嘉立创小程序 wx.login code。"""
    try:
        yyb_session = requests.Session()
        yyb_session.trust_env = False
        response = yyb_session.post(
            f"{server}/wxapp/getCode",
            json={"ref": ref, "app_id": JLC_MINI_APPID},
            timeout=30,
        )
        try:
            data = response.json()
        except ValueError as exc:
            raise RuntimeError(f"YYB响应不是JSON（HTTP {response.status_code}）") from exc
        if not response.ok:
            raise RuntimeError(data.get("error") or data.get("msg") or f"YYB请求失败（HTTP {response.status_code}）")
        if "code" in data and data.get("code") not in (0, "0", None):
            raise RuntimeError(data.get("error") or data.get("msg") or "YYB请求失败")
        result = data.get("result")
        if result is None:
            result = (data.get("data") or {}).get("result")
        if not isinstance(result, dict) or not result.get("code"):
            raise RuntimeError("YYB未返回有效微信code")
        return str(result["code"])
    except Exception as exc:
        print(f"[YYB] 获取code失败：{exc}")
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
    print("[JLC] secretkey 已动态更新")
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
    print(f"[{ref}] CAS授权码获取成功")
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
                print(f"[{ref}] login-by-code 登录成功")
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
                print(f"[{ref}] 登录未通过，已刷新secretkey并使用新CAS授权码重试")
                continue
            print(f"[{ref}] login-by-code 未通过（{detail}）")
            break
        except Exception as e:
            detail = f"第{attempt + 1}次: 请求异常={e}"
            errors.append(detail)
            print(f"[{ref}] login-by-code {detail}")
            break

    raise RuntimeError("CAS登录失败；" + "；".join(errors))






def get_token_for_account(server, ref, remark):
    """每次运行强制重新取码 + 登录，不落盘。"""
    auth_info = login_with_code(server, ref)
    print(f"[{remark}] 登录成功")
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


def try_send_notify(title: str, content: str) -> None:
    """青龙通知（兼容）"""
    try:
        notify_send(title, content)
    except Exception as e:
        print(f"（推送失败：{e}）")


def format_line(ok: bool, text: str) -> str:
    return ("✅ " if ok else "❌ ") + text


def _extract_streak_day(st_data: Dict[str, Any]) -> Optional[int]:
    for k in ("day", "continuousDay", "continueDay", "signDay"):
        v = st_data.get(k)
        if isinstance(v, int):
            return v
        if isinstance(v, str) and v.isdigit():
            return int(v)
    return None


def run_one_account(idx: int, remark: str, token: str, secret: str) -> Tuple[bool, str, Dict[str, Any]]:
    result: Dict[str, Any] = {
        "remark": remark,
        "signed": None,
        "gain_signin": None,
        "gain_day7": None,
        "total": None,
        "expireTime": None,
        "streak_day": None,
    }

    log_lines: List[str] = []
    ok_all = True

    with requests.Session() as s:
        # 1) 查状态
        st = get_sign_status(s, token, secret)
        if not st.get("success", False):
            ok_all = False
            log_lines.append(format_line(False, f"[{remark}] 查询签到状态失败：{json.dumps(st, ensure_ascii=False)}"))
            return ok_all, "\n".join(log_lines), result

        st_data = st.get("data") or {}
        have_signin = st_data.get("haveSignIn") is True
        have_receive = st_data.get("haveReceive") is True
        streak_day = _extract_streak_day(st_data)
        result["streak_day"] = streak_day

        if streak_day is not None:
            log_lines.append(format_line(True, f"[{remark}] 当前连续签到天数：{streak_day} 天"))
        else:
            log_lines.append(format_line(True, f"[{remark}] 当前连续签到天数：未知"))

        # 2) 签到（若未签）
        if have_signin:
            result["signed"] = True
            log_lines.append(format_line(True, f"[{remark}] 今日已签到"))
        else:
            si = do_signin(s, token, secret)
            if not si.get("success", False):
                ok_all = False
                result["signed"] = False
                log_lines.append(format_line(False, f"[{remark}] 签到失败：{json.dumps(si, ensure_ascii=False)}"))
            else:
                si_data = si.get("data") or {}
                gain = si_data.get("gainNum") or 0
                result["signed"] = True
                result["gain_signin"] = gain
                log_lines.append(format_line(True, f"[{remark}] 签到成功，本次获得：{gain} 豆豆"))

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
            log_lines.append(format_line(True, f"[{remark}] 检测到连续签到第 7 天且未领取，开始领取..."))
            rv = receive_voucher(s, token, secret)
            if not rv.get("success", False):
                ok_all = False
                log_lines.append(format_line(False, f"[{remark}] 第七天领取失败：{json.dumps(rv, ensure_ascii=False)}"))
            else:
                got = rv.get("data")
                result["gain_day7"] = got
                log_lines.append(format_line(True, f"[{remark}] 第七天领取成功：+{got} 豆豆"))
        elif streak_day == 7 and have_signin and have_receive:
            log_lines.append(format_line(True, f"[{remark}] 连续签到第 7 天奖励已领取"))

        # 4) 查豆豆总数
        ct = get_doudou_total(s, token, secret)
        if not ct.get("success", False):
            ok_all = False
            log_lines.append(format_line(False, f"[{remark}] 查询豆豆总数失败：{json.dumps(ct, ensure_ascii=False)}"))
        else:
            data = ct.get("data") or {}
            total = data.get("integralVoucher")
            expire_time = data.get("expireTime")
            result["total"] = total
            result["expireTime"] = expire_time
            extra = f"，有效期至：{expire_time}" if expire_time else ""
            log_lines.append(format_line(True, f"[{remark}] 当前豆豆总数：{total}{extra}"))

    return ok_all, "\n".join(log_lines), result


def main() -> int:
    global log_lines

    accounts = parse_accounts()
    results: List[Dict[str, Any]] = []
    any_fail = False

    log_lines.append(f"\n{' ' * 5}{SCRIPT_NAME}")
    log_lines.append("-------- 开 始 执 行 --------")
    log_lines.append(f"账号数量：{len(accounts)}")
    print(f"\n{' ' * 5}{SCRIPT_NAME}")
    print("-------- 开 始 执 行 --------")
    print(f"账号数量：{len(accounts)}")

    for idx, acc in enumerate(accounts, start=1):
        remark = acc["remark"]

        log_lines.append(f"\n📋 账号 [{idx}/{len(accounts)}]")
        log_lines.append(f"📋 当前账号：{remark or f'账号{idx}'}")
        print(f"\n📋 账号 [{idx}/{len(accounts)}]")
        print(f"📋 当前账号：{remark or f'账号{idx}'}")

        # yyb 模式：每次运行强制重新取码 + 登录换取 token（不落盘）
        if acc.get("mode") == "yyb":
            try:
                token, secret = get_token_for_account(acc["server"], acc["ref"], remark)
                log_lines.append("✅ 登录成功")
            except Exception as e:
                any_fail = True
                log_lines.append(format_line(False, f"❌ 登录失败: {e}"))
                print(format_line(False, f"❌ 登录失败: {e}"))
                if idx < len(accounts):
                    time.sleep(2)
                continue
        else:
            token, secret = acc["token"], acc["secret"]
            log_lines.append("📌 手动模式，跳过登录")

        ok, log_text, res = run_one_account(idx, remark, token, secret)
        log_lines.append(log_text)
        res["ok"] = ok
        results.append(res)

        if not ok:
            any_fail = True

        # 账号间延迟
        if idx < len(accounts):
            time.sleep(2)

    log_lines.append("\n-------- 执 行 结 束 --------")
    print("\n-------- 执 行 结 束 --------")

    # 推送精简摘要（不刷全量日志；成功数进标题，适配 PushPlus 免费版微信只显示标题）
    if JLC_NOTIFY:
        success = sum(1 for r in results if r.get("ok"))
        title = f"{SCRIPT_NAME}｜成功 {success}/{len(results)}"
        summary = []
        for r in results:
            remark = r.get("remark") or "?"
            if r.get("ok"):
                parts = []
                if r.get("gain_signin"):
                    parts.append(f"签到+{r['gain_signin']}豆豆")
                elif r.get("signed"):
                    parts.append("已签到")
                if r.get("gain_day7"):
                    parts.append(f"第7天+{r['gain_day7']}豆豆")
                if r.get("total") is not None:
                    parts.append(f"总数{r['total']}")
                summary.append(f"✅ [{remark}] {'，'.join(parts) if parts else '完成'}")
            else:
                summary.append(f"❌ [{remark}] 执行失败")
        try_send_notify(title, "\n".join(summary))

    return 1 if any_fail else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print("❌ 脚本异常：", str(e))
        traceback.print_exc()
        if JLC_NOTIFY:
            try_send_notify("JLC 签到脚本异常", str(e))
        sys.exit(1)