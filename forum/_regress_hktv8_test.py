#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""HKTV论坛脚本的离线 mock 回归（不联网）。测试版并入正式版后本文件即删除。

覆盖：
  · 纯函数：GBK 解码 / cookie 前缀探测 / auth 有效性 / 登录态解析 / 登录口径 / 摘要 / 分类
  · 缓存：JSON 往返 / 旧式纯文本 / 损坏 JSON 兜底
  · login_attempt 全分支：ok / cred / deleted / captcha / nomark
  · resolve_cookie 三级顺序：缓存命中 / 登录成功写缓存 / env 兜底不写缓存 / FORCE_LOGIN / 全失败
  · WAF 挑战页自动重发
  · main 端到端：签到成功 / 已签到 / 失效重登续签 / 重登失败退出 / 未取到 formhash / 无凭证
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import time as _time

TMP = tempfile.mkdtemp(prefix="hktv_reg_")
CACHE = os.path.join(TMP, "hktv8.cookie")

os.environ["HKTV8_SITE"] = "https://mock.local"
os.environ["HKTV8_MIN_INTERVAL_MS"] = "0"
os.environ["HKTV8_COOKIE_CACHE"] = CACHE
os.environ.pop("HKTV8_COOKIE", None)
os.environ.pop("HKTV8_ACCOUNT", None)
os.environ.pop("HKTV8_FORCE_LOGIN", None)
os.environ.pop("MY_PROXY", None)
os.environ.pop("HKTV8_PROXY", None)

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import hktv8_test as X   # noqa: E402

# 让测试跑得快：sleep 全变 no-op
class _T(object):
    time = staticmethod(_time.time)
    sleep = staticmethod(lambda *a, **k: None)
X.time = _T

S = "https://mock.local"
PREFIX = "vsyQ_8bcf_"

PASS = 0
FAIL = []


def ck(name, cond, extra=""):
    global PASS
    if cond:
        PASS += 1
        print("  ✓ %s" % name)
    else:
        FAIL.append(name)
        print("  ✗ %s   %s" % (name, extra))


# ============ 假响应 / 假 HTTP ============
class Hdrs(object):
    def __init__(self, pairs=None):
        self._p = list(pairs or [])

    def get(self, k, default=None):
        for kk, vv in self._p:
            if kk.lower() == k.lower():
                return vv
        return default

    def items(self):
        return list(self._p)

    def multi_items(self):
        return list(self._p)

    def __contains__(self, k):
        return any(kk.lower() == k.lower() for kk, _ in self._p)


class FakeResp(object):
    """故意让 .text 按 utf-8 解（模拟库猜错编码），以验证 text_of 会按声明 charset/GBK 兜底"""
    def __init__(self, status=200, text="", cookies=None, charset="gbk", ct=None):
        self.status_code = status
        self._charset = charset
        body = text if isinstance(text, bytes) else str(text).encode(charset, "replace")
        self.content = body
        mime = ct or ("text/html; charset=" + charset)
        self.headers = Hdrs([("content-type", mime)] +
                            [("set-cookie", c) for c in (cookies or [])])
        self.cookies = {}
        self.raw = None

    @property
    def text(self):
        return self.content.decode("utf-8", "replace")


class Script(object):
    def __init__(self):
        self.handlers = []
        self.calls = []
        self.counts = {}

    def add(self, method, sub, resp, exact=False):
        self.handlers.append((method.upper(), sub, resp, exact))
        return self

    def request(self, method, url, **kw):
        self.calls.append((method.upper(), url, kw.get("data")))
        for m, sub, resp, exact in self.handlers:
            if method.upper() != m:
                continue
            if (url == sub) if exact else (sub in url):
                key = (m, sub)
                n = self.counts.get(key, 0)
                self.counts[key] = n + 1
                return resp(n) if callable(resp) else resp
        return FakeResp(404, "no route: %s %s" % (method, url))

    def n(self, method, sub):
        return self.counts.get((method.upper(), sub), 0)


def install(t):
    X._http = t
    return t


def reset_cache():
    try:
        os.remove(CACHE)
    except OSError:
        pass


# ============ 桩数据 ============
HOME_ANON = ("<html><head><script>var discuz_uid = '0'; var cookiepre = 'vsyQ_8bcf_';</script>"
             "</head><body><form id=\"lsform\">"
             "<input type=\"hidden\" name=\"formhash\" value=\"aaaaaaaa\" /></form>"
             "<a href=\"plugin.php?id=dsu_paulsign:sign\">每日签到</a></body></html>")
HOME_IN = HOME_ANON.replace("'0'", "'123456'") + \
    ("<a href=\"member.php?mod=logging&amp;action=logout&amp;formhash=aaaaaaaa\">退出</a>")
HOME_NOFH = "<html><script>var discuz_uid = '0';</script><body>没有 formhash 的页面</body></html>"

LOGIN_PAGE = ("<html><body><form method=\"post\" name=\"login\" id=\"loginform_LcDZ3\" "
              "action=\"member.php?mod=logging&amp;action=login&amp;loginsubmit=yes&amp;loginhash=LcDZ3\">"
              "<input type=\"hidden\" name=\"formhash\" value=\"aaaaaaaa\" />"
              "<select name=\"fastloginfield\"><option value=\"username\">用户名</option>"
              "<option value=\"uid\">UID</option><option value=\"email\">Email</option></select>"
              "<input type=\"text\" name=\"username\" /><input type=\"password\" name=\"password\" />"
              "</form></body></html>")
LOGIN_PAGE_CAP = LOGIN_PAGE.replace("<input type=\"text\" name=\"username\" />",
                                    "<input type=\"text\" name=\"username\" />"
                                    "<input name=\"seccodeverify\" />")

LOGIN_OK = ('<?xml version="1.0" encoding="gbk"?><root><![CDATA['
            '<script type="text/javascript" reload="1">'
            "window.location.href='https://www.hktv8.com/';</script>]]></root>")
LOGIN_FAIL = ('<?xml version="1.0" encoding="gbk"?><root><![CDATA['
              "登录失败，您还可以尝试 4 次<script type=\"text/javascript\" reload=\"1\">"
              "if(typeof errorhandle_ls=='function') {errorhandle_ls('登录失败，您还可以尝试 4 次',"
              " {'loginperm':'4'});}</script>]]></root>")
LOGIN_WEIRD = '<?xml version="1.0" encoding="gbk"?><root><![CDATA[请稍候…]]></root>'

SIGN_FORM = ('<?xml version="1.0" encoding="gbk"?><root><![CDATA['
             '<h3 class="flb"><em>每日签到</em></h3>'
             '<form id="qiandao" method="post" action="plugin.php?id=dsu_paulsign:sign&amp;operation=qiandao&amp;infloat=1&amp;sign_as=1">'
             '<input type="hidden" name="formhash" value="aaaaaaaa">'
             '<input id="kx_s" type="radio" name="qdxq" value="kx"></form>]]></root>')
SIGN_EXPIRED = ('<?xml version="1.0" encoding="gbk"?><root><![CDATA[<h3 class="flb"><em>提示信息</em>'
                '<span><a href="javascript:;" class="flbc" onclick="hideWindow(\'dsu_paulsign\');" title="关闭">关闭</a></span></h3>'
                '<div class="c altw"><div class="alert_info">您需要先登录才能继续本操作'
                '<script type="text/javascript" reload="1">'
                "if(typeof succeedhandle_dsu_paulsign=='function') "
                "{succeedhandle_dsu_paulsign('member.php?mod=logging&action=login', "
                "'您需要先登录才能继续本操作', {});}</script></div></div>]]></root>")
SIGN_OK = ('<?xml version="1.0" encoding="gbk"?><root><![CDATA['
           '<script type="text/javascript" reload="1">setTimeout("window.location.reload()", 3000);</script>'
           '<div class="f_c"><h3 class="flb"><em id="return_win">签到提示</em>'
           '<span><a href="javascript:;" class="flbc" onclick="hideWindow(\'qwindow\')" title="关闭">关闭</a></span></h3>'
           '<div class="c">恭喜你签到成功!获得随机奖励 TV币 5 . </div></div>]]></root>')
SIGN_ALREADY = ('<?xml version="1.0" encoding="gbk"?><root><![CDATA['
                '<div class="c">您今天已经签到过了，明天再来吧</div>]]></root>')
# 已签到时浮窗的另一种形态：签到表单不再渲染，且文案不一定含「已签到」字样
# （模板一改版就会落 unknown —— 新逻辑不再筛关键词，直接按已登录态默认判已签到）
SIGN_FWIN_BARE = ('<?xml version="1.0" encoding="gbk"?><root><![CDATA['
                  '<div class="c altw"><div class="alert_info">连签 12 天 · 今日排名 8</div></div>'
                  ']]></root>')

CK_LOGIN_PAGE = ["%ssaltkey=xyz789; path=/; domain=.mock.local" % PREFIX]
CK_LOGIN_OK = ["%sauth=REALTOKEN123456; path=/; domain=.mock.local" % PREFIX,
               "%ssaltkey=xyz789; path=/; domain=.mock.local" % PREFIX,
               "%ssid=WQhk2E; path=/; domain=.mock.local" % PREFIX,
               "server_session_1=aa; path=/"]
CHALLENGE_BODY = ('<html><meta charset="utf-8" /><title></title><div></div></html>\n'
                  '<script> window.location.href ="/member.php?mod=logging&action=login"; </script>\n')
CHALLENGE_CK = ["f1fdc669e8a7d25cf56c402a667b95ca=cebfe1c7856c39dfb8450e975455666a; path=/"]

REAL_PAGE_500 = "<html><body>" + ("真实 Discuz 页面" * 500) + "</body></html>"


def challenge_resp():
    return FakeResp(403, CHALLENGE_BODY, cookies=CHALLENGE_CK, charset="utf-8",
                    ct="text/html;charset=utf8")


def run_main():
    buf = io.StringIO()
    code = 0
    with contextlib.redirect_stdout(buf):
        try:
            X.main()
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
    return code, buf.getvalue()


def sc_login_ok(t, times=None):
    """搭一条「登录页 + 登录成功」的路由"""
    t.add("GET", "mod=logging&action=login", FakeResp(200, LOGIN_PAGE, cookies=CK_LOGIN_PAGE))
    t.add("POST", "loginsubmit=yes",
          (lambda n: FakeResp(200, LOGIN_OK, cookies=CK_LOGIN_OK)) if times is None
          else (lambda n: FakeResp(200, LOGIN_OK, cookies=CK_LOGIN_OK) if n < times
                else FakeResp(200, LOGIN_FAIL)))


print("=" * 70)
print("### 1. 纯函数 ###")
ck("text_of：GBK 字节按 charset 头正确解码",
   X.text_of(FakeResp(200, "您需要先登录才能继续本操作")) == "您需要先登录才能继续本操作",
   X.text_of(FakeResp(200, "您需要先登录才能继续本操作")))
ck("text_of：.text 是 utf-8 猜测（乱码），content+gbk 才是对的",
   X.text_of(FakeResp(200, "签到成功")) == "签到成功")
ck("text_of：bytes 入参", X.text_of("恭喜".encode("gbk")) == "恭喜")
ck("text_of：str 入参原样返回", X.text_of("已经是字符串") == "已经是字符串")
ck("text_of：None → 空串", X.text_of(None) == "")

ck("detect_prefix：两段式 vsyQ_8bcf_", X.detect_prefix({PREFIX + "saltkey": "1"}) == PREFIX)
ck("detect_prefix：三段式 SgL6_2132_", X.detect_prefix({"SgL6_2132_auth": "1"}) == "SgL6_2132_")
ck("detect_prefix：只有无关 cookie → 空",
   X.detect_prefix({"server_session_0f34": "1", "f1fdc669e8a7d25": "2"}) == "")

ck("is_deleted：deleted / ' deleted ' / 空", X.is_deleted("deleted") and X.is_deleted(" DELETED ")
   and not X.is_deleted("abc"))
ck("is_auth_valid：有效", X.is_auth_valid({PREFIX + "auth": "abc"}, "") is True)
ck("is_auth_valid：deleted 无效", X.is_auth_valid({PREFIX + "auth": "deleted"}, "") is False)
ck("is_auth_valid：缺失无效", X.is_auth_valid({PREFIX + "sid": "x"}, "") is False)

ck("parse_uid：已登录", X.parse_uid("var discuz_uid = '123456';") == 123456)
ck("parse_uid：未登录", X.parse_uid("var discuz_uid = '0';") == 0)
ck("parse_uid：没有该变量", X.parse_uid("<html></html>") == 0)
ck("is_logged_in_html：看退出链接", X.is_logged_in_html(HOME_IN) and not X.is_logged_in_html(HOME_ANON))

ck("pick_login_field：邮箱", X.pick_login_field("a@b.com") == "email")
ck("pick_login_field：纯数字 → uid", X.pick_login_field("123456") == "uid")
ck("pick_login_field：其他 → username", X.pick_login_field("abc_123") == "username")

ck("parse_account：# 分隔", X.parse_account("u#p") == ("u", "p"))
ck("parse_account：: 分隔（密码含冒号要保住）", X.parse_account("u:p:x") == ("u", "p:x"))
ck("parse_account：无分隔 → (None, None)", X.parse_account("nope") == (None, None))

ck("brief：CDATA 正文能取出（不被 <[^>]+> 吞掉）", "恭喜你签到成功" in X.brief(SIGN_OK),
   X.brief(SIGN_OK))
ck("brief：正文只剩模板词时，从 script 字符串里抢救",
   "您需要先登录才能继续本操作" in X.brief(SIGN_EXPIRED), X.brief(SIGN_EXPIRED))
ck("brief：空输入 → 空串", X.brief("") == "")

ck("classify_sign：成功", X.classify_sign(SIGN_OK) == "success")
ck("classify_sign：已签到", X.classify_sign(SIGN_ALREADY) == "already")
ck("classify_sign：未登录（您需要先登录才能继续本操作）", X.classify_sign(SIGN_EXPIRED) == "expired")
ck("classify_sign：未知", X.classify_sign("<root><![CDATA[呃]]></root>") == "unknown")

ck("is_challenge：小挑战页 + utf8 → True", X.is_challenge(challenge_resp()) is True)
ck("is_challenge：真实大页面 → False",
   X.is_challenge(FakeResp(200, REAL_PAGE_500)) is False)
ck("is_challenge：200 的小 utf8 页但无跳转 → False",
   X.is_challenge(FakeResp(200, "<html>tiny</html>", charset="utf-8",
                           ct="text/html;charset=utf8")) is False)

print()
print("=" * 70)
print("### 2. 缓存读写 ###")
reset_cache()
ck("缓存不存在 → None", X.read_cache() is None)
X.write_cache(PREFIX + "auth=T1; " + PREFIX + "saltkey=S1", "login")
c = X.read_cache()
ck("JSON 往返：cookie 原样", c and c["cookie"] == PREFIX + "auth=T1; " + PREFIX + "saltkey=S1", c)
ck("JSON 往返：source 保留", c and c["source"] == "login", c)
with open(CACHE, "w", encoding="utf-8") as f:
    f.write(PREFIX + "auth=LEGACY; " + PREFIX + "saltkey=S")
c = X.read_cache()
ck("旧式纯文本兼容", c and c["source"] == "legacy-plain" and "LEGACY" in c["cookie"], c)
with open(CACHE, "w", encoding="utf-8") as f:
    f.write("{坏掉的 json")
ck("损坏 JSON → 不抛异常", X.read_cache() is None or True)
reset_cache()

print()
print("=" * 70)
print("### 3. login_attempt 全分支 ###")
print("-- 3.1 成功 --")
t = install(Script())
sc_login_ok(t)
state, jar = X.login_attempt("someone@example.com", "pw", 1)
ck("state = ok", state == "ok", state)
ck("拿到 auth 且非 deleted", X.is_auth_valid(jar, "") is True, jar)
ck("登录口径按 @ 判为 email",
   any("fastloginfield=email" in str(d) for _, _, d in t.calls if d), t.calls)
ck("POST 体含 formhash/quickforward/handlekey",
   all(k in "".join(str(d) for _, _, d in t.calls if d)
       for k in ("formhash=aaaaaaaa", "quickforward=yes", "handlekey=ls")))
ck("请求 URL 走 HAR 同款快捷登录通道",
   any("infloat=yes&lssubmit=yes&inajax=1" in u for _, u, _ in t.calls))
ck("请求 URL 刻意不带 loginhash（照 HAR 的成功请求）",
   not any("loginhash=" in u for _, u, _ in t.calls if "loginsubmit" in u),
   [u for _, u, _ in t.calls if "loginsubmit" in u])

print("-- 3.2 凭据错误（登录失败…还可以尝试 N 次）--")
t = install(Script())
t.add("GET", "mod=logging&action=login", FakeResp(200, LOGIN_PAGE, cookies=CK_LOGIN_PAGE))
t.add("POST", "loginsubmit=yes", FakeResp(200, LOGIN_FAIL))
state, jar = X.login_attempt("__probe_nobody__", "wrong", 1)
ck("state = cred", state == "cred", state)
ck("密码错误 → 只发一次 POST（Discuz 会累计失败次数）", t.n("POST", "loginsubmit=yes") == 1,
   t.n("POST", "loginsubmit=yes"))

print("-- 3.3 auth=deleted --")
t = install(Script())
t.add("GET", "mod=logging&action=login", FakeResp(200, LOGIN_PAGE, cookies=CK_LOGIN_PAGE))
t.add("POST", "loginsubmit=yes",
      FakeResp(200, LOGIN_OK, cookies=[PREFIX + "auth=deleted; path=/"]))
state, jar = X.login_attempt("u", "p", 1)
ck("state = deleted", state == "deleted", state)

print("-- 3.4 登录页要求验证码 --")
t = install(Script())
t.add("GET", "mod=logging&action=login", FakeResp(200, LOGIN_PAGE_CAP, cookies=CK_LOGIN_PAGE))
t.add("POST", "loginsubmit=yes", FakeResp(200, LOGIN_WEIRD))
state, jar = X.login_attempt("u", "p", 1)
ck("state = captcha", state == "captcha", state)

print("-- 3.5 无法判定（既没凭证也没明确失败原因）--")
t = install(Script())
t.add("GET", "mod=logging&action=login", FakeResp(200, LOGIN_PAGE, cookies=CK_LOGIN_PAGE))
t.add("POST", "loginsubmit=yes", FakeResp(200, LOGIN_WEIRD))
state, jar = X.login_attempt("u", "p", 1)
ck("state = nomark", state == "nomark", state)

print("-- 3.6 登录页取不到 formhash --")
t = install(Script())
t.add("GET", "mod=logging&action=login", FakeResp(200, "<html>无表单</html>"))
state, jar = X.login_attempt("u", "p", 1)
ck("state = nomark 且不发 POST", state == "nomark" and t.n("POST", "loginsubmit=yes") == 0, state)

print("-- 3.7 deleted 会重试一次，cred 不会 --")
MAX = X.MAX_LOGIN_ATTEMPT
t = install(Script())
t.add("GET", "mod=logging&action=login", FakeResp(200, LOGIN_PAGE, cookies=CK_LOGIN_PAGE))
t.add("POST", "loginsubmit=yes",
      lambda n: FakeResp(200, LOGIN_OK, cookies=[PREFIX + "auth=deleted; path=/"]) if n == 0
      else FakeResp(200, LOGIN_OK, cookies=CK_LOGIN_OK))
jar = X.login_and_get_cookie("u", "p")
ck("deleted → 自动重试后成功", jar and X.is_auth_valid(jar, ""), jar)
t = install(Script())
t.add("GET", "mod=logging&action=login", FakeResp(200, LOGIN_PAGE, cookies=CK_LOGIN_PAGE))
t.add("POST", "loginsubmit=yes", FakeResp(200, LOGIN_FAIL))
ck("cred → 不重试、返回 None", X.login_and_get_cookie("u", "p") is None
   and t.n("POST", "loginsubmit=yes") == 1, t.n("POST", "loginsubmit=yes"))

print()
print("=" * 70)
print("### 4. resolve_cookie 三级顺序 ###")
print("-- 4.1 缓存命中 → 不登录 --")
reset_cache()
X.write_cache(PREFIX + "auth=CACHED; " + PREFIX + "saltkey=S", "login")
t = install(Script())
sc_login_ok(t)
c = X.resolve_cookie("u", "p")
ck("用缓存", c is not None and c.jar.get(PREFIX + "auth") == "CACHED", c and c.jar)
ck("缓存命中 → 不发登录 POST", t.n("POST", "loginsubmit=yes") == 0)

print("-- 4.2 无缓存 + 登录成功 → 写缓存 --")
reset_cache()
t = install(Script())
sc_login_ok(t)
c = X.resolve_cookie("u", "p")
saved = X.read_cache()
ck("走登录", c is not None and X.is_auth_valid(c.jar, ""), c and c.jar)
ck("登录成功 → 写回缓存(source=login)",
   saved and saved.get("source") == "login" and "REALTOKEN123456" in saved.get("cookie", ""), saved)

print("-- 4.3 缓存是 deleted → 视为无效，改走登录 --")
reset_cache()
X.write_cache(PREFIX + "auth=deleted; " + PREFIX + "saltkey=S", "login")
t = install(Script())
sc_login_ok(t)
c = X.resolve_cookie("u", "p")
ck("deleted 缓存被弃用 → 走登录成功", c is not None and X.is_auth_valid(c.jar, ""))
ck("确实发了登录 POST", t.n("POST", "loginsubmit=yes") == 1)
reset_cache()

print("-- 4.4 无缓存 + 登录失败 + 有 env cookie → env 兜底且不写缓存 --")
reset_cache()
os.environ["HKTV8_COOKIE"] = PREFIX + "auth=ENVTOKEN; " + PREFIX + "saltkey=S; zz=9"
t = install(Script())
t.add("GET", "mod=logging&action=login", FakeResp(200, LOGIN_PAGE, cookies=CK_LOGIN_PAGE))
t.add("POST", "loginsubmit=yes", FakeResp(200, LOGIN_FAIL))
c = X.resolve_cookie("u", "p")
ck("env 兜底可用", c is not None and c.jar.get("zz") == "9", c and c.jar)
ck("env 兜底 → 不写缓存", X.read_cache() is None, X.read_cache())

print("-- 4.5 FORCE_LOGIN → 忽略缓存、跳过 env --")
reset_cache()
X.write_cache(PREFIX + "auth=CACHED; " + PREFIX + "saltkey=S", "login")
X.FORCE_LOGIN = True
t = install(Script())
t.add("GET", "mod=logging&action=login", FakeResp(200, LOGIN_PAGE, cookies=CK_LOGIN_PAGE))
t.add("POST", "loginsubmit=yes", FakeResp(200, LOGIN_FAIL))
c = X.resolve_cookie("u", "p")
ck("FORCE_LOGIN → 跳过缓存与 env → None", c is None, c)
X.FORCE_LOGIN = False

print("-- 4.6 三样都没有 --")
reset_cache()
os.environ.pop("HKTV8_COOKIE", None)
t = install(Script())
t.add("GET", "mod=logging&action=login", FakeResp(200, LOGIN_PAGE, cookies=CK_LOGIN_PAGE))
t.add("POST", "loginsubmit=yes", FakeResp(200, LOGIN_FAIL))
ck("无缓存/登录失败/无 env → None", X.resolve_cookie("u", "p") is None)

print()
print("=" * 70)
print("### 5. WAF 挑战页自动重发 ###")
t = install(Script())
t.add("GET", S + "/", lambda n: challenge_resp() if n == 0 else FakeResp(200, HOME_ANON), exact=True)
c = X.Client("")
fh, uid, r, ht = X.get_home(c)
ck("挑战页被自动重发化解（最终 200）", r.status_code == 200 and fh == "aaaaaaaa", (r.status_code, fh))
ck("计数 +1", c.challenges == 1, c.challenges)

t = install(Script())
t.add("GET", S + "/", challenge_resp(), exact=True)
c = X.Client("")
fh, uid, r, ht = X.get_home(c)
ck("一直挑战 → 重试到上限即返回挑战页（不再死循环）",
   r.status_code == 403 and c.challenges == X.CHALLENGE_RETRY, (r.status_code, c.challenges))

print()
print("=" * 70)
print("### 6. main 端到端 ###")
print("-- 6.1 缓存有效 + 签到成功 --")
reset_cache()
X.write_cache(PREFIX + "auth=CACHED; " + PREFIX + "saltkey=S", "login")
t = install(Script())
t.add("GET", S + "/", FakeResp(200, HOME_IN), exact=True)
t.add("GET", "ajaxtarget=fwin_content_dsu_paulsign", FakeResp(200, SIGN_FORM))
t.add("POST", "operation=qiandao", FakeResp(200, SIGN_OK))
code, out = run_main()
ck("退出码 0", code == 0, code)
ck("日志含「签到成功」", "🎉 签到成功" in out)
ck("没有触发登录", "🔐 尝试账号密码登录" not in out and t.n("POST", "loginsubmit=yes") == 0)
ck("签到 POST 体 = formhash + qdxq",
   any(str(d) == "formhash=aaaaaaaa&qdxq=kx" for _, _, d in t.calls if d),
   [d for _, _, d in t.calls if d])

print("-- 6.2 已签到 --")
reset_cache()
X.write_cache(PREFIX + "auth=CACHED; " + PREFIX + "saltkey=S", "login")
t = install(Script())
t.add("GET", S + "/", FakeResp(200, HOME_IN), exact=True)
t.add("GET", "ajaxtarget=fwin_content_dsu_paulsign", FakeResp(200, SIGN_FORM))
t.add("POST", "operation=qiandao", FakeResp(200, SIGN_ALREADY))
code, out = run_main()
ck("退出码 0 且提示已签到", code == 0 and "✅ 今日已签到" in out, (code, out[-200:]))

print("-- 6.3 浮窗提示未登录 → 自动重登 → 续签成功 --")
reset_cache()
X.write_cache(PREFIX + "auth=STALE; " + PREFIX + "saltkey=S", "login")
os.environ["HKTV8_ACCOUNT"] = "someone@example.com#pw"
t = install(Script())
t.add("GET", S + "/", FakeResp(200, HOME_IN), exact=True)
t.add("GET", "ajaxtarget=fwin_content_dsu_paulsign",
      lambda n: FakeResp(200, SIGN_EXPIRED if n == 0 else SIGN_FORM))
t.add("POST", "operation=qiandao", FakeResp(200, SIGN_OK))
sc_login_ok(t)
code, out = run_main()
ck("退出码 0", code == 0, code)
ck("触发自动重登", "自动重登后续签一次" in out or "自动重登" in out, out[-400:])
ck("重登后签到成功", "🎉 签到成功" in out)
saved = X.read_cache()
ck("缓存被刷新（source=login-relogin）", saved and saved.get("source") == "login-relogin", saved)

print("-- 6.4 浮窗提示未登录 → 重登失败 → 退出 1 --")
reset_cache()
X.write_cache(PREFIX + "auth=STALE; " + PREFIX + "saltkey=S", "login")
t = install(Script())
t.add("GET", S + "/", FakeResp(200, HOME_IN), exact=True)
t.add("GET", "ajaxtarget=fwin_content_dsu_paulsign", FakeResp(200, SIGN_EXPIRED))
t.add("GET", "mod=logging&action=login", FakeResp(200, LOGIN_PAGE, cookies=CK_LOGIN_PAGE))
t.add("POST", "loginsubmit=yes", FakeResp(200, LOGIN_FAIL))
code, out = run_main()
ck("退出码 1", code == 1, code)
ck("报登录态失效", "登录态失效" in out, out[-300:])

print("-- 6.5 首页取不到 formhash → 重登后成功 --")
reset_cache()
X.write_cache(PREFIX + "auth=CACHED; " + PREFIX + "saltkey=S", "login")
t = install(Script())
t.add("GET", S + "/", lambda n: FakeResp(200, HOME_NOFH if n == 0 else HOME_IN), exact=True)
t.add("GET", "ajaxtarget=fwin_content_dsu_paulsign", FakeResp(200, SIGN_FORM))
t.add("POST", "operation=qiandao", FakeResp(200, SIGN_OK))
sc_login_ok(t)
code, out = run_main()
ck("退出码 0 且最终签到成功", code == 0 and "🎉 签到成功" in out, (code, out[-300:]))

print("-- 6.6 无凭证、三级全失败 → 退出 1 --")
reset_cache()
os.environ.pop("HKTV8_ACCOUNT", None)
t = install(Script())
code, out = run_main()
ck("退出码 1", code == 1, code)
ck("提示三级都失败", "三级都失败" in out, out[-200:])

print("-- 6.7 签到结果未知 → 退出 1 --")
reset_cache()
X.write_cache(PREFIX + "auth=CACHED; " + PREFIX + "saltkey=S", "login")
t = install(Script())
t.add("GET", S + "/", FakeResp(200, HOME_IN), exact=True)
t.add("GET", "ajaxtarget=fwin_content_dsu_paulsign", FakeResp(200, SIGN_FORM))
t.add("POST", "operation=qiandao", FakeResp(200, '<root><![CDATA[呃，说不清]]></root>'))
code, out = run_main()
ck("退出码 1 且报「结果未知」", code == 1 and "结果未知" in out, (code, out[-200:]))

print("-- 6.8 预检模式不签到 --")
reset_cache()
X.PROBE = True
t = install(Script())
t.add("GET", S + "/", FakeResp(200, HOME_ANON), exact=True)
t.add("GET", "mod=logging&action=login", FakeResp(200, LOGIN_PAGE, cookies=CK_LOGIN_PAGE))
t.add("GET", "ajaxtarget=fwin_content_dsu_paulsign", FakeResp(200, SIGN_EXPIRED))
code, out = run_main()
X.PROBE = False
ck("预检退出码 0", code == 0, code)
ck("预检不发签到 POST", t.n("POST", "operation=qiandao") == 0)
ck("预检报告匿名态判定 expired", "匿名态判定: expired" in out, out[-600:])

print("-- 6.9 首页显示未登录（Cookie 失效）→ 门禁拦住、不发浮窗、重登后续签 --")
reset_cache()
X.write_cache(PREFIX + "auth=STALE; " + PREFIX + "saltkey=S", "login")
os.environ["HKTV8_ACCOUNT"] = "someone@example.com#pw"
t = install(Script())
t.add("GET", S + "/", lambda n: FakeResp(200, HOME_ANON if n == 0 else HOME_IN), exact=True)
t.add("GET", "ajaxtarget=fwin_content_dsu_paulsign", FakeResp(200, SIGN_FORM))
t.add("POST", "operation=qiandao", FakeResp(200, SIGN_OK))
sc_login_ok(t)
code, out = run_main()
ck("退出码 0 且签到成功", code == 0 and "🎉 签到成功" in out, (code, out[-300:]))
ck("报「首页显示未登录」", "首页显示未登录" in out, out[-500:])
ck("未登录那轮不发签到浮窗（全程仅 1 次）",
   t.n("GET", "ajaxtarget=fwin_content_dsu_paulsign") == 1,
   t.n("GET", "ajaxtarget=fwin_content_dsu_paulsign"))
ck("首页被请求 2 次（第二次是重登后）", t.n("GET", S + "/") == 2, t.n("GET", S + "/"))

print("-- 6.10 已登录 + 浮窗无 formhash（文案也无关键词）→ 默认判今日已签到 --")
reset_cache()
X.write_cache(PREFIX + "auth=CACHED; " + PREFIX + "saltkey=S", "login")
os.environ.pop("HKTV8_ACCOUNT", None)
t = install(Script())
t.add("GET", S + "/", FakeResp(200, HOME_IN), exact=True)
t.add("GET", "ajaxtarget=fwin_content_dsu_paulsign", FakeResp(200, SIGN_FWIN_BARE))
code, out = run_main()
ck("退出码 0", code == 0, code)
ck("判为「今日已签到」（不再筛关键词）", "✅ 今日已签到" in out, out[-300:])
ck("不发签到 POST", t.n("POST", "operation=qiandao") == 0)
ck("不误触发重登", t.n("POST", "loginsubmit=yes") == 0)

reset_cache()
os.environ.pop("HKTV8_ACCOUNT", None)
os.environ.pop("HKTV8_COOKIE", None)

print()
print("=" * 70)
print("通过 %d / 失败 %d" % (PASS, len(FAIL)))
if FAIL:
    for n in FAIL:
        print("  ✗ %s" % n)
    print("存在失败项")
    sys.exit(1)
print("全部通过")
