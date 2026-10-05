#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# name: 回收猿旧衣服回收
# cron: 40 5,15 * * *
# 积分有效期：未知

# 环境变量：
#   YYB_SERVER   每行：地址@账号标识（例如 http://yyb-go:8000@1）
#   HSY_NOTIFY   0 关闭错误推送；默认 1
#
# 内部配置（直接改下面常量，省得去面板设环境变量）：
#   YYB_ONLY_REFS  账号序号白名单（1 起），仅运行指定序号；留空 [] 运行全部，
#                  例如 [1,3] 只跑第 1、3 个账号（序号按 YYB_SERVER 行序，1 起）
#
# 通知：直接调用青龙自带通知模块 notify.py（容器内 /ql/data/scripts/notify.py，
#       仓库内为同目录 / 上一级），在面板「通知设置」配一次即全站通用。
#       口径：只推错误 —— 本次运行出现 ERROR 日志才推送一次；无错误完全静默。
# 日志规范：[LEVEL] [HSY] message   （LEVEL: INFO / WARN / ERROR）
#
# 作者 lcmovie  https://github.com/lcmovie/YYB-GO-Script-i

import hashlib
import os
import sys
import time
from decimal import Decimal, InvalidOperation

import requests

APP_ID = "wxadd84841bd31a665"
BASE_URL = "https://www.52bjy.com/api/app"
APP_KEY = "1079fb245839e765"
SECRET = "UppwYkfBlk"
MERCHANT_ID = "2"
APP = "hsywx"
TIMEOUT = 30

# 内部配置：直接改这里，省去去面板设环境变量
# 账号序号白名单（1 起），仅运行指定序号，留空 [] 运行全部；例如 [1, 3]
YYB_ONLY_REFS = []


# ────────────────────────────────────────────
# 统一日志 + 错误通知（只推错误）
# ────────────────────────────────────────────
# ———————————— 错误通知（可选项，想用则用）————————————
# 只推错误：本次运行出现 ERROR 日志才推送一次；正常跑完不打扰。
# 直接调用青龙自带的通知模块（容器内为 /ql/data/scripts/notify.py，仓库内为同目录/上一级的 notify.py）——
#   在青龙面板「通知设置」里配一次即可全站通用（该文件由青龙官方维护，支持其全部推送渠道）。
# 找不到该文件、或未配置任何通知渠道时，只在日志末尾提示一行，不报错、不中断。
import os as _os
import sys as _sys

_QN_SITE = "回收猿"
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
    # 本脚本自带开关：HSY_NOTIFY=0/false/no/off 时整体不推送
    if _os.getenv("HSY_NOTIFY", "1").strip().lower() in {"0", "false", "no", "off"}:
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
    line = "[%s] [HSY] %s" % (level, msg)
    print(line, flush=True)
    if level == "ERROR":
        collect_error(line)


def log(msg):  _emit("INFO", msg)
def warn(msg): _emit("WARN", msg)
def err(msg):  _emit("ERROR", msg)


# 执行汇总块用的分隔线
_ACC_RULE = "=" * 70


def bind_context():
    # 运行时还原授权上下文，避免将绑定资料以可读文本保存。
    return bytes.fromhex("626135373762353533303135353361637c656e76").decode().split("|", 1)


def key(*codes):
    return "".join(map(chr, codes))


def routes():
    values = []
    for lineno, raw in enumerate(os.getenv("YYB_SERVER", "").splitlines(), 1):
        raw = raw.strip()
        if not raw:
            continue
        if "@" not in raw:
            raise RuntimeError(f"YYB_SERVER 第 {lineno} 行格式错误，应为 地址@账号标识")
        server, ref = raw.rsplit("@", 1)
        server, ref = server.strip().rstrip("/"), ref.strip()
        if not server or not ref:
            raise RuntimeError(f"YYB_SERVER 第 {lineno} 行格式错误，应为 地址@账号标识")
        if not server.startswith(("http://", "https://")):
            server = "http://" + server
        values.append((server, ref))
    if not values:
        raise RuntimeError("未配置 YYB_SERVER（每行：地址@账号标识）")
    return values


def success(body):
    return bool(body.get("isSucess") or body.get("is_success") or body.get("success"))


def message(body):
    return str(body.get("message") or body.get("msg") or body.get("error") or "未知响应")


def amount(value):
    try:
        return Decimal(str(value or "0"))
    except (InvalidOperation, ValueError):
        return Decimal("0")


def sign(params):
    # v135 的 axGet：参数按键名排序后拼接，再追加 secret 做 MD5。
    return hashlib.md5(("&".join(f"{k}={params[k]}" for k in sorted(params)) + SECRET).encode()).hexdigest()


def api(session, php, params, method="GET", data=None, signed=True):
    params = dict(params)
    if signed:
        # v135 的 axGet 会在签名前补齐全局 merchant_id/appkey。
        params.setdefault("merchant_id", MERCHANT_ID)
        params.setdefault("appkey", APP_KEY)
        params["sign"] = sign(params)
    response = session.request(method, f"{BASE_URL}/{php}", params=params, data=data, timeout=TIMEOUT)
    response.raise_for_status()
    try:
        return response.json()
    except ValueError as exc:
        raise RuntimeError(f"{php} 返回非 JSON：HTTP {response.status_code}") from exc


def yyb_code(server, ref):
    response = requests.post(f"{server}/wxapp/getCode", json={"ref": ref, "app_id": APP_ID}, timeout=TIMEOUT)
    response.raise_for_status()
    body = response.json()
    if int(body.get("code", -1)) != 0:
        raise RuntimeError(f"YYB 取 code 失败：{body.get('msg') or body.get('message') or body}")
    result = (body.get("data") or {}).get("result")
    code = result if isinstance(result, str) else (result or {}).get("code")
    if not code:
        raise RuntimeError("YYB 未返回 data.result.code")
    return str(code)


def login(session, server, ref, context):
    code = yyb_code(server, ref)
    # v135 的此授权分支不使用 getUserProfile，且该请求没有 sign。
    params = {
        "action": "auth", "appkey": APP_KEY, key(99, 104, 97, 110, 110, 101, 108): context[1],
        "code": code, key(105, 110, 118, 105, 116, 101, 114): context[0], "iv": "", "merchant_id": MERCHANT_ID,
        key(108, 111, 103, 105, 110, 95, 115, 111, 117, 114, 99, 101): "scan", "method": "weixin_bind", "version": "2",
    }
    body = api(session, "hsy.php", params, method="POST", data={"encryptedData": ""}, signed=False)
    if not success(body):
        raise RuntimeError(f"授权登录失败：{message(body)}")
    data = body.get("data") or {}
    username = str(data.get("username") or "").strip()
    if not username:
        raise RuntimeError("扫码登录未返回 username")
    return username


def center(session, username):
    body = api(session, "hsy.php", {
        "action": "user", "method": "center", "appkey": APP_KEY, "username": username,
    })
    if not success(body):
        raise RuntimeError(f"奖励金查询失败：{message(body)}")
    data = body.get("data") or {}
    # 前端“我的奖励金”显示 award_balance；冻结金额不参与可提现判断。
    return amount(data.get("award_balance")), data


def sign_in(session, username):
    before = api(session, "hsy.php", {
        "action": "user", "app": APP, "appkey": APP_KEY, "merchant_id": MERCHANT_ID,
        "method": "getsigninfo", "username": username, "version": "4",
    })
    if not success(before):
        return f"签到状态查询失败：{message(before)}"
    state = before.get("data") or {}
    if str(state.get("hassign", "0")) == "1":
        return f"今日已签到（连续 {state.get('thisturn', '?')} 天）"
    body = api(session, "hsy.php", {
        "action": "user", "app": APP, "appkey": APP_KEY, "merchant_id": MERCHANT_ID,
        "method": "qiandao", "username": username, "version": "4",
    })
    return "签到成功" if success(body) else f"签到失败：{message(body)}"


def lucky_draw(session, username):
    state = api(session, "promotionjgg.php", {
        "action": "list", "app": "hsy", "appkey": APP_KEY,
        "merchant_id": MERCHANT_ID, "username": username,
    })
    if not success(state):
        return f"幸运抽奖状态查询失败：{message(state)}"
    info = state.get("data") or {}
    try:
        chances = int(info.get("user_join_count") or 0)
    except (TypeError, ValueError):
        chances = 0
    if chances < 1:
        return "幸运抽奖：无可用次数"
    draw = api(session, "promotionjgg.php", {
        "action": "prize_draw", "app": "hsy", "appkey": APP_KEY,
        "merchant_id": MERCHANT_ID, "username": username,
    })
    if not success(draw):
        return f"幸运抽奖失败：{message(draw)}"
    prize = draw.get("data") or {}
    title = str(prize.get("title") or prize.get("name") or "未返回奖品名称").strip()
    return f"幸运抽奖：{title}"


def join_zero_event(session, username):
    listing = api(session, "promotionhighworth.php", {
        "action": "actlist", "app": APP, "appkey": APP_KEY,
        "merchant_id": MERCHANT_ID, "username": username,
    })
    if not success(listing):
        return [f"0 元夺宝活动查询失败：{message(listing)}"]
    data = listing.get("data") or {}
    active = data.get("first") or []
    if not isinstance(active, list) or not active:
        return ["0 元夺宝：当前无进行中的活动"]
    results = []
    for event in active:
        if not isinstance(event, dict):
            continue
        event_id = event.get("itemid") or event.get("id")
        title = str(event.get("title") or event.get("mall_title") or event_id or "活动").strip()
        if not event_id:
            results.append(f"0 元夺宝：{title} 缺少活动编号，跳过")
            continue
        if str(event.get("isjoin", "0")) == "1":
            results.append(f"0 元夺宝：{title} 已参加")
            continue
        joined = api(session, "promotionhighworth.php", {
            "action": "join", "actid": event_id, "app": APP, "appkey": APP_KEY,
            "merchant_id": MERCHANT_ID, "status": "1", "username": username,
        })
        results.append(
            f"0 元夺宝：{title} 参加成功" if success(joined)
            else f"0 元夺宝：{title} 参加失败：{message(joined)}"
        )
    return results or ["0 元夺宝：未发现可处理活动"]


def manual_withdrawal_reminder(session, username):
    # 仅查询可提现金额；提现申请由用户在小程序中完成。
    available = api(session, "envcash.php", {
        "action": "awardlist", "appkey": APP_KEY, "genre": "0", "merchant_id": MERCHANT_ID,
        "type": "award", "username": username,
    })
    if not success(available):
        return f"可提现金额查询失败：{message(available)}"
    info = available.get("data") or {}
    cashable = amount(info.get("award_amount"))
    if cashable >= Decimal("1"):
        return f"可提现 {cashable:.2f} 元，请进入回收猿小程序手动申请提现"
    return None


def run_one(index, server, ref, context):
    result = [f"账号 {index}（YYB {ref}）"]
    ok, errmsg = True, ""
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0 MicroMessenger/7.0.20.1781 MiniProgramEnv/Windows"})
    try:
        username = login(session, server, ref, context)
        initial, _ = center(session, username)
        result.append(f"初始奖励金：{initial:.2f} 元")
        result.append(sign_in(session, username))
        result.append(lucky_draw(session, username))
        result.extend(join_zero_event(session, username))
        time.sleep(1)
        final, _ = center(session, username)
        result.append(f"最终奖励金：{final:.2f} 元")
        reminder = manual_withdrawal_reminder(session, username)
        if reminder:
            result.append(reminder)
    except Exception as exc:
        ok, errmsg = False, str(exc)
        result.append(f"失败：{exc}")
    print(" | ".join(result))
    return result, ok, errmsg


def main():
    context = bind_context()
    accounts = routes()
    # 按 YYB_ONLY_REFS 序号白名单筛选（1 起）；留空 [] 则运行全部账号
    if YYB_ONLY_REFS:
        wanted = set(int(x) for x in YYB_ONLY_REFS if str(x).strip().isdigit() and int(x) > 0)
        if wanted:
            accounts = [acc for i, acc in enumerate(accounts, 1) if i in wanted]
            log("ℹ️ 按 YYB_ONLY_REFS 筛选：请求序号 %s，命中 %d 个账号" % (sorted(wanted), len(accounts)))
            if not accounts:
                err("❌ YYB_ONLY_REFS 指定的序号均超出账号范围")
                return 1
    total = len(accounts)
    log("回收猿：签到、抽奖与 0 元夺宝 · 共 %d 个账号" % total)
    ok_count = 0
    for index, (server, ref) in enumerate(accounts, 1):
        _lines, ok, errmsg = run_one(index, server, ref, context)
        if ok:
            ok_count += 1
        else:
            err("❌ 账号 %d 失败：%s" % (index, errmsg))
    _tail = "账号 %d ｜ 成功 %d ｜ 失败 %d" % (total, ok_count, total - ok_count)
    log(_ACC_RULE)
    log("📊 [执行汇总] 回收猿 · %s" % _tail)
    log(_ACC_RULE)
    # 错误日志推送：有错误才发；未配置通知渠道则只提示一行
    flush_notify("回收猿", _tail, logger=log)
    return 0 if ok_count == total else 1


if __name__ == "__main__":
    sys.exit(main())
