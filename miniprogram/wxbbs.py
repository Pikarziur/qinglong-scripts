# =========================================================
# name: 微信笔笔省
# cron: 30 6,16 * * *
# =========================================================
#
# 任务流程：
#   1. 读取 YYB_SERVER 账号基座，按 YYB_ONLY_REFS 序号白名单筛选（1 起）
#   2. 调用 YYBGO 的 /wxapp/getCode 获取 wx.login code
#   3. 用 code 完成登录（jscode → session_token）
#   4. 查询余额 / 领券 / 提现额度等任务，输出汇总
# 可控参数：
#   YYB_SERVER      必填。格式「地址@ref#备注」，多账号换行分隔
#   YYB_ONLY_REFS   账号序号白名单（1 起）。默认 ["1"]（只跑第 1 个账号）；留空 [] 跑全部
#   PROXY_API_URL    可选。代理 API，返回「ip:端口」文本，填写后请求走代理
#
# 日志规范：[LEVEL] [WXBBS] message   （LEVEL: INFO / WARN / ERROR）
# =========================================================

YYB_ONLY_REFS = ["1"] # 账号序号白名单（1 起），默认 ["1"] = 只跑第 1 个账号；留空 [] = 跑全部

import json
import random
import re
import time
import requests
import os
import base64
import hashlib
import traceback
import ssl
from datetime import datetime, timedelta

# ———————————— 错误通知（可选项，想用则用）————————————
# 只推错误：本次运行出现 ERROR 日志才推送一次；正常跑完不打扰。
# 直接调用青龙自带的通知模块（容器内为 /ql/data/scripts/notify.py，仓库内为同目录/上一级的 notify.py）——
#   在青龙面板「通知设置」里配一次即可全站通用（该文件由青龙官方维护，支持其全部推送渠道）。
# 找不到该文件、或未配置任何通知渠道时，只在日志末尾提示一行，不报错、不中断。
import os as _os
import sys as _sys

_QN_SITE = "微信支付提现笔笔省"
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

MULTI_ACCOUNT_SPLIT = ["\n", "@"] # 分隔符列表
MULTI_ACCOUNT_PROXY = False # 是否使用多账号代理，默认不使用，True则使用多账号代理

class YYBGoEnhancedAdapter:
    """YYB-Go-Enhanced 的 wx.login code 客户端。"""

    def __init__(self, wx_appid):
        self.wx_appid = wx_appid
        self.log_msgs = []

    def log(self, msg, level="info"):
        self.log_msgs.append(str(msg))
        prefix = {"error": "ERROR", "warning": "WARN"}.get(level, "INFO")
        line = f"[{prefix}] [WXBBS] {msg}"
        print(line, flush=True)
        if prefix == "ERROR":
            collect_error(line)

    @staticmethod
    def parse_entry(entry):
        value = str(entry or "").strip()
        if "@" not in value:
            raise ValueError("格式应为 地址@账号ref")
        server, ref = value.rsplit("@", 1)
        server, ref = server.strip().rstrip("/"), ref.strip()
        if not server or not ref:
            raise ValueError("地址和账号ref均不能为空")
        if not server.startswith(("http://", "https://")):
            server = "http://" + server
        return server, ref

    def get_code(self, entry):
        try:
            server, ref = self.parse_entry(entry)
            session = requests.Session()
            session.trust_env = False  # 取码访问Docker内网，不继承青龙代理
            response = session.post(
                f"{server}/wxapp/getCode",
                json={"ref": ref, "app_id": self.wx_appid},
                timeout=(10, 60),
            )
            response.raise_for_status()
            body = response.json()
            if response.status_code < 200 or response.status_code >= 300:
                raise RuntimeError(body.get("error") or body.get("msg") or f"HTTP {response.status_code}")
            if "code" in body and body.get("code") not in (0, "0", None):
                raise RuntimeError(body.get("error") or body.get("msg") or "YYB请求失败")
            result = body.get("result")
            if result is None:
                result = (body.get("data") or {}).get("result")
            code = (result or {}).get("code")
            if code:
                self.log(f"🔑 [YYB] 账号 {ref} 获取code成功")
                return code
            self.log(f"❌ [YYB] 获取code失败: {str(body)[:300]}", "error")
        except Exception as exc:
            self.log(f"💥 [YYB] 获取code异常: {exc}", "error")
        return None

class TLSAdapter(requests.adapters.HTTPAdapter):
    """
    自定义TLS
    解决unsafe legacy renegotiation disabled
    貌似python太高版本依然会报错
    """
    def init_poolmanager(self, *args, **kwargs):
        ctx = ssl.create_default_context()
        ctx.set_ciphers("DEFAULT@SECLEVEL=1")
        ctx.options |= 0x4   # <-- the key part, OP_LEGACY_SERVER_CONNECT
        kwargs["ssl_context"] = ctx
        return super(TLSAdapter, self).init_poolmanager(*args, **kwargs)

class AutoTask:
    def __init__(self, script_name):
        """
        初始化自动任务类
        :param script_name: 脚本名称，用于日志显示
        """
        self.script_name = script_name
        self.proxy_url = os.getenv("PROXY_API_URL") # 代理api，返回一条txt文本，内容为代理ip:端口
        self.wx_appid = "wxdb3c0e388702f785" # 微信小程序id
        self.wechat_code_adapter = YYBGoEnhancedAdapter(self.wx_appid)
        self.host = "discount.wxpapp.wechatpay.cn"
        self.nickname = ""
        self.token = ""
        self.points = 0.00
        self.user_agent = "Mozilla/5.0 (Linux; Android 12; M2012K11AC Build/SKQ.1.220303.001; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/134.0.6998.136 Mobile Safari/537.36 XWEB/1340129 MMWEBSDK/20240301 MMWEBID/9871 MicroMessenger/8.0.48.2580(0x28003036) WeChat/arm64 Weixin NetType/WIFI Language/zh_CN ABI/arm64 MiniProgramEnv/android"

    def log(self, msg, level="info"):
        self.wechat_code_adapter.log(msg, level)

    def acc_banner(self, idx, total, ident=""):
        """打印账号分隔标识（开始）。多账号同跑时把各账号日志隔开，便于阅读与定位。"""
        self.log("=" * 70)
        self.log("👤 账号 %d/%d%s" % (idx, total, (" ｜ " + str(ident)) if ident else ""))
        self.log("=" * 70)

    def acc_footer(self, idx, total):
        """打印账号分隔标识（结束）。"""
        self.log("🔚 账号 %d/%d 处理结束" % (idx, total))

    def dict_keys_to_lower(self, obj):
        """
        递归将字典的所有键名转为小写
        """
        if isinstance(obj, dict):
            return {k.lower(): self.dict_keys_to_lower(v) for k, v in obj.items()}
        elif isinstance(obj, list):
            return [self.dict_keys_to_lower(i) for i in obj]
        else:
            return obj

    def hide_phone(self, phone):
        """
        隐藏手机号中间4位
        """
        return phone[:3] + "****" + phone[-4:]

    def get_proxy(self):
        """
        获取代理
        :return: 代理
        """
        if not self.proxy_url:
            self.log("ℹ️ [获取代理] 没有找到环境变量PROXY_API_URL，不使用代理", level="warning")
            return None
        url = self.proxy_url
        response = requests.get(url)
        proxy = response.text
        self.log(f"🛡️ [获取代理] {proxy}")
        return proxy

    def check_proxy(self, proxy, session):
        """
        检查代理
        :param proxy: 代理
        :param session: session
        :return: 是否可用
        """
        try:
            url = f"https://{self.host}/"
            response = session.get(url, timeout=5)
            if response.status_code == 200:
                self.log(f"🛡️ [检查代理] {proxy} 应该可用")
                return True
            else:
                self.log(f"⚠️ [检查代理] {response.text}")
                return False
        except Exception as e:
            return False

    def check_env(self):
        """
        检查环境变量
        :return: 环境变量字符串
        """
        try:
            yyb_server = os.getenv("YYB_SERVER", "").strip()
            if not yyb_server:
                self.log("🚫 [检查环境变量] 没有找到YYB_SERVER；格式：地址@账号ref，多账号换行", level="error")
                return None
            for entry in yyb_server.splitlines():
                entry = entry.strip()
                if not entry:
                    continue
                try:
                    _server, _ref = self.wechat_code_adapter.parse_entry(entry)
                    yield entry
                except ValueError as exc:
                    self.log(f"⚠️ [检查环境变量] 跳过无效配置 {entry!r}: {exc}", level="error")
        except Exception as e:
            self.log(f"💥 [检查环境变量] 发生错误: {str(e)}\n{traceback.format_exc()}", level="error")
            raise

    # ── 已移除本地 token 缓存（原 save/load/remove_account_info）──
    # 改为每次运行强制重新取码 + 登录，参照 cdf.py 的不落盘方案。

    def login(self, session, code):
        """
        登录
        :param session: session
        :param code: code
        :return: 登录结果
        """
        try:
            url = f"https://{self.host}/txbbs-user/user/login"
            session.headers['jscode'] = code
            response = session.get(url, timeout=15)
            response_json = response.json()
            time.sleep(random.randint(3, 5))
            if int(response_json['errcode']) == 0:
                self.token = response_json['data']['session_token']
                session.headers['Session-Token'] = self.token
                # 注意：不要在此打印 token 明文（青龙日志会持久化并可能推送出去）
                return self.token
            else:
                self.log(f"❌ [登录] 失败 错误信息: {response_json.get('msg', '未知错误')}", level="warning")
                return False
        except requests.RequestException as e:
            self.log(f"💥 [登录] 发生网络错误: {str(e)}\n{traceback.format_exc()}", level="error")
            return False
        except Exception as e:
            self.log(f"💥 [登录] 发生错误: {str(e)}\n{traceback.format_exc()}", level="error")
            return False

    def get_balance(self, session):
        """
        获取用户余额
        :param session: session
        :return: 用户余额
        """
        try:
            url = f"https://{self.host}/txbbs-mall/cashoutfree/getbalance"
            response = session.get(url, timeout=15)
            response_json = response.json()
            time.sleep(random.randint(3, 5))
            if int(response_json['errcode']) == 0:
                self.points = int(response_json['data']['balance']) // 100
                return self.points
            else:
                self.log(f"❌ [{self.nickname}] 获取用户余额 发生错误: {response_json.get('msg', '未知错误')}", level="warning")
                return None
        except Exception as e:
            self.log(f"💥 [{self.nickname}] 获取用户余额 发生错误: {str(e)}\n{traceback.format_exc()}", level="error")
            return None

    def get_gifts_list(self, session):
        """
        获取优惠券列表
        :param session: session
        :return: 优惠券列表
        """
        try:
            url = f"https://{self.host}/txbbs-mall/gift/listgifts?longitude=0&latitude=0"
            response = session.get(url, timeout=15)
            response_json = response.json()
            time.sleep(random.randint(3, 5))
            if int(response_json['errcode']) == 0:
                gifts = response_json['data']['gift_info_list']
                return gifts
            else:
                self.log(f"❌ [{self.nickname}] 获取优惠券列表 发生错误: {response_json.get('msg', '未知错误')}", level="warning")
                return []
        except Exception as e:
            self.log(f"💥 [{self.nickname}] 获取优惠券列表 发生错误: {str(e)}\n{traceback.format_exc()}", level="error")
            return []

    def redeem_gift(self, session, gift_id):
        """
        领取优惠券
        :param session: session
        :param gift_id: 优惠券ID
        :return: 领取结果
        """
        try:
            url = f"https://{self.host}/txbbs-mall/gift/redeemgift"
            payload = {"gift_id": gift_id}
            response = session.post(url, json=payload, timeout=15)
            response_json = response.json()
            time.sleep(random.randint(3, 5))
            if int(response_json['errcode']) == 0:
                gift_info = response_json['data']['gift_info']
                gift_name = gift_info.get('coupon_info', {}).get('name', '未知名称')
                self.log(f"🎁 [{self.nickname}] 领取优惠券成功: {gift_name}")
                return True
            else:
                error_msg = response_json.get('msg', '领取失败，未获取到具体信息')
                self.log(f"❌ [{self.nickname}] 领取优惠券失败: {error_msg}", level="warning")
                return False
        except Exception as e:
            self.log(f"💥 [{self.nickname}] 领取优惠券发生错误: {str(e)}\n{traceback.format_exc()}", level="error")
            return False

    def run(self):
        """
        运行任务
        """
        try:
            self.log(f"🚀 【{self.script_name}】开始执行任务")
            entries = list(self.check_env())
            # 按 YYB_ONLY_REFS 序号白名单筛选（1 起）；留空 [] 则运行全部账号
            if YYB_ONLY_REFS:
                wanted = set(int(x) for x in YYB_ONLY_REFS if str(x).strip().isdigit() and int(x) > 0)
                if wanted:
                    entries = [e for i, e in enumerate(entries, 1) if i in wanted]
                    self.log(f"ℹ️ 按 YYB_ONLY_REFS 筛选：请求序号 {sorted(wanted)}，命中 {len(entries)} 个账号")
            total_accounts = len(entries)
            self.log(f"👥 共 {len(entries)} 个账号待执行")
            if not entries:
                self.log("🚫 没有可执行账号（YYB_SERVER 为空或全部被 YYB_ONLY_REFS 过滤）", level="error")
                return
            ok_cnt = 0
            for index, wx_id in enumerate(entries, 1):
                # 清理账号信息
                self.nickname = f"账号{index}"
                self.token = ""
                self.acc_banner(index, total_accounts, "标识：" + str(wx_id))
                session = requests.Session()
                headers = {
                    "User-Agent": self.user_agent,
                    "authority": self.host
                }
                session.headers.update(headers)

                if MULTI_ACCOUNT_PROXY:
                    proxy = self.get_proxy()
                    if proxy:
                        session.proxies.update({"http": f"http://{proxy}", "https": f"http://{proxy}"})
                        # # 检查代理，不可用重新获取
                        # while not self.check_proxy(proxy, session):
                        #     proxy = self.get_proxy()
                        #     session.proxies.update({"http": f"http://{proxy}", "https": f"http://{proxy}"})

                # 每次运行强制重新取码 + 登录，拿全新 token（不落盘、不复用本地缓存）
                code = self.wechat_code_adapter.get_code(wx_id)
                if not code:
                    self.log(f"❌ [{self.nickname}] YYB取码失败，跳过该账号", level="error")
                    session.close()
                    self.acc_footer(index, total_accounts)
                    continue
                token = self.login(session, code)
                if not token:
                    self.log(f"❌ [{self.nickname}] 登录失败，跳过该账号", level="error")
                    session.close()
                    self.acc_footer(index, total_accounts)
                    continue
                self.token = token
                session.headers['Session-Token'] = token
                # 校验刚登录的 token（失效则重登一次，仍失败则跳过；全程不落盘）
                if self.get_balance(session) is None:
                    self.log(f"🔁 [{self.nickname}] 登录态校验失败，尝试重登一次", level="warning")
                    code = self.wechat_code_adapter.get_code(wx_id)
                    if not code:
                        self.log(f"❌ [{self.nickname}] 授权失败，跳过该账号", level="error")
                        session.close()
                        self.acc_footer(index, total_accounts)
                        continue
                    token = self.login(session, code)
                    if not token:
                        self.log(f"❌ [{self.nickname}] 重新登录失败，跳过该账号", level="error")
                        session.close()
                        self.acc_footer(index, total_accounts)
                        continue
                    self.token = token
                    session.headers['Session-Token'] = token
                # 获取优惠券列表
                gifts_list = self.get_gifts_list(session)
                for gift in gifts_list:
                    if gift.get('gift_type') == 'GT_COUPON' and gift.get('gift_status') == 'GS_AVAILABLE':
                        gift_id = gift.get('gift_id')
                        self.redeem_gift(session, gift_id)
                # 再次获取用户余额
                self.get_balance(session)
                self.log(f"💳 [{self.nickname}] 当前提现免费券: {self.points}元")
                ok_cnt += 1
                # 清理session
                session.close()
                self.acc_footer(index, total_accounts)
            # 不落盘：每次运行的 token 均为临时登录获取，不写入本地文件
            self.log("=" * 70)
            self.log(f"📊 [执行汇总] {self.script_name} · 账号 {total_accounts} ｜ 成功 {ok_cnt} ｜ 失败 {total_accounts - ok_cnt}")
            self.log("=" * 70)            
            # 错误日志推送：有错误才发；未配置通知渠道则只提示一行
            flush_notify(
                "微信支付提现笔笔省",
                f"账号 {total_accounts} ｜ 成功 {ok_cnt} ｜ 失败 {total_accounts - ok_cnt}",
                logger=self.log,
            )
        except Exception as e:
            self.log(f"💥 【{self.script_name}】执行过程中发生错误: {str(e)}\n{traceback.format_exc()}", level="error")

if __name__ == "__main__":
    auto_task = AutoTask("微信支付提现笔笔省")
    auto_task.run()
