#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# =========================================================
# name:  南+论坛
# cron: 10 0,12 * * *
# =========================================================
"""
南+论坛 (bbs.south-plus.org) 日常任务脚本
论坛架构：phpwind (PW)
============================================================================
站点要点：
  · 账号密码登录只能走站点自带的 WAP 通道（见下方⚠️，主站登录强制验证码）
  · 任务页未登录时返回的是「您还没有登录或注册，暂时不能使用此功能!!」
    （不是「请先登录」），脚本据此触发「失效自动重登续签」
  · 站点强制「刷新不要快于 1 秒」，脚本内置请求节流（SOUTHPLUS_MIN_INTERVAL_MS）

⚠️ 重要：主站 /login.php 强制要求「认证码」（验证码，150x60 强干扰图，含多层叠字 + 波浪扭曲，
   人眼都难读），**无法自动化**。本站的 WAP 模块 /wap/index.php?prog=login 无验证码，
   故账号密码自动登录走 WAP 通道；登录后拿到的 Cookie 与主站共用（同一 domain/path=/）。

基于用户提供的 HAR 逆向，关键请求：

  1. 任务页  GET  /plugin.php?H_name-tasks.html
             -> 页面 JS 含 var verifyhash = '82511105';（任务接口所需的 verify）
             -> 已登录标记：页面含 action-quit（退出链接）
  2. 领任务  GET  /plugin.php?H_name=tasks&action=ajax&actions=job&cid=15
                   &nowtime=<ms>&verify=<verifyhash>
  3. 领奖励  GET  /plugin.php?H_name=tasks&action=ajax&actions=job2&cid=15
                   &nowtime=<ms>&verify=<verifyhash>

注：日常任务 cid 在 HAR 中实证为 15（"已申请[日常]"），如需适配其他任务改 CID 即可。
    日常任务为"每日登录"类，登录即完成，故 job 之后可直接 job2。

凭证获取顺序：本地缓存（主） → 账号密码登录（辅） → SOUTHPLUS_COOKIE（最后兜底）

环境变量：
  SOUTHPLUS_ACCOUNT      建议必配。账号密码，格式「账号#密码」；无缓存时靠它自动登录
  SOUTHPLUS_COOKIE       可选。登录后的 Cookie 字符串，**最后兜底**。
                         刻意不写回缓存 —— 否则下一轮会直接从缓存命中，等于把它抬到登录之前
  SOUTHPLUS_COOKIE_CACHE 可选。缓存文件路径，默认 /ql/data/southplus.cookie（不可写则回退脚本同目录）
  SOUTHPLUS_FORCE_LOGIN  可选。=1 忽略缓存，强制走账号密码登录并覆盖缓存（调试用；
                         开启时也不会走 SOUTHPLUS_COOKIE 兜底，免得掩盖"登录到底成没成"）
  SOUTHPLUS_PROBE        可选。=1 仅预检：网络连通性 + 登录通道探测，不登录、不签到
  SOUTHPLUS_DUMP_SIGN    可选。=1 签到/领奖成功时也把服务器响应原文打出来（默认关，排查用）
  SOUTHPLUS_TIMEOUT      可选。单次请求超时毫秒，默认 20000
  SOUTHPLUS_MIN_INTERVAL_MS 可选。请求最小间隔毫秒，默认 1200
                         （站点强制「刷新不要快于 1 秒」，太快会被挡在提示页）
  SOUTHPLUS_EXPIRE       可选。Cookie 预期过期日，如 2026-10-15。设了会提前 3 天提醒
  MY_PROXY               必填。HTTP/HTTPS 代理（CONNECT 隧道），如 http://192.168.31.233:7890
                         南+ 不代理无法访问，未配置将直接报错退出
  SOUTHPLUS_PROXY        可选。同 MY_PROXY，仅本脚本的覆盖别名；两者都配时以它为准
  SOUTHPLUS_NO_PROXY     可选。=1 忽略上述代理强制直连（⚠️ 实测本站直连不通，仅调试用）
  SOUTHPLUS_SITE         可选。站点地址，默认 https://bbs.south-plus.org；仅供本地 mock 回归测试

依赖 (青龙依赖管理里加一行):  curl_cffi
    （用于模拟 Chrome 过 Cloudflare，否则可能被 403/503 拦）；缺依赖时自动退回 requests

日志规范：[LEVEL] [SOUTHPLUS] emoji message   （LEVEL: INFO / WARN / ERROR）
报错输出：所有失败路径都会打印「HTTP 状态 + 关键响应头 + 完整响应原文 + 异常堆栈」，
          原文默认不截断（超过 64KB 才截断并注明剩余量）；成功路径不打印任何响应内容。
============================================================================
"""

import json
import os
import random
import re
import sys
import time
import traceback
from datetime import datetime, timedelta
from urllib.parse import quote as _q

# ========== 统一日志 ==========
def _emit(level, msg):
    print("[%s] [SOUTHPLUS] %s" % (level, msg), flush=True)

def log(msg):  _emit("INFO", msg)
def warn(msg): _emit("WARN", msg)
def err(msg):  _emit("ERROR", msg)


# ========== 版本标识 ==========
# 每次实质性改动 +1。启动日志会带上它，用来确认「容器里跑的到底是哪一版」
# （踩过坑：本机改了、容器没同步，日志看着像"修复没生效"，实际是跑着旧文件）。
SCRIPT_VER = "2026-10-01a"

# ========== 常量 ==========
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

DUMP_LIMIT = 64 * 1024          # 响应原文打印上限
CID = 15                        # 日常任务 cid（HAR 实证）
TASKS_PATH = "/plugin.php?H_name-tasks.html"
WAP_LOGIN_PATH = "/wap/index.php?prog=login"

# 匿名态就会存在的 PW cookie（登录成功后会出现这些之外的字段，如 winduser 系列）
ANON_COOKIE_NAMES = ("lastvisit", "lastpos", "ol_offset", "cknum", "lastreply", "lastvisitv")


def _truthy(v):
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")


# ========== 配置（全部支持环境变量覆盖）==========
SITE = (os.environ.get("SOUTHPLUS_SITE") or "https://bbs.south-plus.org").rstrip("/")
TASK_PAGE = SITE + TASKS_PATH
WAP_LOGIN = SITE + WAP_LOGIN_PATH
TIMEOUT_MS = int(os.environ.get("SOUTHPLUS_TIMEOUT") or "20000")
TIMEOUT = max(3.0, TIMEOUT_MS / 1000.0)
MIN_INTERVAL = max(0.0, int(os.environ.get("SOUTHPLUS_MIN_INTERVAL_MS") or "1200") / 1000.0)
DUMP_SIGN = _truthy(os.environ.get("SOUTHPLUS_DUMP_SIGN"))
PROBE = _truthy(os.environ.get("SOUTHPLUS_PROBE"))
FORCE_LOGIN = _truthy(os.environ.get("SOUTHPLUS_FORCE_LOGIN"))
SOUTHPLUS_EXPIRE = (os.environ.get("SOUTHPLUS_EXPIRE") or "").strip()

# 代理：SOUTHPLUS_PROXY（本脚本覆盖别名）> MY_PROXY（与 xsijishe.py / 其他脚本统一）
NO_PROXY = _truthy(os.environ.get("SOUTHPLUS_NO_PROXY"))
_env_proxy = (os.environ.get("SOUTHPLUS_PROXY") or "").strip()
_my_proxy = (os.environ.get("MY_PROXY") or "").strip()
PROXY = "" if NO_PROXY else (_env_proxy or _my_proxy)
PROXY_SRC = "SOUTHPLUS_PROXY" if _env_proxy else ("MY_PROXY" if _my_proxy else "")
PROXY_DESC = ("已按 SOUTHPLUS_NO_PROXY=1 强制直连" if NO_PROXY
              else ((PROXY + "（来源 " + PROXY_SRC + "）") if PROXY else "未配置"))


# ========== HTTP 后端（优先 curl_cffi 过 Cloudflare，缺失则退回 requests）==========
_BACKEND = None
_http = None
try:
    from curl_cffi import requests as _http      # type: ignore
    _BACKEND = "curl_cffi"
except ImportError:
    try:
        import requests as _http                 # type: ignore
        _BACKEND = "requests"
    except ImportError:
        _http = None


# ========== 缓存文件 ==========
def resolve_cache_path():
    p = (os.environ.get("SOUTHPLUS_COOKIE_CACHE") or "").strip()
    if p:
        return p
    primary = "/ql/data/southplus.cookie"
    # ⚠️ 只在青龙持久目录「已存在且可写」时使用，**不要主动 makedirs** ——
    #    否则在 Windows 上 "/ql/data" 会被解析成 C:\ql\data，凭空在系统盘造出目录。
    try:
        d = os.path.dirname(primary)
        if os.path.isdir(d) and os.access(d, os.W_OK):
            return primary
    except Exception:
        pass
    # 青龙持久目录不可用（如本机调试）→ 回退脚本同目录
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), ".southplus.cookie")


CACHE_FILE = resolve_cache_path()


def read_cache():
    """读缓存。返回 dict(cookie/source/savedAt) 或 None。兼容 JSON 与旧式纯文本 Cookie。"""
    try:
        if not os.path.exists(CACHE_FILE):
            return None
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            txt = f.read().strip()
        if not txt:
            return None
        if txt.startswith("{"):
            try:
                o = json.loads(txt)
                ck = (o or {}).get("cookie") or ""
                if isinstance(ck, str) and ck.strip():
                    return {"cookie": ck.strip(), "source": o.get("source", "-"),
                            "savedAt": o.get("savedAt", "-")}
                return None
            except Exception as e:
                warn("⚠️ 缓存文件 JSON 解析失败（%s），按纯文本 Cookie 处理" % e)
                warn("⚠️ 缓存文件内容前 200 字符: %s" % txt[:200])
        return {"cookie": txt, "source": "legacy-plain", "savedAt": "-"}
    except Exception as e:
        warn("⚠️ 读取 Cookie 缓存失败: %s" % e)
        return None


def write_cache(cookie_str, source):
    if not cookie_str:
        return
    try:
        d = os.path.dirname(CACHE_FILE)
        if d:
            os.makedirs(d, exist_ok=True)
        payload = json.dumps({
            "v": 1,
            "source": source or "-",
            "savedAt": datetime.now().isoformat(timespec="seconds"),
            "cookie": cookie_str,
        }, ensure_ascii=False, indent=2)
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            f.write(payload)
        try:
            os.chmod(CACHE_FILE, 0o600)
        except Exception:
            pass
        log("💾 Cookie 已写入缓存（%s）: %s" % (source or "-", CACHE_FILE))
    except Exception as e:
        warn("⚠️ Cookie 缓存写入失败: %s" % e)


# ========== Cookie 工具 ==========
def parse_cookie(s):
    """'k=v; k2=v2' → dict（保留重复键的最后一次赋值）"""
    d = {}
    for part in str(s or "").split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, v = part.split("=", 1)
        k = k.strip()
        if k:
            d[k] = v.strip()
    return d


def cookie_str_of(d):
    return "; ".join("%s=%s" % (k, v) for k, v in d.items())


# phpwind 的 cookie 前缀（形如 eb9e6_）由站点随机生成，这里动态探测，避免写死。
# ⚠️ 前缀可能是**多段**的（Discuz 系站点常见 SgL6_2132_ 这种两段形式），
#    所以中段要写成 `(?:_[A-Za-z0-9]+)*_`；只写 `[A-Za-z0-9]+_` 会漏掉多段前缀。
_RE_PREFIX = re.compile(r"^([A-Za-z0-9]+(?:_[A-Za-z0-9]+)*_)(?:" + "|".join(ANON_COOKIE_NAMES) +
                        r"|winduser|winduserid|windpwd|cknum|lastvisit)$")


def detect_prefix(cookies):
    """从 cookie 字典里探测 PW 前缀（返回 'eb9e6_' 形式，探测不到返回 ''）"""
    for k in cookies or {}:
        m = _RE_PREFIX.match(k)
        if m:
            return m.group(1)
    return ""


def has_login_cookie(jar):
    """PW 登录后会下发 winduser 系列 cookie（匿名态只有 lastvisit/lastpos/ol_offset）。
       注意：这里只作**辅助**判断，最终以「任务页是否含 action-quit」在线校验为准。"""
    for k in (jar or {}):
        tail = k.rsplit("_", 1)[-1].lower()
        if "winduser" in tail or tail in ("windpwd", "winduserid"):
            return True
    return False


# ========== 响应摘要 / 报错详情 ==========
def text_of(resp):
    # ⚠️ 同时接受「响应对象」和「裸字符串」：调用方有时传 response（如 do_job(c,...)），
    #    有时直接传已抽好的正文；只 getattr(resp,'text') 的话，传字符串会静默得到空串，
    #    导致 classify 一律判成 unknown（很难查）。
    if isinstance(resp, str):
        return resp
    if isinstance(resp, bytes):
        try:
            return resp.decode("utf-8", "replace")
        except Exception:
            return str(resp)
    t = getattr(resp, "text", "")
    if isinstance(t, bytes):
        try:
            t = t.decode("utf-8", "replace")
        except Exception:
            t = str(t)
    return t or ""


def brief(text, n=100):
    """把响应正文压成一行便于阅读的摘要（去 script/style / 去标签 / 压空白 / 截断）。

    ⚠️ 踩过两个坑，都在这里收口：
      1) 写成 `<[^>]+>` 会把论坛的 `<![CDATA[...]]>` 整段正文当标签吞掉 → 摘要恒为空。
         所以标签正则必须以字母或 / 开头，并且先把 CDATA 的括号剥掉。
      2) 提示语常常写在 <script> 的字符串里，HTML 骨架只剩模板词。直接删掉 script 就只剩噪声，
         所以删之前先把 script 里的字符串抢救出来当候选。
    """
    s = str(text or "")
    if not s.strip():
        return ""
    s = re.sub(r"<\?[\s\S]*?\?>", " ", s)
    s = re.sub(r"<!DOCTYPE[^>]*>", " ", s, flags=re.I)

    salvaged = []
    def _grab(m):
        # ⚠️ re.sub 的 replacement 函数收到的是 **Match 对象**（不是 JS 那样的字符串），
        #    必须先 .group(0) 取出整段 script 再扫里面的引号字符串。
        blk = m.group(0)
        for mm in re.finditer(r"['\"]([^'\"]{1,120})['\"]", blk):
            salvaged.append(mm.group(1))
        return " "
    s = re.sub(r"<script[\s\S]*?</script>", _grab, s, flags=re.I)

    s = re.sub(r"<style[\s\S]*?</style>", " ", s, flags=re.I)
    s = s.replace("<![CDATA[", " ").replace("]]>", " ")
    s = re.sub(r"<[a-zA-Z/][^>]*>", " ", s)
    for a, b in (("&nbsp;", " "), ("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'), ("&amp;", "&")):
        s = s.replace(a, b)
    s = re.sub(r"\s+", " ", s).strip()

    UI_NOISE = ("提示信息", "关闭", "确定", "返回上一页", "返回", "点击这里", "点击此处", "继续访问")

    def _strip(t):
        x = str(t or "")
        for w in UI_NOISE:
            x = x.replace(w, " ")
        return re.sub(r"\s+", " ", x).strip()

    out = _strip(s)
    if not out:
        cand = []
        for t in salvaged:
            t = _strip(t)
            if not t or not re.search(r"[\u4e00-\u9fa5]", t) or t in cand:
                continue
            cand.append(t)
        out = " / ".join(cand)
    return (out[:n] + "…") if len(out) > n else out


def dump_resp(label, resp):
    """打印完整响应上下文（HTTP 状态 + 关键响应头 + 响应原文）"""
    try:
        lines = ["📥 [%s] HTTP %s" % (label, getattr(resp, "status_code", "?"))]
        for h in ("content-type", "server", "location", "cf-ray", "cf-mitigated", "retry-after"):
            v = resp.headers.get(h) if getattr(resp, "headers", None) else None
            if v:
                lines.append("   %s: %s" % (h, v))
        t = text_of(resp)
        if len(t) > DUMP_LIMIT:
            lines.append("   ── 响应原文（前 %d 字节，共 %d 字节，余 %d 字节已截断）──"
                         % (DUMP_LIMIT, len(t), len(t) - DUMP_LIMIT))
            t = t[:DUMP_LIMIT]
        else:
            lines.append("   ── 响应原文（共 %d 字节）──" % len(t))
        print("\n".join(lines), flush=True)
        print(t, flush=True)
        print("   ── 原文结束 ──", flush=True)
    except Exception as e:
        warn("⚠️ 打印响应详情失败: %s" % e)


def dump_err(label, e):
    warn("💥 [%s] %s: %s" % (label, type(e).__name__, e))
    try:
        print(traceback.format_exc(), flush=True)
    except Exception:
        pass


# ========== 请求节流（站点强制「刷新不要快于 1 秒」）==========
_last_ts = [0.0]


def _pace():
    if MIN_INTERVAL <= 0:
        return
    wait = _last_ts[0] + MIN_INTERVAL - time.time()
    if wait > 0:
        time.sleep(wait + random.uniform(0.05, 0.35))
    _last_ts[0] = time.time()


# ========== HTTP 客户端 ==========
class Client(object):
    """自己维护 Cookie 字典，显式走 `Cookie:` 头 —— 避开各 HTTP 库在 cookie 域/路径上的差异"""

    def __init__(self, proxy=""):
        self.jar = {}
        self.proxy = proxy
        self._impersonate = "chrome" if _BACKEND == "curl_cffi" else None
        self.base_headers = {
            "User-Agent": UA,
            "Accept": "*/*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Referer": SITE + "/",
        }

    # ---- cookie ----
    def jar_str(self):
        return cookie_str_of(self.jar)

    def absorb(self, resp):
        for sc in _set_cookie_list(resp):
            kv = sc.split(";", 1)[0].strip()
            if "=" not in kv:
                continue
            k, v = kv.split("=", 1)
            k = k.strip()
            if k:
                self.jar[k] = v.strip()

    # ---- 请求 ----
    def request(self, url, method="GET", headers=None, data=None, cookies=None,
                allow_redirects=True, timeout=None, no_retry=False):
        hdrs = dict(self.base_headers)
        if headers:
            hdrs.update(headers)
        hdrs["Cookie"] = (cookies if cookies is not None else self.jar_str())
        to = timeout or TIMEOUT
        last = None
        tries = 1 if no_retry else 2
        for i in range(tries):
            _pace()
            t0 = time.time()
            try:
                kw = dict(headers=hdrs, data=data, timeout=to, allow_redirects=allow_redirects)
                if self.proxy:
                    kw["proxies"] = {"http": self.proxy, "https": self.proxy}
                if self._impersonate:
                    kw["impersonate"] = self._impersonate
                r = _http.request(method, url, **kw)
                r._x_elapsed = time.time() - t0    # 便于日志打耗时
                self.absorb(r)
                return r
            except Exception as e:
                last = e
                if i < tries - 1:
                    warn("🔁 请求失败（第 %d/%d 次）: %s: %s" % (i + 1, tries, type(e).__name__, e))
        raise last


def _set_cookie_list(resp):
    """取响应里的全部 Set-Cookie（保留多条，兼容 curl_cffi / requests）"""
    out = []
    h = getattr(resp, "headers", None)
    if h is not None:
        fn = getattr(h, "multi_items", None)          # curl_cffi.requests.Headers
        if callable(fn):
            try:
                out = [str(v) for k, v in fn() if str(k).lower() == "set-cookie"]
            except Exception:
                out = []
        if not out:
            for nm in ("get_list", "getlist", "get_all"):  # 其他库的多值接口
                fn = getattr(h, nm, None)
                if callable(fn):
                    try:
                        out = [str(x) for x in (fn("set-cookie") or [])]
                    except Exception:
                        out = []
                    if out:
                        break
    if not out:
        raw = getattr(resp, "raw", None)
        raw_h = getattr(raw, "headers", None)
        if raw_h is not None:
            fn = getattr(raw_h, "getlist", None) or getattr(raw_h, "get_all", None)
            if callable(fn):
                try:
                    out = [str(x) for x in (fn("Set-Cookie") or [])]
                except Exception:
                    out = []
    if not out:
        cj = getattr(resp, "cookies", None) or getattr(resp, "_cookies", None)
        if cj is not None:
            try:
                items = cj.items() if hasattr(cj, "items") else []
                out = ["%s=%s" % (k, v) for k, v in items]
            except Exception:
                out = []
    return out


def elapsed_of(resp):
    return getattr(resp, "_x_elapsed", None)


def _urlquote(s):
    return _q(str(s), safe="")


# ========== 账号密码登录（phpwind WAP 通道，免验证码）==========
def extract_wap_msg(text):
    """从 WAP(WML) 响应里抽出 <card id="msg"> 的提示语"""
    m = re.search(r"<card[^>]*id=[\"']msg[\"'][^>]*>[\s\S]*?<p>([\s\S]*?)</p>", str(text or ""), re.I)
    if not m:
        return ""
    s = re.sub(r"<[^>]+>", " ", m.group(1))
    s = s.replace("&#160;", " ").replace("&nbsp;", " ")
    return re.sub(r"\s+", " ", s).strip()


# 单次登录尝试。返回 (state, jar, extra)，state 含义：
#   'ok'       登录成功（出现了 winduser 系列 cookie 或提示语明确成功）
#   'cred'     账号/密码问题 → 绝不重试
#   'question' 需要安全提问答案 → 本脚本无法自动过
#   'captcha'  通道要求验证码（WAP 正常不该出现）
#   'nomark'   既没成功迹象也没明确失败原因（多为节流/网络抖动）→ 可重试一次
def wap_login_attempt(user, pwd, attempt_no):
    c = Client(PROXY)
    try:
        log("🌐 访问 WAP 登录页（免验证码通道）...")
        g = c.request(WAP_LOGIN, headers={"Referer": SITE + "/wap/"})
        gt = text_of(g)
        if getattr(g, "status_code", 0) >= 400:
            warn("⚠️ WAP 登录页返回 HTTP %s" % getattr(g, "status_code", "?"))
            dump_resp("WAP登录页", g)
        if not re.search(r"name=[\"']?pwuser", gt, re.I):
            warn("⚠️ WAP 登录页未出现 pwuser 表单字段 → 该通道可能已变更")
            dump_resp("WAP登录页", g)
        prefix = detect_prefix(c.jar)
        before = set(c.jar.keys())

        body = "&".join([
            "lgt=0",                            # 0=用户名
            "pwuser=" + _urlquote(user),
            "pwpwd=" + _urlquote(pwd),
            "question=0",                       # 0=无安全提问
            "customquest=",
            "answer=",
        ])
        log("📤 提交 WAP 登录%s..." % ("（第 %d 次尝试）" % attempt_no if attempt_no > 1 else ""))
        r = c.request(WAP_LOGIN, method="POST",
                      headers={"Content-Type": "application/x-www-form-urlencoded",
                               "Referer": WAP_LOGIN,
                               "Origin": SITE},
                      data=body, no_retry=True)
        t = text_of(r)
        msg = extract_wap_msg(t)
        new_keys = [k for k in (set(c.jar.keys()) - before)]
        prefix = detect_prefix(c.jar) or prefix

        # ⚠️ 判定要「提示语 + cookie」双证据：WAP 成功页文案可能随版本变化，
        #    而 PW 登录一定会下发 winduser 系列 cookie（匿名态只有 lastvisit/lastpos/ol_offset）。
        if msg:
            if re.search(r"不存在|没有找到|未找到该用户", msg):
                warn("🚫 账号不存在：%s（请核对 SOUTHPLUS_ACCOUNT 的「账号#密码」）" % msg)
                return "cred", c.jar, prefix
            if re.search(r"密码(错误|不正确|不对|有误)", msg):
                warn("🚫 账号或密码不正确：%s" % msg)
                return "cred", c.jar, prefix
            if re.search(r"安全提问|问答|答案", msg):
                warn("🚫 该账号设置了安全提问，WAP 通道无法自动登录：%s" % msg)
                return "question", c.jar, prefix
            if re.search(r"验证码|认证码", msg):
                warn("🚫 该通道也要求验证码：%s" % msg)
                return "captcha", c.jar, prefix

        if has_login_cookie(c.jar) or re.search(r"登录成功|欢迎您|欢迎回来", msg or ""):
            log("🔑 登录成功（拿到 %s 系列 cookie，共 %d 个字段）"
                % (prefix + "winduser" if prefix else "winduser", len(c.jar)))
            log("🍪 本次新增 cookie: %s" % (", ".join(new_keys) if new_keys else "无"))
            return "ok", c.jar, prefix

        warn("⚠️ WAP 登录未拿到登录态（HTTP %s）｜提示语: %s"
             % (getattr(r, "status_code", "?"), msg or brief(t, 60) or "(空)"))
        warn("⚠️ 本次新增 cookie: %s" % (", ".join(new_keys) if new_keys else "无"))
        dump_resp("WAP登录POST", r)
        return "nomark", c.jar, prefix
    except Exception as e:
        dump_err("WAP 登录流程", e)
        return "nomark", {}, ""


# 只有「没有明确结论（nomark，多为节流/网络抖动）」才重试一次；
# 账号密码错误 / 需安全提问 / 需验证码 —— 重试无意义且可能加速风控，绝不重试。
MAX_LOGIN_ATTEMPT = 2


def login_and_get_cookie(user, pwd):
    if not user or not pwd:
        warn("⚠️ 未配置账号密码（SOUTHPLUS_ACCOUNT=账号#密码），无法自动登录")
        return None
    for i in range(1, MAX_LOGIN_ATTEMPT + 1):
        state, jar, _ = wap_login_attempt(user, pwd, i)
        if state == "ok":
            return jar
        if state in ("cred", "question", "captcha"):
            return None
        if i < MAX_LOGIN_ATTEMPT:
            warn("🔁 WAP 登录结果不明确，稍后重试一次（第 %d/%d 次）..." % (i + 1, MAX_LOGIN_ATTEMPT))
            time.sleep(random.uniform(2.0, 3.0))
    return None


def parse_account(s):
    """解析「账号#密码」（也兼容「账号:密码」；优先 # 以免密码里的冒号被误切）"""
    s = str(s or "").strip()
    if not s:
        return None, None
    if "#" in s:
        u, p = s.split("#", 1)
        return u.strip(), p.strip()
    if ":" in s:
        u, p = s.split(":", 1)
        return u.strip(), p.strip()
    return None, None


# ========== 登录态在线校验 ==========
def is_logged_in(html):
    """PW 已登录页面里一定有「退出」链接（action-quit-verify-...html）"""
    return "action-quit" in str(html or "")


def fetch_tasks_page(c):
    r = c.request(TASK_PAGE, headers={"Referer": SITE + "/"})
    return r, text_of(r)


def extract_verifyhash(html):
    # 页面 JS: var verifyhash = '82511105';
    i = str(html or "").find("verifyhash")
    if i < 0:
        return None
    q1 = html.find("'", i)
    if q1 < 0:
        return None
    q2 = html.find("'", q1 + 1)
    if q2 < 0:
        return None
    return html[q1 + 1:q2]


# ========== 凭证解析：缓存（主） → 账号密码登录（辅） → SOUTHPLUS_COOKIE（最后兜底）==========
# ⚠️ 这个顺序是**有意**排的：环境变量 Cookie 放在账号密码**之后**。
#    它只在「缓存和登录都拿不到」时救场（典型场景：站点临时上验证码/风控，登录走不通，
#    但手里还有一份自浏览器导出的有效 Cookie）。
#    ⇒ 环境变量 Cookie **不写回缓存**：否则下一轮会直接从缓存命中，等于把它抬到登录之前，
#      与"最后兜底"的定位相矛盾。
#
# 注：这里**只负责把 Cookie 取出来**，「这份 Cookie 到底还有没有登录态」由调用方
#     访问任务页在线校验（PW 的登录 cookie 名随版本不同，离线判定不可靠）。
def resolve_cookie(user, pwd):
    # 1) 本地缓存为主
    if not FORCE_LOGIN:
        cached = read_cache()
        if cached and cached.get("cookie", "").strip():
            c = Client(PROXY)
            c.jar = parse_cookie(cached["cookie"])
            log("📦 使用本地缓存 Cookie（来源: %s | 写入: %s | %d 个字段%s）"
                % (cached.get("source", "-"), cached.get("savedAt", "-"), len(c.jar),
                   ("｜前缀 " + detect_prefix(c.jar)) if detect_prefix(c.jar) else ""))
            return c
        log("📦 缓存不存在或为空（首次运行会走账号密码登录）")
    else:
        warn("🔁 SOUTHPLUS_FORCE_LOGIN 已开启：忽略缓存，强制账号密码登录")

    # 2) 账号密码登录：无缓存 / 缓存失效时自动登录，成功后把新 Cookie 写回缓存
    if user and pwd:
        log("🔐 尝试账号密码登录获取 Cookie（走 WAP 免验证码通道）...")
        fresh = login_and_get_cookie(user, pwd)
        if fresh:
            c = Client(PROXY)
            c.jar = fresh
            write_cache(cookie_str_of(fresh), "login")
            return c
        warn("⚠️ 账号密码登录未拿到有效 Cookie，继续尝试 SOUTHPLUS_COOKIE 兜底")
    else:
        warn("⚠️ SOUTHPLUS_ACCOUNT 未配置或格式不对（应为「账号#密码」），跳过账号密码登录")

    # 3) SOUTHPLUS_COOKIE 环境变量：最后兜底
    #    调试开关开启时跳过 —— 那时要看的就是"登录本身成功没有"，兜底会把失败掩盖成成功
    if FORCE_LOGIN:
        warn("🔁 SOUTHPLUS_FORCE_LOGIN 已开启：跳过 SOUTHPLUS_COOKIE 兜底（否则会掩盖登录是否真的成功）")
        return None
    env_ck = (os.environ.get("SOUTHPLUS_COOKIE") or "").strip()
    if not env_ck:
        return None
    c = Client(PROXY)
    c.jar = parse_cookie(env_ck)
    log("🌱 账号密码登录也拿不到 Cookie，改用 SOUTHPLUS_COOKIE 环境变量兜底（本次不写缓存，%d 个字段）"
        % len(c.jar))
    return c


# ========== 领任务 / 领奖励 ==========
def call_task_api(c, action, verify):
    url = (SITE + "/plugin.php?H_name=tasks&action=ajax"
           "&actions=%s&cid=%d&nowtime=%d&verify=%s"
           % (action, CID, int(time.time() * 1000), verify))
    return c.request(url, headers={"Referer": TASK_PAGE})


def extract_cdata(txt):
    """从 <ajax><![CDATA[...]]></ajax> 抽出可读内容（不展示原始 XML 标签）"""
    s = text_of(txt)
    i = s.find("<![CDATA[")
    if i < 0:
        return s.strip()
    j = s.find("]]>", i)
    if j < 0:
        return s[i + 9:].strip()
    return s[i + 9:j].strip()


def short_resp(txt, n=60):
    """响应简要信息（去标签/去制表符/截断），避免完整打印原始 XML"""
    msg = extract_cdata(txt).replace("\t", " ").replace("\n", " ").strip()
    return (msg[:n] + "...") if len(msg) > n else msg


def classify_job(txt):
    """任务接口响应分类： success / done(冷却期内即已完成) / expired(未登录) / unknown

    ⚠️ 本站未登录时任务接口返回的是「您还没有登录或注册，暂时不能使用此功能!!」
       （实测），**不是**「请先登录」。漏了这一条的话，Cookie 失效会落到 unknown
       → 直接报错退出，不会走「失效自动重登续签」。
    """
    t = text_of(txt)
    if "success" in t:
        return "success"
    if "还没超过" in t:
        return "done"
    if re.search(r"还没有登录|未登录|请先登录|需要登录|游客|注册后才能|不能使用此功能", t):
        return "expired"
    return "unknown"


def do_job(c, verify):
    log("🔧 申请日常任务...")
    try:
        r = call_task_api(c, "job", verify)
    except Exception as e:
        dump_err("job 接口", e)
        raise
    kind = classify_job(r)
    if kind == "success":
        log("✅ 申请成功")
        return "ok", r
    if kind == "done":
        log("♻️ 日常任务今日已完成（冷却期内，无需重复申请）")
        return "done", r
    if kind == "expired":
        warn("🍪 任务接口提示未登录：%s" % short_resp(r))
        return "expired", r
    warn("⚠️ 申请未成功: %s" % short_resp(r))
    return "fail", r


def do_job2(c, verify, job_status):
    log("🎁 领取日常任务奖励...")
    try:
        r = call_task_api(c, "job2", verify)
    except Exception as e:
        dump_err("job2 接口", e)
        raise
    kind = classify_job(r)
    if kind == "success":
        log("🎉 领奖成功")
        return "ok", r
    if kind == "expired":
        warn("🍪 领奖接口提示未登录：%s" % short_resp(r))
        return "expired", r
    # 若申请已是冷却/已完成态，则领奖返回"未申请任务"属预期（已领过），不算失败
    if job_status == "done" and "未申请任务" in text_of(r):
        log("✅ 奖励今日已领取（无需重复领取）")
        return "done", r
    warn("⚠️ 领奖未成功: %s" % short_resp(r))
    return "fail", r


# ========== 预检 ==========
def run_probe(user, pwd):
    log("🔎 预检模式：只探网络与登录通道，不登录、不签到")
    log("📁 Cookie 缓存路径: %s" % CACHE_FILE)
    log("⏱️ 单次超时 %dms｜请求最小间隔 %dms｜HTTP 后端: %s"
        % (TIMEOUT_MS, int(MIN_INTERVAL * 1000), _BACKEND or "(无)"))
    log("🔌 代理: %s" % PROXY_DESC)

    c = Client(PROXY)
    log("🌐 请求首页...")
    try:
        r = c.request(SITE + "/")
        t = text_of(r)
        log("✅ 首页 HTTP %s｜%d 字节｜%.2fs｜已登录标记(action-quit): %s"
            % (getattr(r, "status_code", "?"), len(t),
               elapsed_of(r) or 0.0, "有" if is_logged_in(t) else "无"))
    except Exception as e:
        dump_err("首页请求", e)
        warn("⏭️ 首页不可达：先修网络（确认 mihomo 在跑、7890 端口可达）再回来")
        log("🏁 预检结束（未做任何登录/签到动作）")
        return

    log("🌐 请求主站登录页（预期：含认证码字段，说明主站无法自动登录）...")
    try:
        rl = c.request(SITE + "/login.php", headers={"Referer": SITE + "/"})
        tl = text_of(rl)
        cap = re.search(r"gdcode|ck\.php", tl, re.I)
        log("✅ 主站登录页 HTTP %s｜%d 字节｜%.2fs" % (getattr(rl, "status_code", "?"),
                                                 len(tl), elapsed_of(rl) or 0.0))
        log("🛡️ 主站登录认证码字段: %s" % ("存在 → 主站无法自动登录（改用 WAP 通道）" if cap else "无"))
    except Exception as e:
        dump_err("主站登录页请求", e)

    log("🌐 请求 WAP 登录页（自动登录通道）...")
    try:
        rw = c.request(WAP_LOGIN, headers={"Referer": SITE + "/wap/"})
        tw = text_of(rw)
        fields = sorted(set(re.findall(r"<(?:input|select)[^>]*name=[\"']?([a-zA-Z_]+)", tw, re.I)))
        capw = re.search(r"gdcode|ck\.php|验证码|认证码", tw, re.I)
        log("✅ WAP 登录页 HTTP %s｜%d 字节｜%.2fs" % (getattr(rw, "status_code", "?"),
                                                len(tw), elapsed_of(rw) or 0.0))
        log("🔑 WAP 表单字段: %s" % (", ".join(fields) if fields else "(未解析到)"))
        log("🛡️ WAP 验证码字段: %s" % ("存在 → 自动登录会被拦" if capw else "无（可纯账号密码自动登录）"))
        if not re.search(r"name=[\"']?pwuser", tw, re.I):
            warn("⚠️ WAP 登录页未出现 pwuser 字段 → 自动登录链路可能已变更")
            dump_resp("WAP登录页", rw)
    except Exception as e:
        dump_err("WAP 登录页请求", e)

    log("🌐 请求任务页...")
    try:
        rt, tt = fetch_tasks_page(c)
        vh = extract_verifyhash(tt)
        log("✅ 任务页 HTTP %s｜%d 字节｜%.2fs"
            % (getattr(rt, "status_code", "?"), len(tt), elapsed_of(rt) or 0.0))
        log("🔑 verifyhash: %s" % (vh or "未取到"))
        log("🔓 当前登录态: %s" % ("已登录" if is_logged_in(tt) else "未登录（预检不带 Cookie，属正常）"))
    except Exception as e:
        dump_err("任务页请求", e)

    cached = read_cache()
    if cached:
        jar = parse_cookie(cached["cookie"])
        log("📦 缓存存在：来源 %s｜写入 %s｜%d 个字段%s"
            % (cached.get("source", "-"), cached.get("savedAt", "-"), len(jar),
               ("｜前缀 " + detect_prefix(jar)) if detect_prefix(jar) else ""))
    else:
        log("📦 缓存不存在（首次运行会走账号密码登录）")
    log("👤 SOUTHPLUS_ACCOUNT %s" % ("已配置" if (user and pwd) else "未配置"))
    env_ck = (os.environ.get("SOUTHPLUS_COOKIE") or "").strip()
    log("🍪 SOUTHPLUS_COOKIE %s" % ("已配置（作为最后兜底）" if env_ck
                                   else "未配置（可选，仅在缓存与账号密码登录都失败时兜底）"))
    log("🏁 预检结束（未做任何登录/签到动作）")


# ========== Cookie 过期提醒 ==========
def check_cookie_expire(expire_str):
    """手动过期日检测（PW Cookie 无内置 exp，需用户手动配 SOUTHPLUS_EXPIRE）"""
    if not expire_str:
        return
    try:
        exp = datetime.strptime(expire_str, "%Y-%m-%d") + timedelta(hours=23, minutes=59, seconds=59)
    except ValueError:
        warn("📝 SOUTHPLUS_EXPIRE 格式错误，应为 YYYY-MM-DD")
        return
    delta = exp - datetime.now()
    remain = delta.days + (1 if delta.seconds > 0 else 0)
    log("🍪 Cookie %s | 过期: %s"
        % (("剩余 %d 天" % remain) if remain >= 0 else ("已过期 %d 天" % -remain), expire_str))
    if remain < 0:
        warn("⏰ Cookie 已过期，请重新登录抓取！")
    elif remain <= 3:
        warn("⏰ Cookie 即将过期（%d 天），建议尽快更新！" % remain)


# ========== 主流程 ==========
def main():
    log("🚀 南+论坛 日常任务开始（脚本 v%s）" % SCRIPT_VER)

    if _http is None:
        err("📦 缺少 HTTP 依赖：请执行 pip install curl_cffi（推荐，用于过 Cloudflare）或 pip install requests")
        sys.exit(1)
    if _BACKEND == "requests":
        warn("⚠️ 未装 curl_cffi，已退回 requests：Cloudflare 校验较严时可能被 403 拦，"
             "建议在青龙依赖管理里加一行 curl_cffi")

    if not PROXY:
        err("🛡️ 未配置 MY_PROXY，南+ 需走代理才能访问，请先配置代理"
            "（如 MY_PROXY=http://192.168.31.233:7890）")
        sys.exit(1)

    user, pwd = parse_account(os.environ.get("SOUTHPLUS_ACCOUNT"))
    if PROBE:
        run_probe(user, pwd)
        return

    # 1. 三级凭证解析（只负责取到 Cookie，是否有效由下面的任务页在线校验）
    c = resolve_cookie(user, pwd)
    if c is None:
        err("❌ 无法获取 Cookie（按「缓存 → 账号密码登录 → SOUTHPLUS_COOKIE」三级都失败；"
            "请检查 SOUTHPLUS_ACCOUNT=账号#密码，或配一个 SOUTHPLUS_COOKIE 兜底；缓存路径: %s）" % CACHE_FILE)
        sys.exit(1)

    check_cookie_expire(SOUTHPLUS_EXPIRE)

    # 2. 任务页取 verifyhash → 申请 + 领奖；Cookie 失效时自动重登一次
    for attempt in range(2):
        log("🌐 访问任务页...")
        try:
            rt, html = fetch_tasks_page(c)
        except Exception as e:
            dump_err("任务页请求", e)
            err("❌ 访问任务页失败（代理/网络异常）: %s" % e)
            sys.exit(1)

        if not is_logged_in(html):
            if attempt == 0 and (user and pwd) and not FORCE_LOGIN:
                warn("🍪 当前 Cookie 未处于登录态，尝试账号密码重新登录后重试一次...")
                fresh = login_and_get_cookie(user, pwd)
                if fresh:
                    c.jar = fresh
                    write_cache(cookie_str_of(fresh), "login-relogin")
                    continue
                warn("⚠️ 重新登录未成功，无法继续")
            err("🚫 未处于登录态：请检查 SOUTHPLUS_ACCOUNT 或更新 SOUTHPLUS_COOKIE"
                "（HTTP %s，%d 字节）" % (getattr(rt, "status_code", "?"), len(html)))
            dump_resp("任务页", rt)
            sys.exit(1)

        log("🔓 已处于登录态（HTTP %s，%d 字节）"
            % (getattr(rt, "status_code", "?"), len(html)))
        verify = extract_verifyhash(html)
        if not verify:
            err("🚫 未能从任务页提取 verifyhash，登录态可能异常")
            dump_resp("任务页", rt)
            sys.exit(1)
        log("🔑 verifyhash = %s" % verify)

        try:
            j1, r1 = do_job(c, verify)
        except Exception as e:
            err("❌ 申请任务请求失败: %s" % e)
            sys.exit(1)

        if j1 == "expired":
            if attempt == 0 and (user and pwd) and not FORCE_LOGIN:
                warn("🍪 登录态已失效，自动重登后续做一次...")
                fresh = login_and_get_cookie(user, pwd)
                if fresh:
                    c.jar = fresh
                    write_cache(cookie_str_of(fresh), "login-relogin")
                    continue
                warn("⚠️ 重登失败，无法续做")
            err("🚫 登录态失效：请检查 SOUTHPLUS_ACCOUNT 或更新 SOUTHPLUS_COOKIE")
            dump_resp("job 接口", r1)
            sys.exit(1)

        time.sleep(random.uniform(1.0, 2.5))
        try:
            j2, r2 = do_job2(c, verify, j1)
        except Exception as e:
            err("❌ 领奖请求失败: %s" % e)
            sys.exit(1)

        if j2 == "expired":
            if attempt == 0 and (user and pwd) and not FORCE_LOGIN:
                warn("🍪 领奖时登录态失效，自动重登后续做一次...")
                fresh = login_and_get_cookie(user, pwd)
                if fresh:
                    c.jar = fresh
                    write_cache(cookie_str_of(fresh), "login-relogin")
                    continue
                warn("⚠️ 重登失败，无法续做")
            err("🚫 登录态失效：请检查 SOUTHPLUS_ACCOUNT 或更新 SOUTHPLUS_COOKIE")
            dump_resp("job2 接口", r2)
            sys.exit(1)

        if DUMP_SIGN:
            if j1 in ("ok", "done"):
                dump_resp("job", r1)
            if j2 in ("ok", "done"):
                dump_resp("job2", r2)

        if j1 == "done":
            # 申请已是冷却/已完成态 → 今日任务确定已完成，领奖失败也属预期
            log("✅ 日常任务：今日已完成（无需重复操作）")
        elif j1 == "ok" and j2 in ("ok", "done"):
            log("🎉 日常任务：申请 + 领奖 成功")
        elif j1 == "ok" and j2 == "fail":
            warn("⚠️ 任务已申请，但领奖未成功（可能任务尚未完成）")
        elif j1 == "fail":
            err("❌ 日常任务申请失败，请查看上方响应")
        else:
            err("❌ 日常任务执行未完全成功，请查看上方响应")
        return


if __name__ == "__main__":
    main()
