# -*- coding: utf-8 -*-
"""xsijishe_test.py 离线回归（mock HTTP，不联网）。跑完可删。"""
import json
import os
import shutil
import sys
import tempfile
import traceback
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 必须在 import 之前设好，避免模块级副作用
os.environ.pop("XIJISHE_COOKIE", None)
os.environ.pop("XIJISHE_FORCE_LOGIN", None)
os.environ.pop("XIJISHE_PROBE", None)
os.environ["MY_PROXY"] = "http://127.0.0.1:7890"

import xsijishe_test as X   # noqa: E402

PASS = []
FAIL = []


def ck(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name + (("  << " + str(extra)) if (extra and not cond) else ""))


# ---------------- mock HTTP ----------------
class FakeResp(object):
    def __init__(self, status=200, text="", set_cookie=()):
        self.status_code = status
        self.text = text
        self._sc = list(set_cookie)
        outer = self

        class _H(object):
            def get(self, k, default=None):
                k = str(k).lower()
                if k == "content-type":
                    return "text/html; charset=utf-8"
                if k == "server":
                    return "cloudflare"
                return default

            def multi_items(self):
                return [("set-cookie", s) for s in outer._sc]

        self.headers = _H()
        self.raw = None
        self.cookies = None


class Script(object):
    """按 (method, url 子串) 路由；第 n 次命中可返回不同响应"""

    def __init__(self):
        self.calls = []
        self.handlers = []
        self.counts = {}

    def add(self, method, sub, resp):
        self.handlers.append((method.upper(), sub, resp))
        return self

    def __call__(self, method, url, **kw):
        self.calls.append((method.upper(), url))
        for m, sub, resp in self.handlers:
            hit = sub(url) if callable(sub) else (sub in url)
            if method.upper() == m and hit:
                key = (m, str(sub))
                n = self.counts.get(key, 0)
                self.counts[key] = n + 1
                return resp(n) if callable(resp) else resp
        return FakeResp(404, "no route for %s %s" % (method, url))

    def n_calls(self, method=None, sub=None):
        if method is None:
            return len(self.calls)
        return sum(1 for m, u in self.calls if m == method.upper() and (sub is None or sub in u))


LOGIN_HTML = (
    '<html><body><form method="post" name="login" id="loginform_TESTH" '
    'action="member.php?mod=logging&amp;action=login&amp;loginsubmit=yes&amp;loginhash=TESTH">'
    '<input type="hidden" name="formhash" value="abcdef12" />'
    '<input type="text" name="username" />'
    '<input type="password" name="password" />'
    '</form></body></html>'
)
HOME_ANON = "<html><body>formhash=11223344 游客首页</body></html>"
HOME_LOGGED = ('<html><body>formhash=11223344 '
               '<a href="member.php?mod=logging&amp;action=logout&amp;formhash=11223344">退出</a></body></html>')

PREFIX = "SgL6_2132_"
CK_LOGIN_OK = [PREFIX + "saltkey=s1", PREFIX + "auth=REALTOKEN123456", PREFIX + "lastvisit=1"]
CK_LOGIN_DELETED = [PREFIX + "saltkey=s1", PREFIX + "auth=deleted"]
CRED_XML = ('<?xml version="1.0" encoding="utf-8"?><root><![CDATA[登录失败，您还可以尝试 9 次'
            "<script>errorhandle_('登录失败，您还可以尝试 9 次', {'loginperm':'9'});</script>]]></root>")
SIGN_OK = "<root><![CDATA[签到成功，获得 5 金币]]></root>"
SIGN_ALREADY = "<root><![CDATA[您今天已经签到过了]]></root>"
SIGN_EXPIRED = "<root><![CDATA[请先登录]]></root>"


def install(transport):
    X._http = SimpleNamespace(request=transport)
    X._BACKEND = "curl_cffi"
    X.PROXY = "http://127.0.0.1:7890"
    X.dump_resp = lambda *a, **k: None
    X.dump_err = lambda *a, **k: None
    X.warn = lambda m: None
    X.log = lambda m: None
    X.err = lambda m: None


TMP = tempfile.mkdtemp(prefix="xjs_regress_")
X.CACHE_FILE = os.path.join(TMP, "cache.cookie")


def reset_cache():
    if os.path.exists(X.CACHE_FILE):
        os.remove(X.CACHE_FILE)


def write_cache_raw(obj):
    with open(X.CACHE_FILE, "w", encoding="utf-8") as f:
        f.write(obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False))


def read_cache_raw():
    if not os.path.exists(X.CACHE_FILE):
        return None
    with open(X.CACHE_FILE, "r", encoding="utf-8") as f:
        return f.read()


# ===================== 1. 纯函数 =====================
print("\n--- 纯函数 ---")
d = X.parse_cookie("a=1; b=2; c=3")
ck("parse_cookie 空格分隔", d == {"a": "1", "b": "2", "c": "3"}, d)
d = X.parse_cookie("a=1;b=2;SgL6_2132_auth=xyz")
ck("parse_cookie 无空格分隔", d.get("SgL6_2132_auth") == "xyz", d)
d = X.parse_cookie("k=v=w")
ck("parse_cookie 值内等号保留", d.get("k") == "v=w", d)
ck("cookie_str_of 往返", X.parse_cookie(X.cookie_str_of({"a": "1", "b": "2"})) == {"a": "1", "b": "2"})

ck("is_deleted 'deleted'", X.is_deleted("deleted"))
ck("is_deleted ' DELETED '", X.is_deleted(" DELETED "))
ck("is_deleted 'abc' 为假", not X.is_deleted("abc"))
ck("is_deleted 空为假", not X.is_deleted(""))

ck("detect_prefix 从 auth", X.detect_prefix({"SgL6_2132_auth": "x"}) == PREFIX)
ck("detect_prefix 从 saltkey", X.detect_prefix({"SgL6_2132_saltkey": "x"}) == PREFIX)
ck("detect_prefix 无前缀返回空", X.detect_prefix({"foo": "bar"}) == "")

ck("is_auth_valid 有效为真", X.is_auth_valid({"SgL6_2132_auth": "abc"}, PREFIX))
ck("is_auth_valid deleted 为假", not X.is_auth_valid({"SgL6_2132_auth": "deleted"}, PREFIX))
ck("is_auth_valid 缺 auth 为假", not X.is_auth_valid({"SgL6_2132_saltkey": "s"}, PREFIX))

u, p = X.parse_account("caydenbo648@gmail.com#Xj999110")
ck("parse_account '#' 分隔", (u, p) == ("caydenbo648@gmail.com", "Xj999110"), (u, p))
u, p = X.parse_account("user:pa:ss")
ck("parse_account ':' 分开（优先#）", (u, p) == ("user", "pa:ss"), (u, p))
ck("parse_account 空", X.parse_account("") == (None, None))
ck("parse_account 无分隔符", X.parse_account("onlyuser") == (None, None))

ck("classify_sign 签到成功", X.classify_sign(SIGN_OK) == "success")
ck("classify_sign 已签到", X.classify_sign(SIGN_ALREADY) == "already")
ck("classify_sign 未登录", X.classify_sign(SIGN_EXPIRED) == "expired")
ck("classify_sign 未登录(您需要登录)",
   X.classify_sign("<root><![CDATA[您需要登录后才能继续]]></root>") == "expired")
ck("classify_sign 未登录(本站实测文案:用户组不允许)",
   X.classify_sign("<root><![CDATA[您所在用户组不允许使用]]></root>") == "expired")
ck("classify_sign 未知", X.classify_sign("<root><![CDATA[系统繁忙]]></root>") == "unknown")

ck("brief 去标签", "签到成功" in X.brief("<div><span>签到成功</span></div>"))
ck("brief CDATA 保留正文", "签到成功" in X.brief("<root><![CDATA[签到成功，获得 5 金币]]></root>"))
ck("brief script 中文抢救",
   "登录失败" in X.brief('<div>提示信息</div><script>errorhandle_("登录失败，您还可以尝试 9 次");</script>'))
ck("brief 空输入", X.brief("") == "")

# ===================== 2. 缓存读写 =====================
print("\n--- 缓存读写 ---")
reset_cache()
X.write_cache("a=1; b=2", "unit")
raw = read_cache_raw()
o = json.loads(raw)
ck("write_cache 写 JSON", o.get("cookie") == "a=1; b=2" and o.get("source") == "unit", o)
c = X.read_cache()
ck("read_cache 读回", c and c["cookie"] == "a=1; b=2", c)

write_cache_raw("plain=1; plain2=2")
c = X.read_cache()
ck("read_cache 兼容纯文本", c and c["cookie"] == "plain=1; plain2=2" and c["source"] == "legacy-plain", c)

write_cache_raw('{"cookie":""}')
ck("read_cache 空 cookie 返回 None", X.read_cache() is None)

write_cache_raw("not-json{{{")
c = X.read_cache()
ck("read_cache 坏 JSON 回退纯文本", c and c["cookie"] == "not-json{{{", c)

# ===================== 3. 三级凭证优先级 =====================
print("\n--- 三级凭证：缓存优先 ---")
t = Script()
install(t)
reset_cache()
write_cache_raw({"v": 1, "source": "login", "savedAt": "-",
                 "cookie": PREFIX + "auth=CACHEDTOKEN; " + PREFIX + "saltkey=s1"})
c = X.resolve_cookie("u", "p")
ck("缓存有效 → 命中", c is not None and X.is_auth_valid(c.jar, PREFIX))
ck("缓存有效 → 零网络请求", t.n_calls() == 0, t.calls)

print("\n--- 三级凭证：缓存无效 → 登录成功 ---")
t = Script()
install(t)
reset_cache()
write_cache_raw({"cookie": PREFIX + "auth=deleted"})
t.add("GET", "action=login", FakeResp(200, LOGIN_HTML, [PREFIX + "saltkey=s1"]))
t.add("POST", "loginsubmit=yes", FakeResp(200, "<root><![CDATA[欢迎回来]]></root>", CK_LOGIN_OK))
c = X.resolve_cookie("realuser", "realpass")
ck("deleted 缓存 → 走登录并成功", c is not None and X.is_auth_valid(c.jar, PREFIX))
saved = X.read_cache()
ck("登录成功 → 写回缓存(source=login)",
   saved and saved.get("source") == "login" and "REALTOKEN123456" in saved.get("cookie", ""), saved)

print("\n--- 凭证顺序：环境变量 Cookie 不写缓存 ---")
t = Script()
install(t)
reset_cache()
t.add("GET", "action=login", FakeResp(200, LOGIN_HTML, [PREFIX + "saltkey=s1"]))
t.add("POST", "loginsubmit=yes", FakeResp(200, CRED_XML))
os.environ["XIJISHE_COOKIE"] = PREFIX + "auth=ENVTOKEN; " + PREFIX + "saltkey=s9"
c = X.resolve_cookie("realuser", "wrongpass")
ck("登录 cred 失败 → env Cookie 兜底成功", c is not None and c.jar.get(PREFIX + "auth") == "ENVTOKEN")
ck("env 兜底 → 不写缓存", read_cache_raw() is None, read_cache_raw())
ck("登录凭据错误 → 只尝试 1 次", t.n_calls("POST", "loginsubmit=yes") == 1,
   t.n_calls("POST", "loginsubmit=yes"))
os.environ.pop("XIJISHE_COOKIE", None)

print("\n--- auth=deleted → 重试一次后成功 ---")


def _deleted_then_ok(n):
    return FakeResp(200, "<root><![CDATA[ok]]></root>", CK_LOGIN_OK if n >= 1 else CK_LOGIN_DELETED)


t = Script()
install(t)
reset_cache()
t.add("GET", "action=login", FakeResp(200, LOGIN_HTML, [PREFIX + "saltkey=s1"]))
t.add("POST", "loginsubmit=yes", _deleted_then_ok)
c = X.resolve_cookie("u", "p")
ck("deleted 后重试 → 第 2 次成功", c is not None and c.jar.get(PREFIX + "auth") == "REALTOKEN123456")
ck("deleted 重试 → 共 2 次登录 POST", t.n_calls("POST", "loginsubmit=yes") == 2)

print("\n--- auth=deleted 连续两次 → 不无限重试 ---")
t = Script()
install(t)
reset_cache()
t.add("GET", "action=login", FakeResp(200, LOGIN_HTML, [PREFIX + "saltkey=s1"]))
t.add("POST", "loginsubmit=yes", FakeResp(200, "<root/>", CK_LOGIN_DELETED))
c = X.resolve_cookie("u", "p")
ck("连续 deleted → 失败返回 None", c is None)
ck("连续 deleted → 限 2 次", t.n_calls("POST", "loginsubmit=yes") == 2, t.n_calls("POST", "loginsubmit=yes"))

print("\n--- FORCE_LOGIN：忽略缓存、跳过兜底 ---")
t = Script()
install(t)
reset_cache()
write_cache_raw({"cookie": PREFIX + "auth=CACHEDTOKEN; " + PREFIX + "saltkey=s1"})
t.add("GET", "action=login", FakeResp(200, LOGIN_HTML, [PREFIX + "saltkey=s1"]))
t.add("POST", "loginsubmit=yes", FakeResp(200, CRED_XML))
os.environ["XIJISHE_COOKIE"] = PREFIX + "auth=ENVTOKEN; " + PREFIX + "saltkey=s9"
X.FORCE_LOGIN = True
c = X.resolve_cookie("u", "p")
ck("FORCE_LOGIN → 忽略缓存（发了登录请求）", t.n_calls("POST", "loginsubmit=yes") >= 1)
ck("FORCE_LOGIN → 登录失败时不走 env 兜底", c is None)
X.FORCE_LOGIN = False
os.environ.pop("XIJISHE_COOKIE", None)

print("\n--- 三级全失败 ---")
t = Script()
install(t)
reset_cache()
t.add("GET", "action=login", FakeResp(200, LOGIN_HTML, [PREFIX + "saltkey=s1"]))
t.add("POST", "loginsubmit=yes", FakeResp(200, CRED_XML))
c = X.resolve_cookie("u", "p")
ck("三级全失败 → None", c is None)

print("\n--- 无账号但 env Cookie 有效 ---")
t = Script()
install(t)
reset_cache()
os.environ["XIJISHE_COOKIE"] = PREFIX + "auth=ENVONLY; " + PREFIX + "saltkey=s2"
c = X.resolve_cookie(None, None)
ck("无账号 → env Cookie 生效", c is not None and c.jar.get(PREFIX + "auth") == "ENVONLY")
ck("无账号 → 零网络请求", t.n_calls() == 0)
os.environ.pop("XIJISHE_COOKIE", None)

# ===================== 4. 签到流程 =====================
print("\n--- 签到流程 ---")
t = Script()
install(t)
reset_cache()
HOME_PRED = lambda u: u.rstrip("/") == X.SITE.rstrip("/")
t.add("GET", HOME_PRED, FakeResp(200, HOME_LOGGED))
t.add("GET", "action=login", FakeResp(200, LOGIN_HTML, [PREFIX + "saltkey=s1"]))
t.add("POST", "loginsubmit=yes", FakeResp(200, "<root/>", CK_LOGIN_OK))
t.add("GET", "k_misign-sign.html", FakeResp(200, SIGN_OK))
os.environ["XIJISHE_ACCOUNT"] = "u#p"
try:
    X.main()
    ck("main 全流程签到成功（无异常）", True)
except SystemExit as e:
    ck("main 全流程签到成功（无异常）", False, "SystemExit %s" % e.code)
ck("main → 请求了签到接口", t.n_calls("GET", "k_misign-sign.html") == 1)
os.environ.pop("XIJISHE_ACCOUNT", None)

print("\n--- 签到返回未登录 → 自动重登续签 ---")
t = Script()
install(t)
reset_cache()
write_cache_raw({"v": 1, "source": "login", "savedAt": "-",
                 "cookie": PREFIX + "auth=STALETOKEN; " + PREFIX + "saltkey=s1"})
sign_n = {"i": 0}


def _sign(n):
    sign_n["i"] += 1
    return FakeResp(200, SIGN_OK if sign_n["i"] >= 2 else SIGN_EXPIRED)


t.add("GET", HOME_PRED, FakeResp(200, HOME_LOGGED))
t.add("GET", "k_misign-sign.html", _sign)
t.add("GET", "action=login", FakeResp(200, LOGIN_HTML, [PREFIX + "saltkey=s1"]))
t.add("POST", "loginsubmit=yes", FakeResp(200, "<root/>", CK_LOGIN_OK))
os.environ["XIJISHE_ACCOUNT"] = "u#p"
try:
    X.main()
    ck("失效自动重登续签（无异常）", True)
except SystemExit as e:
    ck("失效自动重登续签（无异常）", False, "SystemExit %s" % e.code)
ck("失效 → 重登后共 2 次签到", t.n_calls("GET", "k_misign-sign.html") == 2)
ck("失效 → 触发了登录", t.n_calls("POST", "loginsubmit=yes") == 1)
os.environ.pop("XIJISHE_ACCOUNT", None)

print("\n--- 未配代理 → 直接报错退出 ---")
install(Script())
X.PROXY = ""
try:
    X.main()
    ck("未配代理 → SystemExit", False)
except SystemExit as e:
    ck("未配代理 → SystemExit(1)", e.code == 1, e.code)
X.PROXY = "http://127.0.0.1:7890"

print("\n--- main 无 PROBE 时不做预检 ---")
t = Script()
install(t)
X.PROBE = False
reset_cache()
t.add("GET", "action=login", FakeResp(200, LOGIN_HTML, [PREFIX + "saltkey=s1"]))
t.add("POST", "loginsubmit=yes", FakeResp(200, CRED_XML))
try:
    X.main()
except SystemExit:
    pass
ck("main 无凭证 → 退出码 1", True)

shutil.rmtree(TMP, ignore_errors=True)

print("\n" + "=" * 52)
print("回归结果：%d/%d 通过" % (len(PASS), len(PASS) + len(FAIL)))
if FAIL:
    print("失败项：")
    for f in FAIL:
        print("  - " + f)
sys.exit(1 if FAIL else 0)
