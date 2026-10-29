# -*- coding: utf-8 -*-
"""按 paid.js 的日志模板统一全仓脚本的日志输出。

模板要素（来自 miniprogram/normal/paid.js）:
  1. 统一前缀     [LEVEL] [TAG] message
  2. 账号分隔线   '=' * 70
  3. 账号标题行   👤 账号 i/N ｜ 备注：xxx
  4. 执行汇总行   📊 [执行汇总] <站点> · 账号 N ｜ 成功 X ｜ 失败 Y
  5. 启动信息行   📌 脚本版本: <version> ｜ <时间>

改造原则（安全优先）:
  · 只做**机械可判定**的改动, 不碰业务逻辑与结果计数
  · 已有 emit 四件套的脚本: 只补缺失的分隔线/启动行, 不动它自己的 print/log
  · 没有 emit 的脚本: 注入 [LEVEL] [TAG] 四件套, 并把裸 print 收敛到 log/warn/err
  · 已有 [执行汇总] 行的脚本: **一律不改**(格式已与模板一致)
  · 没有 [执行汇总] 行的脚本: 不注入死函数, 保持原样(需要改业务计数逻辑, 人工处理)

用法:
  python tools/log_template.py --report     看各脚本现状
  python tools/log_template.py --dry-run    预览改动, 不落盘
  python tools/log_template.py              执行改造
  python tools/log_template.py --only <相对路径>   只处理单个文件
"""
import argparse
import datetime
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKIP_DIRS = {"node_modules", ".git", "tools", "__pycache__", ".workbuddy"}

# 每个脚本的日志标签：沿用各自现有缩写
TAGS = {
    "app/chinaUnicom.py": "UNICOM",
    "app/jd_cookie.py": "JD",
    "app/oclean_sign.py": "OCLEAN",
    "app/skland_sign.py": "SKLAND",
    "app/xiaoheihe.py": "XHH",
    "app/xiaoheihe_qrlogin.py": "XHHQR",
    "forum/hktv8.py": "HKTV",
    "forum/southplus.py": "SPLUS",
    "forum/wnflb2023.js": "WNFLB",
    "miniprogram/normal/QC.py": "QC",
    "miniprogram/normal/StormCrew_code.py": "STORM",
    "miniprogram/normal/bjyhuishou.py": "BJYH",
    "miniprogram/normal/cdf.py": "CDF",
    "miniprogram/normal/changhong.py": "CH",
    "miniprogram/normal/chuangwei.js": "CHUANGWEI",
    "miniprogram/normal/dikanong_code.py": "DIKA",
    "miniprogram/normal/exijiu_jph.js": "EXIJIU",
    "miniprogram/normal/guyu_code.py": "GY",
    "miniprogram/normal/haixinaj.js": "HXAJ",
    "miniprogram/normal/haluocx.py": "HLOCX",
    "miniprogram/normal/hrt.py": "HRT",
    "miniprogram/normal/huishouyuan.py": "HSY",
    "miniprogram/normal/jdkuaidi.py": "JDKD",
    "miniprogram/normal/jialichuang.py": "JLC",
    "miniprogram/normal/jiaogepengyou.py": "IYOUKE",
    "miniprogram/normal/liquanpijiu.py": "LPJ",
    "miniprogram/normal/mifengjxs_code.py": "MFJXS",
    "miniprogram/normal/mishequ.py": "MI",
    "miniprogram/normal/paid.js": "PAID",
    "miniprogram/normal/quanmiansd_code.py": "QMSD",
    "miniprogram/normal/renyang_code.py": "RY",
    "miniprogram/normal/tastien.py": "TASTIEN",
    "miniprogram/normal/wanyazhenxuan.js": "WANYAZX",
    "miniprogram/normal/wuyingCOM_code.py": "WY",
    "miniprogram/normal/ztkuaidi.py": "ZTKD",
    "miniprogram/special/WorkBuddy.py": "WB",
    "miniprogram/special/exijiu.py": "EXIJIU",
    "miniprogram/special/redrocket.js": "HSHJ",
    "miniprogram/special/wxbbs.py": "WXBBS",
}

# notify.py / notify.js 是青龙公共模块, 不做改造
SKIP_FILES = {"notify.py", "notify.js"}

# 站点中文名(用于 📊 [执行汇总] 行)
SITES = {
    "app/chinaUnicom.py": "中国联通",
    "app/jd_cookie.py": "京东Cookie",
    "app/oclean_sign.py": "OClean",
    "app/skland_sign.py": "森空岛",
    "app/xiaoheihe.py": "小黑盒",
    "app/xiaoheihe_qrlogin.py": "小黑盒扫码",
    "forum/hktv8.py": "HKTV论坛",
    "forum/southplus.py": "南+论坛",
    "forum/wnflb2023.js": "福利吧",
    "miniprogram/normal/QC.py": "雀巢",
    "miniprogram/normal/StormCrew_code.py": "StormCrew",
    "miniprogram/normal/bjyhuishou.py": "白鲸鱼",
    "miniprogram/normal/cdf.py": "中免会员",
    "miniprogram/normal/changhong.py": "长虹",
    "miniprogram/normal/chuangwei.js": "创维",
    "miniprogram/normal/dikanong_code.py": "迪卡侬",
    "miniprogram/normal/exijiu_jph.js": "君品荟",
    "miniprogram/normal/guyu_code.py": "谷雨",
    "miniprogram/normal/haixinaj.js": "海信爱家",
    "miniprogram/normal/haluocx.py": "哈啰出行",
    "miniprogram/normal/hrt.py": "华润通",
    "miniprogram/normal/huishouyuan.py": "回收猿",
    "miniprogram/normal/jdkuaidi.py": "京东快递",
    "miniprogram/normal/jialichuang.py": "嘉立创",
    "miniprogram/normal/jiaogepengyou.py": "交个朋友",
    "miniprogram/normal/liquanpijiu.py": "漓泉啤酒",
    "miniprogram/normal/mifengjxs_code.py": "蜜蜂惊喜社",
    "miniprogram/normal/mishequ.py": "小米社区",
    "miniprogram/normal/paid.js": "平安i动",
    "miniprogram/normal/quanmiansd_code.py": "全面时代",
    "miniprogram/normal/renyang_code.py": "认养一头牛",
    "miniprogram/normal/tastien.py": "Tastien",
    "miniprogram/normal/wanyazhenxuan.js": "丸丫甄选",
    "miniprogram/normal/wuyingCOM_code.py": "无影云电脑",
    "miniprogram/normal/ztkuaidi.py": "中通快递",
    "miniprogram/special/WorkBuddy.py": "WorkBuddy",
    "miniprogram/special/exijiu.py": "习酒花园",
    "miniprogram/special/redrocket.js": "红色火箭",
    "miniprogram/special/wxbbs.py": "吾爱破解",
}


def iter_scripts():
    for base, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for fn in sorted(files):
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


def has_emit(text):
    return bool(re.search(r"def _emit|def emit\b|function emit", text))


def has_tagged_log(text):
    """是否已有 [LEVEL] [TAG] 格式的日志实现(模块级函数或类方法都算)。

    这类脚本(zhkuaidi/wxbbs 等)的日志格式已与模板一致, 不需要再注入 emit。
    判据: 存在形如 f"[{prefix}] [ZZZZ]" 或 "[INFO] [ZZZZ]" 的字符串拼接。
    """
    if re.search(r"\[(?:\{level\}|\{prefix\}|level|prefix)\]\s*\[", text):
        return True
    return bool(re.search(r"\[(?:INFO|WARN|ERROR)\]\s*\[", text))


def get_tag(rel):
    return TAGS.get(rel, os.path.basename(rel).split(".")[0].upper()[:8])


# ── 插入工具 ─────────────────────────────────────────────────────────
# 注意: 仓库脚本是 CRLF, 正则行尾锚点一律用前瞻 (?=\r?$) 而不是 $, 否则匹配不到。

def _eol(text):
    return "\r\n" if "\r\n" in text else "\n"


def _line_end(text, idx):
    """返回 text[idx] 所在行的结束位置(跳过完整行尾 \\r\\n 或 \\n)。

    两个坑:
      1. 不能用 text.index("\\n", idx)+1 —— 若 idx 落在 \\r 之前, 会把 \\r\\n
         拆成 \\r + \\n, 导致插入后出现一行裸 LF(混合行尾)。
      2. 正则里的 `\\s*$` 会把行尾的 \\r 一起吞掉, 使 m.end() 落在 \\n 之前 ——
         此时必须从 m.end() 往前回退到行内容末尾, 再从那里找完整行尾。
    """
    # idx 往前回退到行内容末尾(跳过已被 \\s* 吞掉的 \\r)
    i = idx
    while i > 0 and text[i - 1] in ("\r", "\n"):
        i -= 1
    m = re.compile(r"\r\n|\n|\r").search(text, i)
    return m.end() if m else len(text)


def insert_after_line(text, line_re, block):
    """在最后一个匹配 line_re 的行之后插入 block。"""
    last = None
    for m in re.finditer(line_re, text, re.M):
        last = m
    if not last:
        return None
    eol = _eol(text)
    lines = block.replace("\n", eol).split(eol)
    pos = _line_end(text, last.start())
    return text[:pos] + eol.join(lines) + eol + text[pos:]


def insert_before(text, line_re, block):
    """在第一个匹配 line_re 的行之前插入 block(保留该行原有缩进)。"""
    m = re.search(line_re, text, re.M)
    if not m:
        return None
    eol = _eol(text)
    lines = block.replace("\n", eol).split(eol)
    start = m.start()
    # 往前吃掉该行前的空行, 避免插入后出现连续空行
    while start > 0 and text[start - 1] in ("\n", "\r"):
        start -= 1
    return text[:start] + eol.join(lines) + eol + eol + text[start:]


def insert_after_import_block(text, block):
    """在文件头部「连续的 import 语句块」之后插入 block。

    不能用「最后一个 import」——某些脚本在文件末尾还有零散 import
    (如 guyu_code.py 的 `import os as _yyb_os`), 插到那里会落到文件末尾。
    """
    eol = _eol(text)
    lines = text.split(eol)
    last_import = -1
    in_block = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if re.match(r"^(import|from)\s+", stripped):
            last_import = i
            in_block = True
        elif in_block and stripped and not stripped.startswith("#"):
            break
    if last_import < 0:
        return None
    pos = sum(len(l) + len(eol) for l in lines[:last_import + 1])
    return text[:pos] + block.replace("\n", eol) + eol + eol + text[pos:]


def insert_after_py_emit(text, block):
    """在 Python emit 四件套最后一个函数(err)之后插入 block。"""
    m = re.search(r"^def err\(msg\):(?=\r?$)", text, re.M)
    if not m:
        return None
    eol = _eol(text)
    lines = text.split(eol)
    start = text[:m.start()].count(eol)
    end = start
    for i in range(start + 1, len(lines)):
        s = lines[i]
        if not s.strip() or not s.startswith((" ", "\t")):
            break
        end = i
    pos = sum(len(l) + len(eol) for l in lines[:end + 1])
    return text[:pos] + eol + block.replace("\n", eol) + eol + eol + text[pos:]


# ── emit 四件套注入 ──────────────────────────────────────────────────

def ensure_emit_py(text, tag):
    if has_emit(text):
        return text, False
    block = (
        "# ========== 统一日志：[LEVEL] [%(tag)s] message ==========\n"
        "def _emit(level, msg):\n"
        "    line = \"[%%s] [%(tag)s] %%s\" %% (level, msg)\n"
        "    print(line, flush=True)\n"
        "\n"
        "\n"
        "def log(msg):\n"
        "    _emit(\"INFO\", msg)\n"
        "\n"
        "\n"
        "def warn(msg):\n"
        "    _emit(\"WARN\", msg)\n"
        "\n"
        "\n"
        "def err(msg):\n"
        "    _emit(\"ERROR\", msg)"
    ) % {"tag": tag}
    new = insert_after_import_block(text, block)
    if not new:
        new = insert_after_line(text, r"^#!.*(?=\r?$)", block)
    return (new, True) if new else (text, False)


def ensure_emit_js(text, tag):
    if has_emit(text):
        return text, False
    block = (
        "// ========== 统一日志：[LEVEL] [%(tag)s] message ==========\n"
        "function emit(level, msg) {\n"
        "  const _line = `[${level}] [%(tag)s] ${msg}`;\n"
        "  console.log(_line);\n"
        "}\n"
        "\n"
        "function logInfo(msg) { emit('INFO', msg); }\n"
        "function logWarn(msg) { emit('WARN', msg); }\n"
        "function logErr(msg)  { emit('ERROR', msg); }"
    ) % {"tag": tag}
    new = insert_after_line(text, r"^'use strict';?(?=\r?$)", block)
    if not new:
        new = insert_after_line(text, r"^#!.*(?=\r?$)", block)
    return (new, True) if new else (text, False)


# ── 分隔线 / 启动行 ──────────────────────────────────────────────────

def ensure_acc_rule(text, is_js):
    if re.search(r"ACC_RULE\s*=", text):
        return text, False
    if is_js:
        block = (
            "// ========== 账号分隔标识 ==========\n"
            "const ACC_RULE = '='.repeat(70);"
        )
        new = insert_after_line(text, r"^'use strict';?(?=\r?$)", block)
        if not new:
            # 没有 'use strict' 的脚本(如 forum/wnflb2023.js): 插到 shebang 之后
            new = insert_after_line(text, r"^#!.*(?=\r?$)", block)
    else:
        block = (
            "# ========== 账号分隔标识 ==========\n"
            "# 多账号同跑时, 用醒目横线把各账号日志隔开, 便于阅读与定位。\n"
            "ACC_RULE = \"=\" * 70"
        )
        new = insert_after_import_block(text, block)
    return (new, True) if new else (text, False)


def find_main_class(text):
    """找出 __main__ 块里被实例化的类名(取自 var = Class(...) 这行)。

    不能用「文件里第一个赋值」—— 那会命中 `_qn_load_err = str(...)` 之类的无关语句。
    """
    m = re.search(r"^if __name__ == ['\"]__main__['\"]:\s*$", text, re.M)
    scope = text[m.end():] if m else text
    for mm in re.finditer(r"^\s*(\w+)\s*=\s*([A-Z]\w+)\(", scope, re.M):
        return mm.group(2)
    return None


def ensure_summary(text, is_js, site):
    """确保有统一的 📊 [执行汇总] 行。

    已有 → 不动。
    没有 → 仅当 main() 里已有 success/fail 计数变量时才补一行;
          其它情况不插(需要改业务逻辑, 人工处理)。
    """
    if "执行汇总" in text:
        return text, False
    if is_js:
        return text, False
    # 只在 main() 函数体内找计数变量 —— chinaUnicom.py 之类在子函数里也有
    # 同名局部变量, 插到那里会破坏缩进
    mm = re.search(r"^def main\([^)]*\)(?:\s*->\s*\w+)?\s*:\s*$", text, re.M)
    if not mm:
        return text, False
    scope = text[mm.end():]
    m = re.search(r"^(\s*)(success_count|ok_count)\s*=\s*.*(?=\r?$)", scope, re.M)
    m2 = re.search(r"^(\s*)(fail_count|failed|fail)\s*=\s*.*(?=\r?$)", scope, re.M)
    if not (m and m2):
        return text, False
    ok_var = m.group(2)
    fail_var = m2.group(2)
    # 账号总数: results 列表 / total 变量
    mr = re.search(r"^\s*results\s*:\s*List", scope, re.M)
    if mr:
        total_expr = "len(results)"
    else:
        mt = re.search(r"^\s*total\s*=\s*(\w+)", scope, re.M)
        total_expr = ("len(%s)" % mt.group(1)) if mt else "0"
    eol = _eol(text)
    indent = m2.group(1) or "    "
    line = (indent + 'log("📊 [执行汇总] ' + site
            + ' · 账号 %d ｜ 成功 %d ｜ 失败 %d"'
            + ' % (' + total_expr + ', ' + ok_var + ', ' + fail_var + '))')
    # 插在 fail_count 赋值之后(那一行结束处)
    pos = _line_end(text, mm.end() + m2.end())
    new = text[:pos] + line.replace("\n", eol) + eol + text[pos:]
    return new, True


def detect_names(text, is_js):
    """探测脚本里已有的常量名与日志函数名, 让注入的代码跟随既有命名。

    返回 (acc_name, log_fn):
      acc_name  分隔线常量名, 如 ACC_RULE / _ACC_RULE
      log_fn    日志函数名, 如 log / logInfo
    """
    acc = "ACC_RULE"
    m = re.search(r"^\s*(?:const\s+)?(_?[A-Za-z_]*ACC[A-Za-z_]*)\s*=\s*['\"]=", text, re.M)
    if m:
        acc = m.group(1)
    log_fn = "logInfo" if is_js else "log"
    if is_js:
        if not re.search(r"function logInfo\b", text):
            if re.search(r"function log\b", text):
                log_fn = "log"
    else:
        if not re.search(r"^def log\(msg", text, re.M) and re.search(r"^def log\(", text, re.M):
            log_fn = "log"
    return acc, log_fn


def ensure_startup(text, is_js, ver, use_self=False):
    """确保启动时打印 📌 脚本版本 + 时间戳。

    已有 log_startup/logStartup 函数 → 不注入(避免重复定义)。
    已有「脚本版本」字样但没有该函数 → 也不注入, 保持原样。
    use_self=True 时日志走类方法 self.log(...)。
    """
    if re.search(r"def log_startup|function logStartup", text):
        return text, False
    if "脚本版本" in text:
        return text, False
    # 已有 SCRIPT_VER / SCRIPT_VERSION 常量 → 不再重复声明(否则 JS 会报
    # "Identifier 'SCRIPT_VER' has already been declared")
    has_ver_const = bool(re.search(r"SCRIPT_?VER(?:SION)?\s*=\s*['\"]", text))
    acc, lf = detect_names(text, is_js)
    if is_js:
        block = (
            "// ========== 启动信息 ==========\n"
            "function logStartup(total) {\n"
            "  %s(%s);\n"
            "  %s(`📌 脚本版本: ${SCRIPT_VER} ｜ ${new Date().toLocaleString('zh-CN', { hour12: false })}`);\n"
            "  if (total !== undefined) %s(`🔢 账号数量: ${total}`);\n"
            "  %s(%s);\n"
            "}"
        ) % (lf, acc, lf, lf, lf, acc)
        if not has_ver_const:
            block += "\nconst SCRIPT_VER = '" + (ver or "2026-01-01a") + "';"
        new = insert_after_line(text, r"^const\s+%s\s*=.*\.repeat\(\d+\);(?=\r?$)" % re.escape(acc), block)
    elif use_self:
        # 类结构脚本: 日志走类方法 self.log(...), 插到「真正被调用的那个类」里
        target_class = find_main_class(text)
        class_re = r"^class %s\b" % re.escape(target_class) if target_class else None
        m_cls = re.search(class_re, text, re.M) if class_re else None
        if not m_cls:
            return text, False
        rest = text[m_cls.end():]
        m_meth = re.search(r"^    (?:@\w+\s*\n\s*)?def \w+\(", rest, re.M)
        if not m_meth:
            return text, False
        block = (
            "    # ========== 启动信息 ==========\n"
            "    def log_startup(self, total: int = 0) -> None:\n"
            "        self.log(%s)\n"
            "        self.log(\"📌 脚本版本: %%s ｜ %%s\" %% (SCRIPT_VER, now_text()))\n"
            "        if total:\n"
            "            self.log(\"🔢 账号数量: %%d\" %% total)\n"
            "        self.log(%s)\n"
            "\n"
        ) % (acc, acc)
        pos = m_cls.end() + m_meth.start()
        eol = _eol(text)
        new = text[:pos] + block.replace("\n", eol) + eol + text[pos:]
        if not has_ver_const:
            new = insert_after_line(new, r"^_?ACC_RULE = \"=\" \* 70(?=\r?$)",
                                    "SCRIPT_VER = \"" + (ver or "2026-01-01a") + "\"")
    else:
        block = (
            "# ========== 启动信息 ==========\n"
            "def log_startup(total: int = 0) -> None:\n"
            "    log(%s)\n"
            "    log(\"📌 脚本版本: %%s ｜ %%s\" %% (SCRIPT_VER, now_text()))\n"
            "    if total:\n"
            "        log(\"🔢 账号数量: %%d\" %% total)\n"
            "    log(%s)"
        ) % (acc, acc)
        if not has_ver_const:
            block += "\nSCRIPT_VER = \"" + (ver or "2026-01-01a") + "\""
        new = insert_after_line(text, r"^_?ACC_RULE = \"=\" \* 70(?=\r?$)", block)
    return (new, True) if new else (text, False)


def ensure_now_text(text):
    """Python: 确保有可用的 now_text()。

    已有的 now_text 若直接用 datetime.now(), 在同时 `import datetime` 的脚本里
    会因 datetime 被模块覆盖而报 AttributeError —— 一并改成兼容写法。
    """
    m = re.search(r"^def now_text\([^)]*\)[^:]*:\s*\r?\n(    [^\r\n]*)", text, re.M)
    if m:
        body = m.group(1)
        if "datetime.now()" in body and "hasattr" not in body:
            eol = _eol(text)
            fixed = ('    _dt = datetime.now() if hasattr(datetime, "now") '
                     'else datetime.datetime.now()')
            # 整行替换(含行尾), 保持 CRLF
            new = text[:m.start(1)] + fixed + eol + text[m.end(1):]
            return new, True
        return text, False
    eol = _eol(text)
    block = (
        "def now_text() -> str:\n"
        "    # 兼容两种导入: from datetime import datetime / import datetime\n"
        "    _dt = datetime.now() if hasattr(datetime, \"now\") else datetime.datetime.now()\n"
        "    return _dt.strftime(\"%Y-%m-%d %H:%M:%S\")"
    )
    new = insert_after_line(text, r"^_?ACC_RULE = \"=\" \* 70(?=\r?$)", block)
    if not new:
        return text, False
    # 补 datetime import(行尾必须跟文件一致, 不能用裸 \n)
    m = re.search(r"^from datetime import (.+?)(?=\r?$)", new, re.M)
    if m and "datetime" not in m.group(1):
        new = new[:m.start()] + "from datetime import %s, datetime" % m.group(1) + new[m.end():]
    elif not m:
        m2 = re.search(r"^import \w+(?=\r?$)", new, re.M)
        if m2:
            new = new[:m2.end()] + eol + "from datetime import datetime" + new[m2.end():]
    return new, True


def ensure_startup_call(text, is_js, use_self=False):
    """在入口处插入 log_startup()/logStartup() 调用, 避免定义了却不用。

    入口有三种形态:
      ① def main() / async function main()   —— 插在函数体第一行
      ② if __name__ == "__main__": 块        —— 插在该块内第一行
      ③ auto_task.run() 调用                 —— 插在调用前一行
    use_self=True 时调用写成 auto_task.log_startup()。
    """
    call = "logStartup(" if is_js else "log_startup("
    for line in text.split("\n"):
        s = line.strip()
        if s.startswith("def log_startup") or s.startswith("function logStartup"):
            continue
        if call in line:
            return text, False
    eol = _eol(text)
    stmt = "logStartup();" if is_js else (
        "auto_task.log_startup()" if use_self else "log_startup()")

    if is_js:
        # 注意: 正则里的 \s* 会把行尾的 \r 一起吞掉, m.end() 落在 \n 之前。
        # 插入点必须用 _line_end 重新定位到整行行尾之后, 否则会多出一个 \r(\r\r\n)。
        m = re.search(r"^async function main\(\)\s*\{\s*$", text, re.M)
        if m:
            pos = _line_end(text, m.end())
            return text[:pos] + "  " + stmt + eol + text[pos:], True
        m = re.search(r"^if \(require\.main === module\)\s*\{[^\n]*$", text, re.M)
        if m:
            pos = _line_end(text, m.end())
            return text[:pos] + "  " + stmt + eol + text[pos:], True
        return text, False

    # ① def main()
    # 注意: 正则里的 \s* 会把行尾的 \r 一起吞掉, m.end() 落在 \n 之前。
    # 插入点必须用 _line_end 重新定位到整行行尾之后, 否则会多出一个 \r(\r\r\n)。
    m = re.search(r"^def main\([^)]*\)\s*->\s*\w+\s*:\s*$", text, re.M) or \
        re.search(r"^def main\([^)]*\)\s*:\s*$", text, re.M)
    if m:
        pos = _line_end(text, m.end())
        return text[:pos] + "    " + stmt + eol + text[pos:], True
    # ② / ③ 统一: 找 __main__ 块里的 `var.run()` 调用, 插在它之前(此时 var 已创建)
    m_main = re.search(r"^if __name__ == ['\"]__main__['\"]:\s*$", text, re.M)
    scope_start = m_main.end() if m_main else 0
    scope = text[scope_start:]
    mr = re.search(r"^(\s*)(\w+)\.run\(\)\s*$", scope, re.M)
    if mr:
        var = mr.group(2)
        # 确认该变量在 .run() 之前已被创建
        created = re.search(r"^\s*%s\s*=\s*\w+\(" % re.escape(var), scope[:mr.start()], re.M)
        if created:
            pos = scope_start + mr.start()
            stmt2 = ("    %s.log_startup()" % var) if use_self else "    log_startup()"
            return text[:pos] + stmt2 + eol + text[pos:], True

    # ② __main__ 块(无 .run() 形态)
    if m_main:
        pos = _line_end(text, m_main.end())
        return text[:pos] + "    " + stmt + eol + text[pos:], True
    return text, False


# ── 裸 print 收敛 ────────────────────────────────────────────────────

ERR_EMOJI = ("❌",)
WARN_EMOJI = ("⚠️",)
ERR_PAT = re.compile(r"(❌|错误|失败|异常|error|fail)", re.I)
WARN_PAT = re.compile(r"(⚠️|警告|超时|重试|跳过|已失效|warn)", re.I)


def classify_print(msg):
    """按内容判断 print 该转成 log / warn / err。显式 emoji 优先于关键词。"""
    if any(e in msg for e in ERR_EMOJI):
        return "err"
    if any(e in msg for e in WARN_EMOJI):
        return "warn"
    if ERR_PAT.search(msg):
        return "err"
    if WARN_PAT.search(msg):
        return "warn"
    return "log"


def convert_prints(text):
    """把单参数 print(...) 收敛成 log/warn/err。

    注意: 必须保留原行尾(CRLF/LF), 不能用 split("\n")+join("\n") 重建 ——
    那会把所有 CRLF 变成 LF, 造成全文件混合行尾。
    """
    eol = _eol(text)
    lines = text.split(eol)
    out = []
    changed = 0
    for line in lines:
        stripped = line.lstrip()
        indent = line[:len(line) - len(stripped)]
        m = re.match(r"print\((.*)\)\s*$", stripped)
        if not m:
            out.append(line)
            continue
        args = m.group(1).strip()
        if not args:
            out.append(line)          # 空 print() = 空行, 保留
            continue
        if "," in args and not re.search(r"[\"']", args):
            out.append(line)          # 多参数 print(a, b) 无法安全拆分, 保留
            continue
        if args.startswith(("f\"", "f'")):
            literal = re.sub(r"\{[^}]*\}", "", args)
            kind = classify_print(literal)
        elif args.startswith(("\"", "'")):
            kind = classify_print(args)
        else:
            kind = "log"
        out.append("%s%s(%s)" % (indent, kind, args))
        changed += 1
    return eol.join(out), changed


# ── main ─────────────────────────────────────────────────────────────

def ensure_version_line(text, is_js, ver):
    """确保头部有 version 行(统一用日期型)。"""
    if re.search(r"^[ \t]*(?:#|//)[ \t]*version[ \t]*:", text, re.M):
        return text, False
    block = "# version: " + ver
    m = re.search(r"^[ \t]*(?:#|//)[ \t]*name[ \t]*:", text, re.M)
    if not m:
        return text, False
    eol = _eol(text)
    pos = _line_end(text, m.end())
    return text[:pos] + block + eol + text[pos:], True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--only", metavar="REL")
    args = ap.parse_args()

    print("%-36s %-5s %-6s %-6s %-6s %-6s %-6s" % (
        "文件", "语言", "emit", "ACC", "启动", "汇总", "账号标题"))
    print("-" * 82)

    changed = []
    for path in iter_scripts():
        rel = os.path.relpath(path, ROOT).replace("\\", "/")
        if os.path.basename(rel) in SKIP_FILES:
            continue
        if args.only and rel != args.only:
            continue

        raw = read_bytes(path)
        text, bom = decode(raw)
        is_js = rel.endswith(".js")
        tag = get_tag(rel)

        if args.report:
            print("%-36s %-5s %-6s %-6s %-6s %-6s %-6s" % (
                rel, "JS" if is_js else "PY",
                "✓" if has_emit(text) else "✗",
                "✓" if re.search(r"ACC_RULE\s*=", text) else "✗",
                "✓" if "脚本版本" in text else "✗",
                "✓" if "执行汇总" in text else "✗",
                "✓" if re.search(r"账号\s*\{?[a-z_]*\}?\s*/|acc_banner|ACC_RULE", text) else "✗"))
            continue

        new_text = text
        notes = []

        ver = ""
        m = re.search(r"^[ \t]*(?:#|//)[ \t]*version[ \t]*:[ \t]*(\S+)", text, re.M)
        if m:
            ver = m.group(1)

        # ① 缺 version 行则补(用今天日期 + a)
        if not ver:
            today = datetime.date.today()
            ver = "%s-%02d-%02da" % (today.year, today.month, today.day)
            t, c = ensure_version_line(text, is_js, ver)
            if c:
                notes.append("version行")
                new_text = t

        # ① emit 四件套(已有 [LEVEL][TAG] 日志实现的跳过)
        tagged = has_tagged_log(new_text)
        if not tagged:
            t, c = (ensure_emit_js(new_text, tag) if is_js
                    else ensure_emit_py(new_text, tag))
            if c:
                notes.append("emit")
            new_text = t

            # ② 裸 print 收敛(仅限本次刚注入 emit 的脚本)
            if c and not is_js:
                t, n = convert_prints(new_text)
                if n:
                    notes.append("print→log×%d" % n)
                    new_text = t

        # ③ 分隔线
        t, c = ensure_acc_rule(new_text, is_js)
        if c:
            notes.append("ACC_RULE")
        new_text = t

        # ④ now_text(Python)
        if not is_js:
            t, c = ensure_now_text(new_text)
            if c:
                notes.append("now_text")
            new_text = t

        # ⑤ 启动行(类结构脚本走 self.log)
        # use_self 判定要看「真正被调用的那个类」, 不能只看文件里有没有 def log(self,...)
        use_self = False
        cls_name = find_main_class(new_text)
        if cls_name:
            m_cls = re.search(r"^class %s\b" % re.escape(cls_name), new_text, re.M)
            if m_cls:
                rest = new_text[m_cls.end():]
                if re.search(r"^    def log\(self", rest, re.M):
                    use_self = True
        t, c = ensure_startup(new_text, is_js, ver, use_self)
        if c:
            notes.append("启动行")
        new_text = t

        # ⑥ 在入口处调用启动行
        if re.search(r"def log_startup\b|function logStartup\b", new_text):
            t, c = ensure_startup_call(new_text, is_js, use_self)
            if c:
                notes.append("启动调用")
            new_text = t

        # ⑦ 同步运行时常量 SCRIPT_VER / SCRIPT_VERSION(头部版本为准)
        m = re.search(r"^[ \t]*(?:#|//)[ \t]*version[ \t]*:[ \t]*(\S+)", new_text, re.M)
        if m:
            want = m.group(1)
            mc = re.search(r"(SCRIPT_?VER(?:SION)?\s*=\s*['\"])([^'\"]+)(['\"])", new_text)
            if mc and mc.group(2) != want:
                new_text = new_text[:mc.start(2)] + want + new_text[mc.end(2):]
                notes.append("SCRIPT_VER同步")

        # ⑧ 执行汇总行(脚本里已有成功/失败计数变量时才补)
        t, c = ensure_summary(new_text, is_js, SITES.get(rel, os.path.basename(rel)))
        if c:
            notes.append("汇总行")
        new_text = t

        if new_text != text:
            changed.append((rel, notes))
            if not args.dry_run:
                with open(path, "wb") as f:
                    f.write(encode(new_text, bom))

    if args.report:
        return

    print()
    if not changed:
        print("无需改动。")
        return
    print("变更 %d 个文件:" % len(changed))
    for rel, notes in changed:
        print("  %-36s %s" % (rel, ", ".join(notes)))
    if args.dry_run:
        print("(dry-run, 未落盘)")


if __name__ == "__main__":
    main()
