#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# =========================================================
# name: 长虹智慧家居签到
# cron: 6 7,16 * * *
# =========================================================
#
# 任务流程：
#   1. 读取 YYB_SERVER 账号基座，按 YYB_ONLY_REFS 白名单过滤 ref
#   2. 调用 YYBGO 的 /wxapp/getCode 获取每个账号的 wx.login code
#   3. 用 code 完成微信登录，进入长虹小程序会话
#   4. 执行签到任务并查询积分，输出汇总后发送通知
# 可控参数：
#   YYB_SERVER      必填。格式「地址@ref#备注」，多账号换行分隔
#   YYB_ONLY_REFS   白名单常量。留空 [] 跑全部；填 ["1","2"] 只跑对应 ref
#   CH_AGGR_ID      可选。手动指定签到活动 ID；留空则从首页自动发现
#   CH_NOTIFY       通知开关，默认开启；填 0/false/off/no 关闭
#   CH_IPV4_ONLY     网络模式，默认 1（仅 IPv4）；填 0 恢复双栈解析
#
# =========================================================

YYB_ONLY_REFS = []  # 只跑 YYB 里 ref 等于这些的账号，留空 [] = 跑全局 YYB_SERVER 里的全部账号

import base64
import importlib
import json
import os
import re
import sys
import socket
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

import requests
import urllib3.util.connection

APP_ID = "wx36c3413e8fe39263"
BASE = "https://hongke.changhong.com/gw/applet"
TITLE = "长虹智慧家居签到"


def configure_network():
    # ql2 实测：AF_UNSPEC/AAAA解析EAI_AGAIN，A记录及IPv4 HTTPS正常。
    # 仅影响当前脚本进程；不硬编码IP、不关闭TLS验证。
    if os.getenv("CH_IPV4_ONLY", "1") != "0":
        urllib3.util.connection.allowed_gai_family = lambda: socket.AF_INET


class TaskError(Exception):
    pass


def request(session, method, url, label, **kwargs):
    try:
        response = session.request(method, url, timeout=(10, 35),
                                   allow_redirects=False, **kwargs)
        if response.status_code != 200:
            raise TaskError(f"{label}：HTTP {response.status_code}")
        result = response.json()
        if not isinstance(result, dict):
            raise TaskError(f"{label}：响应格式异常")
        return result
    except (requests.RequestException, ValueError) as exc:
        # 不打印可能含 URL、凭据或个人信息的原始异常及服务端原文。
        detail = str(exc)
        reason = "DNS解析失败" if "NameResolutionError" in detail or "Failed to resolve" in detail else type(exc).__name__
        raise TaskError(f"{label}：网络或JSON异常（{reason}）") from None


def parse_entry(line):
    if "@" not in line:
        raise TaskError("YYB_SERVER 格式应为 服务地址@账号标识")
    server, ref = (part.strip() for part in line.rsplit("@", 1))
    if not server.startswith(("http://", "https://")):
        server = "http://" + server
    u = urlsplit(server)
    if not u.hostname or not ref or u.query or u.fragment or u.username:
        raise TaskError("YYB_SERVER 地址或账号标识无效")
    return server.rstrip("/"), ref


def entry_ref(line):
    """取出 YYB_SERVER 行的 ref；格式错误返回 None，交给 parse_entry 统一报错。"""
    try:
        return parse_entry(line)[1]
    except TaskError:
        return None


class Changhong:
    def __init__(self, server, ref):
        self.server, self.ref = server, ref
        self.yyb = requests.Session()
        self.yyb.trust_env = False
        self.web = requests.Session()
        self.web.headers.update({
            "content-type": "application/json",
            "Referer": f"https://servicewechat.com/{APP_ID}/330/page-frame.html",
            "User-Agent": "Mozilla/5.0 MicroMessenger/8.0.50 MiniProgramEnv/iOS",
        })

    def close(self):
        self.yyb.close()
        self.web.close()

    def yyb_call(self, endpoint, **extra):
        body = request(self.yyb, "POST", self.server + "/wxapp/" + endpoint,
                       "YYB " + endpoint,
                       json={"ref": self.ref, "app_id": APP_ID, **extra})
        result = (body.get("data") or {}).get("result")
        if body.get("code") != 0 or not isinstance(result, dict):
            raise TaskError(f"YYB {endpoint}失败，请检查账号授权及YYB日志")
        return result

    def api(self, method, path, **kwargs):
        body = request(self.web, method, BASE + path, path, **kwargs)
        if str(body.get("code")) != "200":
            raise TaskError(f"{path}：业务请求失败，请检查小程序登录/授权/活动状态")
        return body.get("data")

    def login(self):
        # 用户资料为辅助字段；不把YYB宿主OpenID误当作长虹小程序OpenID。
        profile, info = {}, {}
        try:
            info = self.yyb_call("operateWxData", payload={
                "api_name": "webapi_getuserinfo", "data": {"lang": "zh_CN"},
                "with_credentials": True, "from_component": True,
                "operate_directly": False,
            })
            profile = info.get("userInfo") or {}
            if not profile and info.get("rawData"):
                profile = json.loads(info["rawData"])
            if not profile and info.get("data"):
                raw = info["data"]
                if isinstance(raw, dict):
                    profile = raw
                else:
                    try:
                        profile = json.loads(raw)
                    except (ValueError, TypeError):
                        profile = json.loads(base64.b64decode(raw))
            if not isinstance(profile, dict):
                profile = {}
        except (TaskError, ValueError, TypeError):
            print("  微信资料不可用，将由长虹服务端通过新code识别账号")
        code = self.yyb_call("getCode").get("code")
        if not isinstance(code, str) or not code.strip():
            raise TaskError("YYB未返回有效的wx.login code")
        common = {
            "jsCode": code, "invitation": "", "system": "iOS 16.0",
            "userName": profile.get("nickName", ""),
            "sex": {1: "男", 2: "女"}.get(profile.get("gender"), "未知"),
            "avatarUrl": profile.get("avatarUrl", ""),
        }
        # HAR 先通过此接口换取长虹小程序身份，再提交手机号授权。
        identity = self.api("POST", "/appletUser/getTokenByJsCode", json={
            **common, "iv": info.get("iv", ""),
        }, headers={"token": "", "smarthome": ""})
        if not isinstance(identity, dict):
            raise TaskError("长虹身份响应格式异常")
        data = identity
        if not data.get("token"):
            if not identity.get("openId") or not identity.get("unionId"):
                raise TaskError("长虹未返回小程序OpenID/UnionID，请检查微信授权")
            phone = self.yyb_call("getPhoneNumber")
            encrypted = phone.get("encryptedData") or phone.get("encrypted_data")
            if not all(isinstance(v, str) and v for v in
                       (phone.get("code"), encrypted, phone.get("iv"))):
                raise TaskError("YYB手机号授权数据不完整，请先在小程序授权手机号")
            # HAR 中第二步沿用第一步jsCode，由业务端处理；不再次向微信兑换。
            data = self.api("POST", "/appletUser/getTokenByCode", json={
                **common, "encryptedData": encrypted, "iv": phone["iv"],
                "code": phone["code"], "openId": identity["openId"],
                "unionId": identity["unionId"],
            }, headers={"token": "", "smarthome": ""})
        if not isinstance(data, dict) or not isinstance(data.get("token"), str) or not data["token"]:
            raise TaskError("长虹登录未返回token，需核对授权字段")
        # 实测静默登录返回token时isRegistered=0；手机号登录HAR为1。
        # 不能把该分支标记当作登录成败，必须用token访问业务接口验证。
        self.web.headers.update({"token": data["token"], "smarthome": data["token"]})
        # 实际业务查询验证登录态，不能仅凭拿到code/token判定成功。
        self.api("GET", "/mine/getAppletUser")

    def activity_id(self):
        explicit = os.getenv("CH_AGGR_ID", "").strip()
        if explicit:
            return explicit
        menu = self.api("POST", "/homePage/getShortcutMenuList")
        found = set()

        def walk(value):
            if isinstance(value, list):
                for item in value:
                    walk(item)
            elif isinstance(value, dict):
                if "签到" in str(value.get("name", "")):
                    match = re.search(r"activityData=([A-Za-z0-9_-]+)", str(value.get("webUrl", "")))
                    if match:
                        found.add(match.group(1))
                for item in value.values():
                    if isinstance(item, (list, dict)):
                        walk(item)

        walk(menu)
        if len(found) != 1:
            raise TaskError("首页未发现唯一签到活动，请设置 CH_AGGR_ID")
        return found.pop()

    def state(self, aggr_id):
        data = self.api("GET", "/aggr/aggregationInfo", params={"aggrId": aggr_id})
        if not isinstance(data, dict):
            raise TaskError("活动详情格式异常")
        today = datetime.now(timezone(timedelta(hours=8))).date().isoformat()
        if not (str(data.get("startTime", ""))[:10] <= today <= str(data.get("endTime", ""))[:10]):
            raise TaskError("活动未开始或已结束，请更新活动入口")
        if str(data.get("status")) != "1" or str(data.get("isCan")) != "1":
            raise TaskError("当前账号无法参与该活动")
        signs = [x["aggrAssemblySignin"] for x in data.get("aggrAssemblyList", [])
                 if isinstance(x, dict) and isinstance(x.get("aggrAssemblySignin"), dict)]
        if len(signs) != 1 or signs[0].get("isSignin") not in (0, 1):
            raise TaskError("签到组件或状态无法识别")
        return signs[0]

    def run(self):
        self.login()
        aggr_id = self.activity_id()
        before = self.state(aggr_id)
        if before["isSignin"] == 1:
            status = "今日已签到"
        else:
            error = None
            try:
                self.api("POST", "/aggr/signin", params={"aggrId": aggr_id})
            except TaskError as exc:
                error = exc
            # 签到POST超时也只查询结果，不盲目重复提交。
            if self.state(aggr_id)["isSignin"] != 1:
                raise TaskError(str(error) if error else "签到后状态未变为已签到")
            status = "签到成功（已复查）"
        try:
            points = self.api("GET", "/homePage/getUserPoint")
            # 实测刚签到时可能暂返0，稍后查询恢复实际余额。
            if before["isSignin"] == 0 and points == 0:
                for delay in (2, 4):
                    time.sleep(delay)
                    points = self.api("GET", "/homePage/getUserPoint")
                    if points != 0:
                        break
            if isinstance(points, (int, float)) and not isinstance(points, bool):
                status += f"，当前积分：{points}"
                if before["isSignin"] == 0 and points == 0:
                    status += "（接口可能尚未更新，请稍后查看）"
            else:
                status += "，积分格式无法识别"
        except TaskError:
            status += "，积分查询失败"
        return status


def notify(message, title=None):
    # 通知开关默认开：CH_NOTIFY 填 0/false/off/no 才关
    if os.getenv("CH_NOTIFY", "1").strip().lower() in ("0", "false", "off", "no"):
        return
    # notify.py 定位：脚本同目录 → 仓库根（全仓共享一份）→ 青龙默认脚本目录
    here = Path(__file__).resolve().parent
    for folder in (here, here.parent, Path("/ql/data/scripts"), Path("/ql/scripts")):
        if str(folder) not in sys.path:
            sys.path.append(str(folder))
    try:
        importlib.import_module("notify").send(title or TITLE, message)
        print("青龙通知模块调用完成（送达情况以通知渠道为准）")
    except Exception as exc:
        print(f"青龙通知不可用或调用失败（{type(exc).__name__}），不影响签到结果")


def main():
    configure_network()
    lines = [x.strip() for x in os.getenv("YYB_SERVER", "").splitlines() if x.strip()]
    # 只跑 YYB_ONLY_REFS 里列出的 ref；空列表 = 跑全部
    if YYB_ONLY_REFS:
        lines = [x for x in lines if entry_ref(x) in YYB_ONLY_REFS]
        if not lines:
            print("⚠️ 顶部 YYB_ONLY_REFS = %s 过滤后无账号，留空 [] 可跑全部" % YYB_ONLY_REFS)
    results, fail_count = [], 0
    if not lines:
        results.append("未配置 YYB_SERVER")
        fail_count = 1
    for index, line in enumerate(lines, 1):
        client = None
        try:
            client = Changhong(*parse_entry(line))
            result = client.run()
        except TaskError as exc:
            result = str(exc); fail_count += 1
        except Exception as exc:
            result = f"处理异常（{type(exc).__name__}）"; fail_count += 1
        finally:
            if client:
                client.close()
        results.append(f"账号{index}：{result}")
        print(results[-1])
    if not lines:
        print(results[0])
    # 控制台执行汇总
    _n = len(lines)
    print("──── 长虹 执行汇总 ────")
    print(f"账号 {_n}｜成功 {_n - fail_count}｜失败 {fail_count}")
    print("────────────────────")
    # 渲染分账号汇总（面向 notify，简洁精要；纯签到无积分，账号行只显示账号）
    _seq = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
    _content = []
    for _i, _r in enumerate(results, 1):
        _acct, _sep, _rest = _r.partition("：")
        _acct = _acct if _sep else f"账号{_i}"
        _em = _seq[_i - 1] if _i <= len(_seq) else f"{_i}."
        _content.append(f"{_em} [{_acct}]")
        _rest = _rest.strip()
        if "异常" in _rest or "失败" in _rest or _rest.startswith("❌"):
            _content.append("❌ " + (_rest[1:].lstrip() if _rest.startswith("❌") else _rest))
        else:
            _content.append("✔️ " + _rest)
    notify("\n".join(_content), "====== 长虹 汇总日志 ======")
    return 1 if fail_count else 0


if __name__ == "__main__":
    sys.exit(main())
