"""
name: 微信笔笔省 - 提现额度
入口: 微信小程序 (https://a.c1ns.cn/X3ucP)
功能: 领券、查询
变量: YYB_SERVER (YYB-Go-Enhanced地址@账号ref，多个账号换行分割)
        PROXY_API_URL (代理api，返回一条txt文本，内容为代理ip:端口)
        LY_NOTIFY (通知开关，默认开启；填 0/false/off/no 关闭)
# cron: 21 7,16 * * *

------------更新日志------------
2025/8/21   V1.0    初始化脚本
2026/8/7    V2.0    适配YYB-Go-Enhanced及青龙单文件运行
"""

YYB_ONLY_REFS = ["1"] # 只跑 YYB 里 ref 等于这些的账号，留空 [] = 跑全局 YYB_SERVER 里的全部账号

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
import sys
from datetime import datetime, timedelta

MULTI_ACCOUNT_SPLIT = ["\n", "@"] # 分隔符列表
MULTI_ACCOUNT_PROXY = False # 是否使用多账号代理，默认不使用，True则使用多账号代理
NOTIFY = (os.getenv("LY_NOTIFY", "1") or "1").strip().lower() not in ("0", "false", "off", "no") # 是否推送日志，默认开启，填 0/false/off/no 关闭

# 共享 notify 模块定位：仓库根目录放一份 notify.py，全部脚本共用（不再各目录放副本）
# 兼容旧布局：脚本同目录若已有 notify.py（老版本自愈下载留下的），优先用它
_NOTIFY_DIR = os.path.dirname(os.path.abspath(__file__))
if not os.path.exists(os.path.join(_NOTIFY_DIR, "notify.py")):
    _NOTIFY_DIR = os.path.dirname(_NOTIFY_DIR)   # 脚本同目录没有 → 用仓库根那份
if _NOTIFY_DIR not in sys.path:
    sys.path.insert(0, _NOTIFY_DIR)

class YYBGoEnhancedAdapter:
    """YYB-Go-Enhanced 的 wx.login code 客户端。"""

    def __init__(self, wx_appid):
        self.wx_appid = wx_appid
        self.log_msgs = []

    def log(self, msg, level="info"):
        self.log_msgs.append(str(msg))
        prefix = {"error": "ERROR", "warning": "WARN"}.get(level, "INFO")
        print(f"[{prefix}] {msg}")

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
                self.log(f"[YYB] 账号 {ref} 获取code成功")
                return code
            self.log(f"[YYB] 获取code失败: {str(body)[:300]}", "error")
        except Exception as exc:
            self.log(f"[YYB] 获取code异常: {exc}", "error")
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
            self.log("[获取代理] 没有找到环境变量PROXY_API_URL，不使用代理", level="warning")
            return None
        url = self.proxy_url
        response = requests.get(url)
        proxy = response.text
        self.log(f"[获取代理] {proxy}")
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
                self.log(f"[检查代理] {proxy} 应该可用")
                return True
            else:
                self.log(f"[检查代理] {response.text}")
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
                self.log("[检查环境变量] 没有找到YYB_SERVER；格式：地址@账号ref，多账号换行", level="error")
                return None
            for entry in yyb_server.splitlines():
                entry = entry.strip()
                if not entry:
                    continue
                try:
                    _server, _ref = self.wechat_code_adapter.parse_entry(entry)
                    # 只保留 YYB_ONLY_REFS 里列出的 ref；空列表 = 全保留
                    if YYB_ONLY_REFS and _ref not in YYB_ONLY_REFS:
                        continue
                    yield entry
                except ValueError as exc:
                    self.log(f"[检查环境变量] 跳过无效配置 {entry!r}: {exc}", level="error")
        except Exception as e:
            self.log(f"[检查环境变量] 发生错误: {str(e)}\n{traceback.format_exc()}", level="error")
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
                self.log(f"[登录] 失败 错误信息: {response_json.get('msg', '未知错误')}", level="warning")
                return False
        except requests.RequestException as e:
            self.log(f"[登录] 发生网络错误: {str(e)}\n{traceback.format_exc()}", level="error")
            return False
        except Exception as e:
            self.log(f"[登录] 发生错误: {str(e)}\n{traceback.format_exc()}", level="error")
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
                self.log(f"[{self.nickname}] 获取用户余额 发生错误: {response_json.get('msg', '未知错误')}", level="warning")
                return None
        except Exception as e:
            self.log(f"[{self.nickname}] 获取用户余额 发生错误: {str(e)}\n{traceback.format_exc()}", level="error")
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
                self.log(f"[{self.nickname}] 获取优惠券列表 发生错误: {response_json.get('msg', '未知错误')}", level="warning")
                return []
        except Exception as e:
            self.log(f"[{self.nickname}] 获取优惠券列表 发生错误: {str(e)}\n{traceback.format_exc()}", level="error")
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
                self.log(f"[{self.nickname}] 领取优惠券成功: {gift_name}")
                return True
            else:
                error_msg = response_json.get('msg', '领取失败，未获取到具体信息')
                self.log(f"[{self.nickname}] 领取优惠券失败: {error_msg}", level="warning")
                return False
        except Exception as e:
            self.log(f"[{self.nickname}] 领取优惠券发生错误: {str(e)}\n{traceback.format_exc()}", level="error")
            return False

    def load_notify(self):
        """
        加载青龙官方 notify 模块。
        使用仓库根共享的 notify.py（脚本同目录有旧副本时优先用它，向后兼容）；
        不存在时依次尝试 CDN 镜像→官方源→ghproxy 下载。
        任何失败都不抛异常（避免在 finally 里报错盖掉真正的业务异常），返回 None。
        """
        notify_path = os.path.join(_NOTIFY_DIR, "notify.py")
        if not os.path.exists(notify_path):
            # 国内直连 raw.githubusercontent.com 常被重置，故 CDN 镜像优先
            urls = [
                "https://cdn.jsdelivr.net/gh/whyour/qinglong@develop/sample/notify.py",
                "https://raw.githubusercontent.com/whyour/qinglong/refs/heads/develop/sample/notify.py",
                "https://ghproxy.net/https://raw.githubusercontent.com/whyour/qinglong/refs/heads/develop/sample/notify.py",
            ]
            for url in urls:
                try:
                    response = requests.get(url, timeout=15)
                    if response.status_code == 200 and "def send" in response.text:
                        with open(notify_path, "wb") as f:
                            f.write(response.content)
                        self.log(f"[通知] notify.py 下载成功（{url.split('/')[2]}）")
                        break
                except Exception as e:
                    self.log(f"[通知] notify.py 下载失败（{url.split('/')[2]}）: {e}", level="warning")
        if not os.path.exists(notify_path):
            return None
        try:
            import notify
            return notify
        except Exception as e:
            self.log(f"[通知] notify.py 导入失败: {e}", level="error")
            return None

    def run(self):
        """
        运行任务
        """
        # 推送摘要：每个账号一行；完整运行日志只留在青龙日志里，不推给第三方渠道
        push_lines = []
        total_accounts = 0
        try:
            self.log(f"【{self.script_name}】开始执行任务")
            entries = list(self.check_env())
            total_accounts = len(entries)
            self.log(f"共 {len(entries)} 个账号待执行")
            if not entries:
                self.log("没有可执行账号（YYB_SERVER 为空或全部被 YYB_ONLY_REFS 过滤）", level="error")
                return
            for index, wx_id in enumerate(entries, 1):
                # 清理账号信息
                self.nickname = f"账号{index}"
                self.token = ""
                self.log("")
                self.log(f"------ 账号{index} 开始执行任务 ------")
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
                    self.log(f"[{self.nickname}] YYB取码失败，跳过该账号", level="error")
                    push_lines.append(f"{self.nickname}: ❌ YYB取码失败")
                    session.close()
                    continue
                token = self.login(session, code)
                if not token:
                    self.log(f"[{self.nickname}] 登录失败，跳过该账号", level="error")
                    push_lines.append(f"{self.nickname}: ❌ 登录失败")
                    session.close()
                    continue
                self.token = token
                session.headers['Session-Token'] = token
                # 校验刚登录的 token（失效则重登一次，仍失败则跳过；全程不落盘）
                if self.get_balance(session) is None:
                    self.log(f"[{self.nickname}] 登录态校验失败，尝试重登一次", level="warning")
                    code = self.wechat_code_adapter.get_code(wx_id)
                    if not code:
                        self.log(f"[{self.nickname}] 授权失败，跳过该账号", level="error")
                        push_lines.append(f"{self.nickname}: ❌ 授权失败")
                        session.close()
                        continue
                    token = self.login(session, code)
                    if not token:
                        self.log(f"[{self.nickname}] 重新登录失败，跳过该账号", level="error")
                        push_lines.append(f"{self.nickname}: ❌ 重新登录失败")
                        session.close()
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
                self.log(f"[{self.nickname}] 当前提现免费券: {self.points}元")
                push_lines.append(f"{self.nickname}: ✅ 提现免费券 {self.points}元")
                # 清理session
                session.close()
                self.log(f"------ 账号{index} 执行任务结束 ------")
            # 不落盘：每次运行的 token 均为临时登录获取，不写入本地文件
        except Exception as e:
            self.log(f"【{self.script_name}】执行过程中发生错误: {str(e)}\n{traceback.format_exc()}", level="error")
        finally:
            if NOTIFY:
                # 推送失败不能影响脚本退出状态，整段兜住
                try:
                    notify = self.load_notify()
                    if notify is None:
                        self.log("[通知] 未找到 notify.py，跳过推送")
                    else:
                        # 只推精简摘要（每账号一行）：第三方渠道有正文长度上限，
                        # 且 PushPlus 免费版在微信里只显示标题，成功数必须进 title。
                        ok_cnt = sum(1 for _l in push_lines if "✅" in _l)
                        if total_accounts:
                            title = f"{self.script_name} {ok_cnt}/{total_accounts} 成功"
                            if ok_cnt < total_accounts:
                                title += f"  ❌{total_accounts - ok_cnt}"
                        else:
                            title = f"{self.script_name} 无账号可执行"
                        content = "\n".join(push_lines) or "无账号可执行，请检查 YYB_SERVER / YYB_ONLY_REFS"
                        notify.send(title, content)
                        self.log(f"[通知] 推送已提交：{title}")
                except Exception as e:
                    self.log(f"[通知] 推送失败: {e}", level="error")

if __name__ == "__main__":
    auto_task = AutoTask("微信支付提现笔笔省")
    auto_task.run()
