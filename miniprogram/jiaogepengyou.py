# =========================================================
# name: 交个朋友
# cron: 25 5,15 * * *
# =========================================================
#
# 任务流程：
#   1. 读取账号：优先用 IYOUKE_TOKEN（手动 bearer）；否则读 YYB_SERVER + YYB_ONLY_REFS 经 YYBGO
#   2. 调用 YYBGO 的 /wxapp/getCode 获取 wx.login code
#   3. 用 code 请求 appLogin 换取 access_token（每次运行重新登录，不落盘）
#   4. 签到：先 GET /dtapi/pointsSign/user/pointsInfo/query 看 signTodayResult 是否今日已签；
#      未签才 GET /dtapi/pointsSign/user/sign?date=YYYY/MM/DD 签到，再查 pointsInfo 取总积分与连续天数
#   5. 输出汇总（已签/签到成功/失败一目了然）
# 可控参数：
#   IYOUKE_TOKEN    可选。手动 bearer token，空格分隔多账号，优先级最高（免 YYB）
#   YYB_SERVER      必填（无 IYOUKE_TOKEN 时）。格式「地址@ref」，空格/换行分隔
#   YYB_ONLY_REFS   账号序号白名单（1 起）。留空 [] 跑全部；填 [1,2] 只跑第 1、2 个账号（环境变量同名可覆盖）
#   IYOUKE_APP_ID   可选。小程序 AppID，默认 wx3b294e7a0ba29bc3
#   IYOUKE_VERSION  可选。接口版本号，默认 3.5.4
#
# 日志规范：[LEVEL] [IYOUKE] message   （LEVEL: INFO / WARN / ERROR）
#
# =========================================================
#!/usr/bin/env python3
# -*- coding: utf-8 -*-


import os
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

import requests

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

_QN_SITE = "交个朋友"
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
    line = f"[{level}] [IYOUKE] {msg}"
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

# ===================== 调试开关 =====================
DEBUG = False
DEBUG_ENV = {
    "IYOUKE_TOKEN": "85f3ca0b55c54f18939e16e76012f93f1790164361106",
}
if DEBUG:
    for _k, _v in DEBUG_ENV.items():
        os.environ.setdefault(_k, _v)
    warn("⚠️ 调试模式已开启（DEBUG=True），使用脚本内置 DEBUG_ENV 配置")
# ===================================================

# ===================== 环境变量控制（置顶） =====================
# ① 手动 token 兜底（可选，优先级最高，免 YYB）：直接填 bearer token，空格分隔多账号
IYOUKE_TOKEN = os.getenv("IYOUKE_TOKEN", "").strip()
# ② 配合 YYBGO 的账号基座（必填其一）：每项 "地址@ref"，空格/换行分隔，多账号
YYB_SERVER_RAW = os.getenv("YYB_SERVER", "").strip()
# ③ YYB 只跑这些 ref 的白名单过滤器：留空 = 跑 YYB_SERVER 全部账号；填 ["1","2"] = 只跑这些
YYB_ONLY_REFS = []   # 账号序号白名单（1 起），留空 [] 跑全部；例如 [1,3] 只跑第 1、3 个账号
# 接口参数（可选环境变量覆盖；缺省用抓包所得默认值）
APP_ID = os.getenv("IYOUKE_APP_ID", "wx3b294e7a0ba29bc3").strip()
APP_VERSION = os.getenv("IYOUKE_VERSION", "3.5.4").strip()
# ===================================================

SCRIPT_NAME = "交个朋友积分签到"

# ===================== 接口常量 =====================
API_BASE = "https://smp-api.iyouke.com"
ENV_VERSION = "release"
REFERER = f"https://servicewechat.com/{APP_ID}/101/page-frame.html"
XY_EXTRA = f"appid={APP_ID};version={APP_VERSION};envVersion={ENV_VERSION};senceId=1089"
USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_1_2 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Mobile/15E148 MicroMessenger/8.0.75(0x18004b66) "
    "NetType/WIFI Language/zh_CN"
)
# ===================================================


def _split_accounts(s: str) -> List[str]:
    """支持 空格 / 换行 / & 分隔（空格是账号分隔符，token 内不含空格）。"""
    if not s:
        return []
    s = s.replace("\r\n", "\n").replace("\r", "\n").replace("&", "\n")
    return [p.strip() for p in s.split() if p.strip()]


def _parse_yyb_line(item: str) -> Optional[Tuple[str, str, str]]:
    """解析一行 YYB_SERVER：地址@ref#备注。无备注时备注=ref。无效返回 None。"""
    item = (item or "").strip()
    if not item or "@" not in item:
        return None
    if "#" in item:
        entry_part, remark = item.split("#", 1)
        entry_part, remark = entry_part.strip(), remark.strip()
    else:
        entry_part, remark = item.strip(), ""
    server, ref = entry_part.rsplit("@", 1)
    server = server.strip().rstrip("/")
    if not server.startswith("http"):
        server = "http://" + server
    return server, ref.strip(), (remark or ref.strip())


class YYBClient:
    """调用 YYBGO 拿 wx.login 的 code（每个账号）。"""

    def __init__(self, appid: str):
        self.appid = appid

    def get_code(self, server: str, ref: str) -> str:
        try:
            s = requests.Session()
            s.trust_env = False
            r = s.post(server + "/wxapp/getCode",
                       json={"ref": ref, "app_id": self.appid}, timeout=(10, 90))
            r.raise_for_status()
            body = r.json()
            code_field = body.get("code")
            if isinstance(code_field, int) and code_field not in (0, None):
                raise RuntimeError(body.get("msg") or str(body))
            data = body.get("data") or {}
            result = body.get("result") or (isinstance(data, dict) and data.get("result")) or {}
            code = (isinstance(result, dict) and result.get("code")) or None
            if not code:
                raise RuntimeError("YYBGO 返回空 code")
            return code
        except Exception as e:
            raise RuntimeError("YYBGO 取码失败: " + str(e))


def app_login(wx_code: str) -> str:
    """用 YYBGO 给的 wx.login code 换 iyouke access_token（每次重新登录，不落盘）。"""
    headers = {
        "appId": APP_ID,
        "envVersion": ENV_VERSION,
        "content-type": "application/json",
        "xy-extra-data": f"appid={APP_ID};version={APP_VERSION};envVersion={ENV_VERSION};senceId=1089",
        "version": APP_VERSION,
        "Accept-Encoding": "gzip,compress,br,deflate",
        "User-Agent": USER_AGENT,
        "Referer": REFERER,
    }
    try:
        r = requests.post(API_BASE + "/dtapi/appLogin",
                          json={"appType": 1, "principal": wx_code},
                          headers=headers, timeout=(10, 30))
        r.raise_for_status()
        body = r.json()
    except Exception as e:
        raise RuntimeError("appLogin 请求异常: " + str(e))
    if isinstance(body.get("code"), int) and body.get("code") not in (0, None) and not body.get("access_token"):
        raise RuntimeError("appLogin 失败: " + str(body.get("msg") or body))
    token = body.get("access_token")
    if not token:
        raise RuntimeError("appLogin 响应未返回 access_token: " + str(body))
    return token


def _yyb_entries() -> List[Tuple[str, str, str]]:
    """从 YYB_SERVER 读全部账号，再用 YYB_ONLY_REFS 按 ref 过滤。返回 (server, ref, remark)。"""
    raw = os.getenv("YYB_SERVER") or YYB_SERVER_RAW
    if not raw:
        return []
    base: List[Tuple[str, str, str]] = []
    for item in _split_accounts(raw):
        parsed = _parse_yyb_line(item)
        if parsed:
            base.append(parsed)
    # 按 YYB_ONLY_REFS 序号白名单筛选（1 起）；留空 [] 则运行全部账号
    raw_filter = os.getenv("YYB_ONLY_REFS")
    seqs = list(YYB_ONLY_REFS)
    if raw_filter:
        seqs = []
        for tok in str(raw_filter).replace(",", " ").replace("[", " ").replace("]", " ").split():
            seqs.append(tok)
    if seqs:
        wanted = set(int(x) for x in seqs if str(x).strip().isdigit() and int(x) > 0)
        if wanted:
            base = [e for i, e in enumerate(base, 1) if i in wanted]
            log("ℹ️ 按 YYB_ONLY_REFS 筛选：请求序号 %s，命中 %d 个账号" % (sorted(wanted), len(base)))
    return base


def _headers(token: str) -> Dict[str, str]:
    return {
        "appId": APP_ID,
        "envVersion": ENV_VERSION,
        "content-type": "application/json",
        "Authorization": "bearer" + token,
        "xy-extra-data": XY_EXTRA,
        "version": APP_VERSION,
        "Accept-Encoding": "gzip,compress,br,deflate",
        "User-Agent": USER_AGENT,
        "Referer": REFERER,
    }


def _req_get(token: str, path: str, params: Optional[Dict] = None) -> Optional[Dict]:
    try:
        r = requests.get(API_BASE + path, params=params, headers=_headers(token), timeout=15)
        return r.json()
    except Exception as e:
        warn(f"⚠️ 请求失败 {path}: {e}")
        return None


def sign_one(token: str) -> Dict[str, Any]:
    """执行单个账号签到：先查 pointsInfo 判断是否已签，未签才调用 sign，
    再查 pointsInfo 取最新总积分与连续天数。对齐 HAR 真实链路。"""
    result: Dict[str, Any] = {"ok": False, "signed": False, "reward": 0,
                              "total": None, "series": None, "msg": ""}

    # 1) 签到前状态：signTodayResult=true 表示今日已签；pointsNums=当前总积分
    pre = _req_get(token, "/dtapi/pointsSign/user/pointsInfo/query")
    pre_data = (pre or {}).get("data") or {}
    if pre_data.get("pointsNums") is not None:
        result["total"] = int(pre_data["pointsNums"])
    if pre_data.get("seriesDays") is not None:
        result["series"] = int(pre_data["seriesDays"])
    if pre_data.get("signTodayResult") is True:
        result["signed"] = True
        result["msg"] = "今日已签"
        return result

    # 2) 未签 → 执行签到（GET /dtapi/pointsSign/user/sign?date=YYYY/MM/DD）
    today = time.strftime("%Y/%m/%d")
    resp = _req_get(token, "/dtapi/pointsSign/user/sign", params={"date": today})
    if not resp:
        result["msg"] = "网络/解析失败"
        return result
    if resp.get("success") is not True and resp.get("error") not in (0, None):
        errmsg = str(resp.get("errorMsg") or resp.get("error_msg")
                     or resp.get("msg") or resp.get("message") or resp)
        if "已签" in errmsg or "重复" in errmsg:
            result["signed"] = True  # 服务端明确"已签到/重复签到" → 视为今日已签
            result["msg"] = "今日已签"
        else:
            result["msg"] = errmsg or str(resp)
        return result

    # 3) 签到成功：取本次奖励（signReward 基础奖励 + extraSignReward 额外奖励）
    data = resp.get("data") or {}
    reward = int(data.get("signReward") or 0)
    extra = int(data.get("extraSignReward") or 0)
    result["reward"] = reward + extra
    result["ok"] = True
    msg = f"签到+{reward}积分"
    if extra:
        msg += f"（额外+{extra}）"
    result["msg"] = msg

    # 4) 签到后状态：取权威总积分与连续天数
    post = _req_get(token, "/dtapi/pointsSign/user/pointsInfo/query")
    post_data = (post or {}).get("data") or {}
    if post_data.get("pointsNums") is not None:
        result["total"] = int(post_data["pointsNums"])
    if post_data.get("seriesDays") is not None:
        result["series"] = int(post_data["seriesDays"])
    return result


def parse_accounts() -> List[Dict[str, Any]]:
    """返回账号列表。优先级：IYOUKE_TOKEN（手动）> YYB_SERVER（YYBGO 取码）。"""
    accounts: List[Dict[str, Any]] = []
    if IYOUKE_TOKEN:
        for i, tok in enumerate(_split_accounts(IYOUKE_TOKEN), 1):
            accounts.append({"mode": "manual", "token": tok, "remark": f"手动{i}"})
    for server, ref, remark in _yyb_entries():
        accounts.append({"mode": "yyb", "server": server, "ref": ref, "remark": remark})
    if not accounts:
        raise RuntimeError(
            "缺少账号配置：请设置 YYB_SERVER（配合 YYBGO，格式 地址@ref）"
            "或 IYOUKE_TOKEN（手动 token，免 YYB）。"
        )
    return accounts


def main() -> int:
    results: List[Dict] = []
    accounts = parse_accounts()
    log(f"🚀 {SCRIPT_NAME} 开始 · 共 {len(accounts)} 个账号")
    any_fail = False
    for idx, acc in enumerate(accounts, 1):
        acc_banner(idx, len(accounts), "备注：" + str(acc["remark"]))
        # 取 token
        if acc["mode"] == "manual":
            token = acc["token"]
            log("🔑 使用手动 token")
        else:
            try:
                code = YYBClient(APP_ID).get_code(acc["server"], acc["ref"])
                token = app_login(code)
                log(f"🔑 YYBGO 取码+登录成功（token 前6位 {token[:6]}…）")
            except Exception as e:
                err(f"❌ 登录失败: {e}")
                results.append({"ok": False, "signed": False, "reward": 0,
                                "total": None, "msg": f"登录失败: {e}",
                                "idx": idx, "remark": acc["remark"]})
                any_fail = True
                acc_footer(idx, len(accounts))
                continue
        res = sign_one(token)
        tail = (f"，总积分 {res['total']}" if res["total"] is not None else "") \
            + (f"，连续 {res['series']} 天" if res.get("series") is not None else "")
        if res["ok"]:
            log(f"✅ {res['msg']}{tail}")
        elif res["signed"]:
            log(f"🟡 {res['msg']}{tail}")
        else:
            err(f"❌ {res['msg']}{tail}")
        results.append({"ok": res["ok"], "signed": res["signed"],
                        "reward": res["reward"], "total": res["total"],
                        "msg": res["msg"], "idx": idx, "remark": acc["remark"]})
        if not (res["ok"] or res["signed"]):
            any_fail = True
        acc_footer(idx, len(accounts))

    success = sum(1 for r in results if r["ok"])
    signed = sum(1 for r in results if r["signed"])
    for _i, r in enumerate(results, 1):
        _acct = f"[{r['remark']}]"
        detail = ""
        if r["total"] is not None:
            detail = f"总积分{r['total']}"
            if (r.get("reward") or 0) > 0:
                detail += f"(+{r['reward']})"
        if r["ok"]:
            extra = f"（连续{r['series']}天）" if r.get("series") is not None else ""
            log(f"✅ {_acct} 签到成功{extra} {detail}".rstrip())
        elif r["signed"]:
            log(f"🟡 {_acct} 今日已签 {detail}".rstrip())
        else:
            err(f"❌ {_acct} 签到失败：{r.get('msg') or ''}")
    log(_ACC_RULE)
    log(f"📊 [执行汇总] {SCRIPT_NAME} · 账号 {len(results)} ｜ 成功 {success} ｜ 已签 {signed} ｜ 失败 {len(results) - success - signed}")
    log(_ACC_RULE)    
    # 错误日志推送：有错误才发；未配置通知渠道则只提示一行
    flush_notify(
        "交个朋友",
        f"账号 {len(results)} ｜ 成功 {success} ｜ 已签 {signed} ｜ 失败 {len(results) - success - signed}",
        logger=log,
    )
    return 1 if any_fail else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        err(f"💥 脚本异常: {e}")
        import traceback
        err(traceback.format_exc())
        sys.exit(1)
