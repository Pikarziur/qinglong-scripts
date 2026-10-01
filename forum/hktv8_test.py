#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# =========================================================
# name:  HKTV论坛 - 每日签到（测试版）
# cron: 10 0,12 * * *
# =========================================================
"""
HKTV论坛 www.hktv8.com 每日签到 - 青龙脚本 (Discuz! + dsu_paulsign 签到插件) · 测试版
============================================================================
【测试版】说明：本文件验证通过后并入 forum/hktv8.py（本仓库此前没有 hktv8 脚本，属新建）

凭证获取顺序：本地缓存（主） → 账号密码登录（辅） → HKTV8_COOKIE（最后兜底）

站点要点（全部为实测，非猜测）：
  · Discuz! + GBK 编码（响应 `charset=gbk`）—— 解码必须按 GBK，否则中文提示全乱码
  · cookie 前缀 `vsyQ_8bcf_`（本站实际值，脚本按后缀动态探测，不写死）
  · 登录支持三种口径：用户名 / UID / Email（`fastloginfield`），脚本按账号串里有没有 `@` 自动选
    实测该站支持邮箱登录（账号串含 `@` 即用 fastloginfield=email）
  · 登录页**无验证码**，纯账号密码可登（前端 pwmd5 只是不在链路里裸传明文，
    Discuz/UCenter 服务端对「32 位 MD5」幂等，故脚本直接传明文）
  · ⚠️ 站点有 **WAF 挑战式拦截**：随机触发 `403` + 一张 145 字节的
    `<script>window.location.href="/原URL"</script>` 小页面（并下发一个随机名 cookie）。
    浏览器靠 JS 跳回同 URL 即通过 —— 脚本内置「识别挑战页 → 带新 cookie 重发」，
    否则会把挑战页当成登录页解析、formhash 取空后提交，得到 Discuz 的 System Error。
  · ⚠️ 未登录时签到插件返回的是「**您需要先登录才能继续本操作**」
    （**不是**「请先登录」），脚本据此触发「失效自动重登续签」

任务流程（依据用户提供的 www.hktv8.com.har 逆向，全链路实测核对过）：
  1. 取 Cookie，按此顺序：
       ① 本地缓存（命中即用）
       ② 账号密码登录（缓存缺失/失效时；成功写回缓存供下次使用）
       ③ HKTV8_COOKIE 环境变量（只有①②都拿不到时才用，**不写回缓存**）
  2. GET 首页，正则提取签到插件所需的 formhash + 用 `discuz_uid` 判定登录态
  3. GET 签到插件浮窗 `plugin.php?id=dsu_paulsign:sign&<formhash>&...&inajax=1`，取表单 formhash
  4. POST `plugin.php?id=dsu_paulsign:sign&operation=qiandao&infloat=1&sign_as=1&inajax=1`
     表单体 `formhash=<hash>&qdxq=<心情>` 完成签到
  5. 判断 成功 / 已签 / 未登录；登录态失效时自动重登并续签一次

环境变量：
  HKTV8_ACCOUNT      建议必配。账号密码，格式「账号#密码」；账号填用户名或邮箱均可（含 @ 自动按邮箱登录）
  HKTV8_COOKIE       可选。手工 Cookie 字符串，**最后兜底**。
                     刻意不写回缓存 —— 否则下一轮会直接从缓存命中，等于把它抬到登录之前
  HKTV8_COOKIE_CACHE 可选。缓存文件路径，默认 /ql/data/hktv8.cookie（不可写则回退脚本同目录）
  HKTV8_FORCE_LOGIN  可选。=1 忽略缓存，强制走账号密码登录并覆盖缓存（调试用；
                     开启时也不会走 HKTV8_COOKIE 兜底，免得掩盖"登录到底成没成"）
  HKTV8_PROBE        可选。=1 仅预检：网络 + 登录页 + 签到插件匿名态探测，不登录、不签到
  HKTV8_DUMP_SIGN    可选。=1 签到成功/已签到时也把服务器响应原文打出来（默认关，排查用）
  HKTV8_QDXQ         可选。签到心情，取值 kx/ng/ym/wl/nu/ch/fd/yl/shuai，默认 kx（开心）
  HKTV8_TIMEOUT      可选。单次请求超时毫秒，默认 20000
  HKTV8_MIN_INTERVAL_MS 可选。请求最小间隔毫秒，默认 1000（站点有 WAF，放慢一点更稳）
  HKTV8_CHALLENGE_RETRY 可选。WAF 挑战页最大重试次数，默认 3
  MY_PROXY           可选。HTTP/HTTPS 代理（CONNECT 隧道），如 http://192.168.31.233:7890
                     ⚠️ 本机实测**直连可用**（首页 200 / 0.21s），故不强制；容器内不通再配
  HKTV8_PROXY        可选。同 MY_PROXY，仅本脚本的覆盖别名；两者都配时以它为准
  HKTV8_NO_PROXY     可选。=1 忽略上述代理强制直连
  HKTV8_SITE         可选。站点地址，默认 https://www.hktv8.com；仅供本地 mock 回归测试

依赖 (青龙依赖管理里加一行):  curl_cffi
    （用于模拟 Chrome 指纹，该站有 WAF）；缺依赖时自动退回 requests

日志规范：[LEVEL] [HKTV8] emoji message   （LEVEL: INFO / WARN / ERROR）
报错输出：所有失败路径都会打印「HTTP 状态 + 关键响应头 + 完整响应原文 + 异常堆栈」，
          原文默认不截断（超过 64KB 才截断并注明剩余量）；成功路径不打印任何响应内容。
============================================================================
"""

import json
import os
import re
import sys
import time
import traceback
from datetime import datetime

# ========== 统一日志 ==========
def _emit(level, msg):
    print("[%s] [HKTV8] %s" % (level, msg), flush=True)

def log(msg):  _emit("INFO", msg)
def warn(msg): _emit("WARN", msg)
def err(msg):  _emit("ERROR", msg)


# ========== 版本标识 ==========
# 每次实质性改动 +1。启动日志会带上它，用来确认「容器里跑的到底是哪一版」
# （踩过坑：本机改了、容器没同步，日志看着像"修复没生效"，实际是跑着旧文件）。
SCRIPT_VER = "2026-10-01a"

# ========== 常量 ==========
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36")

DUMP_LIMIT = 64 * 1024          # 响应原文打印上限
LOGIN_PAGE = "/member.php?mod=logging&action=login"
# ⚠️ 这里只放脚本路径、查询串分开拼：早先写成 "/plugin.php?id=dsu_paulsign:sign" 再拼 "?id=..."，
#    结果变成 `?id=dsu_paulsign:sign?id=dsu_paulsign:sign` → 站点回「插件不存在或已关闭」。
SIGN_PATH = "/plugin.php"
SIGN_ID = "id=dsu_paulsign:sign"
QDXQ_ALLOWED = ("kx", "ng", "ym", "wl", "nu", "ch", "fd", "yl", "shuai")


def _truthy(v):
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")


def _int_env(name, default):
    try:
        return int(str(os.environ.get(name) or "").strip() or default)
    except Exception:
        return default


# ========== 配置（全部支持环境变量覆盖）==========
SITE = (os.environ.get("HKTV8_SITE") or "https://www.hktv8.com").rstrip("/")
TIMEOUT_MS = _int_env("HKTV8_TIMEOUT", 20000)
TIMEOUT = max(3.0, TIMEOUT_MS / 1000.0)
MIN_INTERVAL_MS = max(0, _int_env("HKTV8_MIN_INTERVAL_MS", 1000))
CHALLENGE_RETRY = max(1, _int_env("HKTV8_CHALLENGE_RETRY", 3))
DUMP_SIGN = _truthy(os.environ.get("HKTV8_DUMP_SIGN"))
PROBE = _truthy(os.environ.get("HKTV8_PROBE"))
FORCE_LOGIN = _truthy(os.environ.get("HKTV8_FORCE_LOGIN"))
_qdxq = (os.environ.get("HKTV8_QDXQ") or "kx").strip().lower()
QDXQ = _qdxq if _qdxq in QDXQ_ALLOWED else "kx"

# 代理：HKTV8_PROXY（本脚本覆盖别名）> MY_PROXY（与 southplus.py / xsijishe.py 统一）
# ⚠️ 本站实测本机直连可用，故**不强制**代理；未配置 = 纯直连
NO_PROXY = _truthy(os.environ.get("HKTV8_NO_PROXY"))
_env_proxy = (os.environ.get("HKTV8_PROXY") or "").strip()
_my_proxy = (os.environ.get("MY_PROXY") or "").strip()
PROXY = "" if NO_PROXY else (_env_proxy or _my_proxy)
PROXY_SRC = "HKTV8_PROXY" if _env_proxy else ("MY_PROXY" if _my_proxy else "")
PROXY_DESC = ("已按 HKTV8_NO_PROXY=1 强制直连" if NO_PROXY
              else ((PROXY + "（来源 " + PROXY_SRC + "）") if PROXY else "未配置（直连）"))


# ========== HTTP 后端（优先 curl_cffi，缺失则退回 requests）==========
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
    p = (os.environ.get("HKTV8_COOKIE_CACHE") or "").strip()
    if p:
        return p
    primary = "/ql/data/hktv8.cookie"
    # ⚠️ 只在青龙持久目录「已存在且可写」时使用，**不要主动 makedirs** ——
    #    否则在 Windows 上 "/ql/data" 会被解析成 C:\ql\data，凭空在系统盘造出目录。
    try:
        d = os.path.dirname(primary)
        if os.path.isdir(d) and os.access(d, os.W_OK):
            return primary
    except Exception:
        pass
    # 青龙持久目录不可用（如本机调试）→ 回退脚本同目录
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), ".hktv8.cookie")


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


# ⚠️ Discuz 的 `dsetcookie()` 在「删除该 cookie」时下发的值是 `deleted`（不是空串）。
#    所以 `auth=deleted` 表示**已清除登录态**，绝不能当成有效凭证 —— 实测踩过：
#    首次登录后 auth 被清为 deleted，脚本却判为登录成功、写入缓存，导致紧接着的签到返回「未登录」。
def is_deleted(v):
    return str(v or "").strip().lower() == "deleted"


# Discuz 的 cookie 前缀由站点随机生成（本站 `vsyQ_8bcf_`，西集社 `SgL6_2132_`），
# **按后缀动态探测**：前缀 = 「以 _saltkey/_auth/_sid 结尾的键名」去掉该后缀。
# 这样两段式（vsyQ_8bcf_）与三段式（SgL6_2132_）前缀都能正确识别，且不必猜前缀的构成规则。
_PREFIX_SUFFIXES = ("saltkey", "auth", "sid", "lastvisit", "lastact")


def detect_prefix(cookies):
    """从 cookie 字典里探测 Discuz 前缀（返回 'vsyQ_8bcf_' 形式，探测不到返回 ''）"""
    for k in cookies or {}:
        for suf in _PREFIX_SUFFIXES:
            if k.endswith("_" + suf) and len(k) > len(suf) + 1:
                return k[: -len(suf)]
    return ""


def auth_key_of(cookies, prefix):
    return (prefix + "auth") if prefix else ""


def is_auth_valid(cookies, prefix):
    """离线判断 cookie 里有没有可用的登录态：auth 存在且不是 deleted"""
    if not prefix:
        prefix = detect_prefix(cookies)
    ak = auth_key_of(cookies, prefix)
    v = cookies.get(ak, "") if ak else ""
    return bool(v) and not is_deleted(v)


# ========== 响应摘要 / 报错详情 ==========
_ENC_TRY = ("gbk", "utf-8")


def text_of(resp):
    """取响应正文。

    ⚠️ 本站响应是 `charset=gbk`（Discuz 的 GBK 版），必须按 GBK 解码 ——
       直接信 `.text` 在部分库/缺 charset 时会按 utf-8 猜，中文提示全变乱码，
       分类正则随之全部失效（southplus.py 踩过同类坑：取值函数要兼容多种入参）。
       这里同时接受「响应对象 / str / bytes」。
    """
    if resp is None:
        return ""
    if isinstance(resp, (bytes, bytearray)):
        raw = bytes(resp)
    else:
        raw = getattr(resp, "content", None)
    if isinstance(raw, (bytes, bytearray)) and len(raw):
        encs = []
        try:
            ct = str(getattr(resp, "headers", {}).get("content-type") or "")
            m = re.search(r"charset=([\w-]+)", ct, re.I)
            if m:
                encs.append(m.group(1))
        except Exception:
            pass
        for e in list(encs) + list(_ENC_TRY):
            try:
                return bytes(raw).decode(e)
            except Exception:
                continue
        return bytes(raw).decode("gbk", "replace")
    t = getattr(resp, "text", resp)
    if isinstance(t, bytes):
        try:
            return t.decode("gbk", "replace")
        except Exception:
            return str(t)
    return t or ""


def brief(text, n=100):
    """把响应正文压成一行便于阅读的摘要（去 script/style / 去标签 / 压空白 / 截断）。

    ⚠️ 踩过两个坑，都在这里收口：
      1) 写成 `<[^>]+>` 会把 Discuz 的 `<![CDATA[...]]>` 整段正文当标签吞掉 → 摘要恒为空。
         所以标签正则必须以字母或 / 开头，并且先把 CDATA 的括号剥掉。
      2) Discuz 的 showmessage/showDialog 是把提示语写在 <script> 的字符串里，
         HTML 骨架只剩"提示信息 / 关闭 / 确定"这类模板词。直接删掉 script 就只剩噪声，
         所以删之前先把 script 里的中文字符串抢救出来当候选。
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
        for mm in re.finditer(r"['\"]([^'\"]{1,160})['\"]", blk):
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
        hdrs = getattr(resp, "headers", None)
        for h in ("content-type", "server", "location", "cf-ray", "cf-mitigated", "retry-after"):
            if hdrs is not None:
                v = hdrs.get(h)
                if v:
                    lines.append("   %s: %s" % (h, v))
        t = text_of(resp)
        raw = getattr(resp, "content", None)
        nbytes = len(raw) if isinstance(raw, (bytes, bytearray)) else len(t)
        if len(t) > DUMP_LIMIT:
            lines.append("   ── 响应原文（前 %d 字节，共 %d 字节，余 %d 字节已截断）──"
                         % (DUMP_LIMIT, nbytes, len(t) - DUMP_LIMIT))
            t = t[:DUMP_LIMIT]
        else:
            lines.append("   ── 响应原文（共 %d 字节）──" % nbytes)
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


# ========== WAF 挑战页 ==========
def is_challenge(resp):
    """识别站点 WAF 的挑战页。

    本站随机返回：HTTP 403 + 一张很短的页面
        <html><meta charset="utf-8" /><title></title><div></div></html>
        <script> window.location.href ="/原URL"; </script>
    并同时下发一个随机名 cookie（32 位 hex）。浏览器执行 JS 跳回原 URL 即通过；
    脚本则靠「拿到新 cookie 后原样重发」通过。
    """
    if resp is None:
        return False
    try:
        raw = getattr(resp, "content", b"") or b""
        if not isinstance(raw, (bytes, bytearray)):
            raw = str(raw).encode("utf-8", "replace")
        tiny = len(raw) <= 600
        has_js = b"window.location.href" in raw
        ct = str(getattr(resp, "headers", {}).get("content-type") or "").lower()
        code = getattr(resp, "status_code", 0)
        # 真实 Discuz 页面 >10KB 且 charset=gbk；挑战页很小、charset=utf8、带 JS 跳转
        return bool(tiny and (has_js or code == 403) and ("utf8" in ct or "utf-8" in ct or code == 403))
    except Exception:
        return False


# ========== HTTP 客户端 ==========
class Client(object):
    """自己维护 Cookie 字典，显式走 `Cookie:` 头 —— 避开各 HTTP 库在 cookie 域/路径上的差异"""

    def __init__(self, proxy=""):
        self.jar = {}
        self.proxy = proxy
        self.challenges = 0          # 累计遇到多少次 WAF 挑战页（预检会报出来）
        self._impersonate = "chrome" if _BACKEND == "curl_cffi" else None
        self._last_ts = 0.0
        self.base_headers = {
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
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

    def clear(self):
        self.jar = {}

    # ---- 节流 ----
    def _throttle(self):
        if MIN_INTERVAL_MS <= 0:
            return
        gap = MIN_INTERVAL_MS / 1000.0 - (time.time() - self._last_ts)
        if gap > 0:
            time.sleep(gap)

    # ---- 请求 ----
    def request(self, url, method="GET", headers=None, data=None, cookies=None,
                allow_redirects=True, timeout=None, no_retry=False):
        hdrs = dict(self.base_headers)
        if headers:
            hdrs.update(headers)
        hdrs["Cookie"] = (cookies if cookies is not None else self.jar_str())
        to = timeout or TIMEOUT
        last = None
        # no_retry：不重发（用于 Discuz 会「累计失败次数」的接口，如登录 POST）
        #     —— 但 **WAF 挑战页照旧重发**：那说明请求被 WAF 拦在应用之外，服务端根本没收到
        max_try = 1 if no_retry else 2
        for i in range(max_try):
            self._throttle()
            t0 = time.time()
            try:
                kw = dict(headers=hdrs, data=data, timeout=to, allow_redirects=allow_redirects)
                if self.proxy:
                    kw["proxies"] = {"http": self.proxy, "https": self.proxy}
                if self._impersonate:
                    kw["impersonate"] = self._impersonate
                r = _http.request(method, url, **kw)
                self._last_ts = time.time()
                r._x_elapsed = self._last_ts - t0    # 便于日志打耗时
                self.absorb(r)

                # WAF 挑战页：带着刚拿到的 cookie 原样重发（最多 CHALLENGE_RETRY 次）
                chal = 0
                while is_challenge(r) and chal < CHALLENGE_RETRY:
                    chal += 1
                    self.challenges += 1
                    warn("🛡️ 命中站点风控挑战页（HTTP %s，%d 字节），带新 Cookie 重发（第 %d/%d 次）..."
                         % (getattr(r, "status_code", "?"), len(getattr(r, "content", b"") or b""),
                            chal, CHALLENGE_RETRY))
                    self._throttle()
                    t0 = time.time()
                    r = _http.request(method, url, **kw)
                    self._last_ts = time.time()
                    r._x_elapsed = self._last_ts - t0
                    self.absorb(r)
                return r
            except Exception as e:
                last = e
                if i < max_try - 1:
                    warn("🔁 请求失败（第 %d/%d 次）: %s: %s" % (i + 1, max_try, type(e).__name__, e))
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
    from urllib.parse import quote
    return quote(str(s), safe="")


# ========== 登录态判定 ==========
def parse_uid(html):
    """Discuz 页面 JS 变量里带登录态：discuz_uid = '123456'（未登录是 '0'）"""
    m = re.search(r"discuz_uid\s*=\s*'(\d+)'", str(html or ""))
    return int(m.group(1)) if m else 0


def is_logged_in_html(html):
    """已登录页面里一定有「退出」链接（member.php?mod=logging&action=logout）"""
    return bool(re.search(r"action=logout", str(html or ""), re.I))


# ========== 账号密码登录（Discuz）==========
def pick_login_field(user):
    """该站支持 用户名 / UID / Email 三种口径，按账号串自动选（含 @ 判为邮箱）"""
    u = str(user or "").strip()
    if "@" in u:
        return "email"
    if u.isdigit():
        return "uid"
    return "username"


# 单次登录尝试。返回 (state, cookies)，state 含义：
#   'ok'      拿到有效 auth（非 deleted）
#   'deleted' 服务端把 auth 置为 deleted —— 这是「清除登录态」而非「凭据错误」，**可安全重试一次**
#   'cred'    响应明确是「登录失败，您还可以尝试 N 次」→ 凭据问题，绝不重试（Discuz 会累计失败次数）
#   'captcha' 站点要求验证码/安全提问 → 本脚本无法自动过
#   'nomark'  没拿到凭证、也没有明确失败原因（风控 / 响应不是登录结果页）
def login_attempt(user, pwd, attempt_no):
    c = Client(PROXY)
    field = pick_login_field(user)
    try:
        # 1) 匿名访问登录页，取 formhash + loginhash（首次请求可能先吃一个 WAF 挑战页，
        #    Client.request 内部会自动重发）
        log("🌐 访问登录页取 formhash...")
        page_url = SITE + LOGIN_PAGE
        page = c.request(page_url, headers={"Referer": SITE + "/"})
        page_text = text_of(page)
        if getattr(page, "status_code", 0) >= 400:
            warn("⚠️ 登录页返回 HTTP %s" % page.status_code)
            dump_resp("登录页", page)

        m = (re.search(r'name=["\']formhash["\'][^>]*value=["\']([0-9a-f]{8})["\']', page_text, re.I)
             or re.search(r"formhash=([0-9a-f]{8})", page_text))
        if not m:
            warn("⚠️ 登录页未取到 formhash，无法构造登录请求 → 登录链路不可用")
            dump_resp("登录页", page)
            return "nomark", {}
        formhash = m.group(1)

        lh = (re.search(r"loginhash=([A-Za-z0-9]+)", page_text)
              or re.search(r"loginform_([A-Za-z0-9]+)", page_text))
        loginhash = lh.group(1) if lh else ""

        # 验证码探测：登录页出现 seccodeverify/secqaa 说明本次要求验证码（无法自动过）
        need_captcha = bool(re.search(r"seccodeverify|secqaa|seccode_", page_text, re.I))
        prefix = detect_prefix(c.jar)
        log("🔑 formhash = %s%s%s" % (formhash,
                                      ("｜loginhash = " + loginhash) if loginhash else "",
                                      ("｜cookie 前缀 = " + prefix) if prefix else ""))
        log("🆔 登录口径: %s（账号串%s含 @）" % (field, "" if "@" in str(user) else "不"))

        # 2) 提交登录。密码传**明文**即可：Discuz/UCenter 服务端对「32 位 MD5」是幂等的
        #    （strlen($password)==32 直接采用），前端 pwmd5 只是为了不在链路里裸传明文。
        #    URL 与表单体**严格照 HAR**：infloat=yes & lssubmit=yes & inajax=1（首页快捷登录通道）。
        #    ⚠️ 刻意**不**带 `loginhash`：HAR 里那次真实登录成功用的就是这条不带 loginhash 的 URL
        #       （首页 #lsform 表单本就没有它）。少传一个参数 = 少一个可能对不上的校验点；
        #       loginhash 只用于日志/预检展示。
        login_url = (SITE + "/member.php?mod=logging&action=login&loginsubmit=yes"
                     "&infloat=yes&lssubmit=yes&inajax=1")
        body = "&".join([
            "fastloginfield=" + _urlquote(field),
            "username=" + _urlquote(user),
            "cookietime=2592000",
            "password=" + _urlquote(pwd),
            "formhash=" + formhash,
            "quickforward=yes",
            "handlekey=ls",
            "referer=" + _urlquote(SITE + "/"),
        ])
        log("📤 提交登录%s..." % ("（第 %d 次尝试）" % attempt_no if attempt_no > 1 else ""))
        r = c.request(login_url, method="POST",
                      headers={"Content-Type": "application/x-www-form-urlencoded",
                               "Referer": page_url,
                               "Origin": SITE,
                               "X-Requested-With": "XMLHttpRequest"},
                      data=body, no_retry=True)   # 登录失败会被 Discuz 计数，重试会加速触发账号/IP 锁定
        r_text = text_of(r)

        # 3) 判定：必须拿到**有效** auth（deleted 不算）
        prefix = detect_prefix(c.jar) or prefix
        ak = auth_key_of(c.jar, prefix)
        sk = (prefix + "saltkey") if prefix else ""
        auth = c.jar.get(ak, "") if ak else ""
        salt = c.jar.get(sk, "") if sk else ""
        if auth and not is_deleted(auth):
            log("🔑 登录成功（%s，共 %d 个 Cookie 字段%s）"
                % (ak, len(c.jar), "" if salt else "；⚠️ 未拿到 saltkey"))
            log("🍪 %s=%s***" % (ak, auth[:12]))
            return "ok", c.jar

        # auth=deleted：Discuz 的清除标记。登录流程本身没报错，只是会话被清 —— 可安全重试一次。
        if is_deleted(auth):
            raw_sc = _set_cookie_list(r)
            warn("⚠️ 服务端把 %s 置为 deleted（Discuz 的「清除登录态」标记），本次登录态无效"
                 "（响应共 %d 条 Set-Cookie）" % (ak or "auth", len(raw_sc)))
            return "deleted", c.jar

        # Discuz 会把失败原因塞在 XML CDATA 里，例如：
        #   <![CDATA[登录失败，您还可以尝试 4 次 ... {'loginperm':'4'}]]>
        perm = (re.search(r"还可以尝试\s*(\d+)\s*次", r_text)
                or re.search(r"['\"]loginperm['\"]\s*:\s*['\"](\d+)['\"]", r_text))
        warn("⚠️ 登录失败（HTTP %s）：未拿到登录凭证" % getattr(r, "status_code", "?"))
        if re.search(r"登录失败|密码错误|用户名不存在|用户不存在|不存在", r_text):
            warn("🚫 账号或密码不正确%s：请核对 HKTV8_ACCOUNT 的「账号#密码」，"
                 "不要连续重试以免被临时禁止登录（该站允许 5 次）"
                 % ("（短时间内还可尝试 %s 次）" % perm.group(1) if perm else ""))
            if len(str(user)) <= 2:
                warn("🚫 另外注意：当前账号只有 %d 个字符（%s），像是占位符没被替换成真实账号"
                     % (len(str(user)), user))
            dump_resp("登录POST", r)
            return "cred", c.jar
        if re.search(r"验证码|seccode|secqaa|安全提问|question", r_text) or need_captcha:
            warn("🚫 站点要求验证码/安全提问：本次登录已被风控，建议先用浏览器登录一次或稍后再试")
            dump_resp("登录POST", r)
            return "captcha", c.jar
        warn("⚠️ 未拿到 auth，常见原因：账号密码错误 / 需安全提问 / 触发登录频率限制 / 响应不是登录结果页")
        dump_resp("登录POST", r)
        return "nomark", c.jar
    except Exception as e:
        dump_err("登录流程", e)
        return "nomark", {}


# 外层封装：先试一次；只有「auth 被清成 deleted」这种**非凭据问题**才会重试一次
# （wnflb2023 实测该站首次登录常返回 auth=deleted，第二次即正常；凭据错误则绝不重试）
MAX_LOGIN_ATTEMPT = 2


def login_and_get_cookie(user, pwd):
    if not user or not pwd:
        warn("⚠️ 未配置账号密码（HKTV8_ACCOUNT=账号#密码），无法自动登录")
        return None
    for i in range(1, MAX_LOGIN_ATTEMPT + 1):
        state, jar = login_attempt(user, pwd, i)
        if state == "ok":
            return jar
        if state == "deleted" and i < MAX_LOGIN_ATTEMPT:
            warn("🔁 登录态被服务端清空（非账号密码问题），重试登录（第 %d/%d 次）..."
                 % (i + 1, MAX_LOGIN_ATTEMPT))
            continue
        if state == "deleted":
            warn("🚫 连续两次都被清成 deleted：站点可能在风控 / 要求安全提问，建议先用浏览器登录一次")
        return None
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


# ========== 凭证解析：缓存（主） → 账号密码登录（辅） → HKTV8_COOKIE（最后兜底）==========
# ⚠️ 这个顺序是**有意**排的：环境变量 Cookie 放在账号密码**之后**。
#    它只在「缓存和登录都拿不到」时救场（典型场景：站点临时上验证码/风控，登录走不通，
#    但手里还有一份自浏览器导出的有效 Cookie）。
#    ⇒ 环境变量 Cookie **不写回缓存**：否则下一轮会直接从缓存命中，等于把它抬到登录之前，
#      与"最后兜底"的定位相矛盾。
def resolve_cookie(user, pwd):
    c = Client(PROXY)

    # 1) 本地缓存为主
    if not FORCE_LOGIN:
        cached = read_cache()
        if cached:
            jar = parse_cookie(cached["cookie"])
            prefix = detect_prefix(jar)
            if is_auth_valid(jar, prefix):
                c.jar = jar
                log("📦 使用本地缓存 Cookie（来源: %s | 写入: %s）"
                    % (cached.get("source", "-"), cached.get("savedAt", "-")))
                return c
            ak = auth_key_of(jar, prefix) or "auth"
            if is_deleted(jar.get(ak, "")):
                warn("⚠️ 缓存里的 %s 是 deleted（Discuz 的「已登出」标记），视为无效，改走账号密码登录" % ak)
            else:
                warn("⚠️ 缓存存在但缺少 %s，视为无效，改走账号密码登录（缓存内 %d 个字段: %s）"
                     % (ak, len(jar), ", ".join(list(jar.keys())[:12]) or "(空)"))
        else:
            log("📦 缓存不存在（首次运行会走账号密码登录）")
    else:
        warn("🔁 HKTV8_FORCE_LOGIN 已开启：忽略缓存，强制账号密码登录")

    # 2) 账号密码登录：无缓存 / 缓存无效时自动登录，成功后把新 Cookie 写回缓存
    if user and pwd:
        log("🔐 尝试账号密码登录获取 Cookie...")
        fresh = login_and_get_cookie(user, pwd)
        if fresh:
            c.jar = fresh
            write_cache(cookie_str_of(fresh), "login")
            return c
        warn("⚠️ 账号密码登录未拿到有效 Cookie，继续尝试 HKTV8_COOKIE 兜底")
    else:
        warn("⚠️ HKTV8_ACCOUNT 未配置或格式不对（应为「账号#密码」），跳过账号密码登录")

    # 3) HKTV8_COOKIE 环境变量：最后兜底
    #    调试开关开启时跳过 —— 那时要看的就是"登录本身成功没有"，兜底会把失败掩盖成成功
    if FORCE_LOGIN:
        warn("🔁 HKTV8_FORCE_LOGIN 已开启：跳过 HKTV8_COOKIE 兜底（否则会掩盖登录是否真的成功）")
        return None
    env_ck = (os.environ.get("HKTV8_COOKIE") or "").strip()
    if not env_ck:
        return None
    jar = parse_cookie(env_ck)
    prefix = detect_prefix(jar)
    if is_auth_valid(jar, prefix):
        c.jar = jar
        log("🌱 账号密码登录也拿不到 Cookie，改用 HKTV8_COOKIE 环境变量兜底（本次不写缓存）")
        return c
    ak = auth_key_of(jar, prefix) or "auth"
    if is_deleted(jar.get(ak, "")):
        warn("⚠️ HKTV8_COOKIE 里的 %s 是 deleted（Discuz 的「已登出」标记），不可用" % ak)
    else:
        warn("⚠️ HKTV8_COOKIE 里没有 %s，不可用（共 %d 个字段: %s）"
             % (ak, len(jar), ", ".join(list(jar.keys())[:12]) or "(空)"))
    return None


# ========== 签到（dsu_paulsign 插件）==========
def get_home(c):
    """GET 首页：取 formhash + 用 discuz_uid 判登录态（HAR 实证：未登录 '0' / 已登录 '123456'）"""
    r = c.request(SITE + "/", headers={"Referer": SITE + "/"})
    t = text_of(r)
    if getattr(r, "status_code", 0) != 200:
        warn("⚠️ 首页返回 HTTP %s" % getattr(r, "status_code", "?"))
    m = (re.search(r'name="formhash"\s+value="([0-9a-f]{8})"', t)
         or re.search(r"formhash=([0-9a-f]{8})", t))
    return (m.group(1) if m else None), parse_uid(t), r, t


def fetch_sign_formhash(c, formhash):
    """GET 签到插件浮窗，取表单里的 formhash。

    HAR 实证：浏览器点「每日签到」走的是
      plugin.php?id=dsu_paulsign:sign&<formhash>&infloat=yes&handlekey=dsu_paulsign&inajax=1&ajaxtarget=fwin_content_dsu_paulsign
    （那个裸的 `&<formhash>` 段是插件 JS 自己拼的；formhash 是会话级全局值，
      登录后首页与签到页取值一致，故这里直接沿用首页取到的值）
    """
    url = (SITE + SIGN_PATH + "?" + SIGN_ID + "&" + formhash
           + "&infloat=yes&handlekey=dsu_paulsign&inajax=1&ajaxtarget=fwin_content_dsu_paulsign")
    r = c.request(url, headers={"Referer": SITE + "/", "X-Requested-With": "XMLHttpRequest"})
    t = text_of(r)
    if getattr(r, "status_code", 0) != 200:
        warn("⚠️ 签到浮窗返回 HTTP %s" % getattr(r, "status_code", "?"))
    m = re.search(r'<input[^>]*name="formhash"[^>]*value="([0-9a-f]{8})"', t, re.I) \
        or re.search(r'name="formhash"\s+value="([0-9a-f]{8})"', t, re.I)
    return (m.group(1) if m else None), r, t


def classify_sign(text):
    t = re.sub(r"\s+", " ", str(text or ""))
    if re.search(r"签到成功|恭喜你签到|签到获得", t):
        return "success"
    if re.search(r"已经签到|已签到|今日已签|今天已经|明天再来|下次再来|签到过了|已经签过", t):
        return "already"
    # ⚠️ 本站未登录时 dsu_paulsign 返回的是「您需要先登录才能继续本操作」（实测），**不是**「请先登录」。
    #    漏了这一条的话，Cookie 失效会落到 unknown → 直接报错退出，不会走「失效自动重登续签」。
    if re.search(r"请先登录|未登录|需要登录|您需要先登录|您还没有登录|游客|无权|权限不足|不允许", t):
        return "expired"
    return "unknown"


def do_sign(c, formhash):
    """dsu_paulsign 签到接口（URL 与表单体严格照 HAR）"""
    url = SITE + SIGN_PATH + "?" + SIGN_ID + "&operation=qiandao&infloat=1&sign_as=1&inajax=1"
    body = "formhash=%s&qdxq=%s" % (formhash, QDXQ)
    r = c.request(url, method="POST",
                  headers={"Content-Type": "application/x-www-form-urlencoded",
                           "Referer": SITE + "/",
                           "X-Requested-With": "XMLHttpRequest"},
                  data=body)
    t = text_of(r)
    return classify_sign(t), r, t


# ========== 预检 ==========
def run_probe(user, pwd):
    log("🔎 预检模式：只探网络 / 登录页 / 签到插件匿名态，不登录、不签到")
    log("📁 Cookie 缓存路径: %s" % CACHE_FILE)
    log("⏱️ 单次超时 %dms｜请求间隔 %dms｜HTTP 后端: %s" % (TIMEOUT_MS, MIN_INTERVAL_MS, _BACKEND or "(无)"))
    log("🔌 代理: %s" % PROXY_DESC)

    c = Client(PROXY)

    log("🌐 请求首页...")
    try:
        fh, uid, r, t = get_home(c)
        log("✅ 首页 HTTP %s｜%d 字节｜%.2fs｜discuz_uid = %s（%s）｜action=logout: %s"
            % (getattr(r, "status_code", "?"), len(t), elapsed_of(r) or 0.0, uid,
               "已登录" if uid else "未登录", "有" if is_logged_in_html(t) else "无"))
        log("🔑 首页 formhash: %s" % (fh or "未取到"))
    except Exception as e:
        dump_err("首页请求", e)
        warn("⏭️ 首页不可达：先修网络（容器内若直连不通，配 MY_PROXY 指向 mihomo）再回来")
        log("🏁 预检结束（未做任何登录/签到动作）")
        return

    log("🌐 请求登录页...")
    try:
        rp = c.request(SITE + LOGIN_PAGE, headers={"Referer": SITE + "/"})
        tp = text_of(rp)
        fh2 = (re.search(r'name=["\']formhash["\'][^>]*value=["\']([0-9a-f]{8})["\']', tp, re.I)
               or re.search(r"formhash=([0-9a-f]{8})", tp))
        lh = re.search(r"loginhash=([A-Za-z0-9]+)", tp)
        cap = re.search(r"seccodeverify|secqaa|seccode_", tp, re.I)
        fields = sorted(set(re.findall(r'<option value="(username|uid|email)"', tp)))
        log("✅ 登录页 HTTP %s｜%d 字节｜%.2fs" % (getattr(rp, "status_code", "?"), len(tp),
                                                 elapsed_of(rp) or 0.0))
        log("🔑 formhash: %s" % (fh2.group(1) if fh2 else "未取到"))
        log("🔑 loginhash: %s" % (lh.group(1) if lh else "未取到（可能非必需）"))
        log("🆔 支持登录口径: %s" % (", ".join(fields) if fields else "未解析到（默认 username）"))
        log("🛡️ 登录验证码字段: %s" % ("存在 → 账号密码登录会被拦" if cap else "无（纯账号密码可登）"))
        prefix = detect_prefix(c.jar)
        log("🍪 登录页下发 Cookie: %d 个字段%s%s"
            % (len(c.jar), ("｜Discuz 前缀 " + prefix) if prefix else "",
               "（含 saltkey，登录链路可用）" if (prefix and (prefix + "saltkey") in c.jar)
               else "（未拿到 saltkey，登录可能有问题）"))
        if not fh2:
            warn("⚠️ 登录页未取到 formhash → 账号密码登录大概率不可用")
            dump_resp("登录页", rp)
    except Exception as e:
        dump_err("登录页请求", e)

    log("🌐 请求签到插件（匿名态，预期「您需要先登录才能继续本操作」）...")
    try:
        ck = Client(PROXY)
        if fh:
            rf, rs, rt = fetch_sign_formhash(ck, fh)
            kind = classify_sign(rt)
            log("✅ 签到插件 HTTP %s｜%d 字节｜摘要: %s" % (getattr(rs, "status_code", "?"),
                                                        len(rt), brief(rt, 80) or "(空)"))
            log("🧪 匿名态判定: %s（%s）" % (kind, "符合预期" if kind == "expired" else "⚠️ 与预期不符"))
            log("🔑 签到表单 formhash: %s" % (rf or "未取到（未登录时本就取不到，正常）"))
        else:
            warn("⏭️ 首页没取到 formhash，跳过签到插件探测")
    except Exception as e:
        dump_err("签到插件探测", e)

    cached = read_cache()
    if cached:
        jar = parse_cookie(cached["cookie"])
        prefix = detect_prefix(jar)
        log("📦 缓存存在：来源 %s｜写入 %s｜%s %s"
            % (cached.get("source", "-"), cached.get("savedAt", "-"),
               auth_key_of(jar, prefix) or "auth",
               "✓" if is_auth_valid(jar, prefix) else
               ("✗(deleted)" if is_deleted(jar.get(auth_key_of(jar, prefix), "")) else "✗")))
    else:
        log("📦 缓存不存在（首次运行会走账号密码登录）")
    log("👤 HKTV8_ACCOUNT %s%s" % ("已配置" if (user and pwd) else "未配置",
                                   ("（登录口径 %s）" % pick_login_field(user)) if (user and pwd) else ""))
    env_ck = (os.environ.get("HKTV8_COOKIE") or "").strip()
    if not env_ck:
        log("🍪 HKTV8_COOKIE 未配置（可选，仅在缓存与账号密码登录都失败时兜底）")
    else:
        jar = parse_cookie(env_ck)
        prefix = detect_prefix(jar)
        log("🍪 HKTV8_COOKIE %s" % ("已配置（auth ✓，作为最后兜底）" if is_auth_valid(jar, prefix)
                                   else "已配置但不含有效 auth，兜底不可用"))
    log("🎭 签到心情 qdxq = %s" % QDXQ)
    log("🛡️ 本次共命中 WAF 挑战页 %d 次（已被脚本自动重发化解）" % c.challenges)
    log("🏁 预检结束（未做任何登录/签到动作）")


# ========== 主流程 ==========
def main():
    log("🚀 HKTV论坛 每日签到开始（脚本 v%s · 测试版）" % SCRIPT_VER)

    if _http is None:
        err("📦 缺少 HTTP 依赖：请执行 pip install curl_cffi（推荐）或 pip install requests")
        sys.exit(1)
    if _BACKEND == "requests":
        warn("⚠️ 未装 curl_cffi，已退回 requests：该站有 WAF，指纹不匹配时更易被挑战页拦住，"
             "建议在青龙依赖管理里加一行 curl_cffi")

    user, pwd = parse_account(os.environ.get("HKTV8_ACCOUNT"))
    if PROBE:
        run_probe(user, pwd)
        return

    # 1. 三级凭证解析
    c = resolve_cookie(user, pwd)
    if c is None:
        err("❌ 无法获取 Cookie（按「缓存 → 账号密码登录 → HKTV8_COOKIE」三级都失败；"
            "请检查 HKTV8_ACCOUNT=账号#密码，或配一个 HKTV8_COOKIE 兜底；缓存路径: %s）" % CACHE_FILE)
        sys.exit(1)

    # 2. 取 formhash → 签到；登录态失效时自动重登并续签一次
    tried_relogin = False
    for attempt in range(2):
        log("🌐 访问首页...")
        try:
            formhash, uid, resp, home_text = get_home(c)
        except Exception as e:
            dump_err("首页请求", e)
            err("❌ 请求首页失败: %s" % e)
            sys.exit(1)

        if not formhash:
            warn("⚠️ 首页未取到 formhash（HTTP %s，%d 字节）"
                 % (getattr(resp, "status_code", "?"), len(home_text)))
            if not tried_relogin and (user and pwd) and not FORCE_LOGIN:
                warn("🔁 尝试账号密码重新登录并续签一次...")
                fresh = login_and_get_cookie(user, pwd)
                if fresh:
                    c.jar = fresh
                    write_cache(cookie_str_of(fresh), "login-relogin")
                    tried_relogin = True
                    continue
            err("❌ 未找到 formhash，Cookie 可能已过期")
            dump_resp("首页", resp)
            sys.exit(1)

        log("🔑 formhash: %s｜discuz_uid: %s（%s）"
            % (formhash, uid, "已登录" if uid else "未登录"))

        log("📡 请求签到浮窗取签到表单...")
        try:
            sign_fh, fwin, ftext = fetch_sign_formhash(c, formhash)
        except Exception as e:
            dump_err("签到浮窗请求", e)
            err("❌ 签到浮窗请求失败: %s" % e)
            sys.exit(1)

        if not sign_fh:
            kind = classify_sign(ftext)
            brief_txt = brief(ftext, 120)
            if kind == "expired" or uid == 0:
                if not tried_relogin and (user and pwd) and not FORCE_LOGIN:
                    warn("🍪 登录态已失效（%s），自动重登后续签一次..." % (brief_txt or "未登录"))
                    fresh = login_and_get_cookie(user, pwd)
                    if fresh:
                        c.jar = fresh
                        write_cache(cookie_str_of(fresh), "login-relogin")
                        tried_relogin = True
                        continue
                    warn("⚠️ 重登失败，无法续签")
                err("🚫 登录态失效：请检查 HKTV8_ACCOUNT 或更新 HKTV8_COOKIE")
                dump_resp("签到浮窗", fwin)
                sys.exit(1)
            warn("⚠️ 签到浮窗里没解析到 formhash（HTTP %s，%d 字节）｜摘要: %s"
                 % (getattr(fwin, "status_code", "?"), len(ftext), brief_txt))
            if kind == "already":
                log("✅ 今日已签到｜%s" % brief_txt)
                if DUMP_SIGN:
                    dump_resp("签到浮窗", fwin)
                return
            dump_resp("签到浮窗", fwin)
            sys.exit(1)

        log("🔑 签到表单 formhash: %s" % sign_fh)
        log("📡 提交签到（心情 %s）..." % QDXQ)
        try:
            kind, sresp, stext = do_sign(c, sign_fh)
        except Exception as e:
            dump_err("签到请求", e)
            err("❌ 签到请求失败: %s" % e)
            sys.exit(1)

        log("📡 HTTP %s" % getattr(sresp, "status_code", "?"))

        if kind == "expired":
            if not tried_relogin and (user and pwd) and not FORCE_LOGIN:
                warn("🍪 登录态已失效（%s），自动重登后续签一次..." % brief(stext, 60))
                fresh = login_and_get_cookie(user, pwd)
                if fresh:
                    c.jar = fresh
                    write_cache(cookie_str_of(fresh), "login-relogin")
                    tried_relogin = True
                    continue
                warn("⚠️ 重登失败，无法续签")
            err("🚫 登录态失效：请检查 HKTV8_ACCOUNT 或更新 HKTV8_COOKIE")
            dump_resp("签到", sresp)
            sys.exit(1)

        if kind == "success":
            log("🎉 签到成功｜%s" % (brief(stext, 90) or "服务器未返回提示"))
        elif kind == "already":
            log("✅ 今日已签到｜%s" % (brief(stext, 90) or "服务器未返回提示"))
        else:
            err("❓ 结果未知: %s" % brief(stext, 200))
            dump_resp("签到", sresp)
            sys.exit(1)
        if DUMP_SIGN:
            dump_resp("签到", sresp)
        return


if __name__ == "__main__":
    main()
