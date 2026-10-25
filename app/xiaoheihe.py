#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# =========================================================
# name:  小黑盒
# cron: 20 5 * * *
# 积分有效期：永久有效
# =========================================================
#
# 任务流程：
#   1. 读取凭据（两条支线，可只配一条或两条都配）
#      · token 支线：XHH_USER_PKEY —— 浏览器登录 www.xiaoheihe.cn 后的 user_pkey Cookie
#      · 密码支线：XHH_ACCOUNTS  —— 手机号 + 密码，脚本自动登录换 user_pkey
#   2. GET /account/restore_login/ 验证登录态并取回昵称
#   3. GET /task/list_v2/ 读取「签到」任务状态与奖励
#   4. 未签到则 GET /task/sign_v3/sign 完成签到
#   5. 复查签到状态，输出执行汇总
# 可控参数：
#   XHH_USER_PKEY   token 支线。一行一个 user_pkey，多账号换行；留空则用下方 ACCOUNTS
#   XHH_ACCOUNTS    密码支线。一行一个「手机号#密码」（先账号后密码）；
#                   多账号换行；也兼容逗号/竖线分隔，但 # 优先
#   XHH_HEYBOX_ID   可选。heybox_id 一般能从 user_pkey 自动解析，解析不出时按顺序补
#   ACCOUNTS / PWD_ACCOUNTS / HEYBOX_IDS   顶部常量，格式同上（环境变量优先）
#
# 说明：
#   · 接口走 web 端（xiaoheihe.cn），hkey 签名算法由前端 JS 逆向还原
#   · token 支线的 user_pkey 由服务端签发，有效期约 7 天；失效会明确提示重新获取
#   · 密码支线每次运行现登录换凭据，user_pkey 过期也不用管
#   · 依赖：cryptography（密码支线要 RSA 加密 + 设备指纹）；token 支线只用标准库
#
# 日志规范：[LEVEL] [XHH] message   （LEVEL: INFO / WARN / ERROR）
# =========================================================

import base64
import gzip
import hashlib
import json
import os
import random
import re
import time
import urllib.error
import urllib.request
import uuid
from urllib.parse import urlencode


# ────────────────────────────────────────────
# 设备指纹 dId（创蓝/数美反爬）——用于生成登录所需的 x_xhh_tokenid
#   森空岛登录接口要求请求头带 x_xhh_tokenid（89 字符设备指纹，B 开头）；
#   小黑盒 web 登录同样要求，且服务端会校验 dId 真伪（编造的会返回「设备信息无效」）。
#   这里直接复用同一套创蓝 deviceprofile 算法，只是 organization/appId/公钥换成小黑盒的。
# ────────────────────────────────────────────
SM_CONFIG = {
    "organization": "0yD85BjYvGFAvHaSQ1mc",
    "appId": "heybox_website",
    "publicKey": "MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQCXj9exmI4nQjmT52iwr+yf7hAQ06bfSZHTAHUfRBYiagCf/whhd8es0R79wBigpiHLd28TKA8b8mGR8OiiI1hV+qfynCWihvp3mdj8MiiH6SU3lhro2hkfYzImZB0RmWr2zE4Xt1+A6Oyp6bf+W7JSxYUXHw3nNv7Td4jw4jEFKQIDAQAB",
    "protocol": "https",
    "apiHost": "fp-it.portal101.cn"
}

# cryptography / requests 全部延迟导入 —— 只用 token 支线时无需安装任何第三方依赖

# 查询 dId 的接口
devices_info_url = "https://fp-it.portal101.cn/deviceprofile/v4"

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
    import warnings
    from cryptography.hazmat.decrepit.ciphers.algorithms import TripleDES
    from cryptography.hazmat.primitives.ciphers.base import Cipher
    from cryptography.hazmat.primitives.ciphers.modes import ECB
    # 单密钥 TripleDES 的弃用警告会污染青龙日志，这里静音（功能不受影响）
    try:
        from cryptography.utils import CryptographyDeprecationWarning
        warnings.filterwarnings("ignore", category=CryptographyDeprecationWarning)
    except Exception:
        pass
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
    from cryptography.hazmat.primitives.ciphers.algorithms import AES
    from cryptography.hazmat.primitives.ciphers.base import Cipher
    from cryptography.hazmat.primitives.ciphers.modes import CBC
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
    import requests
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    pk = serialization.load_der_public_key(base64.b64decode(SM_CONFIG['publicKey']))

    uid = str(uuid.uuid4()).encode('utf-8')
    priId = hashlib.md5(uid).hexdigest()[0:16]
    # ep不一定对，先走走看
    ep = pk.encrypt(uid, padding.PKCS1v15())
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
        'appId': 'heybox_website',
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


def get_xhh_tokenid():
    """生成小黑盒登录所需的 x_xhh_tokenid（89 字符，B 开头）。"""
    return get_d_id()

# ────────────────────────────────────────────
# 内部配置：直接改这里，省得去面板设环境变量
# ────────────────────────────────────────────
ACCOUNTS = [
    # "<在此粘贴 user_pkey>",
    # 多账号就多写几行，每个一行
]

# 账号密码登录（可选）：每行一个「手机号#密码」（先账号后密码）。填了就多一条支线：
#   脚本自动用手机号+密码登录小黑盒，拿到 user_pkey 后照常签到，user_pkey 过期也不用管。
#   手机号格式：+8613800138000（含国际区号）；也支持直接写 13800138000，脚本会自动补 +86。
#   环境变量 XHH_ACCOUNTS 用同样格式：一行一个「手机号#密码」。
PWD_ACCOUNTS = [
    # ("+8613800138000", "你的小黑盒密码"),
]

# 可选：heybox_id 一般能从 user_pkey 自动解析出来。
# 万一解析不出，按账号顺序在这里补（逗号分隔），例："22659548,12345678"
HEYBOX_IDS = []

# ────────────────────────────────────────────
# 接口常量
# ────────────────────────────────────────────
API = "https://api.xiaoheihe.cn"

# 小黑盒 App 版本号。服务端按它判定「是否达到签到奖励的版本门槛」，
# 填太低会返回「当前版本过低，无法获得任务奖励」。
CLIENT_VERSION = "1.3.398"

# web 端固定参数（x_app 必须是 heybox，填 heybox_website 服务端会走 web 分支判定版本）
BASE_PARAMS = {
    "app": "heybox",
    "client_type": "web",
    "os_type": "web",
    "version": "999.0.4",
    "web_version": "3.0",
    "x_client_type": "web",
    "x_app": "heybox",
    "x_os_type": "Windows",
    "x_client_version": CLIENT_VERSION,
}

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36")

TIMEOUT = 25

# ────────────────────────────────────────────
# 账号密码登录（可选支线）
#   web 前端登录流程：
#     POST /account/login/  body={}，query 带 phone_num + pwd（都用 RSA 加密）
#     请求头需带 x_xhh_tokenid（设备指纹）+ forbidden_auto_tologin:true
#   登录成功后服务端返回 pkey，前端把它写成 user_pkey cookie —— 直接拿来当凭据用。
# ────────────────────────────────────────────
# 登录接口的 version 必须用 App 版本（填 999.0.4 会报「验证参数错误」）
LOGIN_VERSION = CLIENT_VERSION

# 登录密码的 RSA 公钥（取自 web 前端 index-BcnLlQNw.js 的 Oc() 函数，jsencrypt/PKCS1v15）
LOGIN_RSA_PUBKEY = (
    "MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDZgjVwAiKTjZ55nG+mW6r3TSU4"
    "ECvNYqDMIS/bhCj2QaH5GI/KZb2TBp+CBvUj9SLFnmJQ0kzHzHoGZCQ88VevCffF7JePGF9cmK"
    "QqotlfTKbV4oxV5iLz7JSG6b/Vg7AXtrTolNtWsa8HiB0tI0YClYaQlOXm4UxLeSxQwSFETwIDAQAB"
)


def rsa_encrypt(text):
    """按前端 jsencrypt 的方式 RSA/PKCS1v15 加密，返回原始 base64。"""
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import padding
    except ImportError:
        raise Exception("缺少依赖 cryptography，请青龙「依赖管理 → Python3」添加：cryptography")
    pk = serialization.load_der_public_key(base64.b64decode(LOGIN_RSA_PUBKEY))
    return base64.b64encode(
        pk.encrypt(text.encode("utf-8"), padding.PKCS1v15())).decode("ascii")


def normalize_phone(phone):
    """手机号统一成国际格式 +8613800138000。"""
    p = str(phone or "").strip().replace(" ", "")
    if p.startswith("+"):
        return p
    if p.startswith("86") and len(p) == 13:
        return "+" + p
    return "+86" + p

# ────────────────────────────────────────────
# 统一日志
# ────────────────────────────────────────────
# 只推错误：本次运行出现 ERROR 日志才推送一次；正常跑完不打扰。
# 直接调用青龙自带的通知模块（容器内为 /ql/data/scripts/notify.py，仓库内为同目录/上一级的 notify.py）——
#   在青龙面板「通知设置」里配一次即可全站通用（该文件由青龙官方维护，支持其全部推送渠道）。
# 找不到该文件、或未配置任何通知渠道时，只在日志末尾提示一行，不报错、不中断。
import os as _os
import sys as _sys

_QN_SITE = "XHH"
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
    line = f"[{level}] [XHH] {msg}"
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
# hkey 签名（由小黑盒 web 前端 JS 逆向还原）
#   路径 → 字符表映射 → 三路交错 → MD5 → 前5位映射 + 2位校验和
# ────────────────────────────────────────────
_CHARSET = "AB45STUVWZEFGJ6CH01D237IXYPQRKLMN89"


def _gf_mul(e):
    """GF(2^8) 上的乘 2。"""
    return (255 & ((e << 1) ^ 27)) if (e & 128) else (e << 1)


def _gf_sq(e):
    return _gf_mul(e) ^ e


def _gf_cu(e):
    return _gf_sq(_gf_mul(e))


def _gf_ym(e):
    return _gf_cu(_gf_sq(_gf_mul(e)))


def _gf_gm(e):
    return _gf_ym(e) ^ _gf_cu(e) ^ _gf_sq(e)


def _km_mix(e_arr):
    """4 字节混合变换，输出用于计算校验和。

    注意：必须返回完整数组（长度与入参一致），只有前 4 位被替换，
    后面的字节保持原值并参与求和 —— 这是校验和的组成部分。
    """
    e = list(e_arr)
    t = [0, 0, 0, 0]
    t[0] = _gf_gm(e[0]) ^ _gf_ym(e[1]) ^ _gf_cu(e[2]) ^ _gf_sq(e[3])
    t[1] = _gf_sq(e[0]) ^ _gf_gm(e[1]) ^ _gf_ym(e[2]) ^ _gf_cu(e[3])
    t[2] = _gf_cu(e[0]) ^ _gf_sq(e[1]) ^ _gf_gm(e[2]) ^ _gf_ym(e[3])
    t[3] = _gf_ym(e[0]) ^ _gf_cu(e[1]) ^ _gf_sq(e[2]) ^ _gf_gm(e[3])
    e[0], e[1], e[2], e[3] = t[0], t[1], t[2], t[3]
    return e


def _map_cut(s, charset, n):
    """按字符表映射每个字符；n<0 表示取字符表前 len+n 个。"""
    i = charset[:n]
    return "".join(i[ord(c) % len(i)] for c in s)


def _map_all(s, charset):
    """按完整字符表映射每个字符。"""
    return "".join(charset[ord(c) % len(charset)] for c in s)


def gen_hkey(path, timestamp, nonce):
    """生成 web 端请求签名 hkey（7 位）。"""
    parts = [p for p in path.split("/") if p]
    norm = "/" + "/".join(parts) + "/"
    comps = [
        _map_cut(str(timestamp), _CHARSET, -2),   # 时间戳：字符表前 33 位
        _map_all(norm, _CHARSET),                  # 路径：完整字符表
        _map_all(nonce, _CHARSET),                 # 随机串：完整字符表
    ]
    mx = max(len(c) for c in comps)
    inter = "".join(c[k] for k in range(mx) for c in comps if k < len(c))
    m = hashlib.md5(inter[:20].encode("utf-8")).hexdigest()
    prefix = _map_cut(m[:5], _CHARSET, -4)        # 字符表前 31 位
    km = _km_mix([ord(c) for c in m[-6:]])
    return prefix + "%02d" % (sum(km) % 100)


def gen_nonce():
    """32 位大写十六进制随机串。"""
    return "".join(random.choice("0123456789ABCDEF") for _ in range(32))

# ────────────────────────────────────────────
# 请求封装
# ────────────────────────────────────────────
def parse_heybox_id(user_pkey):
    """从 user_pkey 里解析 heybox_id。

    user_pkey 是 base64url 编码的会话串，解码后形如
    「1792805064.31_22659548vqzunnwzmfpsaizu」，即「时间戳.毫秒_账号ID+随机串」。
    服务端在末尾会多带 1~2 个转义字符，所以这里按不同长度逐个试解码，
    取第一个能匹配出账号 ID 的结果。
    """
    s = str(user_pkey or "").strip()
    if not s:
        return ""
    s = s.replace("-", "+").replace("_", "/")
    for cut in range(0, 6):
        t = s[:len(s) - cut] if cut else s
        if len(t) < 8 or len(t) % 4 == 1:
            continue
        try:
            dec = base64.b64decode(t + "=" * ((4 - len(t) % 4) % 4)).decode("utf-8", "replace")
        except Exception:
            continue
        m = re.match(r"^[\d.]+_(\d+)", dec)
        if m:
            return m.group(1)
    return ""


def api_call(path, user_pkey="", heybox_id="", extra=None, headers=None,
             method="GET", body=None, base_over=None):
    """带签名地请求一个接口，返回 (http状态码, 解析后的dict或原文)。

    user_pkey  —— 会话凭据，写在 Cookie: user_pkey=...
    headers   —— 额外请求头（如登录接口要的 x_xhh_tokenid / forbidden_auto_tologin）
    base_over —— 覆盖 BASE_PARAMS 里的某些参数（如登录接口的 version）
    """
    t = int(time.time())
    nonce = gen_nonce()
    params = dict(BASE_PARAMS)
    if base_over:
        params.update(base_over)
    params["_time"] = str(t)
    params["nonce"] = nonce
    params["hkey"] = gen_hkey(path, t, nonce)
    if heybox_id:
        params["heybox_id"] = heybox_id
    if extra:
        params.update(extra)
    url = API + path + "?" + urlencode(params)
    h = {
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": "https://xiaoheihe.cn/",
        "Origin": "https://xiaoheihe.cn",
    }
    if user_pkey:
        h["Cookie"] = "user_pkey=%s" % user_pkey
    if headers:
        h.update(headers)
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        resp = urllib.request.urlopen(req, timeout=TIMEOUT)
        code, raw = resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        code, raw = e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return 0, {"_exc": str(e)}
    try:
        return code, json.loads(raw)
    except Exception:
        return code, {"_raw": raw[:300]}


def api_get(path, user_pkey, heybox_id="", extra=None):
    """GET 请求的便捷封装。"""
    return api_call(path, user_pkey, heybox_id, extra)


def login_by_password(phone, pwd):
    """账号密码登录，返回 (user_pkey, heybox_id)。

    流程：生成设备指纹 x_xhh_tokenid → RSA 加密手机号与密码 → POST /account/login/
    → 成功时响应里带 pkey（即 user_pkey）。
    """
    phone = normalize_phone(phone)
    tokenid = get_xhh_tokenid()
    headers = {
        "Cookie": "x_xhh_tokenid=%s" % tokenid,
        "x_xhh_tokenid": tokenid,
        "forbidden_auto_tologin": "true",
    }
    code, js = api_call(
        "/account/login/",
        user_pkey="",
        extra={"phone_num": rsa_encrypt(phone), "pwd": rsa_encrypt(pwd)},
        headers=headers,
        method="POST",
        body={},
        base_over={"version": LOGIN_VERSION},
    )
    if code != 200 or not isinstance(js, dict):
        raise Exception("登录请求失败（HTTP %s）：%s"
                        % (code, (js.get("_exc") or js.get("_raw") or js)))
    status = js.get("status")
    msg = js.get("msg") or ""
    if status != "ok":
        raise Exception("登录失败：%s%s" % (
            msg or "未知错误",
            "（若提示用户名格式错误，多半是手机号格式不对，"
            "请用 +国际区号 的格式，如 +8613800138000）" if "格式" in msg else ""))
    res = js.get("result") or {}
    # 登录成功后前端把 session.pkey 写成 user_pkey cookie
    pkey = (res.get("pkey") or res.get("user_pkey")
            or (res.get("session") or {}).get("pkey") or "")
    hid = (res.get("heybox_id") or res.get("userid")
           or (res.get("profile") or {}).get("heybox_id")
           or (res.get("account_detail") or {}).get("userid") or "")
    if not pkey:
        raise Exception("登录返回里没找到 pkey：%s" % str(js)[:200])
    return pkey, hid

# ────────────────────────────────────────────
# 业务逻辑
# ────────────────────────────────────────────
def _find_sign_task(task_json):
    """从 task/list_v2/ 结果里挑出「签到」任务。"""
    try:
        groups = task_json.get("result", {}).get("task_list", []) or []
    except Exception:
        return None
    for grp in groups:
        for t in (grp.get("tasks") or []):
            if t.get("type") == "sign":
                return t
    return None


def _awards(task):
    """奖励描述列表，如 ['+30', '+30', '+1']。"""
    out = []
    for a in ((task or {}).get("award_desc_v2") or []):
        d = a.get("desc")
        if d:
            out.append(d)
    return out


def _user_info(task_json):
    """从 task/list_v2/ 结果里取账号等级与 H币余额。

    字段位置：result.user.level_info → coin(H币余额) / level / exp / max_exp
    """
    try:
        li = ((task_json.get("result") or {}).get("user") or {}).get("level_info") or {}
        return {
            "coin": li.get("coin"),
            "level": li.get("level"),
            "exp": li.get("exp"),
            "max_exp": li.get("max_exp"),
        }
    except Exception:
        return {}


def run_account(idx, total, user_pkey="", heybox_id="", pwd_pair=None):
    """处理单个账号，返回一行结果描述。

    user_pkey —— 已有的会话凭据（token 支线）
    pwd_pair  —— (手机号, 密码)；没有 user_pkey 时用它登录换凭据（账号密码支线）
    """
    ident = ("user_pkey=%s…" % user_pkey[:12]) if user_pkey else "账号密码登录"
    acc_banner(idx, total, ident)

    # 账号密码支线：先登录换 user_pkey
    if not user_pkey and pwd_pair:
        phone, pwd = pwd_pair
        try:
            log("🔑 用账号密码登录：%s" % phone)
            user_pkey, hid = login_by_password(phone, pwd)
            heybox_id = heybox_id or hid
            log("🔑 登录成功（user_pkey=%s…）" % user_pkey[:12])
        except Exception as e:
            err("账号密码登录失败：%s" % e)
            return "登录失败：%s" % e

    if not user_pkey:
        err("没有可用的凭据")
        return "缺少凭据"

    if not heybox_id:
        heybox_id = parse_heybox_id(user_pkey)
    if not heybox_id:
        err("无法从 user_pkey 解析 heybox_id，请把该值填进脚本顶部 HEYBOX_IDS")
        return "缺少 heybox_id"

    # 1) 验证登录态
    code, js = api_get("/account/restore_login", user_pkey, heybox_id)
    if code != 200 or not isinstance(js, dict):
        err("登录态校验失败（HTTP %s）：%s" % (code, js.get("_exc") or js.get("_raw") or js))
        return "登录态校验失败"
    status = js.get("status")
    msg = js.get("msg") or ""
    if status != "ok":
        if "验证参数错误" in msg or "非法请求" in msg:
            err("服务端校验不通过（msg=%s）：请确认 user_pkey 复制完整、"
                "heybox_id 正确（当前 %s）" % (msg, heybox_id))
        else:
            err("登录态已失效（status=%s，msg=%s）：user_pkey 有效期约 7 天，请重新登录 "
                "www.xiaoheihe.cn 后从浏览器 DevTools → Application → Cookies 复制 user_pkey"
                % (status, msg))
        return "登录态失效"
    res = js.get("result") or {}
    profile = res.get("profile") or {}
    nickname = profile.get("nickname") or "未知用户"
    log("👤 %s（heybox_id=%s）" % (nickname, heybox_id or profile.get("heybox_id", "")))

    # 2) 读签到任务状态
    code, js = api_get("/task/list_v2/", user_pkey, heybox_id)
    if code != 200 or not isinstance(js, dict):
        err("任务列表获取失败（HTTP %s）：%s" % (code, js.get("_exc") or js.get("_raw") or js))
        return "任务列表获取失败"
    if js.get("status") != "ok":
        err("任务列表返回异常：status=%s msg=%s" % (js.get("status"), js.get("msg", "")))
        return "任务列表异常"

    task = _find_sign_task(js)
    if task is None:
        err("任务列表里没有「签到」任务，可能接口改版或账号异常")
        return "未找到签到任务"

    state = task.get("state")
    desc = task.get("desc") or ""
    awards = _awards(task)
    if "版本过低" in desc:
        err("服务端判定版本过低，无法获得签到奖励：%s" % desc)
        return "版本过低，无奖励"
    log("📋 签到任务：state=%s 奖励=%s %s" % (state, awards, ("｜" + desc) if desc else ""))

    ui = _user_info(js)
    if ui.get("coin") is not None:
        log("💰 H币余额 %s｜Lv.%s｜经验 %s/%s"
            % (ui["coin"], ui.get("level"), ui.get("exp"), ui.get("max_exp")))

    # 3) 未签到则执行签到
    if state == "finish":
        streak = task.get("sign_in_streak", "?")
        log("⏭️ 今日已签到，无需重复签到（连续签到 %s 天）｜奖励 %s"
            % (streak, "/".join(awards) if awards else "?"))
        return "今日已签到｜连签 %s｜奖励 %s｜余额 %s" % (
            streak, "/".join(awards) if awards else "?", ui.get("coin", "?"))

    log("🔔 今日未签到（state=%s），开始签到…" % state)
    code, js = api_get("/task/sign_v3/sign", user_pkey, heybox_id)
    if code != 200 or not isinstance(js, dict):
        err("签到请求失败（HTTP %s）：%s" % (code, js.get("_exc") or js.get("_raw") or js))
        return "签到请求失败"
    st, msg = js.get("status"), (js.get("msg") or "")
    signed_now = False
    if st == "ok":
        signed_now = True
        log("🎉 签到接口返回成功%s" % ("｜%s" % msg if msg else ""))
    elif "非法" in msg:
        # 服务端只在「今天已经签过」时才回这个文案，属业务层提示，不是错误
        log("⏭️ 服务端提示「%s」→ 今天已经签过了" % msg)
    else:
        warn("签到返回异常：status=%s msg=%s" % (st, msg))

    # 4) 复查签到状态
    code, js = api_get("/task/list_v2/", user_pkey, heybox_id)
    task2 = _find_sign_task(js) if (code == 200 and isinstance(js, dict)) else None
    state2 = (task2 or {}).get("state")
    if task2 and state2 == "finish":
        aw2 = _awards(task2)
        streak = task2.get("sign_in_streak", "?")
        reward = "/".join(aw2) if aw2 else "?"
        ui2 = _user_info(js)
        coin2 = ui2.get("coin")
        delta = ""
        if ui.get("coin") is not None and coin2 is not None:
            d = coin2 - ui["coin"]
            delta = "｜本次到账 +%d H币" % d if d > 0 else ("｜余额 %d" % coin2)
        if signed_now:
            log("🎉 签到成功｜奖励 %s｜连续签到 %s 天%s" % (reward, streak, delta))
            return "签到成功｜连签 %s｜奖励 %s｜余额 %s" % (streak, reward, coin2)
        log("⏭️ 今日已签到（本次未新增）｜奖励 %s｜连续签到 %s 天%s" % (reward, streak, delta))
        return "今日已签到｜连签 %s｜奖励 %s｜余额 %s" % (streak, reward, coin2)

    warn("签到后复查：state=%s，未确认成功" % state2)
    return "签到未确认"

# ────────────────────────────────────────────
# 主流程
# ────────────────────────────────────────────
def load_accounts():
    """读取 token 支线账号：环境变量 XHH_USER_PKEY 优先，其次顶部 ACCOUNTS。

    返回 [(user_pkey, heybox_id), ...]；heybox_id 优先用 HEYBOX_IDS 里按顺序配的，
    没有配置则由 user_pkey 自动解析。
    """
    raw = os.getenv("XHH_USER_PKEY", "") or ""
    items = []
    for line in raw.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            items.append(line)
    if not items:
        for x in ACCOUNTS:
            x = str(x).strip()
            if x and not x.startswith("#"):
                items.append(x)

    ids_env = [x.strip() for x in (os.getenv("XHH_HEYBOX_ID", "") or "").split(",") if x.strip()]
    ids_cfg = [str(x).strip() for x in HEYBOX_IDS if str(x).strip() and not str(x).startswith("#")]
    ids = ids_env if ids_env else ids_cfg

    out = []
    for i, pkey in enumerate(items):
        hid = ids[i] if i < len(ids) else ""
        out.append((pkey, hid))
    return out


def load_pwd_accounts():
    """读取账号密码支线：环境变量 XHH_ACCOUNTS 优先，其次顶部 PWD_ACCOUNTS。

    每行格式：手机号#密码（先账号后密码）。
    也兼容逗号和竖线分隔，但 # 优先 —— 密码里含逗号时不会被截断。
    返回 [(手机号, 密码), ...]
    """
    raw = os.getenv("XHH_ACCOUNTS", "") or ""
    items = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        for sep in ("#", ",", "|"):
            if sep in line:
                parts = line.split(sep, 1)
                items.append((parts[0].strip(), parts[1].strip()))
                break
    if not items:
        for x in PWD_ACCOUNTS:
            try:
                phone, pwd = x
            except Exception:
                continue
            phone, pwd = str(phone).strip(), str(pwd).strip()
            if phone and pwd:
                items.append((phone, pwd))
    return items


def main():
    accounts = load_accounts()
    pwds = load_pwd_accounts()
    total = len(accounts) + len(pwds)
    log("🚀 小黑盒签到开始｜token 账号 %d ｜ 密码账号 %d" % (len(accounts), len(pwds)))
    if not total:
        err("没有配置账号：二选一 —— ① 脚本顶部 ACCOUNTS 填 user_pkey"
            "（环境变量 XHH_USER_PKEY）；② 脚本顶部 PWD_ACCOUNTS 填 (\"手机号\",\"密码\")"
            "（环境变量 XHH_ACCOUNTS，格式 手机号#密码）"
            "（环境变量 XHH_ACCOUNTS）")
        flush_notify(_QN_SITE, "未配置账号")
        return 1

    results = []
    ok = 0
    failed = 0
    idx = 0
    for pkey, hid in accounts:
        idx += 1
        ident = "user_pkey=%s…" % pkey[:12]
        try:
            r = run_account(idx, total, pkey, hid)
        except Exception as e:
            err("账号 %d 处理异常：%s" % (idx, e))
            r = "异常：%s" % e
        results.append((idx, r))
        if r.startswith("签到成功") or r.startswith("今日已签到"):
            ok += 1
        else:
            failed += 1
        acc_footer(idx, total, ident)
        if idx < total:
            time.sleep(2)

    for phone, pwd in pwds:
        idx += 1
        try:
            r = run_account(idx, total, "", "", pwd_pair=(phone, pwd))
        except Exception as e:
            err("账号 %d 处理异常：%s" % (idx, e))
            r = "异常：%s" % e
        results.append((idx, r))
        if r.startswith("签到成功") or r.startswith("今日已签到"):
            ok += 1
        else:
            failed += 1
        acc_footer(idx, total, "手机号 %s" % phone)
        if idx < total:
            time.sleep(2)

    log("=" * 70)
    new_signed = sum(1 for _, r in results if r.startswith("签到成功"))
    already = sum(1 for _, r in results if r.startswith("今日已签到"))
    log("📊 [执行汇总] 小黑盒 ｜ 成功 %d ｜ 失败 %d ｜ 本次新签 %d ｜ 此前已签 %d"
        % (ok, failed, new_signed, already))
    for i, r in results:
        log("  · 账号%d：%s" % (i, r))
    flush_notify(_QN_SITE, "成功 %d ｜ 失败 %d" % (ok, failed))
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    try:
        _rc = main()
    except Exception:
        raise
    _sys.exit(_rc)
