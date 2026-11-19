#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WorkBuddy 明文凭据一键刷新工具  v2026-11-19a

用途：本机 WorkBuddy 客户端重新登录后，重新解密凭据并更新青龙容器里的明文凭据文件，
      让青龙里的 88lin workbuddy-auto-signin 任务恢复可用。

背景：最新客户端写出的 accessToken 是 $wbEncrypted 加密信封，解密必须调用本机
      WorkBuddy.exe 的内置 Electron 运行时；青龙容器是 Linux ARM，无此 exe，解不开。
      所以只能在本机解密出明文 token，再推送到青龙。

用法：
    python refresh_workbuddy_auth.py

前置条件：
    1. 本机 WorkBuddy 客户端已登录目标账号（当前 uid 前缀 23ffe369）
    2. D://WorkBuddy//WorkBuddy.exe 存在
    3. ~/.ssh/n1 可免密登录 N1 (192.168.31.233)

安全说明：
    - 全程不打印 accessToken 明文，只打印 uid 前缀、token 长度与前缀
    - 本机临时文件用后即删；N1 宿主 /tmp 副本也会删除
    - 容器目标文件权限 600
"""
import os, re, sys, json, base64, subprocess, tempfile

VERSION = "2026-11-19a"

# ---- 可调配置 ----
EXE = r"D:\WorkBuddy\WorkBuddy.exe"
N1 = "root@192.168.31.233"
SSH_KEY = os.path.join(os.path.expanduser("~"), ".ssh", "n1")
REMOTE_TMP = "/tmp/workbuddy-auth-plaintext.json"
CONTAINER_AUTH = "/ql/data/config/workbuddy-auth.json"
HELPER_TIMEOUT = 15

# 解密助手 JS（base64 内嵌，源自 88lin/workbuddy-auto-signin signin.py）
AUTH_HELPER_JS = base64.b64decode("Cid1c2Ugc3RyaWN0JzsKY29uc3QgY3J5cHRvID0gcmVxdWlyZSgnY3J5cHRvJyk7CmNvbnN0IGlucHV0TGltaXQgPSA2NTUzNjsKY29uc3QgZmFpbHVyZSA9IHJlYXNvbiA9PiB7IHRocm93IHtyZWFzb259OyB9Owpjb25zdCBvYmplY3QgPSB4ID0+IHggIT09IG51bGwgJiYgdHlwZW9mIHggPT09ICdvYmplY3QnICYmICFBcnJheS5pc0FycmF5KHgpOwpmdW5jdGlvbiBiYXNlNjQodmFsdWUsIGxlbmd0aCkgewogIGlmICh0eXBlb2YgdmFsdWUgIT09ICdzdHJpbmcnIHx8IHZhbHVlLmxlbmd0aCA+IGlucHV0TGltaXQpIGZhaWx1cmUoJ0lOVkFMSURfRk9STUFUJyk7CiAgY29uc3QgYnl0ZXMgPSBCdWZmZXIuZnJvbSh2YWx1ZSwgJ2Jhc2U2NCcpOwogIGlmIChieXRlcy50b1N0cmluZygnYmFzZTY0JykgIT09IHZhbHVlIHx8IChsZW5ndGggIT09IHVuZGVmaW5lZCAmJiBieXRlcy5sZW5ndGggIT09IGxlbmd0aCkpCiAgICBmYWlsdXJlKCdJTlZBTElEX0ZPUk1BVCcpOwogIHJldHVybiBieXRlczsKfQpmdW5jdGlvbiB1dGY4KGJ5dGVzKSB7CiAgY29uc3QgdGV4dCA9IGJ5dGVzLnRvU3RyaW5nKCd1dGY4Jyk7CiAgaWYgKCFCdWZmZXIuZnJvbSh0ZXh0LCAndXRmOCcpLmVxdWFscyhieXRlcykpIGZhaWx1cmUoJ0lOVkFMSURfRk9STUFUJyk7CiAgcmV0dXJuIHRleHQ7Cn0KZnVuY3Rpb24gZGVjb2RlKHZhbHVlKSB7CiAgaWYgKCFvYmplY3QodmFsdWUpIHx8IE9iamVjdC5rZXlzKHZhbHVlKS5zb3J0KCkuam9pbignLCcpICE9PSAnJHdiRW5jcnlwdGVkLGVudmVsb3BlJyB8fCB2YWx1ZS4kd2JFbmNyeXB0ZWQgIT09IDEpCiAgICBmYWlsdXJlKCdVTlNVUFBPUlRFRF9FTlZFTE9QRScpOwogIGxldCBlbnZlbG9wZTsKICB0cnkgeyBlbnZlbG9wZSA9IEpTT04ucGFyc2UodXRmOChiYXNlNjQodmFsdWUuZW52ZWxvcGUpKSk7IH0KICBjYXRjaCAoZSkgeyBmYWlsdXJlKGUucmVhc29uIHx8ICdJTlZBTElEX0ZPUk1BVCcpOyB9CiAgaWYgKCFvYmplY3QoZW52ZWxvcGUpIHx8ICFOdW1iZXIuaXNJbnRlZ2VyKGVudmVsb3BlLnN1aXRlKSkgZmFpbHVyZSgnSU5WQUxJRF9GT1JNQVQnKTsKICBpZiAoZW52ZWxvcGUuc3VpdGUgIT09IDEpIGZhaWx1cmUoJ1VOU1VQUE9SVEVEX0VOVkVMT1BFJyk7CiAgaWYgKE9iamVjdC5rZXlzKGVudmVsb3BlKS5zb3J0KCkuam9pbignLCcpICE9PSAnYXV0aFRhZyxjaXBoZXJ0ZXh0LGtleUlkLG5vbmNlLHN1aXRlJyB8fAogICAgICB0eXBlb2YgZW52ZWxvcGUua2V5SWQgIT09ICdzdHJpbmcnIHx8ICEvXlswLTlhLWZdezE2fSQvLnRlc3QoZW52ZWxvcGUua2V5SWQpKSBmYWlsdXJlKCdJTlZBTElEX0ZPUk1BVCcpOwogIHJldHVybiB7a2V5SWQ6IGVudmVsb3BlLmtleUlkLCBub25jZTogYmFzZTY0KGVudmVsb3BlLm5vbmNlLCAxMiksCiAgICB0YWc6IGJhc2U2NChlbnZlbG9wZS5hdXRoVGFnLCAxNiksIGNpcGhlcnRleHQ6IGJhc2U2NChlbnZlbG9wZS5jaXBoZXJ0ZXh0KX07Cn0KZnVuY3Rpb24gbmF0aXZlU3RvcmFnZSgpIHsKICB0cnkgewogICAgY29uc3Qgc3RvcmFnZSA9IHByb2Nlc3MuX2xpbmtlZEJpbmRpbmcoJ2VsZWN0cm9uX2Jyb3dzZXJfd29ya2J1ZGR5X3N0b3JhZ2UnKTsKICAgIGlmICh0eXBlb2Ygc3RvcmFnZS5sb2dnZXJHZXQgIT09ICdmdW5jdGlvbicpIGZhaWx1cmUoJ1JVTlRJTUVfVU5BVkFJTEFCTEUnKTsKICAgIHJldHVybiBzdG9yYWdlOwogIH0gY2F0Y2ggKF8pIHsgZmFpbHVyZSgnUlVOVElNRV9VTkFWQUlMQUJMRScpOyB9Cn0KZnVuY3Rpb24gZGVjcnlwdChlbnZlbG9wZSkgewogIGxldCBwYXlsb2FkOwogIHRyeSB7IHBheWxvYWQgPSBKU09OLnBhcnNlKG5hdGl2ZVN0b3JhZ2UoKS5sb2dnZXJHZXQoKSk7IH0KICBjYXRjaCAoXykgeyBmYWlsdXJlKCdSVU5USU1FX1VOQVZBSUxBQkxFJyk7IH0KICBsZXQga2V5OwogIGxldCBwbGFpbnRleHQ7CiAgdHJ5IHsKICAgIGlmICghb2JqZWN0KHBheWxvYWQpIHx8IHBheWxvYWQudmVyc2lvbiAhPT0gMSkgZmFpbHVyZSgnUlVOVElNRV9VTkFWQUlMQUJMRScpOwogICAgbGV0IHNlY3JldDsKICAgIHRyeSB7IHNlY3JldCA9IGJhc2U2NChwYXlsb2FkLmF0UmVzdFNlY3JldEtleSwgMzIpOyB9CiAgICBjYXRjaCAoXykgeyBmYWlsdXJlKCdSVU5USU1FX1VOQVZBSUxBQkxFJyk7IH0KICAgIGNvbnN0IGVtcHR5ID0gc2VjcmV0LmV2ZXJ5KGIgPT4gYiA9PT0gMCk7CiAgICBzZWNyZXQuZmlsbCgwKTsKICAgIGlmIChlbXB0eSkgZmFpbHVyZSgnUlVOVElNRV9VTkFWQUlMQUJMRScpOwogICAga2V5ID0gY3J5cHRvLmNyZWF0ZUhhc2goJ3NoYTI1NicpLnVwZGF0ZShwYXlsb2FkLmF0UmVzdFNlY3JldEtleSwgJ3V0ZjgnKS5kaWdlc3QoKTsKICAgIHBheWxvYWQgPSBudWxsOwogICAgaWYgKGNyeXB0by5jcmVhdGVIYXNoKCdzaGEyNTYnKS51cGRhdGUoa2V5KS5kaWdlc3QoJ2hleCcpLnNsaWNlKDAsIDE2KSAhPT0gZW52ZWxvcGUua2V5SWQpCiAgICAgIGZhaWx1cmUoJ0tFWV9NSVNNQVRDSCcpOwogICAgY29uc3QgbHAgPSBzID0+IHsKICAgICAgY29uc3QgYnl0ZXMgPSBCdWZmZXIuZnJvbShzLCAndXRmOCcpOwogICAgICBjb25zdCBsZW5ndGggPSBCdWZmZXIuYWxsb2MoNCk7IGxlbmd0aC53cml0ZVVJbnQzMkJFKGJ5dGVzLmxlbmd0aCk7CiAgICAgIHJldHVybiBCdWZmZXIuY29uY2F0KFtsZW5ndGgsIGJ5dGVzXSk7CiAgICB9OwogICAgY29uc3QgYWFkID0gQnVmZmVyLmNvbmNhdChbQnVmZmVyLmZyb20oJ1dCLUFBRFwwJywgJ2FzY2lpJyksIEJ1ZmZlci5mcm9tKFsxXSksCiAgICAgIGxwKCdXQkVWMScpLCBscCgnc3ltLXYxJyksIEJ1ZmZlci5mcm9tKFswLCAwLCAwLCAxXSksIGxwKGVudmVsb3BlLmtleUlkKSwgQnVmZmVyLmZyb20oWzIsIDAsIDBdKV0pOwogICAgdHJ5IHsKICAgICAgY29uc3QgY2lwaGVyID0gY3J5cHRvLmNyZWF0ZURlY2lwaGVyaXYoJ2Flcy0yNTYtZ2NtJywga2V5LCBlbnZlbG9wZS5ub25jZSwge2F1dGhUYWdMZW5ndGg6IDE2fSk7CiAgICAgIGNpcGhlci5zZXRBQUQoYWFkKTsgY2lwaGVyLnNldEF1dGhUYWcoZW52ZWxvcGUudGFnKTsKICAgICAgcGxhaW50ZXh0ID0gQnVmZmVyLmNvbmNhdChbY2lwaGVyLnVwZGF0ZShlbnZlbG9wZS5jaXBoZXJ0ZXh0KSwgY2lwaGVyLmZpbmFsKCldKTsKICAgIH0gY2F0Y2ggKF8pIHsgZmFpbHVyZSgnREVDUllQVF9GQUlMRUQnKTsgfQogICAgY29uc3QgdG9rZW4gPSB1dGY4KHBsYWludGV4dCk7CiAgICBpZiAoIXRva2VuLmxlbmd0aCB8fCB0b2tlbi5sZW5ndGggPiAzMjc2OCB8fCAhL15bQS1aYS16MC05Ll9+K1wvLV0rPSokLy50ZXN0KHRva2VuKSkKICAgICAgZmFpbHVyZSgnSU5WQUxJRF9GT1JNQVQnKTsKICAgIHJldHVybiB0b2tlbjsKICB9IGZpbmFsbHkgewogICAgaWYgKGtleSkga2V5LmZpbGwoMCk7CiAgICBpZiAocGxhaW50ZXh0KSBwbGFpbnRleHQuZmlsbCgwKTsKICB9Cn0KbGV0IGNodW5rcyA9IFtdLCBzaXplID0gMDsKZnVuY3Rpb24gcmVwbHkodmFsdWUpIHsKICBwcm9jZXNzLnN0ZG91dC53cml0ZShKU09OLnN0cmluZ2lmeSh7dmVyc2lvbjogMSwgLi4udmFsdWV9KSwgKCkgPT4gcHJvY2Vzcy5leGl0KHZhbHVlLm9rID8gMCA6IDEpKTsKfQpwcm9jZXNzLnN0ZGluLm9uKCdkYXRhJywgY2h1bmsgPT4gewogIHNpemUgKz0gY2h1bmsubGVuZ3RoOwogIGlmIChzaXplID4gaW5wdXRMaW1pdCkgcmVwbHkoe29rOiBmYWxzZSwgcmVhc29uOiAnSU5WQUxJRF9GT1JNQVQnfSk7CiAgZWxzZSBjaHVua3MucHVzaChjaHVuayk7Cn0pOwpwcm9jZXNzLnN0ZGluLm9uKCdlcnJvcicsICgpID0+IHJlcGx5KHtvazogZmFsc2UsIHJlYXNvbjogJ0hFTFBFUl9QUk9UT0NPTCd9KSk7CnByb2Nlc3Muc3RkaW4ub24oJ2VuZCcsICgpID0+IHsKICB0cnkgewogICAgY29uc3QgcmVxdWVzdCA9IEpTT04ucGFyc2UodXRmOChCdWZmZXIuY29uY2F0KGNodW5rcykpKTsgY2h1bmtzID0gW107CiAgICBpZiAoIW9iamVjdChyZXF1ZXN0KSB8fCByZXF1ZXN0LnZlcnNpb24gIT09IDEpIGZhaWx1cmUoJ0hFTFBFUl9QUk9UT0NPTCcpOwogICAgaWYgKHJlcXVlc3Qub3BlcmF0aW9uID09PSAncHJvYmUnKSB7CiAgICAgIG5hdGl2ZVN0b3JhZ2UoKTsKICAgICAgaWYgKCFjcnlwdG8uZ2V0Q2lwaGVycygpLmluY2x1ZGVzKCdhZXMtMjU2LWdjbScpKSBmYWlsdXJlKCdSVU5USU1FX1VOQVZBSUxBQkxFJyk7CiAgICAgIHJlcGx5KHtvazogdHJ1ZSwgZWxlY3Ryb246IHByb2Nlc3MudmVyc2lvbnMuZWxlY3Ryb24gfHwgJ3Vua25vd24nfSk7CiAgICB9IGVsc2UgaWYgKHJlcXVlc3Qub3BlcmF0aW9uID09PSAnZGVjcnlwdCcpIHsKICAgICAgcmVwbHkoe29rOiB0cnVlLCBhY2Nlc3NUb2tlbjogZGVjcnlwdChkZWNvZGUocmVxdWVzdC52YWx1ZSkpfSk7CiAgICB9IGVsc2UgZmFpbHVyZSgnSEVMUEVSX1BST1RPQ09MJyk7CiAgfSBjYXRjaCAoZSkgewogICAgY29uc3QgcmVhc29ucyA9IFsnSU5WQUxJRF9GT1JNQVQnLCdVTlNVUFBPUlRFRF9FTlZFTE9QRScsJ1JVTlRJTUVfVU5BVkFJTEFCTEUnLAogICAgICAnS0VZX01JU01BVENIJywnREVDUllQVF9GQUlMRUQnLCdIRUxQRVJfUFJPVE9DT0wnXTsKICAgIHJlcGx5KHtvazogZmFsc2UsIHJlYXNvbjogcmVhc29ucy5pbmNsdWRlcyhlLnJlYXNvbikgPyBlLnJlYXNvbiA6ICdIRUxQRVJfUFJPVE9DT0wnfSk7CiAgfQp9KTsK").decode("utf-8")


def find_cred():
    home = os.path.expanduser("~")
    local = os.environ.get("LOCALAPPDATA") or os.path.join(home, "AppData", "Local")
    rel = os.path.join("CodeBuddyExtension", "Data", "Public", "auth", "workbuddy-desktop.info")
    for p in (os.path.join(local, rel),
              os.path.join(home, ".workbuddy", "auth", "workbuddy-desktop.info")):
        if os.path.isfile(p):
            return p
    raise SystemExit("[FAIL] 未找到 workbuddy-desktop.info，请确认本机客户端已登录")


def valid_token(t):
    return isinstance(t, str) and re.fullmatch(r"[A-Za-z0-9._~+/-]+=*", t) is not None


def decrypt(envelope):
    if not os.path.isfile(EXE):
        raise SystemExit("[FAIL] 客户端 exe 不存在: " + EXE)
    req = json.dumps({"version": 1, "operation": "decrypt", "value": envelope},
                     ensure_ascii=True).encode("ascii")
    env = {k: v for k, v in os.environ.items()
           if not k.upper().startswith(("NODE_", "ELECTRON_", "WORKBUDDY_"))}
    env["ELECTRON_RUN_AS_NODE"] = "1"
    try:
        p = subprocess.run([EXE, "-e", AUTH_HELPER_JS], input=req,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           env=env, timeout=HELPER_TIMEOUT,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        raise SystemExit("[FAIL] 客户端解密助手超时")
    try:
        reply = json.loads(p.stdout.decode("utf-8"))
    except Exception:
        raise SystemExit("[FAIL] 解密助手返回无法解析: " + p.stderr[:200].decode("utf-8", "replace"))
    if reply.get("ok") is not True or p.returncode != 0:
        raise SystemExit("[FAIL] 解密失败: " + str(reply.get("reason")))
    tok = reply.get("accessToken")
    if not valid_token(tok):
        raise SystemExit("[FAIL] 解密结果 token 格式非法")
    return tok


def run_local(cmd):
    r = subprocess.run(cmd)
    if r.returncode != 0:
        raise SystemExit("[FAIL] 本地命令失败: " + " ".join(cmd[:3]))


def run_ssh(args):
    cmd = ["ssh", "-i", SSH_KEY, "-o", "StrictHostKeyChecking=no",
           "-o", "ConnectTimeout=10", N1] + args
    r = subprocess.run(cmd)
    if r.returncode != 0:
        raise SystemExit("[FAIL] ssh 命令失败: " + " ".join(args)[:80])


def main():
    print("WorkBuddy 明文凭据刷新 v" + VERSION)
    cred = find_cred()
    print("[1/5] 凭据文件:", cred)
    with open(cred, encoding="utf-8") as f:
        sess = json.load(f)
    uid = (sess.get("account") or {}).get("uid")
    token = (sess.get("auth") or {}).get("accessToken")
    if not uid or not token:
        raise SystemExit("[FAIL] 凭据缺 accessToken 或 uid")
    if valid_token(token):
        print("[2/5] token 已是明文，跳过解密")
    else:
        print("[2/5] token 是加密信封，调用本机客户端解密...")
        token = decrypt(token)
    out = {"auth": {"accessToken": token}, "account": {"uid": uid}}
    for src, dst, keys in ((sess["auth"], out["auth"], ("domain", "endpoint")),
                           (sess["account"], out["account"], ("enterpriseId",))):
        for k in keys:
            if src.get(k):
                dst[k] = src[k]
    print("[3/5] 明文凭据已生成, uid=" + uid[:10] + ", token_len=" + str(len(token)))

    tf = tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json", delete=False)
    json.dump(out, tf, ensure_ascii=False)
    tf.close()
    try:
        print("[4/5] 推送到 N1 青龙容器...")
        run_local(["scp", "-i", SSH_KEY, "-o", "StrictHostKeyChecking=no",
                   tf.name, N1 + ":" + REMOTE_TMP])
        run_ssh(["docker cp %s qinglong:%s && docker exec qinglong chmod 600 %s && rm -f %s"
                 % (REMOTE_TMP, CONTAINER_AUTH, CONTAINER_AUTH, REMOTE_TMP)])
        print("[5/5] 容器内校验...")
        run_ssh(["docker exec qinglong python3 -c "
                 "\"import json;d=json.load(open('%s'));"
                 "print('uid='+d['account']['uid'][:10],"
                 "'token_len='+str(len(d['auth']['accessToken'])),"
                 "'prefix='+d['auth']['accessToken'][:6]+'...')\"" % CONTAINER_AUTH])
    finally:
        os.unlink(tf.name)
    print("[OK] 完成。青龙里的明文凭据已更新。")


if __name__ == "__main__":
    main()
