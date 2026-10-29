# -*- coding: utf-8 -*-
"""全仓脚本版本号管理工具。

用法:
  python tools/version_tool.py --list          列出所有脚本的版本
  python tools/version_tool.py --bump          所有脚本版本 +1
  python tools/version_tool.py --bump <文件>   指定文件版本 +1
  python tools/version_tool.py --init 2026-10-29a   给缺版本的脚本补初始版本
  python tools/version_tool.py --set 2026-10-29a    把全仓统一成指定版本

版本号统一用日期型, 可自动递增:
  2026-10-29a -> 2026-10-29b (同日字母递增, 超过 z 回到 a)
  跨日再 bump -> 自动切到新日期 + a (如 2026-10-29a -> 2026-10-30a)

版本行统一写在头部注释块里, 格式:
  Python:  # version: 2026-10-29a
  JS:     # version: 2026-10-29a

脚本里若存在 SCRIPT_VER / SCRIPT_VERSION 常量, 会与头部版本行同步改写,
保证「日志里打印的版本」与「头部版本注释」永远一致。
"""
import argparse
import datetime
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKIP_DIRS = {"node_modules", ".git", "tools", "__pycache__", ".workbuddy"}

# 行尾用前瞻 (?=\r?$) 而不是消费 \r: 仓库脚本多为 CRLF,
# 消费掉 \r 会在替换版本行时把该行变成 LF, 造成混合行尾。
VER_LINE = re.compile(r"^(?P<pre>[ \t]*(?:#|//)[ \t]*)版本[ \t]*:[ \t]*(?P<val>\S+)[ \t]*(?=\r?$)", re.M)
VER_LINE2 = re.compile(r"^(?P<pre>[ \t]*(?:#|//)[ \t]*)version[ \t]*:[ \t]*(?P<val>\S+)[ \t]*(?=\r?$)", re.M)
CONST_VER = re.compile(r"""(?:SCRIPT_?VER(?:SION)?)\s*=\s*["']([^"']+)["']""")
DOC_VER = re.compile(r"^version:\s*(\S+)", re.M)
NAME_LINE = re.compile(r"^(?P<pre>[ \t]*(?:#|//)[ \t]*)name[ \t]*:", re.M)
SHEBANG = re.compile(r"^#!.*\n")


def iter_scripts():
    for base, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for fn in files:
            if fn.endswith((".py", ".js")):
                yield os.path.join(base, fn)


def read_bytes(path):
    with open(path, "rb") as f:
        return f.read()


def decode(data):
    if data[:3] == b"\xef\xbb\xbf":
        return data[3:].decode("utf-8"), True
    return data.decode("utf-8"), False


def encode(text, bom):
    raw = text.encode("utf-8")
    return (b"\xef\xbb\xbf" + raw) if bom else raw


def current_version(text):
    """从文本里提取当前版本号, 返回 (版本, 来源) 或 (None, None)。

    优先级: 头部 version: 行 > 头部 版本: 行 > 运行时常量 > docstring。
    头部用英文 `version:` 是全仓统一格式, 中文「版本:」多为更新日志里的历史记录。
    """
    m = VER_LINE2.search(text)
    if m:
        return m.group("val"), "header"
    m = VER_LINE.search(text)
    if m:
        return m.group("val"), "header"
    m = CONST_VER.search(text)
    if m:
        return m.group(1), "const"
    m = DOC_VER.search(text)
    if m:
        return m.group(1), "doc"
    return None, None


def bump_version(ver):
    """版本号 +1。只支持日期型 YYYY-MM-DDx: 同日字母递增, 跨日切到新日期 + a。"""
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})([a-z]?)$", ver or "")
    if not m:
        return ver
    y, mo, d, letter = m.groups()
    if letter and letter < "z":
        return "%s-%s-%s%s" % (y, mo, d, chr(ord(letter) + 1))
    today = datetime.date.today()
    return "%s-%s-%sa" % (today.year, "%02d" % today.month, "%02d" % today.day)


def sync_const(text, ver):
    """把 SCRIPT_VER / SCRIPT_VERSION 常量的值同步成 ver。返回 (新文本, 是否改动)。"""
    m = CONST_VER.search(text)
    if not m or m.group(1) == ver:
        return text, False
    new = text[:m.start(1)] + ver + text[m.end(1):]
    return new, True


def set_version(text, ver):
    """把版本写进头部注释块: 有则规范化, 无则插入。返回 (新文本, 是否改动)。"""
    m = VER_LINE.search(text)
    if m:
        new = text[:m.start()] + "%sversion: %s" % (m.group("pre"), ver) + text[m.end():]
        return new, True
    m = VER_LINE2.search(text)
    if m:
        new = text[:m.start()] + "%sversion: %s" % (m.group("pre"), ver) + text[m.end():]
        return new, True
    eol = "\r\n" if "\r\n" in text else "\n"
    # 插到 name 行之后
    m = NAME_LINE.search(text)
    if m:
        line_end = text.index("\n", m.end()) + 1
        pre = m.group("pre")
        new = text[:line_end] + "%sversion: %s%s" % (pre, ver, eol) + text[line_end:]
        return new, True
    # 没有 name 块: Python 插 shebang 之后, JS 插文件最前
    m = SHEBANG.search(text)
    if m:
        new = text[:m.end()] + "# version: %s%s" % (ver, eol) + text[m.end():]
        return new, True
    new = "// version: %s%s" % (ver, eol) + text
    return new, True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true", help="列出所有脚本版本")
    ap.add_argument("--bump", nargs="?", const="__all__", help="版本 +1")
    ap.add_argument("--init", metavar="VER", help="给缺版本的脚本补初始版本")
    ap.add_argument("--set", dest="setver", metavar="VER", help="把全仓统一成指定版本")
    args = ap.parse_args()

    scripts = sorted(iter_scripts())
    if not (args.list or args.bump or args.init or args.setver):
        args.list = True

    changed = []
    for path in scripts:
        rel = os.path.relpath(path, ROOT)
        text, bom = decode(read_bytes(path))
        ver, src = current_version(text)

        if args.setver:
            # 全仓统一版本: 头部版本行 + 运行时常量一起改
            new_text, _ = set_version(text, args.setver)
            new_text, _ = sync_const(new_text, args.setver)
            if new_text != text:
                with open(path, "wb") as f:
                    f.write(encode(new_text, bom))
                changed.append((rel, ver, args.setver, "统一"))
            continue

        if args.init:
            # 已有版本的: 值不变, 只把版本行规范化到头部; 缺版本的: 用 init 值补上
            target = ver if ver else args.init
            new_text, _ = set_version(text, target)
            new_text, _ = sync_const(new_text, target)
            if new_text != text:
                with open(path, "wb") as f:
                    f.write(encode(new_text, bom))
                changed.append((rel, ver, target, "规范化" if ver else "新增"))
            continue

        if args.bump:
            target = args.bump
            if target != "__all__" and os.path.abspath(target) != os.path.abspath(path):
                continue
            if ver is None:
                continue
            nv = bump_version(ver)
            if nv == ver:
                continue
            new_text, _ = set_version(text, nv)
            new_text, _ = sync_const(new_text, nv)
            with open(path, "wb") as f:
                f.write(encode(new_text, bom))
            changed.append((rel, ver, nv, "递增"))
            continue

        if args.list:
            print("%-46s %-18s %s" % (rel, ver or "(无)", src or ""))

    if changed:
        print()
        print("已变更 %d 个文件:" % len(changed))
        for rel, old, new, act in changed:
            print("  %-46s %s -> %s  (%s)" % (rel, old or "-", new, act))


if __name__ == "__main__":
    main()
