#!/usr/bin/env python3
# =========================================================
# name:  中免会员
# cron: 0 5,15 * * *
# =========================================================
#
# 任务流程：
#   1. 读取 YYB_SERVER 账号基座，按 YYB_ONLY_REFS 序号白名单筛选（1 起）
#   2. 调用 YYBGO 的 /wxapp/getCode 获取每个账号的 wx.login code
#   3. 用 code 请求登录接口换取会员 token（每次运行强制重新登录，不落盘）
#   4. 调用签到接口完成每日签到，并查询今日是否已签 / 连续签到天数
# 可控参数：
#   YYB_SERVER      必填。格式「地址@ref#备注」，多账号换行 / 空格 / & 分隔
#   YYB_ONLY_REFS   账号序号白名单（1 起）。留空 [] 跑全部；填 [1,2] 只跑第 1、2 个账号
#
# 日志规范：[LEVEL] [CDF] message   （LEVEL: INFO / WARN / ERROR）
#
# =========================================================

YYB_ONLY_REFS = []   # 账号序号白名单（1 起），留空 [] 跑全部；例如 [1,3] 只跑第 1、3 个账号

import os, re, time, random, traceback, json
import requests

# ============== 新手配置区 ==============
APP_ID = "wxdf26125d1f97992c"
SIGN_LNG = ""
SIGN_LAT = ""
CDF_VERSION = "5.5.98"
APP_VERSION_PATH = "176"
HOST = "cdfmbrapi.cdfg.com.cn"
UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 16_1_2 like Mac OS X) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Mobile/15E148 MicroMessenger/8.0.75(0x18004b66) NetType/WIFI Language/zh_CN")
# ========================================

LOGIN_URL       = "https://" + HOST + "/api/session/wxSession/v2"
SIGN_URL        = "https://" + HOST + "/api/user/sign"
SIGN_RECORD_URL = "https://" + HOST + "/api/user/signRecord"

# ———————————— 错误通知（可选项，想用则用）————————————
# 只推错误：本次运行出现 ERROR 日志才推送一次；正常跑完不打扰。
# 直接调用青龙自带的通知模块（容器内为 /ql/data/scripts/notify.py，仓库内为同目录/上一级的 notify.py）——
#   在青龙面板「通知设置」里配一次即可全站通用（该文件由青龙官方维护，支持其全部推送渠道）。
# 找不到该文件、或未配置任何通知渠道时，只在日志末尾提示一行，不报错、不中断。
import os as _os
import sys as _sys

_QN_SITE = "中免会员"
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

# ———————————— 统一日志 ————————————
def _emit(level, msg):
    line = f"[{level}] [CDF] {msg}"
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

def today_str():
    y, m, d = time.localtime()[:3]
    # 抓包里是 2026-9-2 格式，对齐返回（日期匹配 + 显示）
    return (
        str(y) + "-" + str(m) + "-" + str(d),
        str(y) + "-" + str(m).zfill(2) + "-" + str(d).zfill(2),
        time.strftime("%Y-%m-%d"),
    )

# ———————————— 基础组件 ————————————
def _common_headers(token=None, page="pages/main/main"):
    h = {
        "Host": HOST,
        "User-Agent": UA,
        "Referer": ("https://servicewechat.com/" + APP_ID + "/"
                    + APP_VERSION_PATH + "/page-frame.html"),
        "pageUrl": page,
        "cdf-v": CDF_VERSION,
        "Accept-Encoding": "gzip,compress,br,deflate",
    }
    if token:
        h["x-access-token"] = token
    return h

def _parse(resp):
    try:
        return resp.json()
    except Exception:
        pass
    m = re.search(r"\{.*\}", (resp.text or "").strip(), re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None

def _ok(jr):
    """返回 (ok, msg, already)"""
    if not isinstance(jr, dict):
        return False, "响应非 JSON", False
    code = jr.get("code")
    msg = (jr.get("msg") or "") + " " + (jr.get("message") or "")
    success = jr.get("success")
    already = any(k in msg for k in ["已签", "重复", "已经", "already", "repeat", "明天再来", "无需重复"])
    if already:
        return True, msg.strip() or "今日已签到", True
    if success is False:
        return False, msg.strip() or "success=false", False
    if code != 1:
        return False, msg.strip() or ("code=" + str(code)), False
    return True, msg.strip() or "成功", False

class YYBClient:
    def __init__(self, appid):
        self.appid = appid
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
        raw = os.getenv("YYB_SERVER") or ""
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                server, ref = self.parse_entry(line)
                yield server, ref, line
            except ValueError:
                pass
    def get_code(self, server, ref):
        try:
            s = requests.Session(); s.trust_env = False
            r = s.post(server + "/wxapp/getCode",
                       json={"ref": ref, "app_id": self.appid}, timeout=(10, 90))
            r.raise_for_status()
            body = r.json()
            if isinstance(body.get("code"), int) and body["code"] not in (0, None):
                raise RuntimeError(body.get("msg") or str(body))
            data = body.get("data") or {}
            result = body.get("result") or (isinstance(data, dict) and data.get("result")) or {}
            code = (isinstance(result, dict) and result.get("code")) or None
            if not code:
                raise RuntimeError("返回空 code")
            openid = (isinstance(data, dict) and data.get("openid")) or None
            return code, openid
        except Exception as e:
            return None, str(e)

def login_with_code(wx_code):
    h = _common_headers(token=None, page="pages/main/main")
    h["content-type"] = "application/x-www-form-urlencoded"
    body = "code=" + requests.utils.quote(wx_code) + "&moduleId=&moduleType="
    try:
        r = requests.post(LOGIN_URL, data=body, headers=h, timeout=(10, 30))
    except Exception as e:
        return None, "请求异常: " + str(e)
    jr = _parse(r)
    ok, msg, _ = _ok(jr)
    if not ok:
        return None, msg or ("HTTP " + str(r.status_code))
    data = jr.get("data") if isinstance(jr.get("data"), dict) else {}
    token = data.get("token")
    if not token:
        return None, "登录响应里没拿到 token"
    return token, None

def do_sign(token):
    h = _common_headers(token, page="packages/game/signin/signin")
    h["content-type"] = "application/x-www-form-urlencoded"
    data = ("repairSignDate=&randomizedIdMap=&lng=" + SIGN_LNG
            + "&lat=" + SIGN_LAT + "&repairSignType=")
    extra_lines = []
    try:
        r = requests.post(SIGN_URL, data=data, headers=h, timeout=(10, 30))
        jr = _parse(r)
    except Exception as e:
        return "异常", "请求异常: " + str(e), extra_lines
    ok, msg, already = _ok(jr)
    if already:
        status = "已签"
    elif ok:
        status = "成功"
    else:
        status = "失败"
    if isinstance(jr, dict) and isinstance(jr.get("data"), dict):
        d = jr["data"]
        for k in ["signDay", "totalDays", "continueSignDays",
                  "points", "score", "balance", "prize", "giftName"]:
            if d.get(k) not in (None, "", []):
                extra_lines.append(("奖励" if k in ("points","score","prize","giftName","balance") else "进度")
                                   + " · " + k + ": " + str(d[k]))
    return status, msg, extra_lines

def get_today_done(token):
    """返回 (today_done, signText 或 None)；拿不到返回 (None, None)"""
    h = _common_headers(token, page="packages/game/signin/signin")
    h["content-type"] = "application/x-www-form-urlencoded"
    try:
        r = requests.post(SIGN_RECORD_URL, data="next=0", headers=h, timeout=(10, 30))
    except Exception:
        return None, None
    jr = _parse(r)
    ok, _, _ = _ok(jr)
    if not ok or not isinstance(jr, dict) or not isinstance(jr.get("data"), dict):
        return None, None
    forms = today_str()
    for rec in (jr["data"].get("signRecords") or []):
        if str(rec.get("date")) in forms:
            return rec.get("signIn"), rec.get("signText")
    return None, None

def ensure_token(server, ref):
    """每次都强制重新取码 + 登录，返回 (token, 状态说明)；失败返回 (None, 原因)。"""
    code, err_msg = YYBClient(APP_ID).get_code(server, ref)
    if not code:
        return None, "YYB取码失败: " + str(err_msg)
    token, err_msg = login_with_code(code)
    if not token:
        return None, "登录失败: " + str(err_msg)
    return token, "新登录"

def _status_emoji_and_tag(status):
    return {
        "成功":   ("✅", "签到成功"),
        "已签":   ("🟡", "今日已签"),
        "失败":   ("❌", "签到失败"),
        "异常":   ("⚠️", "请求异常"),
    }.get(status, ("❔", status))

def run_account(server, ref):
    token, status_msg = ensure_token(server, ref)
    if not token:
        brief = " ".join(str(status_msg).split())[:80]
        err("❌ 账号不可用 · 原因: " + brief)
        return False, "ref " + str(ref) + ": ❌ " + brief
    # 登录状态一行简注（每次都是新登录）
    log("🔑 登录态 · " + status_msg)
    t_short, t_long, _ = today_str()
    before, txt = get_today_done(token)
    sym1 = "✅" if before else "⭕"
    log("📊 签到前 · 今日(" + t_short + ") " + sym1
        + ("  signText=" + str(txt) if txt is not None else ""))
    status, msg, extra = do_sign(token)
    emoji, tag = _status_emoji_and_tag(status)
    line = emoji + " " + tag + "  ──  服务端 msg: " + (msg or "")
    if status in ("成功", "已签"):
        log(line)
    else:
        err(line)
    for one in extra:
        log("🎁 " + one)
    after, txt2 = get_today_done(token)
    sym2 = "✅" if after else "⭕"
    log("📊 签到后 · 今日(" + t_short + ") " + sym2
        + ("  signText=" + str(txt2) if txt2 is not None else ""))
    # 如果之前没签 / 之后签了，加一行 🎉 高亮
    if before is False and after is True:
        log("🎉 本账号首次签到成功！")
    return True, "ref " + str(ref) + ": " + emoji + " " + tag

def main():
    entries = list(YYBClient(APP_ID).entries())
    # 按 YYB_ONLY_REFS 序号白名单筛选（1 起）；留空 [] 则运行全部账号
    if YYB_ONLY_REFS:
        wanted = set(int(x) for x in YYB_ONLY_REFS if str(x).strip().isdigit() and int(x) > 0)
        if wanted:
            entries = [e for i, e in enumerate(entries, 1) if i in wanted]
            log("ℹ️ 按 YYB_ONLY_REFS 筛选：请求序号 %s，命中 %d 个账号" % (sorted(wanted), len(entries)))
    total = len(entries)
    tday, _, _ = today_str()

    log("🚀 中免会员小程序 每日签到开始 · 日期 " + tday + " · 共 " + str(total) + " 个账号")

    if not entries:
        err("🚫 没有可执行账号（青龙环境变量 YYB_SERVER 空，或被 YYB_ONLY_REFS 过滤空）")
        return

    success = 0
    for i, (server, ref, _) in enumerate(entries, start=1):
        acc_banner(i, total, "标识：" + str(ref))
        try:
            ok, line = run_account(server, ref)
            if ok:
                success += 1
        except Exception as e:
            err("💥 账号异常: " + str(e))
            err(traceback.format_exc())
        acc_footer(i, total)
        if i < total:
            wait = random.randint(3, 8)
            # 不打印休息，避免啰嗦；真卡住了用户能从执行计时看在等
            time.sleep(wait)

    log(_ACC_RULE)
    log(f"📊 [执行汇总] 中免会员 · 账号 {total} ｜ 成功 {success} ｜ 失败 {total - success}")
    log(_ACC_RULE)

    # 错误日志推送：有错误才发；未配置通知渠道则只提示一行
    flush_notify("中免会员",
                 f"账号 {total} ｜ 成功 {success} ｜ 失败 {total - success}",
                 logger=log)

if __name__ == "__main__":
    main()
