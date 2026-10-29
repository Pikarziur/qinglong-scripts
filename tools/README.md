# 脚本版本号工具

用于解决「本机改了、容器（青龙）没同步，日志看着像修复没生效，实际跑的是旧文件」这个反复踩的坑。

## 版本号格式

**全仓统一用日期型** `YYYY-MM-DDx`，写在每个脚本**头部注释块**里，紧跟 `# name:` 之后：

```python
# name:  谷雨
# version: 2026-10-29a
# cron: 30 5 * * *
```

JS 脚本同样用 `#`（在 `/* */` 块注释内）；没有头部注释块的文件（`notify.py` / `notify.js`）插在 shebang 之后。

递增规则：**同日字母 a→b→c…，超过 z 回到 a；跨日再 bump 自动切到新日期 + a**。

```
2026-10-29a  →  2026-10-29b  →  2026-10-29c …
2026-10-29z  →  2026-10-29a
2026-10-29a  →  （第二天）2026-10-30a
```

脚本里若存在 `SCRIPT_VER` / `SCRIPT_VERSION` 常量（`app/chinaUnicom.py`、`miniprogram/normal/paid.js`、三个 `forum/` 脚本），工具会**同步改写**，保证「日志里打印的版本」与「头部版本注释」永远一致。

## 用法

```bash
cd <仓库根目录>

# 1. 看当前全仓版本
python3 tools/version_tool.py --list

# 2. 改完某个脚本后，把它 +1（只动这一个文件）
python3 tools/version_tool.py --bump miniprogram/normal/guyu_code.py

# 3. 全仓批量 +1
python3 tools/version_tool.py --bump

# 4. 给缺版本的脚本补初始版本
python3 tools/version_tool.py --init 2026-10-29a

# 5. 把全仓统一成同一个版本（少用，会抹掉各脚本的历史版本）
python3 tools/version_tool.py --set 2026-10-29a
```

`--bump` / `--set` 只改版本行（及对应的运行时常量），不动其它任何内容；文件编码（BOM）、行尾风格都保持原样。

## 现有版本一览

| 版本 | 脚本 | 说明 |
|---|---|---|
| `2026-10-29a` | 其余 34 个 | 2026-10-29 建立版本号体系时的基线 |
| `2026-10-24a` | `app/jd_cookie.py` | 原语义版 `2.4.4`，改为日期型 |
| `2026-10-23a` | `app/chinaUnicom.py` | 原语义版 `v1.1.1-login`，改为日期型 |
| `2026-09-11a` | `miniprogram/normal/paid.js` | 原 `2026-09-11 14:32`（含时间戳，不便递增） |
| `2026-05-13a` | `miniprogram/normal/QC.py` | 原语义版 `v2.1.0`，日期取自脚本内更新日志 |
| `2026-10-01a` | `forum/hktv8.py` | 沿用原有版本 |
| `2026-10-20a` | `forum/southplus.py` | 沿用原有版本 |
| `2026-10-01b` | `forum/wnflb2023.js` | 沿用原有版本 |

> 保留各脚本原有日期而不是一律刷成 `2026-10-29a`，是为了**保留版本沿革**——版本号本身就是「这脚本最后一次实质改动是什么时候」的记录。

## 在青龙上确认跑的是哪一版

青龙容器里的仓库路径通常是 `/ql/data/repo/<owner>_<repo>_main`：

```bash
# 看某个脚本的版本
grep -m1 "version:" /ql/data/repo/Pikarziur_qinglong-scripts_main/miniprogram/normal/guyu_code.py

# 看全仓版本清单
grep -rn "version:" /ql/data/repo/Pikarziur_qinglong-scripts_main/ --include=*.py --include=*.js | grep -v tools/

# 对比本地和容器是否一致
diff <(grep -rh "version:" /ql/data/repo/Pikarziur_qinglong-scripts_main/miniprogram/normal/) \
     <(grep -rh "version:" miniprogram/normal/)
```

## 约定

- **每次实质性改动后**（改了逻辑、接口、参数、cron），跑一次 `--bump` 对应文件，再 commit
- 只改注释/排版可以不 bump
- 提交信息里带上版本号，方便日后回溯：例如 `guyu_code.py v2026-10-29b 修复 groupId 导致的签到失败`

---

## 日志模板统一（tools/log_template.py）

以 `miniprogram/normal/paid.js` 的日志输出为模板，全仓统一。

### 模板要素

| 要素 | 格式 |
|---|---|
| 统一前缀 | `[LEVEL] [TAG] message`（LEVEL = INFO / WARN / ERROR） |
| 账号分隔线 | `=` × 70 |
| 账号标题行 | `👤 账号 i/N ｜ 备注：xxx` |
| 执行汇总行 | `📊 [执行汇总] <站点> · 账号 N ｜ 成功 X ｜ 失败 Y` |
| 启动信息行 | `📌 脚本版本: <version> ｜ <时间>` |

TAG 沿用各脚本原有缩写（`[GY]` `[HXAJ]` `[JLC]` `[MI]` `[IYOUKE]` `[PAID]` …）。

### 用法

```bash
python3 tools/log_template.py --report     # 看各脚本现状
python3 tools/log_template.py --dry-run    # 预览改动, 不落盘
python3 tools/log_template.py              # 执行改造
python3 tools/log_template.py --only <相对路径>   # 只处理单个文件
```

### 改造原则（安全优先）

- 只做**机械可判定**的改动，不碰业务逻辑与结果计数
- 已有 `[LEVEL][TAG]` 日志实现的脚本：只补缺失的分隔线/启动行
- 没有的：注入四件套，并把裸 `print` 收敛到 `log()` / `warn()` / `err()`
- 已有 `[执行汇总]` 行的脚本：**一律不改**（格式已与模板一致）
- `notify.py` / `notify.js` 是青龙公共模块，**跳过**

### 手工补汇总行的例外

以下脚本结构特殊，需要改业务计数逻辑，工具不动，需人工处理：
`app/chinaUnicom.py`（计数在 `user.log` 业务层）、`app/xiaoheihe_qrlogin.py`（单账号流程）、
`app/jd_cookie.py` / `miniprogram/normal/QC.py`（已手工补齐）。
