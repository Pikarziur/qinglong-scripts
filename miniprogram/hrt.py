#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# name: 华润通
# cron: 0 6,16 * * *
"""
青龙环境变量：
  YYB_SERVER  必填，YYB-Go-Enhanced地址@微信账号标识，多账号每行一条
              例：yyb-go:8000@1
  HRT_TOKEN   可选，仅用于首次迁移旧登录态；多账号每行一条并与 YYB_SERVER 对应
  YYB_ONLY_REFS  可选，账号序号白名单（1 起），仅运行指定序号；留空 [] 运行全部，
                 例如 [1,3] 只跑第 1、3 个账号

依赖：requests、pycryptodome（仓库 requirements.txt 已声明）
缓存：/ql/data/config/hrt_tokens.json，失效后会自动通过 YYB-Go-Enhanced 重新登录。
功能：自动登录、查询签到状态、签到领积分，并输出签到前积分、本次增加积分、签到后积分。

日志规范：[LEVEL] [HRT] message   （LEVEL: INFO / WARN / ERROR）

作者：lcmovie https://github.com/lcmovie
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import random
import string
import sys
import time
import urllib.parse
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
from Crypto.Cipher import AES, PKCS1_OAEP
from Crypto.Hash import SHA1
from Crypto.PublicKey import RSA
from Crypto.Util.Padding import pad

# 内部配置：直接改这里，省去去面板设环境变量
# 账号序号白名单（1 起），仅运行指定序号，留空 [] 运行全部；例如 [1, 3]
YYB_ONLY_REFS = []

APP_ID = "wx66c62601b987e69d"
HRT_BASE = "https://mid.huaruntong.cn"
MINI_AUTH = "API_AUTH_MINI"
MINI_SECRET = "addebd90-15e9-4817-a531-1fdd0c7d5230"
MINI_PUBLIC_KEY = """-----BEGIN PUBLIC KEY-----
MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQCKAJ+rCtCbtr4KmkSVnDZq6c38
0R1TFO9KPJiFtC/DvG3ZVi5aeaRb6XcJCeQbmKA4LA4u8ZFNn5xzCu0/tsSwsKFu
/rM/DHtrD3GGaaq3gV27g620dnEiSZrTZ6QV+OOWIELYekl13O/GF7swqrnC2Xak
d3kfPKITQEpRsjCsKwIDAQAB
-----END PUBLIC KEY-----"""
WEB_AUTH = "API_AUTH_WEB"
WEB_SECRET = "c274fc67-19f9-47ba-bb84-585a2e3a1f6a"
WEB_PUBLIC_KEY = """-----BEGIN PUBLIC KEY-----
MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDuAiqDmvn9Rf15o21qkDxN0rUf
ZsX6rVBrtfgY6tamN2Yn+1D3eHZJuKNlucyqeBr6nmfN2srYAX+oyCXr5vWwFclj
PuWh8aSASqyk7MfbAv5Q4VqYS7lsYUQRdw4plZG0NASDeBvHWi3lsHjGfNb7iUv
grk312EDfBHtRgDvB0QIDAQAB
-----END PUBLIC KEY-----"""
H5_APP_ID = "API_AUTH_H5"
H5_AUTH_SECRET = "1c6120fd-5ad3-4c2d-8cb7-b87a707f416d"
SUCCESS = "S0A00000"
TIMEOUT = 25
UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 "
    "MicroMessenger/8.0.75(0x18004b21) NetType/WIFI Language/zh_CN"
)
CACHE_FILE = Path(os.getenv("HRT_CACHE_FILE", "/ql/data/config/hrt_tokens.json"))


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

_QN_SITE = "华润通"
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
    line = f"[{level}] [HRT] {msg}"
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


def compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def safe_message(value: Any, limit: int = 160) -> str:
    text = str(value or "未知错误").replace("\n", " ").strip()
    return text[:limit]


def nested(data: Any, *names: str) -> Any:
    queue = [data]
    while queue:
        item = queue.pop(0)
        if isinstance(item, dict):
            for name in names:
                if item.get(name) not in (None, ""):
                    return item[name]
            queue.extend(item.values())
        elif isinstance(item, list):
            queue.extend(item)
    return None


def crypto4mid(path: str, payload: Dict[str, Any], auth_id: str, secret: str, public_key: str) -> Dict[str, str]:
    # 小程序 fetch 封装先拼接请求 URL，再仅对 queryData 中的相对 apiPath 编码。
    plain: Dict[str, Any] = {
        "apiPath": urllib.parse.quote(path, safe=""),
        "timestamp": int(time.time() * 1000),
        "appId": auth_id,
        **payload,
    }
    parts = []
    for key in sorted(plain):
        value = plain[key]
        if isinstance(value, (dict, list)):
            value = compact(value)
        elif isinstance(value, bool):
            value = "true" if value else "false"
        parts.append(f"{key}={value}")
    plain["signature"] = hmac.new(secret.encode(), "&".join(parts).encode(), hashlib.md5).hexdigest()
    aes_key = "".join(random.choice(string.ascii_letters + string.digits) for _ in range(16)).encode()
    ciphertext = AES.new(aes_key, AES.MODE_CBC, iv=b"\0" * 16).encrypt(pad(compact(plain).encode(), 16))
    wrapped = PKCS1_OAEP.new(RSA.import_key(public_key), hashAlgo=SHA1).encrypt(aes_key)
    return {
        "key": base64.b64encode(wrapped).decode(),
        "data": base64.b64encode(ciphertext).decode(),
    }


@dataclass
class Account:
    server: str
    ref: str
    index: int

    @property
    def label(self) -> str:
        return f"账号{self.index}"


def parse_accounts() -> List[Account]:
    accounts: List[Account] = []
    for line in os.getenv("YYB_SERVER", "").splitlines():
        line = line.strip()
        if not line or "@" not in line:
            continue
        server, ref = line.rsplit("@", 1)
        server, ref = server.strip().rstrip("/"), ref.strip()
        if not server.startswith(("http://", "https://")):
            server = "http://" + server
        if server and ref:
            accounts.append(Account(server, ref, len(accounts) + 1))
    if not accounts:
        raise RuntimeError("未配置 YYB_SERVER（格式：地址@微信账号标识，多账号换行）")
    # YYB_SERVER 每一条配置都对应一个微信账号，按原顺序全部执行。
    return accounts


def load_cache() -> Dict[str, Any]:
    try:
        data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_cache(cache: Dict[str, Any]) -> None:
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp = CACHE_FILE.with_suffix(".tmp")
    temp.write_text(compact(cache), encoding="utf-8")
    os.chmod(temp, 0o600)
    temp.replace(CACHE_FILE)
    os.chmod(CACHE_FILE, 0o600)


class HRT:
    def __init__(self, account: Account):
        self.account = account
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": UA})
        self.token = ""

    def yyb(self, endpoint: str, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        body: Dict[str, Any] = {"ref": self.account.ref, "app_id": APP_ID}
        if extra:
            body.update(extra)
        response = self.session.post(self.account.server + endpoint, json=body, timeout=TIMEOUT)
        response.raise_for_status()
        outer = response.json()
        if int(outer.get("code", -1)) != 0:
            raise RuntimeError(f"YYB {endpoint}：{safe_message(outer.get('msg') or outer.get('message'))}")
        result = outer.get("data", {}).get("result")
        if isinstance(result, str):
            try:
                result = json.loads(result)
            except json.JSONDecodeError:
                pass
        if not isinstance(result, dict):
            raise RuntimeError(f"YYB {endpoint} 未返回有效对象")
        return result

    def encrypted_post(self, path: str, payload: Dict[str, Any], *, mini: bool = False) -> Dict[str, Any]:
        auth_id, secret, public_key = (
            (MINI_AUTH, MINI_SECRET, MINI_PUBLIC_KEY)
            if mini
            else (WEB_AUTH, WEB_SECRET, WEB_PUBLIC_KEY)
        )
        body = crypto4mid(path, payload, auth_id, secret, public_key)
        headers = {
            "Content-Type": "application/json",
            "x-Hrt-Mid-Appid": auth_id,
            "X-HRT-MID-NEWRISK": "NEWRISK" if mini else "newRisk",
            "Referer": f"https://servicewechat.com/{APP_ID}/160/page-frame.html" if mini else "https://cloud.huaruntong.cn/",
        }
        response = self.session.post(HRT_BASE + path, json=body, headers=headers, timeout=TIMEOUT)
        response.raise_for_status()
        result = response.json()
        if result.get("code") != SUCCESS:
            raise RuntimeError(f"{path}：{safe_message(result.get('msg') or result.get('code'))}")
        data = result.get("data")
        return data if isinstance(data, dict) else {}

    def h5_post(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        nonce, timestamp = str(uuid.uuid4()), int(time.time() * 1000)
        signature = hashlib.md5("".join(sorted([H5_APP_ID, H5_AUTH_SECRET, str(timestamp), nonce])).encode()).hexdigest()
        body = {
            "auth": {"appid": H5_APP_ID, "nonce": nonce, "timestamp": timestamp, "signature": signature},
            **payload,
        }
        response = self.session.post(
            HRT_BASE + path,
            json=body,
            headers={"Content-Type": "application/json;charset=UTF-8", "Origin": "https://cloud.huaruntong.cn", "Referer": "https://cloud.huaruntong.cn/"},
            timeout=TIMEOUT,
        )
        response.raise_for_status()
        result = response.json()
        if result.get("code") != SUCCESS:
            raise RuntimeError(f"{path}：{safe_message(result.get('msg') or result.get('code'))}")
        data = result.get("data")
        return data if isinstance(data, dict) else {}

    def valid_token(self, token: str) -> bool:
        if not token:
            return False
        self.token = token
        try:
            # 业务接口成功才视为有效；checkLogined 对请求上下文字段更敏感。
            self.points()
            return True
        except Exception:
            return False

    def get_user_info(self) -> Dict[str, str]:
        result = self.yyb(
            "/wxapp/operateWxData",
            {"payload": {"api_name": "getUserInfo", "data": {"withCredentials": True}, "env": 1}},
        )
        raw = result.get("rawData") or result.get("raw_data") or result.get("data") or ""
        return {
            "rawData": str(raw),
            "signature": str(nested(result, "signature") or ""),
            "encryptedData": str(nested(result, "encryptedData", "encrytData", "encrypted_data") or ""),
            "iv": str(nested(result, "iv") or ""),
            "cloudID": str(nested(result, "cloudID", "cloudId", "cloud_id") or ""),
        }

    def yyb_login(self) -> str:
        # HAR 中的真实登录是两段式：自动登录检查返回 token 或 randomCode；
        # 返回 randomCode 时，再用微信手机号授权 code 调 miniProgramLogin。
        code_result = self.yyb("/wxapp/getCode")
        code = str(nested(code_result, "code") or "")
        if len(code) < 8:
            raise RuntimeError("YYB getCode 未返回有效微信 code")
        info = self.get_user_info()
        if not all(info.get(key) for key in ("rawData", "signature", "encryptedData", "iv")):
            raise RuntimeError("YYB 用户资料字段不完整")
        payload = {
            "code": code,
            "userInfoEncryptedData": {
                "rawData": info["rawData"],
                "signature": info["signature"],
                "encryptedData": info["encryptedData"],
                "iv": info["iv"],
            },
            # 小程序源码先传 WECHAT，但 fetch 封装在加密前会统一覆盖为 APP。
            "channelId": "APP",
            "businessChannel": "WeChatAPP_Member",
            "deviceChannel": "WECHAT",
            "merchantCode": "1651200000001",
            "shopId": "A606",
            "typeId": "10005",
        }
        data = self.encrypted_post("/api/user/member/login/upgraded/miniProgramAutoLoginCheck", payload, mini=True)
        token = str(data.get("token") or "")
        if token:
            return token
        random_code = str(data.get("randomCode") or "")
        if not random_code:
            raise RuntimeError("自动登录检查既未返回 token，也未返回 randomCode")

        phone = self.yyb("/wxapp/getPhoneNumber")
        phone_code = str(nested(phone, "code") or "")
        if not phone_code:
            raise RuntimeError("YYB getPhoneNumber 未返回有效微信手机号授权 code")
        login_payload = {
            "code": phone_code,
            "randomCode": random_code,
            "channelId": "APP",
            "businessChannel": "WeChatAPP_Member",
            "deviceChannel": "WECHAT",
            "merchantCode": "1999000000001",
            "shopId": "117",
            "typeId": "10005",
        }
        login_data = self.encrypted_post(
            "/api/user/member/login/upgraded/miniProgramLogin", login_payload, mini=True
        )
        token = str(login_data.get("token") or "")
        if token:
            return token
        raise RuntimeError("手机号授权登录成功响应中未返回 token")

    def login(self, cached: str, imported: str) -> str:
        for source, token in (("缓存", cached), ("HRT_TOKEN", imported)):
            if token and self.valid_token(token):
                log(f"🔑 登录：{source}有效")
                return token
        token = self.yyb_login()
        if not self.valid_token(token):
            raise RuntimeError("业务登录校验失败")
        log("🔑 登录：YYB-Go-Enhanced 成功")
        return token

    def points(self) -> int:
        data = self.h5_post(
            "/api/points/querySummary",
            {"channelId": "APP", "sysId": "T0000001", "transactionUuid": str(uuid.uuid4()), "pointsType": "100000", "token": self.token},
        )
        value = nested(data, "availablePoints", "points")
        if value is None:
            raise RuntimeError("积分接口未返回可用积分")
        return int(value)

    def sign_state(self) -> Dict[str, Any]:
        return self.h5_post(
            "/api/points/queryWeekSignin",
            {"token": self.token, "transactionUuid": str(uuid.uuid4())},
        )

    def sign(self) -> int:
        data = self.h5_post(
            "/api/points/saveQuestionSignin",
            {
                "token": self.token,
                "answerResult": 1,
                "channelId": "APP",
                "merchantCode": "1641000001532",
                "storeCode": "qiandaosonjifen",
                "sysId": "T0000001",
                "transactionUuid": str(uuid.uuid4()),
                "inviteCode": "",
            },
        )
        return int(data.get("point") or 0)

    def run(self) -> Dict[str, Any]:
        before = self.points()
        state = self.sign_state()
        already = str(state.get("isTodaySignin") or "").upper() == "S"
        api_added = 0 if already else self.sign()
        if not already:
            for wait in (1, 2, 4):
                time.sleep(wait)
                if str(self.sign_state().get("isTodaySignin") or "").upper() == "S":
                    break
            else:
                raise RuntimeError("签到接口已提交，但复查状态仍未签到")
        after = before
        for wait in (0, 2, 4):
            if wait:
                time.sleep(wait)
            after = self.points()
            if already or after - before >= api_added:
                break
        added = after - before
        if not already and api_added > 0 and added == 0:
            added = api_added
        return {"before": before, "added": added, "after": after, "already": already}


def main() -> int:
    try:
        accounts = parse_accounts()
    except Exception as exc:
        err(f"🚫 配置错误：{safe_message(exc)}")
        return 1
    # 序号白名单筛选（1 起）
    if YYB_ONLY_REFS:
        wanted = set(int(x) for x in YYB_ONLY_REFS if str(x).strip().isdigit() and int(x) > 0)
        if wanted:
            accounts = [a for a in accounts if a.index in wanted]
            log(f"ℹ️ 按 YYB_ONLY_REFS 筛选：请求序号 {sorted(wanted)}，命中 {len(accounts)} 个账号")
            if not accounts:
                err("❌ YYB_ONLY_REFS 指定的序号均超出账号范围")
                return 1
    log(f"🚀 华润通 签到开始 · 共 {len(accounts)} 个账号")
    cache = load_cache()
    imported = [x.strip() for x in os.getenv("HRT_TOKEN", "").splitlines() if x.strip()]
    failures = 0
    for pos, account in enumerate(accounts, 1):
        acc_banner(pos, len(accounts), "标识：" + str(account.ref))
        client = HRT(account)
        try:
            cached_entry = cache.get(account.ref) or cache.get(f"__account_{account.index}__") or {}
            token = client.login(str(cached_entry.get("token") or ""), imported[account.index - 1] if account.index <= len(imported) else "")
            client.token = token
            cache[account.ref] = {"token": token, "updated_at": int(time.time())}
            save_cache(cache)
            result = client.run()
            status = "今日已签到" if result["already"] else "签到成功"
            icon = "🟡" if result["already"] else "✅"
            log(f"{icon} {account.label}：{status}")
            log(f"📊 签到前积分：{result['before']}")
            log(f"🎁 本次增加积分：{result['added']}")
            log(f"📈 签到后积分：{result['after']}")
        except Exception as exc:
            failures += 1
            err(f"❌ {account.label}：失败 · 原因：{safe_message(exc)}")
        acc_footer(pos, len(accounts))
    log(_ACC_RULE)
    log(f"📊 [执行汇总] 华润通 · 账号 {len(accounts)} ｜ 成功 {len(accounts) - failures} ｜ 失败 {failures}")
    log(_ACC_RULE)    
    # 错误日志推送：有错误才发；未配置通知渠道则只提示一行
    flush_notify(
        "华润通",
        f"账号 {len(accounts)} ｜ 成功 {len(accounts) - failures} ｜ 失败 {failures}",
        logger=log,
    )
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
