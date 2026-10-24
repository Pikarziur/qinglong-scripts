#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# =========================================================
# name:  森空岛签到
# cron: 35 5 * * *
# 积分有效期：永久有效
# =========================================================
#
# 任务流程：
#   1. 读取 SKYLAND_TOKEN（或下方 TOKENS 常量）里的森空岛通行证 token
#   2. token → grant code → cred + 签名密钥（走鹰角 OAuth 换会话）
#   3. 拉取绑定角色列表，对每个角色调用对应游戏的签到接口
#   4. 解析奖励明细，区分「签到成功 / 今日已签 / 失败」，输出执行汇总
# 可控参数（环境变量名统一 SKYLAND_ 前缀 + 全大写；环境变量优先于下方常量）
#   SKYLAND_TOKEN      首选。森空岛 token，一行一个；也支持 ; 或 , 分隔（兼容社区习惯）
#   SKYLAND_ACCOUNTS   兜底。账号密码，一行一个「手机号,密码」；也支持 # 或 | 分隔
#   TOKENS             顶部常量，格式同上（SKYLAND_TOKEN 优先）
#   ACCOUNTS           顶部常量，格式 [("手机号","密码"), ...]（SKYLAND_ACCOUNTS 优先）
#
# 策略：token 优先。只有当 token 方式有账号失败时，才自动改用账号密码兜底重试。
#   · token 只需在取 token 时登录一次，日常签到的认证面最小（推荐）
#   · 账号密码每次运行都会重新登录，认证面大，偶发人机验证/异地风控的概率更高
#   → 两个都配：平时走 token，token 被登出/清理会话失效时密码自动顶上
#
# token 获取：登录森空岛官网 → 打开
#   https://web-api.skland.com/account/info/hg
#   → 复制 data.content 里那串长字符串（也支持直接粘贴整段 JSON，会自动取 content）
#
# ⚠️ 不要在森空岛官网/App 登出账号，也不要点 App 的「清理会话」——会让 token 立即失效
#
# 依赖：requests、cryptography（设备指纹 dId 的数美加密需要）
#   设备指纹代码已内嵌在本脚本内，单文件即可运行；dId 首次生成后会缓存
#   到 /ql/data/config/skland_did.json（7 天有效），之后不再请求数美接口
#
# 日志规范：[LEVEL] [SKYLAND] message   （LEVEL: INFO / WARN / ERROR）
# =========================================================

import hashlib
import hmac
import json
import os
import tempfile
import threading
import time
from urllib import parse

import requests


# ────────────────────────────────────────────
# 设备指纹 dId（森空岛/数美反爬）
#   服务端会校验 dId 真伪（编造的会返回「设备信息无效」code=10001），
#   所以必须由数美 deviceprofile 接口真实签发，不能随便填。
#   但同一个 dId 可以长期复用，因此这里做了本地缓存，不必每次都请求数美。
# ────────────────────────────────────────────
import base64
import gzip
import hashlib
# 数美加密方法类
import json
import time
import uuid
import warnings

# cryptography 对单密钥 TripleDES 的弃用警告会污染青龙日志，这里静音（功能不受影响）
try:
    from cryptography.utils import CryptographyDeprecationWarning
    warnings.filterwarnings("ignore", category=CryptographyDeprecationWarning)
except Exception:
    pass

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers.algorithms import AES
from cryptography.hazmat.decrepit.ciphers.algorithms import TripleDES
from cryptography.hazmat.primitives.ciphers.base import Cipher
from cryptography.hazmat.primitives.ciphers.modes import CBC, ECB

# 查询dId请求头
devices_info_url = "https://fp-it.portal101.cn/deviceprofile/v4"

# 数美配置
SM_CONFIG = {
    "organization": "UWXspnCCJN4sfYlNfqps",
    "appId": "default",
    "publicKey": "MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQCmxMNr7n8ZeT0tE1R9j/mPixoinPkeM+k4VGIn/s0k7N5rJAfnZ0eMER+QhwFvshzo0LNmeUkpR8uIlU/GEVr8mN28sKmwd2gpygqj0ePnBmOW4v0ZVwbSYK+izkhVFk2V/doLoMbWy6b+UnA8mkjvg0iYWRByfRsK2gdl7llqCwIDAQAB",
    "protocol": "https",
    "apiHost": "fp-it.portal101.cn"
}

PK = serialization.load_der_public_key(base64.b64decode(SM_CONFIG['publicKey']))

DES_RULE = {
    "appId": {
        "cipher": "DES",
        "is_encrypt": 1,
        "key": "uy7mzc4h",
        "obfuscated_name": "xx"
    },
    "box": {
        "is_encrypt": 0,
        "obfuscated_name": "jf"
    },
    "canvas": {
        "cipher": "DES",
        "is_encrypt": 1,
        "key": "snrn887t",
        "obfuscated_name": "yk"
    },
    "clientSize": {
        "cipher": "DES",
        "is_encrypt": 1,
        "key": "cpmjjgsu",
        "obfuscated_name": "zx"
    },
    "organization": {
        "cipher": "DES",
        "is_encrypt": 1,
        "key": "78moqjfc",
        "obfuscated_name": "dp"
    },
    "os": {
        "cipher": "DES",
        "is_encrypt": 1,
        "key": "je6vk6t4",
        "obfuscated_name": "pj"
    },
    "platform": {
        "cipher": "DES",
        "is_encrypt": 1,
        "key": "pakxhcd2",
        "obfuscated_name": "gm"
    },
    "plugins": {
        "cipher": "DES",
        "is_encrypt": 1,
        "key": "v51m3pzl",
        "obfuscated_name": "kq"
    },
    "pmf": {
        "cipher": "DES",
        "is_encrypt": 1,
        "key": "2mdeslu3",
        "obfuscated_name": "vw"
    },
    "protocol": {
        "is_encrypt": 0,
        "obfuscated_name": "protocol"
    },
    "referer": {
        "cipher": "DES",
        "is_encrypt": 1,
        "key": "y7bmrjlc",
        "obfuscated_name": "ab"
    },
    "res": {
        "cipher": "DES",
        "is_encrypt": 1,
        "key": "whxqm2a7",
        "obfuscated_name": "hf"
    },
    "rtype": {
        "cipher": "DES",
        "is_encrypt": 1,
        "key": "x8o2h2bl",
        "obfuscated_name": "lo"
    },
    "sdkver": {
        "cipher": "DES",
        "is_encrypt": 1,
        "key": "9q3dcxp2",
        "obfuscated_name": "sc"
    },
    "status": {
        "cipher": "DES",
        "is_encrypt": 1,
        "key": "2jbrxxw4",
        "obfuscated_name": "an"
    },
    "subVersion": {
        "cipher": "DES",
        "is_encrypt": 1,
        "key": "eo3i2puh",
        "obfuscated_name": "ns"
    },
    "svm": {
        "cipher": "DES",
        "is_encrypt": 1,
        "key": "fzj3kaeh",
        "obfuscated_name": "qr"
    },
    "time": {
        "cipher": "DES",
        "is_encrypt": 1,
        "key": "q2t3odsk",
        "obfuscated_name": "nb"
    },
    "timezone": {
        "cipher": "DES",
        "is_encrypt": 1,
        "key": "1uv05lj5",
        "obfuscated_name": "as"
    },
    "tn": {
        "cipher": "DES",
        "is_encrypt": 1,
        "key": "x9nzj1bp",
        "obfuscated_name": "py"
    },
    "trees": {
        "cipher": "DES",
        "is_encrypt": 1,
        "key": "acfs0xo4",
        "obfuscated_name": "pi"
    },
    "ua": {
        "cipher": "DES",
        "is_encrypt": 1,
        "key": "k92crp1t",
        "obfuscated_name": "bj"
    },
    "url": {
        "cipher": "DES",
        "is_encrypt": 1,
        "key": "y95hjkoo",
        "obfuscated_name": "cf"
    },
    "version": {
        "is_encrypt": 0,
        "obfuscated_name": "version"
    },
    "vpw": {
        "cipher": "DES",
        "is_encrypt": 1,
        "key": "r9924ab5",
        "obfuscated_name": "ca"
    }
}

BROWSER_ENV = {
    'plugins': 'MicrosoftEdgePDFPluginPortableDocumentFormatinternal-pdf-viewer1,MicrosoftEdgePDFViewermhjfbmdgcfjbbpaeojofohoefgiehjai1',
    'ua': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36 Edg/129.0.0.0',
    'canvas': '259ffe69',  # 基于浏览器的canvas获得的值，不知道复用行不行
    'timezone': -480,  # 时区，应该是固定值吧
    'platform': 'Win32',
    'url': 'https://www.skland.com/',  # 固定值
    'referer': '',
    'res': '1920_1080_24_1.25',  # 屏幕宽度_高度_色深_window.devicePixelRatio
    'clientSize': '0_0_1080_1920_1920_1080_1920_1080',
    'status': '0011',  # 不知道在干啥
}


# // 将浏览器环境对象的key全部排序，然后对其所有的值及其子对象的值加入数字并字符串相加。若值为数字，则乘以10000(0x2710)再将其转成字符串存入数组,最后再做md5,存入tn变量（tn变量要做加密）
# //把这个对象用加密规则进行加密，然后对结果做GZIP压缩（结果是对象，应该有序列化），最后做AES加密（加密细节目前不清除），密钥为变量priId
# //加密规则：新对象的key使用相对应加解密规则的obfuscated_name值，value为字符串化后进行进行DES加密，再进行btoa加密

# 通过测试
def _DES(o: dict):
    result = {}
    for i in o.keys():
        if i in DES_RULE.keys():
            rule = DES_RULE[i]
            res = o[i]
            if rule['is_encrypt'] == 1:
                c = Cipher(TripleDES(rule['key'].encode('utf-8')), ECB())
                data = str(res).encode('utf-8')
                # 补足字节
                data += b'\x00' * 8
                res = base64.b64encode(c.encryptor().update(data)).decode('utf-8')
            result[rule['obfuscated_name']] = res
        else:
            result[i] = o[i]
    return result


# 通过测试
def _AES(v: bytes, k: bytes):
    iv = '0102030405060708'
    key = AES(k)
    c = Cipher(key, CBC(iv.encode('utf-8')))
    c.encryptor()
    # 填充明文
    v += b'\x00'
    while len(v) % 16 != 0:
        v += b'\x00'
    return c.encryptor().update(v).hex()


def GZIP(o: dict):
    # 这个压缩结果似乎和前台不太一样,不清楚是否会影响
    json_str = json.dumps(o, ensure_ascii=False)
    stream = gzip.compress(json_str.encode('utf-8'), 2, mtime=0)
    return base64.b64encode(stream)


# 获得tn的值,后续做DES加密用
# 通过测试
def get_tn(o: dict):
    sorted_keys = sorted(o.keys())

    result_list = []

    for i in sorted_keys:
        v = o[i]
        if isinstance(v, (int, float)):
            v = str(v * 10000)
        elif isinstance(v, dict):
            v = get_tn(v)
        result_list.append(v)
    return ''.join(result_list)


def get_smid():
    t = time.localtime()
    _time = '{}{:0>2d}{:0>2d}{:0>2d}{:0>2d}{:0>2d}'.format(t.tm_year, t.tm_mon, t.tm_mday, t.tm_hour, t.tm_min,
                                                           t.tm_sec)
    uid = str(uuid.uuid4())
    v = _time + hashlib.md5(uid.encode('utf-8')).hexdigest() + '00'
    smsk_web = hashlib.md5(('smsk_web_' + v).encode('utf-8')).hexdigest()[0:14]
    return v + smsk_web + '0'


def get_d_id():
    # storageName = '.thumbcache_' + md5(SM_CONFIG['organization']) // 用于从本地存储获得值
    # uid = uuid()
    # priId=md5(uid)[0:16]
    # ep=rsa(uid,publicKey)
    # SMID = localStorage.get(storageName);// 获得本地存储存的值
    # _0x30b2eb为递归md5

    uid = str(uuid.uuid4()).encode('utf-8')
    priId = hashlib.md5(uid).hexdigest()[0:16]
    # ep不一定对，先走走看
    ep = PK.encrypt(uid, padding.PKCS1v15())
    ep = base64.b64encode(ep).decode('utf-8')

    browser = BROWSER_ENV.copy()
    current_time = int(time.time() * 1000)
    browser.update({
        'vpw': str(uuid.uuid4()),
        'svm': current_time,
        'trees': str(uuid.uuid4()),
        'pmf': current_time
    })

    des_target = {
        **browser,
        'protocol': 102,
        'organization': SM_CONFIG['organization'],
        'appId': SM_CONFIG['appId'],
        'os': 'web',
        'version': '3.0.0',
        'sdkver': '3.0.0',
        'box': '',  # 似乎是个SMID，但是第一次的时候是空,不过不影响结果
        'rtype': 'all',
        'smid': get_smid(),
        'subVersion': '1.0.0',
        'time': 0
    }
    des_target['tn'] = hashlib.md5(get_tn(des_target).encode()).hexdigest()

    des_result = _AES(GZIP(_DES(des_target)), priId.encode('utf-8'))

    response = requests.post(devices_info_url, json={
        'appId': 'default',
        'compress': 2,
        'data': des_result,
        'encode': 5,
        'ep': ep,
        'organization': SM_CONFIG['organization'],
        'os': 'web'  # 固定值
    })

    resp = response.json()
    if resp['code'] != 1100:
        raise Exception("did计算失败，请联系作者")
    # 开头必须是B
    return 'B' + resp['detail']['deviceId']

# ────────────────────────────────────────────
# 内部配置：直接改这里，省得去面板设环境变量
# （环境变量 SKYLAND_TOKEN / SKYLAND_ACCOUNTS 优先于这里）
# ────────────────────────────────────────────
# 首选：森空岛 token（登录面最小，只在取 token 时登录一次）
TOKENS = [
    # "<在此粘贴森空岛 token>",
    # 多账号就多写几行，每个一行
]

# 兜底：鹰角通行证账号密码（token 失效时自动顶上，密码不变就永不需要手动维护）
ACCOUNTS = [
    # ("13800138000", "你的鹰角通行证密码"),
]

# ────────────────────────────────────────────
# 接口常量
# ────────────────────────────────────────────
APP_CODE = "4ca99fa6b56cc2ba"                 # 森空岛 OAuth 的 appCode
SKYLAND_WEB = "https://zonai.skland.com"
SIGN_URL = {
    "arknights": SKYLAND_WEB + "/api/v1/game/attendance",
    "endfield": SKYLAND_WEB + "/web/v1/game/endfield/attendance",
}
BINDING_URL = SKYLAND_WEB + "/api/v1/game/player/binding"
GRANT_CODE_URL = "https://as.hypergryph.com/user/oauth2/v2/grant"
CRED_CODE_URL = SKYLAND_WEB + "/web/v1/user/auth/generate_cred_by_code"
TOKEN_PASSWORD_URL = "https://as.hypergryph.com/user/auth/v1/token_by_phone_password"

TIMEOUT = 25

# ────────────────────────────────────────────
# 统一日志
# ────────────────────────────────────────────
# 只推错误：本次运行出现 ERROR 日志才推送一次；正常跑完不打扰。
# 直接调用青龙自带的通知模块（容器内为 /ql/data/scripts/notify.py，仓库内为同目录/上一级的 notify.py）——
#   在青龙面板「通知设置」里配一次即可全站通用（该文件由青龙官方维护，支持其全部推送渠道）。
# 找不到该文件、或未配置任何通知渠道时，只在日志末尾提示一行，不报错、不中断。
import os as _os
import sys as _sys

_QN_SITE = "SKYLAND"
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
    """收尾调用：本次有 ERROR 才推送；未配置或找不到青龙 notify 时只提示一行。"""
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

_qn_sys_excepthook = _sys.excepthook


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


_sys.excepthook = _qn_excepthook


def _emit(level, msg):
    line = f"[{level}] [SKYLAND] {msg}"
    print(line, flush=True)
    if level == "ERROR":
        collect_error(line)


def log(msg):   _emit("INFO", msg)
def warn(msg):  _emit("WARN", msg)
def err(msg):   _emit("ERROR", msg)

# ────────────────────────────────────────────
# 账号分隔标识
# 多账号同跑时，用醒目的横线 + 账号信息把各账号的日志隔开，便于阅读与定位。
# ────────────────────────────────────────────
_ACC_RULE = "=" * 70


def acc_banner(idx, total, ident=""):
    """打印账号分隔标识（开始）。"""
    log(_ACC_RULE)
    log("👤 账号 %d/%d%s" % (idx, total, (" ｜ " + str(ident)) if ident else ""))


def acc_footer(idx, total, ident=""):
    """打印账号分隔标识（结束）。"""
    log(_ACC_RULE)
    log("👤 账号 %d/%d%s 结束" % (idx, total, (" ｜ " + str(ident)) if ident else ""))

# ────────────────────────────────────────────
# 设备指纹 dId（数美反爬，失败会重试）
#   dId 由数美 deviceprofile 接口真实签发，服务端会校验真伪（编造的会被拒：
#   「设备信息无效」code=10001）。但同一个真实 dId 可以长期复用，
#   所以这里做了本地缓存 —— 首次生成后存盘，之后不再请求数美接口。
# ────────────────────────────────────────────
_did_lock = threading.Lock()
_did_cache = None
_DID_TTL = 7 * 24 * 3600          # 缓存有效期 7 天
_DID_CACHE_FILES = [
    "/ql/data/config/skland_did.json",
    os.path.join(tempfile.gettempdir(), "skland_did.json"),
]


def _read_did_cache():
    """读本地缓存的 dId。返回 None 表示没有或已过期。"""
    for path in _DID_CACHE_FILES:
        try:
            if not os.path.isfile(path):
                continue
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            did = data.get("did")
            ts = int(data.get("ts") or 0)
            if not did or not str(did).startswith("B"):
                continue
            if time.time() - ts > _DID_TTL:
                continue
            return str(did)
        except Exception:
            continue
    return None


def _write_did_cache(did):
    """把 dId 存到本地缓存（第一个可写路径）。"""
    payload = json.dumps({"did": did, "ts": int(time.time())}, ensure_ascii=False)
    for path in _DID_CACHE_FILES:
        try:
            d = os.path.dirname(path)
            if d and not os.path.isdir(d):
                continue
            with open(path, "w", encoding="utf-8") as f:
                f.write(payload)
            return path
        except Exception:
            continue
    return None


def get_did():
    """取设备指纹 dId：优先本地缓存 → 否则请求数美生成并缓存。失败重试 3 次。"""
    global _did_cache
    with _did_lock:
        if _did_cache:
            return _did_cache
        cached = _read_did_cache()
        if cached:
            _did_cache = cached
            log("🔏 使用缓存的设备指纹 dId（%s…）" % cached[:10])
            return _did_cache
        last = None
        for i in range(3):
            try:
                did = get_d_id()
                path = _write_did_cache(did)
                _did_cache = did
                log("🔏 已生成设备指纹 dId（%s…）%s"
                    % (did[:10], ("并缓存到 %s" % path) if path else ""))
                return _did_cache
            except Exception as e:
                last = e
                time.sleep(1.5)
        raise Exception("设备指纹 dId 获取失败：%s" % last)


# ────────────────────────────────────────────
# 请求头
# ────────────────────────────────────────────
_UA = ("Mozilla/5.0 (Linux; Android 12; SM-A5560 Build/V417IR; wv) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Version/4.0 Chrome/101.0.4951.61 Safari/537.36; SKLand/1.52.1")


def base_header():
    return {
        "cred": "",
        "User-Agent": _UA,
        "Accept-Encoding": "gzip",
        "Connection": "close",
        "X-Requested-With": "com.hypergryph.skland",
    }


def login_header():
    h = base_header()
    h["dId"] = get_did()
    return h

# ────────────────────────────────────────────
# 签名
#   拼接串 = 路径 + (GET的query / POST的body) + 时间戳 + 四个签名头(紧凑JSON)
#   sign   = MD5( HMAC-SHA256(拼接串, 密钥=cred接口返回的token) )
#   注意：四个签名头顺序必须固定，除 timestamp 外值可任意填
# ────────────────────────────────────────────
def generate_signature(path, body_or_query, token, did):
    t = str(int(time.time()))
    header_ca = {
        "platform": "3",
        "timestamp": t,
        "dId": did,
        "vName": "1.0.0",
    }
    header_ca_str = json.dumps(header_ca, separators=(",", ":"))
    s = path + body_or_query + t + header_ca_str
    hex_s = hmac.new(token.encode("utf-8"), s.encode("utf-8"), hashlib.sha256).hexdigest()
    return hashlib.md5(hex_s.encode("utf-8")).hexdigest(), header_ca


def sign_header(url, method, body, token, cred, did):
    p = parse.urlparse(url)
    h = base_header()
    h["cred"] = cred
    if method.lower() == "get":
        h["sign"], header_ca = generate_signature(p.path, p.query, token, did)
    else:
        payload = json.dumps(body) if body is not None else ""
        h["sign"], header_ca = generate_signature(p.path, payload, token, did)
    for k in header_ca:
        h[k] = header_ca[k]
    return h

# ────────────────────────────────────────────
# 登录链路：token → grant code → cred + 签名密钥
# ────────────────────────────────────────────
def parse_user_token(t):
    """支持直接粘贴官网返回的整段 JSON，自动取 data.content。"""
    try:
        return json.loads(t)["data"]["content"]
    except Exception:
        return t.strip()


def get_grant_code(token):
    r = requests.post(GRANT_CODE_URL, json={
        "appCode": APP_CODE,
        "token": token,
        "type": 0,
    }, headers=login_header(), timeout=TIMEOUT)
    try:
        resp = r.json()
    except Exception:
        raise Exception("获得认证代码失败：HTTP %s" % r.status_code)
    if r.status_code != 200 or resp.get("status") != 0:
        raise Exception(
            "获得认证代码失败：%s ｜ 若提示 token 无效/未登录，说明森空岛通行证已失效"
            "（常见于在官网/App 登出账号或点了「清理会话」），"
            "请重新登录森空岛官网取 token，或改用脚本顶部 ACCOUNTS 的账号密码登录"
            % (resp.get("msg") or resp))
    return resp["data"]["code"]


def get_cred(grant_code):
    r = requests.post(CRED_CODE_URL, json={
        "code": grant_code,
        "kind": 1,
    }, headers=login_header(), timeout=TIMEOUT)
    try:
        resp = r.json()
    except Exception:
        raise Exception("获得 cred 失败：HTTP %s" % r.status_code)
    if resp.get("code") != 0:
        raise Exception("获得 cred 失败：%s" % (resp.get("message") or resp))
    return resp["data"]


def get_cred_by_token(token):
    return get_cred(get_grant_code(token))


def login_by_password(phone, password):
    """手机号 + 密码登录，换成 token。"""
    r = requests.post(TOKEN_PASSWORD_URL, json={
        "phone": phone,
        "password": password,
    }, headers=login_header(), timeout=TIMEOUT)
    try:
        resp = r.json()
    except Exception:
        raise Exception("密码登录失败：HTTP %s" % r.status_code)
    if resp.get("status") != 0:
        raise Exception("密码登录失败：%s" % (resp.get("msg") or resp))
    return resp["data"]["token"]

# ────────────────────────────────────────────
# 角色与签到
# ────────────────────────────────────────────
def get_binding_list(token, cred, did):
    """拉取绑定角色（明日方舟 / 终末地）。"""
    h = sign_header(BINDING_URL, "get", None, token, cred, did)
    r = requests.get(BINDING_URL, headers=h, timeout=TIMEOUT)
    try:
        resp = r.json()
    except Exception:
        raise Exception("角色列表请求失败：HTTP %s" % r.status_code)
    if resp.get("code") != 0:
        msg = resp.get("message") or ""
        if "登录" in msg:
            raise Exception("登录态已失效，请重新获取森空岛 token")
        raise Exception("请求角色列表失败：%s" % msg)
    out = []
    for g in (resp.get("data") or {}).get("list", []):
        code = g.get("appCode")
        if code not in ("arknights", "endfield"):
            continue
        for b in (g.get("bindingList") or []):
            b["appCode"] = code
            out.append(b)
    return out


def _fmt_awards(awards):
    """把 awards 数组拼成「初级作战记录×3」这种文案。"""
    parts = []
    for a in (awards or []):
        res = a.get("resource") or {}
        name = res.get("name") or "未知道具"
        cnt = a.get("count") or 1
        parts.append("%s×%s" % (name, cnt))
    return "、".join(parts) if parts else "（无明细）"


def sign_arknights(role, token, cred, did):
    """明日方舟签到。返回 (状态, 说明) —— 状态 ∈ success/already/fail"""
    body = {"gameId": role.get("gameId"), "uid": role.get("uid")}
    url = SIGN_URL["arknights"]
    h = sign_header(url, "post", body, token, cred, did)
    r = requests.post(url, headers=h, json=body, timeout=TIMEOUT)
    try:
        resp = r.json()
    except Exception:
        return "fail", "响应解析失败（HTTP %s）" % r.status_code
    if resp.get("code") != 0:
        msg = resp.get("message") or ""
        if "已签到" in msg or "重复" in msg:
            return "already", msg
        return "fail", msg or "未知错误"
    data = resp.get("data") or {}
    return "success", _fmt_awards(data.get("awards"))


def sign_endfield(role, token, cred, did):
    """终末地签到（需要角色列表里有 roleId/serverId）。"""
    results = []
    for r in (role.get("roles") or []):
        nickname = r.get("nickname") or ""
        url = SIGN_URL["endfield"]
        h = sign_header(url, "post", None, token, cred, did)
        h.update({
            "Content-Type": "application/json",
            "sk-game-role": "3_%s_%s" % (r.get("roleId"), r.get("serverId")),
            "referer": "https://game.skland.com/",
            "origin": "https://game.skland.com/",
        })
        try:
            resp = requests.post(url, headers=h, timeout=TIMEOUT).json()
        except Exception as e:
            results.append(("fail", nickname, str(e)))
            continue
        if resp.get("code") != 0:
            results.append(("fail", nickname, resp.get("message") or "未知错误"))
            continue
        data = resp.get("data") or {}
        info = data.get("resourceInfoMap") or {}
        parts = []
        for a in (data.get("awardIds") or []):
            aid = a.get("id")
            item = info.get(aid) or {}
            parts.append("%s×%s" % (item.get("name") or "?", item.get("count") or 1))
        results.append(("success", nickname, "、".join(parts) or "（无明细）"))
    return results

# ────────────────────────────────────────────
# 主流程
# ────────────────────────────────────────────
def _split_items(raw):
    """按换行 / 分号 / 逗号切分环境变量值（兼容社区用 ; 分隔的习惯）。"""
    out = []
    for line in raw.replace(";", "\n").replace(",", "\n").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.append(line)
    return out


def load_tokens():
    """读取 token 列表：环境变量 SKYLAND_TOKEN 优先，其次顶部 TOKENS。"""
    raw = os.getenv("SKYLAND_TOKEN", "") or ""
    items = _split_items(raw)
    if not items:
        for x in TOKENS:
            x = str(x).strip()
            if x and not x.startswith("#"):
                items.append(x)
    # 去掉重复，保持顺序
    seen = set()
    out = []
    for t in items:
        t = parse_user_token(t)
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out


def load_password_accounts():
    """读取「手机号 + 密码」账号列表：环境变量 SKYLAND_ACCOUNTS 优先，其次顶部 ACCOUNTS。

    每行格式：手机号,密码（也支持 # 或 | 分隔）。
    """
    raw = os.getenv("SKYLAND_ACCOUNTS", "") or ""
    items = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        for sep in (",", "#", "|"):
            if sep in line:
                parts = line.split(sep, 1)
                items.append((parts[0].strip(), parts[1].strip()))
                break
    if not items:
        for x in ACCOUNTS:
            try:
                phone, pwd = x
            except Exception:
                continue
            phone, pwd = str(phone).strip(), str(pwd).strip()
            if phone and pwd:
                items.append((phone, pwd))
    return items


def run_account(idx, total, ident, token=None, pwd_account=None):
    """处理单个账号。

    token      —— 直接给定的森空岛 token
    pwd_account —— (手机号, 密码) 二元组，先自动登录换 token
    返回 (成功数, 失败数, 结果行列表)
    """
    acc_banner(idx, total, ident)
    ok = 0
    fail = 0
    lines = []
    try:
        if pwd_account:
            phone, pwd = pwd_account
            log("🔑 使用账号密码登录：%s" % phone)
            token = login_by_password(phone, pwd)
        cred_data = get_cred_by_token(token)
        cred = cred_data.get("cred", "")
        sign_key = cred_data.get("token", "")
        if not cred or not sign_key:
            raise Exception("cred 接口返回异常：缺少 cred 或签名密钥")
        did = get_did()
        log("🔑 会话建立成功（cred=%s…）" % cred[:8])

        roles = get_binding_list(sign_key, cred, did)
        if not roles:
            warn("没有查到绑定角色")
            lines.append("无绑定角色")
            return ok, fail + 1, lines

        for role in roles:
            game = role.get("gameName") or role.get("appCode")
            channel = role.get("channelName") or ""
            nick = role.get("nickName") or ""
            tag = "[%s] %s(%s)" % (game, nick, channel)
            if role.get("appCode") == "arknights":
                state, detail = sign_arknights(role, sign_key, cred, did)
                if state == "success":
                    ok += 1
                    log("🎉 %s 签到成功｜获得 %s" % (tag, detail))
                    lines.append("%s 签到成功｜%s" % (tag, detail))
                elif state == "already":
                    ok += 1
                    log("⏭️ %s 今日已签到｜%s" % (tag, detail))
                    lines.append("%s 今日已签到" % tag)
                else:
                    fail += 1
                    err("%s 签到失败：%s" % (tag, detail))
                    lines.append("%s 签到失败：%s" % (tag, detail))
            elif role.get("appCode") == "endfield":
                if not role.get("roles"):
                    log("ℹ️ %s 终末地没有可签到的角色，跳过" % tag)
                    lines.append("%s 终末地无角色，跳过" % tag)
                    continue
                for state, nick2, detail in sign_endfield(role, sign_key, cred, did):
                    t2 = "[%s] %s(%s)" % (game, nick2, channel)
                    if state == "success":
                        ok += 1
                        log("🎉 %s 签到成功｜获得 %s" % (t2, detail))
                        lines.append("%s 签到成功｜%s" % (t2, detail))
                    else:
                        fail += 1
                        err("%s 签到失败：%s" % (t2, detail))
                        lines.append("%s 签到失败：%s" % (t2, detail))
    except Exception as e:
        fail += 1
        err("账号处理失败：%s" % e)
        lines.append("账号处理失败：%s" % e)
    return ok, fail, lines


def _run_group(items, kind):
    """跑一组账号，返回 (成功数, 失败数, 结果行列表)。

    kind = "token" → items 是 token 字符串
    kind = "密码" → items 是 (手机号, 密码) 二元组
    """
    ok_sum = 0
    fail_sum = 0
    lines = []
    for i, it in enumerate(items, 1):
        if kind == "token":
            ident = "token=%s…" % it[:10]
            ok, fail, ls = run_account(i, len(items), ident, token=it)
        else:
            phone, pwd = it
            ident = "手机号 %s" % phone
            ok, fail, ls = run_account(i, len(items), ident, pwd_account=(phone, pwd))
        ok_sum += ok
        fail_sum += fail
        lines += ls
        acc_footer(i, len(items), ident)
        if i < len(items):
            time.sleep(2)
    return ok_sum, fail_sum, lines


def main():
    tokens = load_tokens()
    pwds = load_password_accounts()
    log("🚀 森空岛签到开始｜token 账号 %d ｜ 密码账号 %d" % (len(tokens), len(pwds)))
    if not tokens and not pwds:
        err("没有配置账号：二选一 —— ① 脚本顶部 TOKENS 填森空岛 token（环境变量 SKYLAND_TOKEN）；"
            "② 脚本顶部 ACCOUNTS 填 (\"手机号\", \"鹰角通行证密码\")（环境变量 SKYLAND_ACCOUNTS）")
        flush_notify(_QN_SITE, "未配置账号")
        return 1

    total_ok = 0
    total_fail = 0
    all_lines = []

    if tokens:
        ok_t, fail_t, lines_t = _run_group(tokens, "token")
        if fail_t == 0:
            # token 全部正常，直接用结果（不再跑密码，避免同一账号重复签到）
            total_ok += ok_t
            total_fail += fail_t
            all_lines += lines_t
        elif pwds:
            # token 有失败 → 改用账号密码兜底，结果以密码这一轮为准
            log("⚠️ token 方式有 %d 个账号未成功，自动改用账号密码兜底重试" % fail_t)
            ok_p, fail_p, lines_p = _run_group(pwds, "密码")
            total_ok += ok_p
            total_fail += fail_p
            all_lines += lines_p
        else:
            total_ok += ok_t
            total_fail += fail_t
            all_lines += lines_t
    else:
        ok_p, fail_p, lines_p = _run_group(pwds, "密码")
        total_ok += ok_p
        total_fail += fail_p
        all_lines += lines_p

    log("=" * 70)
    log("📊 [执行汇总] 森空岛 ｜ 签到成功 %d ｜ 失败 %d" % (total_ok, total_fail))
    for line in all_lines:
        log("  · %s" % line)
    flush_notify(_QN_SITE, "成功 %d ｜ 失败 %d" % (total_ok, total_fail))
    return 0 if total_fail == 0 else 1


if __name__ == "__main__":
    try:
        _rc = main()
    except Exception:
        raise
    _sys.exit(_rc)
