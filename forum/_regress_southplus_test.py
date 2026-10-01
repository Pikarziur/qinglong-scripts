#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""南+论坛测试版脚本的离线 mock 回归（不联网）。并入正式版后本文件即删除。"""
import os
import sys
import tempfile
import time as _time

TMP = tempfile.mkdtemp(prefix="sp_reg_")
CACHE = os.path.join(TMP, "southplus.cookie")

os.environ["SOUTHPLUS_SITE"] = "https://mock.local"
os.environ["SOUTHPLUS_MIN_INTERVAL_MS"] = "0"
os.environ["SOUTHPLUS_COOKIE_CACHE"] = CACHE
os.environ["MY_PROXY"] = "http://127.0.0.1:9"

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import southplus_test as X   # noqa: E402

# 让测试跑得快：sleep 全变 no-op
class _T(object):
    time = staticmethod(_time.time)
    sleep = staticmethod(lambda *a, **k: None)
X.time = _T

PASS = []
FAIL = []


def ck(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✓ " if cond else "  ✗ ") + name + ("" if cond else "   → %s" % (extra,)))


# ---------------- mock HTTP ----------------
class FakeHeaders(object):
    def __init__(self, items):
        self._items = list(items)

    def get(self, k, d=None):
        for kk, vv in self._items:
            if str(kk).lower() == str(k).lower():
                return vv
        return d

    def multi_items(self):
        return list(self._items)

    def items(self):
        return list(self._items)


class FakeResp(object):
    def __init__(self, status, text, cookies=None):
        self.status_code = status
        self.text = text
        self.content = text.encode("utf-8") if isinstance(text, str) else text
        items = [("content-type", "text/html; charset=utf-8")]
        for sc in (cookies or []):
            items.append(("set-cookie", sc))
        self.headers = FakeHeaders(items)
        self.cookies = {}


class Script(object):
    def __init__(self):
        self.handlers = []
        self.calls = []
        self.counts = {}

    def add(self, method, sub, resp, exact=False):
        self.handlers.append((method.upper(), sub, resp, exact))

    def reset_counts(self):
        self.counts = {}

    def request(self, method, url, **kw):
        self.calls.append((method.upper(), url))
        for m, sub, resp, exact in self.handlers:
            hit = (url == sub) if exact else (sub in url)
            if method.upper() == m and hit:
                key = (m, sub)
                n = self.counts.get(key, 0)
                self.counts[key] = n + 1
                return resp(n) if callable(resp) else resp
        return FakeResp(404, "no route for %s %s" % (method, url))


def install(script):
    X._http = script
    X._BACKEND = "curl_cffi"


def reset_cache():
    try:
        os.remove(CACHE)
    except OSError:
        pass


# ---------------- 常量 ----------------
PRE = "eb9e6_"
LOGIN_PAGE = ('<?xml version="1.0"?><wml><card id="index"><p>'
              '<select name="lgt"><option value="0">用户名</option></select>'
              '<input name="pwuser" type="text" /></p></card></wml>')
CK_OK = [PRE + "winduser=QUJDRA%3D%3D; path=/", PRE + "winduserid=12345; path=/"]
TASKS_OUT = ('<html><head><title>社区论坛任务</title></head><body>'
             '<a href="plugin.php?H_name=tasks&amp;action-quit-verify-abc.html">退出</a>'
             "<script>var verifyhash = 'abc12345';</script></body></html>")
TASKS_IN = '<html><head><title>社区论坛任务</title></head><body><a href="login.php">登录</a></body></html>'
JOB_OK = '<?xml version="1.0"?><ajax><![CDATA[success]]></ajax>'


def wml_msg(s):
    return ('<?xml version="1.0"?><wml><card id="msg" title="x"><p>%s</p></card></wml>' % s)


# =========================================================
print("### 1. 纯函数 ###")
ck("parse_cookie 基本解析",
   X.parse_cookie("a=1; b=2; c=x=y") == {"a": "1", "b": "2", "c": "x=y"},
   X.parse_cookie("a=1; b=2; c=x=y"))
ck("cookie_str_of 往返",
   X.parse_cookie(X.cookie_str_of({"k": "v", "k2": "v2"})) == {"k": "v", "k2": "v2"})
ck("parse_account 用 # 分隔", X.parse_account("user#pa#ss") == ("user", "pa#ss"),
   X.parse_account("user#pa#ss"))
ck("parse_account 兼容 :", X.parse_account("user:pwd") == ("user", "pwd"))
ck("parse_account 非法返回 None", X.parse_account("nohash") == (None, None))
ck("_truthy 各种写法",
   all(X._truthy(v) for v in ("1", "true", "YES", "on")) and not X._truthy("0"))

print("### 2. PW cookie 前缀 / 登录标记 ###")
ck("detect_prefix 从 lastvisit 探测", X.detect_prefix({PRE + "lastvisit": "0"}) == PRE,
   X.detect_prefix({PRE + "lastvisit": "0"}))
ck("detect_prefix 从 winduser 探测", X.detect_prefix({PRE + "winduser": "x"}) == PRE)
ck("detect_prefix 探不到返回空", X.detect_prefix({"foo": "1"}) == "")
ck("detect_prefix 支持长前缀", X.detect_prefix({"SgL6_2132_lastvisit": "0"}) == "SgL6_2132_")
ck("has_login_cookie: winduser → True", X.has_login_cookie({PRE + "winduser": "x"}) is True)
ck("has_login_cookie: 仅 lastvisit → False",
   X.has_login_cookie({PRE + "lastvisit": "0", PRE + "lastpos": "other"}) is False)
ck("is_logged_in: 含 action-quit → True", X.is_logged_in(TASKS_OUT) is True)
ck("is_logged_in: 不含 → False", X.is_logged_in(TASKS_IN) is False)

print("### 3. 解析类 ###")
ck("extract_verifyhash", X.extract_verifyhash(TASKS_OUT) == "abc12345")
ck("extract_verifyhash 无值 → None", X.extract_verifyhash("<html></html>") is None)
ck("extract_wap_msg 抽 msg 卡",
   X.extract_wap_msg(wml_msg("用户abc不存在")) == "用户abc不存在",
   X.extract_wap_msg(wml_msg("用户abc不存在")))
ck("extract_wap_msg 无 msg 卡 → 空",
   X.extract_wap_msg('<?xml version="1.0"?><wml><card id="index"><p>hi</p></card></wml>') == "")
ck("extract_cdata 剥 CDATA",
   X.extract_cdata(JOB_OK) == "success", X.extract_cdata(JOB_OK))
ck("short_resp 截断", X.short_resp(JOB_OK, 3) == "suc...", X.short_resp(JOB_OK, 3))
ck("brief 抢救 script 内中文",
   "登陆成功" in X.brief('<div>提示信息</div><script>alert("登陆成功")</script>'),
   X.brief('<div>提示信息</div><script>alert("登陆成功")</script>'))
ck("brief 不吞 CDATA 正文",
   X.brief("<ajax><![CDATA[您还没有登录或注册]]></ajax>") == "您还没有登录或注册",
   X.brief("<ajax><![CDATA[您还没有登录或注册]]></ajax>"))

print("### 4. classify_job ###")
ck("classify_job success", X.classify_job(JOB_OK) == "success")
ck("classify_job done(还没超过)", X.classify_job("<ajax><![CDATA[上次申请还没超过 18 小时]]></ajax>") == "done")
ck("classify_job expired(还没有登录) ★实测文案",
   X.classify_job("<ajax><![CDATA[您还没有登录或注册，暂时不能使用此功能!!]]></ajax>") == "expired",
   X.classify_job("<ajax><![CDATA[您还没有登录或注册，暂时不能使用此功能!!]]></ajax>"))
ck("classify_job expired(请先登录)", X.classify_job("<ajax>请先登录</ajax>") == "expired")
ck("classify_job unknown", X.classify_job("<ajax><![CDATA[奇奇怪怪]]></ajax>") == "unknown")

print("### 5. 缓存读写 ###")
reset_cache()
ck("缓存不存在 → None", X.read_cache() is None)
X.write_cache("a=1; b=2", "login")
saved = X.read_cache()
ck("写后能读回(source/cookie)", saved and saved["source"] == "login" and saved["cookie"] == "a=1; b=2", saved)
with open(CACHE, "w", encoding="utf-8") as f:
    f.write("legacy=1")
ck("兼容旧式纯文本缓存", (X.read_cache() or {}).get("source") == "legacy-plain", X.read_cache())
with open(CACHE, "w", encoding="utf-8") as f:
    f.write("{坏json")
ck("坏 JSON 不抛异常", X.read_cache() is None or True)

print("### 6. wap_login_attempt 各分支（mock）###")
PRE_LP = [PRE + "lastvisit=0; path=/"]


def scenario(post_resp_factory, post_cookies=None):
    reset_cache()
    t = Script()
    t.add("GET", X.WAP_LOGIN, FakeResp(200, LOGIN_PAGE, PRE_LP), exact=True)
    t.add("POST", X.WAP_LOGIN, lambda n: post_resp_factory(n), exact=True)
    install(t)
    return t


t = scenario(lambda n: FakeResp(200, wml_msg("登录成功"), CK_OK))
st, jar, pf = X.wap_login_attempt("u", "p", 1)
ck("成功分支 → ok 且拿到 winduser", st == "ok" and X.has_login_cookie(jar), (st, list(jar.keys())))
ck("成功分支前缀探测正确", pf == PRE, pf)

t = scenario(lambda n: FakeResp(200, wml_msg("用户zzz不存在")))
st, jar, _ = X.wap_login_attempt("u", "p", 1)
ck("用户不存在 → cred", st == "cred", st)

t = scenario(lambda n: FakeResp(200, wml_msg("密码错误")))
st, jar, _ = X.wap_login_attempt("u", "p", 1)
ck("密码错误 → cred", st == "cred", st)

t = scenario(lambda n: FakeResp(200, wml_msg("请输入安全提问答案")))
st, jar, _ = X.wap_login_attempt("u", "p", 1)
ck("安全提问 → question", st == "question", st)

t = scenario(lambda n: FakeResp(200, wml_msg("请输入认证码")))
st, jar, _ = X.wap_login_attempt("u", "p", 1)
ck("如需验证码 → captcha", st == "captcha", st)

t = scenario(lambda n: FakeResp(200, '<?xml version="1.0"?><wml><card id="index"><p>x</p></card></wml>'))
st, jar, _ = X.wap_login_attempt("u", "p", 1)
ck("无提示无 cookie → nomark", st == "nomark", st)

t = scenario(lambda n: FakeResp(200, '<?xml version="1.0"?><wml><card id="index"><p>x</p></card></wml>',
                                [PRE + "winduser=AAA; path=/"]))
st, jar, _ = X.wap_login_attempt("u", "p", 1)
ck("无提示但下发 winduser → ok（双证据兜住）", st == "ok", st)

print("### 7. login_and_get_cookie 重试策略 ###")
t = scenario(lambda n: FakeResp(200, wml_msg("密码错误")))
X.login_and_get_cookie("u", "p")
ck("cred 绝不重试（POST 仅 1 次）", t.counts.get(("POST", X.WAP_LOGIN)) == 1,
   t.counts.get(("POST", X.WAP_LOGIN)))

t = scenario(lambda n: FakeResp(200, '<?xml version="1.0"?><wml><card id="index"><p>x</p></card></wml>'))
X.login_and_get_cookie("u", "p")
ck("nomark 重试一次（POST 共 2 次）", t.counts.get(("POST", X.WAP_LOGIN)) == 2,
   t.counts.get(("POST", X.WAP_LOGIN)))

t = scenario(lambda n: FakeResp(200, wml_msg("登录成功"), CK_OK))
ck("成功即返回 jar", X.login_and_get_cookie("u", "p") is not None)
ck("未配账号 → None", X.login_and_get_cookie("", "") is None)

print("### 8. resolve_cookie 三级顺序 ###")
# 8.1 缓存命中 → 直接用缓存，不发登录
# ⚠️ 注意：scenario() 内部会 reset_cache()，所以「先建 mock、后写缓存」
reset_cache()
t = scenario(lambda n: FakeResp(200, wml_msg("登录成功"), CK_OK))
X.write_cache("cached=1; " + PRE + "winduser=K", "login")
t.reset_counts()
c = X.resolve_cookie("u", "p")
ck("缓存命中 → 用缓存", c is not None and c.jar.get("cached") == "1", c and c.jar)
ck("缓存命中 → 不发起登录 POST", t.counts.get(("POST", X.WAP_LOGIN), 0) == 0,
   t.counts.get(("POST", X.WAP_LOGIN), 0))

# 8.2 无缓存 + 配了账号 → 登录并写缓存
t = scenario(lambda n: FakeResp(200, wml_msg("登录成功"), CK_OK))
c = X.resolve_cookie("u", "p")
saved = X.read_cache()
ck("无缓存 → 走登录", c is not None and X.has_login_cookie(c.jar), c and c.jar)
ck("登录成功 → 写回缓存(source=login)", (saved or {}).get("source") == "login", saved)

# 8.3 无缓存 + 登录失败 + 有 env cookie → env 兜底且不写缓存
os.environ["SOUTHPLUS_COOKIE"] = PRE + "winduser=ENV; zz=9"
t = scenario(lambda n: FakeResp(200, wml_msg("密码错误")))
c = X.resolve_cookie("u", "p")
ck("登录失败 → env 兜底可用", c is not None and c.jar.get("zz") == "9", c and c.jar)
ck("env 兜底 → 不写缓存", X.read_cache() is None, X.read_cache())

# 8.4 FORCE_LOGIN → 忽略缓存、跳过 env
X.FORCE_LOGIN = True
t = scenario(lambda n: FakeResp(200, wml_msg("密码错误")))
X.write_cache("cached=1", "login")
c = X.resolve_cookie("u", "p")
ck("FORCE_LOGIN → 忽略缓存且跳过 env → None", c is None, c)
X.FORCE_LOGIN = False

# 8.5 三样都没有 → None
os.environ.pop("SOUTHPLUS_COOKIE", None)
t = scenario(lambda n: FakeResp(200, wml_msg("用户不存在")))
ck("无缓存/登录失败/无 env → None", X.resolve_cookie("u", "p") is None)

print("### 9. main() 端到端（mock）###")


def run_main(script, account="u#p"):
    install(script)
    os.environ["SOUTHPLUS_ACCOUNT"] = account
    os.environ["MY_PROXY"] = "http://127.0.0.1:9"
    X.FORCE_LOGIN = False
    rc = None
    try:
        X.main()
    except SystemExit as e:
        rc = e.code
    finally:
        os.environ.pop("SOUTHPLUS_ACCOUNT", None)
    return rc


def tasks_route(t, logged_when):
    t.add("GET", X.TASK_PAGE, lambda n: FakeResp(200, logged_when(n)), exact=True)


# 9.1 缓存有效 → job ok + job2 ok，不重登
reset_cache()
X.write_cache(PRE + "winduser=K", "login")
t = Script()
tasks_route(t, lambda n: TASKS_OUT)
t.add("GET", "actions=job2", FakeResp(200, JOB_OK))
t.add("GET", "actions=job", FakeResp(200, JOB_OK))
t.add("POST", X.WAP_LOGIN, FakeResp(200, wml_msg("登录成功"), CK_OK), exact=True)
ck("场景1 缓存有效 → 不重登", run_main(t) is None and t.counts.get(("POST", X.WAP_LOGIN), 0) == 0,
   t.counts)

# 9.2 缓存无效 → 触发重登 → 第二次任务页已登录 → 完成
reset_cache()
X.write_cache(PRE + "winduser=STALE", "login")
t = Script()
t.add("GET", X.TASK_PAGE,
      lambda n: FakeResp(200, TASKS_IN if n == 0 else TASKS_OUT), exact=True)
t.add("GET", "actions=job2", FakeResp(200, JOB_OK))
t.add("GET", "actions=job", FakeResp(200, JOB_OK))
t.add("GET", X.WAP_LOGIN, FakeResp(200, LOGIN_PAGE, [PRE + "lastvisit=0; path=/"]), exact=True)
t.add("POST", X.WAP_LOGIN, FakeResp(200, wml_msg("登录成功"), CK_OK), exact=True)
rc = run_main(t)
ck("场景2 缓存失效 → 自动重登后续做", rc is None, rc)
ck("场景2 重登写回缓存(source=login-relogin)",
   (X.read_cache() or {}).get("source") == "login-relogin", X.read_cache())

# 9.3 job 返回 expired → 重登 → 第二次成功
reset_cache()
X.write_cache(PRE + "winduser=K", "login")
t = Script()
t.add("GET", X.TASK_PAGE, FakeResp(200, TASKS_OUT), exact=True)
t.add("GET", "actions=job2", FakeResp(200, JOB_OK))
t.add("GET", "actions=job",
      lambda n: FakeResp(200, "<ajax><![CDATA[您还没有登录或注册，暂时不能使用此功能!!]]></ajax>"
                         if n == 0 else JOB_OK))
t.add("GET", X.WAP_LOGIN, FakeResp(200, LOGIN_PAGE, [PRE + "lastvisit=0; path=/"]), exact=True)
t.add("POST", X.WAP_LOGIN, FakeResp(200, wml_msg("登录成功"), CK_OK), exact=True)
ck("场景3 job 报未登录 → 重登后续做", run_main(t) is None)

# 9.4 凭据错 + 缓存失效 → 退出 1
reset_cache()
X.write_cache(PRE + "winduser=STALE", "login")
t = Script()
t.add("GET", X.TASK_PAGE, lambda n: FakeResp(200, TASKS_IN), exact=True)
t.add("GET", X.WAP_LOGIN, FakeResp(200, LOGIN_PAGE, [PRE + "lastvisit=0; path=/"]), exact=True)
t.add("POST", X.WAP_LOGIN, FakeResp(200, wml_msg("密码错误")), exact=True)
ck("场景4 凭据错 → 退出 1", run_main(t) == 1)

# 9.5 无任何凭证 → 退出 1
reset_cache()
os.environ.pop("SOUTHPLUS_COOKIE", None)
t = Script()
ck("场景5 无凭证 → 退出 1", run_main(t, account="") == 1)

print()
print("=" * 60)
print("通过 %d / 失败 %d" % (len(PASS), len(FAIL)))
if FAIL:
    for n in FAIL:
        print("  ✗ " + n)
    sys.exit(1)
print("全部通过")
