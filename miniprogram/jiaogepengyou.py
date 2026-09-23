# =========================================================
# name: 交个朋友积分签到
# cron: 24 7,16 * * *
# =========================================================
#
# 任务流程：
#   1. 读取账号：优先用 IYOUKE_TOKEN（手动 bearer）；否则读 YYB_SERVER + YYB_ONLY_REFS 经 YYBGO
#   2. 调用 YYBGO 的 /wxapp/getCode 获取 wx.login code
#   3. 用 code 请求 appLogin 换取 access_token（每次运行重新登录，不落盘）
#   4. 执行签到 / 积分任务，输出汇总并发送通知
# 可控参数：
#   IYOUKE_TOKEN    可选。手动 bearer token，空格分隔多账号，优先级最高（免 YYB）
#   YYB_SERVER      必填（无 IYOUKE_TOKEN 时）。格式「地址@ref」，空格/换行分隔
#   YYB_ONLY_REFS   白名单常量。留空 [] 跑全部；填 ["1","2"] 只跑对应 ref
#   IYOUKE_NOTIFY   通知开关，默认开启；填 0/false/off/no 关闭
#   IYOUKE_APP_ID   可选。小程序 AppID，默认 wx3b294e7a0ba29bc3
#   IYOUKE_VERSION  可选。接口版本号，默认 3.5.4
#
# =========================================================
#!/usr/bin/env python3
# -*- coding: utf-8 -*-


import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# notify.py 可能位于仓库根（脚本父目录），确保可导入
_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
for _p in (str(_HERE), str(_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# ===================== 通知推送 =====================
try:
    from notify import send as notify_send
except ImportError:
    def notify_send(title, content):
        print(f"--- 通知 ---\n{title}\n{content}\n-------------")

# ===================== 调试开关 =====================
DEBUG = False
DEBUG_ENV = {
    "IYOUKE_TOKEN": "85f3ca0b55c54f18939e16e76012f93f1790164361106",
}
if DEBUG:
    for _k, _v in DEBUG_ENV.items():
        os.environ.setdefault(_k, _v)
    print("⚠️ 调试模式已开启（DEBUG=True），使用脚本内置 DEBUG_ENV 配置")
# ===================================================

# ===================== 环境变量控制（置顶） =====================
# ① 手动 token 兜底（可选，优先级最高，免 YYB）：直接填 bearer token，空格分隔多账号
IYOUKE_TOKEN = os.getenv("IYOUKE_TOKEN", "").strip()
# ② 配合 YYBGO 的账号基座（必填其一）：每项 "地址@ref"，空格/换行分隔，多账号
YYB_SERVER_RAW = os.getenv("YYB_SERVER", "").strip()
# ③ YYB 只跑这些 ref 的白名单过滤器：留空 = 跑 YYB_SERVER 全部账号；填 ["1","2"] = 只跑这些
YYB_ONLY_REFS = []   # 顶部常量兜底；环境变量 YYB_ONLY_REFS 可覆盖
# 通知开关：默认开；填 0/false/off/no 关闭
IYOUKE_NOTIFY = os.getenv("IYOUKE_NOTIFY", "1").strip().lower() not in ("0", "false", "off", "no")
# 接口参数（可选环境变量覆盖；缺省用抓包所得默认值）
APP_ID = os.getenv("IYOUKE_APP_ID", "wx3b294e7a0ba29bc3").strip()
APP_VERSION = os.getenv("IYOUKE_VERSION", "3.5.4").strip()
# ===================================================

SCRIPT_NAME = "交个朋友积分签到"

# ===================== 接口常量 =====================
API_BASE = "https://smp-api.iyouke.com"
ENV_VERSION = "release"
REFERER = f"https://servicewechat.com/{APP_ID}/101/page-frame.html"
XY_EXTRA = f"appid={APP_ID};version={APP_VERSION};envVersion={ENV_VERSION};senceId=1005"
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
        "xy-extra-data": f"appid={APP_ID};version={APP_VERSION};envVersion={ENV_VERSION};senceId=1106",
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
    raw_filter = os.getenv("YYB_ONLY_REFS")
    filters = _split_accounts(raw_filter) if raw_filter else list(YYB_ONLY_REFS)
    if filters:
        want = {f.strip() for f in filters if f.strip()}
        base = [e for e in base if e[1] in want]
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
        print(f"⚠️ 请求失败 {path}: {e}")
        return None


def sign_one(token: str) -> Dict[str, Any]:
    today = time.strftime("%Y/%m/%d")
    resp = _req_get(token, "/dtapi/pointsSign/user/sign", params={"date": today})
    result: Dict[str, Any] = {"ok": False, "signed": False, "reward": 0,
                              "total": None, "msg": ""}
    if not resp:
        result["msg"] = "网络/解析失败"
        return result
    errmsg = str(resp.get("errorMsg") or resp.get("error_msg")
                 or resp.get("msg") or resp.get("message") or "")
    if resp.get("success") is True and resp.get("error") == 0:
        data = resp.get("data") or {}
        reward = int(data.get("signReward") or 0)
        result["reward"] = reward
        if reward > 0:
            result["ok"] = True
            result["msg"] = f"签到+{reward}积分"
        else:
            result["signed"] = True  # signReward=0 视为今日已签
            result["msg"] = "今日已签"
    elif "已签" in errmsg or "重复" in errmsg:
        result["signed"] = True  # 服务端明确"已签到/重复签到" → 视为今日已签，不算失败
        result["msg"] = "今日已签"
    else:
        result["msg"] = errmsg or str(resp)
    # 查总积分（仅用于展示）
    p = _req_get(token, "/dtapi/pointsSign/user/pointsInfo/query")
    if p and p.get("success") and p.get("data"):
        result["total"] = int(p["data"].get("pointsNums") or 0)
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
    any_fail = False
    for idx, acc in enumerate(accounts, 1):
        print(f"--- 账号 {idx} ({acc['remark']}) ---")
        # 取 token
        if acc["mode"] == "manual":
            token = acc["token"]
            print("  使用手动 token")
        else:
            try:
                code = YYBClient(APP_ID).get_code(acc["server"], acc["ref"])
                token = app_login(code)
                print(f"  YYBGO 取码+登录成功（token 前6位 {token[:6]}…）")
            except Exception as e:
                print(f"  ❌ 登录失败: {e}")
                results.append({"ok": False, "signed": False, "reward": 0,
                                "total": None, "msg": f"登录失败: {e}",
                                "idx": idx, "remark": acc["remark"]})
                any_fail = True
                continue
        res = sign_one(token)
        print(f"  {res['msg']}"
              + (f"，总积分 {res['total']}" if res["total"] is not None else ""))
        results.append({"ok": res["ok"], "signed": res["signed"],
                        "reward": res["reward"], "total": res["total"],
                        "msg": res["msg"], "idx": idx, "remark": acc["remark"]})
        if not (res["ok"] or res["signed"]):
            any_fail = True

    success = sum(1 for r in results if r["ok"])
    signed = sum(1 for r in results if r["signed"])
    title = f"{SCRIPT_NAME}｜成功 {success}/{len(results)}（已签 {signed}）"
    lines = []
    for r in results:
        if r["ok"]:
            parts = [f"签到+{r['reward']}积分"]
            if r["total"] is not None:
                parts.append(f"总积分{r['total']}")
            lines.append(f"✅ [{r['remark']}] {'，'.join(parts)}")
        elif r["signed"]:
            lines.append(f"🔄 [{r['remark']}] 今日已签")
        else:
            lines.append(f"❌ [{r['remark']}] {r['msg']}")
    if IYOUKE_NOTIFY:
        try:
            notify_send(title, "\n".join(lines))
        except Exception as e:
            print(f"⚠️ 推送失败: {e}")
    else:
        print("（通知已关闭 IYOUKE_NOTIFY=0，跳过推送）")
    print("──── 交个朋友 执行汇总 ────")
    print(f"\n{title}\n" + "\n".join(lines))
    return 1 if any_fail else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print(f"❌ 脚本异常: {e}")
        import traceback
        traceback.print_exc()
        if IYOUKE_NOTIFY:
            try:
                notify_send(f"{SCRIPT_NAME} 异常", str(e))
            except Exception:
                pass
        sys.exit(1)
