#!/usr/bin/env node
/*=========================================================
# name:  创维会员中心
# version: 2026-11-18a
# cron: 18 4,14 * * *
# 积分有效期：自账号注册日起每 365 天清算一次（按各账号实际注册时间）
=========================================================*/

// ────────────────────────────────────────────
// 任务流程：
//   1. 读取 YYB_SERVER 账号基座，按 YYB_ONLY_REFS 序号白名单筛选（1 起）
//   2. 调用 YYBGO 的 /wxapp/getCode 获取 wx.login code
//   3. 登录创维小程序，依次执行固定任务（共 5 个）
//   4. 输出汇总（未注册创维的账号自动跳过）
// 可控参数：
//   YYB_SERVER      必填。格式「地址@ref#备注」，多账号换行分隔
//   YYB_ONLY_REFS   账号序号白名单（1 起）。留空 [] 跑全部；填 [1,2] 只跑第 1、2 个账号
//   YYB_TIMEOUT / YYB_RETRY
//                  取 code 单次超时（毫秒）与最多尝试次数（含首次），默认 60000 / 3。
//                  YYB 侧慢是常态，超时给的太紧会把整号判死；失败会打 WARN 并等 2~5 秒随机再试
//   CHUANGW_APPID / APP_VERSION / SDK_VERSION / APP_PATH / APP_SYSTEM / APP_MODEL
//                  可选。小程序设备指纹参数，不填用内置默认（iPhone 15 Pro Max / iOS 26.1）
//   CHUANGW_RUN_TASKS  任务总开关，默认 1（开）；填 0 跳过所有任务只登录
// 积分过期提醒（2026-11-16a 新增，无需配置）：
//   规则：自账号注册日起每 365 天清算一次；快过期分 = 本周期（清算日前 365 天）收入 − 支出；
//         清算日前 7 天（含当天）起提醒，命中即每轮强推一次汇总通知。
//   注册日从流水里 changeDesc 含「注册」的最早一笔 createdAt 识别（每账号各自算，不硬编码），
//   结果缓存到 chuangwei_expire_cache.json；窗口外不发任何额外请求。
// ────────────────────────────────────────────

'use strict';

const crypto = require('crypto');
const vm = require('vm');
const path = require('path');

// ———————————— 错误通知（可选项，想用则用）————————————
// 只推错误：本次运行出现 ERROR 日志才推送一次；正常跑完不打扰。
// 直接调用青龙自带的通知模块（容器内为 /ql/data/scripts/sendNotify.js，官方仓库内为 notify.js）——
//   在青龙面板「通知设置」里配一次即可全站通用（该文件由青龙官方维护，支持其全部推送渠道）。
// 找不到该文件、或未配置任何通知渠道时，只在日志末尾提示一行，不报错、不中断。
const _qfs = require('fs');
const _qpath = require('path');

const _QN_SITE = '创维';
// 青龙官方通知渠道环境变量（任一存在即视为已配置），清单见青龙 sample/config.sample.sh
const _QN_ENVS = [
    'PUSH_KEY', 'BARK_PUSH', 'TG_BOT_TOKEN', 'DD_BOT_TOKEN', 'QYWX_KEY', 'QYWX_AM',
    'IGOT_PUSH_KEY', 'PUSH_PLUS_TOKEN', 'WE_PLUS_BOT_TOKEN', 'GOBOT_URL', 'GOTIFY_URL',
    'DEER_KEY', 'CHAT_URL', 'AIBOTK_KEY', 'CHRONOCAT_URL', 'SMTP_SERVER', 'SMTP_EMAIL',
    'PUSHME_KEY', 'FSKEY', 'QMSG_KEY', 'NTFY_URL', 'WXPUSHER_APP_TOKEN',
    'WXPUSHER_SPT_LIST', 'WEBHOOK_URL', 'OPENILINK_APP_TOKEN', 'WPUSH_APIKEY',
];
let _qnNotify = null;
let _qnLoadErr = '';
try {
    for (const _d of ['/ql/data/scripts', '/ql/scripts', __dirname, _qpath.dirname(__dirname)]) {
        for (const _n of ['sendNotify.js', 'notify.js']) {
            const _f = _qpath.join(_d, _n);
            if (_qfs.existsSync(_f)) { _qnNotify = require(_f).sendNotify; break; }
        }
        if (_qnNotify) break;
    }
} catch (_e) { _qnNotify = null; _qnLoadErr = _e && _e.message ? _e.message : String(_e); }
const _QN_NOMOD = _qnLoadErr
    ? ('[QL] 青龙通知模块加载失败（' + _qnLoadErr + '），错误日志未推送')
    : '[QL] 未找到青龙通知模块（sendNotify.js 或 notify.js），错误日志未推送';

const _qnErrors = [];
const _qnSeen = new Set();
let _qnFlushed = false;

function collectError(line) {
    const s = String(line == null ? '' : line).trim();
    if (!s || _qnSeen.has(s)) return false;
    _qnSeen.add(s);
    if (_qnErrors.length < 30) _qnErrors.push(s);
    return true;
}

async function flushNotify(_site, summary, _logger, force = false) {
    // 收尾调用：本次有 ERROR，或 force=true（积分即将过期）才推送，一次运行最多一条。
    // 未配置或找不到青龙 sendNotify 时只提示一行；站点名以 _QN_SITE 为准。
    if (_qnFlushed || (!_qnErrors.length && !force)) return false;
    _qnFlushed = true;
    if (!_qnNotify) {
        console.log(_QN_NOMOD);
        return false;
    }
    if (!_QN_ENVS.some((k) => process.env[k])) {
        console.log('[QL] 未配置通知渠道，错误日志未推送（可在青龙「通知设置」或环境变量中配置）');
        return false;
    }
    try {
        // 有错仍沿用「执行出错」；仅过期提醒触发时用「执行汇总」，不误报出错
        const _title = _qnErrors.length ? '执行出错' : '执行汇总';
        await _qnNotify('【' + _QN_SITE + '】' + _title, (summary ? summary + '\n\n' : '') + _qnErrors.join('\n'));
        return true;
    } catch (e) {
        console.log('[QL] 错误日志推送失败：' + (e && e.message ? e.message : e));
        return false;
    }
}

// 兜底：脚本中途 process.exit() 或未走到收尾汇总就结束时，补一次推送/提示。
// Node 的 exit 事件里发不出异步请求，所以这里包装 process.exit，等推完再真退出。
function _qnFallback(reason) {
    if (_qnFlushed) return undefined;
    if (!_qnNotify) {
        _qnFlushed = true;
        console.log(_QN_NOMOD);
        return undefined;
    }
    if (!_QN_ENVS.some((k) => process.env[k])) {
        _qnFlushed = true;
        console.log('[QL] 未配置通知渠道，错误日志未推送（可在青龙「通知设置」或环境变量中配置）');
        return undefined;
    }
    if (_qnErrors.length) return flushNotify(_QN_SITE, reason);
    _qnFlushed = true;
    return undefined;
}

if (!global.__qnExitHooked) {
    global.__qnExitHooked = true;
    const _qnOrigExit = process.exit;
    let _qnExiting = false;
    process.exit = function (code) {
        // 已在退出流程中：忽略重复调用。若此处真退出，会截断第一次启动的异步推送。
        if (_qnExiting) return undefined;
        _qnExiting = true;
        let _qnP = null;
        try { _qnP = _qnFallback('脚本中途退出，未走到收尾汇总'); } catch (_e) { _qnP = null; }
        if (_qnP && typeof _qnP.then === 'function') {
            // 保险：推送卡住时最多等 25 秒，之后强制退出
            const _qnT = setTimeout(() => { _qnOrigExit.call(process, code); }, 25000);
            _qnP.catch(() => {}).then(() => { clearTimeout(_qnT); _qnOrigExit.call(process, code); });
            return undefined;
        }
        return _qnOrigExit.call(process, code);
    };
    process.on('beforeExit', () => {
        const _qnP = _qnFallback('脚本未走到收尾汇总');
        if (_qnP && typeof _qnP.then === 'function') _qnP.catch(() => {});
    });
}

// 兜底：脚本顶层未捕获异常（依赖缺失、运行时崩溃等）时，也把错误推一次。
process.on('uncaughtException', (e) => {
    try {
        _qnErrors.push('[ERROR] ' + (e && e.stack ? String(e.stack).split('\n')[0] : String(e)));
    } catch (_) { /* 忽略 */ }
    const _qnP = _qnFallback('脚本异常中断（未捕获异常）');
    if (_qnP && typeof _qnP.then === 'function') {
        _qnP.catch(() => {}).then(() => { console.error(e); process.exit(1); });
        return;
    }
    console.error(e);
    process.exit(1);
});



const YYB_ONLY_REFS = [];  // 账号序号白名单（1 起），留空 [] = 跑 YYB_SERVER 里的全部账号；例如 [1,3] 只跑第 1、3 个账号

// 取 code 的超时与重试：YYB 服务慢响应是常态（实测面板出一次 code 可到 56s），
// 原 20s 硬上限会把整个账号直接判死，这里放宽到 90s。
//
// 重试次数 2026-11-09 由 1 改为 3：原注释担心的「YYB-GO 5 分钟取码限流」
// 已不成立 —— 近 5 天日志里 pacing（限速 429）事件 0 次，同账号相邻取码请求
// 最小间隔 24.7s（限速阈值仅 3s）。
//
// 2026-11-09 诊断：YYB-GO 的连接层超时（上游长连接节点 8s 内不响应，返回 502
// 且响应体无上游错误码）**不触发编队 failover** —— 编队转移清单只含「成员被
// 上游推回」类错误（-14 / 凭据签发拒绝 / 预算耗尽 / 会话不可用 / 账号过期）。
// 所以一个成员超时就整账号失败。而 YYB-GO 每次新请求会按「最近最少使用」把
// 请求轮转到编队的下一个成员，重试正好能命中可用成员。
//
// 实测（创维 appid，ref=1 连打 8 次）：单次成功率 25%，重试 3 次累计 58%。
//
// 重试间隔必须 > YYB 取码最小间隔（code_min_gap_ms = 3000），否则会被限速。
// 原用 2~5 秒随机（有概率落到 2.9s < 3s），这里改为 4~7 秒随机。
const YYB_TIMEOUT = 90000;  // 取 code 单次请求超时（毫秒）
const YYB_RETRY = 5;        // 取 code 最多尝试次数（含首次）

const ENV_NAME = 'chuangw';
const UC_API = 'https://uc-api.skyallhere.com/miniprogram/api';
const UC_ADMIN = 'https://uc-admin.skyallhere.com/api';

// ── 积分过期提醒（2026-11-16a）──
// 真机 HAR 核实：GET {UC_API}/v1/score-log/all?type=all&page=N&pageSize=20
//   响应 code=0, data={ list:[{serialNo, changeDesc, createdAt(+08:00), changeValue}], score, total }
//   注册日：list 里 changeDesc 含「注册」的最早一笔（真机为「会员注册」+300 分）
//   收入/支出：changeValue 正为收、负为支
const EXPIRE_SCORE_URL = `${UC_API}/v1/score-log/all`;
const SCORE_LOG_PAGE_SIZE = 20;
const SCORE_LOG_MAX_PAGES = 20;   // 防接口异常时无限翻页（最多约 400 笔）
const EXPIRE_CYCLE_DAYS = 365;    // 一个清算周期
const EXPIRE_WARN_DAYS = 7;       // 清算日前 N 天（含当天）开始提醒
const EXPIRE_CACHE_FILE = path.join(__dirname, 'chuangwei_expire_cache.json');

const APPID = process.env.CHUANGW_APPID || 'wxff438d3c60c63fb6';
const APP_VERSION = process.env.CHUANGW_APP_VERSION || '8.0.70';
const SDK_VERSION = process.env.CHUANGW_SDK_VERSION || '3.15.2';
const APP_PATH = process.env.CHUANGW_APP_PATH || '/pages/login/login';
const APP_SYSTEM = process.env.CHUANGW_APP_SYSTEM || 'iOS 26.1';
const APP_MODEL = process.env.CHUANGW_APP_MODEL || 'iPhone 15 pro max<iPhone16,2>';

const RUN_TASKS = (process.env.CHUANGW_RUN_TASKS || '1') !== '0';
// 固定任务：5个（不走环境变量）
const TASK_CODES = ['TS00016', 'TS00210', 'TS00203', 'TS00211', 'TS00213'];
const TASK_NAME_MAP = {
  TS00016: '每日签到',
  TS00210: '浏览商品详情',
  TS00201: '购买商品',
  TS00100: '添加产品',
  TS00202: '产品评价',
  TS00203: '分享商品',
  TS00211: '浏览商品详情(成长值)',
  TS00213: '浏览图片详情',
};

// 源码里硬编码私钥（calcSystemSignAndParam）
const TASK_SIGN_PRIVATE_KEY = `-----BEGIN RSA PRIVATE KEY-----
MIIBPAIBAAJBAMJrqTwwvDRo/NP3Pjq0wfeHtfAcwRu5vk5yTfdGmKAAqG9M9Bu8
COIBN/B0lGUcUx4HP4eIvK17HoIut8shun8CAwEAAQJAXVNWymjOfw4ChzFAsud/
0HVZlWgIHmn7+yYNXOyLaQnv8I7GTrVe85lnAvcmboSvpr5KFGzhY0KDpAnCcDsh
QQIhAPzyeP4ncY7cLkftHPUTSg7Mkve/gJUFZN7q2pW0KEGfAiEAxMRcDf8yqSXP
VfUmJpnzranrFRIAs9Eqi1jzbB4KmyECIQCu2hJHZg66uXuInuEQjKf5+PJzLj79
RIBJFEHLkIDvcwIhALvLwSQmvd5MVN9wU1IiOz0zYEfC3+K/LkDCy8kTvwGhAiEA
8OKljQOdOhQcWver4UsvF5jwGPC5CqkPq/not9YLtU4=
-----END RSA PRIVATE KEY-----`;

const fetchFn = globalThis.fetch || ((...args) => import('node-fetch').then(({ default: f }) => f(...args)));

function normalizeServer(raw) {
  const value = String(raw || '').trim();
  if (!value) return '';
  return (/^https?:\/\//i.test(value) ? value : `http://${value}`).replace(/\/+$/, '');
}

function parseAccounts(raw) {
  const accounts = [];
  String(raw || '').split(/[\s&]+/).forEach((line, index) => {
    const value = line.trim();
    if (!value) return;
    const at = value.lastIndexOf('@');
    if (at <= 0 || at === value.length - 1) {
      throw new Error(`YYB_SERVER第${index + 1}行格式错误，应为 地址@账号标识`);
    }
    const server = normalizeServer(value.slice(0, at));
    const refPart = value.slice(at + 1).trim();
    const hash = refPart.indexOf('#');
    const ref = (hash >= 0 ? refPart.slice(0, hash) : refPart).trim();
    const note = (hash >= 0 ? refPart.slice(hash + 1) : '').trim();
    if (!server || !ref) throw new Error(`YYB_SERVER第${index + 1}行地址或账号标识为空`);
    accounts.push({ server, ref, note });
  });
  // 按 YYB_ONLY_REFS 序号白名单筛选（1 起）；留空 [] 则运行全部账号
  if (YYB_ONLY_REFS && YYB_ONLY_REFS.length) {
    const wanted = new Set(YYB_ONLY_REFS.map(x => parseInt(x, 10)).filter(n => Number.isInteger(n) && n > 0));
    const filtered = accounts.filter((_, i) => wanted.has(i + 1));
    logInfo(`ℹ️ [账号过滤] YYB_ONLY_REFS=${JSON.stringify(YYB_ONLY_REFS)} 命中 ${filtered.length}/${accounts.length} 个账号`);
    return filtered;
  }
  return accounts;
}

// ========== 统一日志：[LEVEL] [CHUANGWEI] message ==========
function emit(level, msg) {
  const _line = `[${level}] ${msg}`;
  console.log(_line);
  if (level === 'ERROR') collectError(_line);
}

function logInfo(msg) { emit('INFO', msg); }
function logOk(msg)   { emit('INFO', msg); }
function logWarn(msg) { emit('WARN', msg); }
function logErr(msg)  { emit('ERROR', msg); }

// ====================== 日打标（今日完成标记） ======================
// 记录「今日已完成」的账号，当天再次运行时直接跳过，省下 YYB 取码/取号配额
// 文件：脚本同目录 chuangwei_daily_done.json（复用顶部已 require 的 path / fs）
const DAILY_DONE_FILE = path.join(__dirname, 'chuangwei_daily_done.json');

function _today() {
  const d = new Date();
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

function load_daily_done() {
  try {
    if (_qfs.existsSync(DAILY_DONE_FILE)) {
      const data = JSON.parse(_qfs.readFileSync(DAILY_DONE_FILE, 'utf8'));
      if (data && typeof data === 'object' && !Array.isArray(data)) return data;
    }
  } catch (e) {
    logWarn(`⚠️ [打标] 读取失败: ${e.message || e}`);
  }
  return {};
}

function save_daily_done(cache) {
  try {
    const tmp = `${DAILY_DONE_FILE}.tmp`;
    _qfs.writeFileSync(tmp, JSON.stringify(cache, null, 2), 'utf8');
    _qfs.renameSync(tmp, DAILY_DONE_FILE);
  } catch (e) {
    logErr(`❌ [打标] 保存失败: ${e.message || e}`);
  }
}

function is_done_today(key) {
  const entry = load_daily_done()[key];
  if (!entry || typeof entry !== 'object') return false;
  return String(entry.date || '') === _today();
}

function mark_done_today(key, note = '', expireInfo = null, totalPoints = null) {
  const cache = load_daily_done();
  const today = _today();
  // 只保留当天的记录，跨天自动清理，避免文件无限增长
  const kept = {};
  for (const [k, v] of Object.entries(cache)) {
    if (v && typeof v === 'object' && String(v.date || '') === today) kept[k] = v;
  }
  const now = new Date();
  const p = (n) => String(n).padStart(2, '0');
  kept[key] = {
    date: today,
    doneTime: `${today} ${p(now.getHours())}:${p(now.getMinutes())}:${p(now.getSeconds())}`,
    note: String(note || ''),
    // 缓存本轮查到的过期提醒：当天第二轮被跳过的账号也能输出，不重新登录取码
    expireInfo: expireInfo && typeof expireInfo === 'object' ? expireInfo : null,
    // 缓存账户总积分（当前积分）：被跳过时也能在汇总里显示，不重新登录取码
    totalPoints: (totalPoints === null || totalPoints === undefined) ? null : Number(totalPoints),
  };
  save_daily_done(kept);
  logInfo(`✅ [打标] 今日任务已完成，已记录（${today}）`);
}

function clear_daily_done(key = '') {
  const cache = load_daily_done();
  if (key) delete cache[key];
  else for (const k of Object.keys(cache)) delete cache[k];
  save_daily_done(cache);
}

function section(title) { logInfo(`━━━ ${title} ━━━`); }

// ========== 账号分隔标识：便于多账号日志区分定位 ==========
// ========== 启动信息 ==========
function logStartup(total) {
  logInfo(ACC_RULE);
  _title = `📃 脚本名称：创维会员中心`;
  if (APPID) _title += ` ｜${APPID}`;
  logInfo(_title);
  logInfo(`📌 脚本版本: ${SCRIPT_VER}`);
  if (total !== undefined) logInfo(`🔢 账号数量: ${total}`);
  logInfo(ACC_RULE);
}
const SCRIPT_VER = '2026-11-18a';
const ACC_RULE = '='.repeat(70);
const SUMMARY_SEP = "▶︎ " + "━".repeat(50) + " ◀︎";

// 积分每日快照：按账号存每天最后一次的总积分，用于次日对比涨跌。
const POINTS_HISTORY_FILE = path.join(__dirname, 'chuangwei_points_history.json');
function load_points_history() {
  try {
    if (_qfs.existsSync(POINTS_HISTORY_FILE)) {
      const d = JSON.parse(_qfs.readFileSync(POINTS_HISTORY_FILE, 'utf8'));
      if (d && typeof d === 'object' && !Array.isArray(d)) return d;
    }
  } catch (e) { logWarn(`⚠️ [积分] 历史读取失败: ${e.message || e}`); }
  return {};
}
function save_points_history(cache) {
  try {
    const tmp = `${POINTS_HISTORY_FILE}.tmp`;
    _qfs.writeFileSync(tmp, JSON.stringify(cache, null, 2), 'utf8');
    _qfs.renameSync(tmp, POINTS_HISTORY_FILE);
  } catch (e) { logErr(`❌ [积分] 历史保存失败: ${e.message || e}`); }
}
function record_points_snapshot(account, totalPoints) {
  if (totalPoints === null || totalPoints === undefined) return;
  const val = Number(totalPoints);
  if (!Number.isFinite(val)) return;
  const hist = load_points_history();
  let days = hist[account];
  if (!days || typeof days !== 'object') days = {};
  days[_today()] = val;
  hist[account] = days;
  save_points_history(hist);
}
function points_delta(account, todayVal) {
  try {
    const d = new Date(); d.setDate(d.getDate() - 1);
    const p = (n) => String(n).padStart(2, '0');
    const yesterday = `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
    const hist = load_points_history();
    const days = hist[account];
    if (!days || typeof days !== 'object' || !(yesterday in days)) return null;
    return todayVal - Number(days[yesterday]);
  } catch (e) { return null; }
}
function accBanner(idx, total, ident = '') {
  logInfo(ACC_RULE);
  logInfo(`👤 账号 ${idx}/${total}${ident ? ' ｜ ' + ident : ''}`);
  logInfo(ACC_RULE);
}
function accFooter(idx, total) {
  logInfo(`🔚 账号 ${idx}/${total} 处理结束`);
}

function truncText(s, max = 96) {
  const str = String(s ?? '');
  if (str.length <= max) return str;
  return `${str.slice(0, max - 1)}…`;
}

function maskMiddle(s, left = 10, right = 8) {
  const str = String(s || '');
  if (!str) return '';
  if (str.length <= left + right + 3) return str;
  return `${str.slice(0, left)}...${str.slice(-right)}`;
}

function brief(v, max = 54) {
  if (v === undefined || v === null) return '';
  const s = typeof v === 'string' ? v : JSON.stringify(v);
  return truncText(s, max);
}

function taskName(code) {
  return TASK_NAME_MAP[code] || '任务';
}

function taskStateText(v) {
  return Number(v) === 1 ? '已完成' : '未完成';
}

function isWxidLike(s) {
  const x = String(s || '').trim();
  if (!x) return false;
  if (x.startsWith('wx:')) return true;
  if (x.startsWith('wxid_')) return true;
  return /^[a-zA-Z0-9_-]{4,64}$/.test(x) && !x.includes('.');
}

function decodeJwtPayload(token) {
  try {
    const p = String(token || '').split('.');
    if (p.length < 2) return null;
    const b64 = p[1].replace(/-/g, '+').replace(/_/g, '/');
    const padded = b64 + '='.repeat((4 - (b64.length % 4)) % 4);
    return JSON.parse(Buffer.from(padded, 'base64').toString('utf8'));
  } catch {
    return null;
  }
}

function fmtTs(ts) {
  const d = new Date(Number(ts) * 1000);
  if (Number.isNaN(d.getTime())) return String(ts);
  return d.toLocaleString('zh-CN', { hour12: false, timeZone: 'Asia/Shanghai' });
}

function printTokenInfo(token) {
  const p = decodeJwtPayload(token) || {};
  const iat = Number(p.iat || 0);
  const exp = Number(p.exp || 0);
  if (!iat || !exp) return;
  const validM = Math.floor((exp - iat) / 60);
  const leftM = Math.max(0, Math.floor((exp - Math.floor(Date.now() / 1000)) / 60));
  logInfo(`Token有效期≈${validM}分钟，剩余≈${leftM}分钟`);
  logInfo(`签发时间: ${fmtTs(iat)}`);
  logInfo(`过期时间: ${fmtTs(exp)}`);
}

function commonHeaders(extra = {}) {
  return {
    'Content-Type': 'application/json',
    'App-Version': APP_VERSION,
    'App-Sdkversion': SDK_VERSION,
    'App-System': APP_SYSTEM,
    'App-Model': APP_MODEL,
    'App-Path': APP_PATH,
    'User-Agent': 'Mozilla/5.0 MicroMessenger MiniProgram',
    ...extra,
  };
}

function miniH5UA() {
  return `Mozilla/5.0 (iPhone; CPU iPhone OS 26_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 MicroMessenger/8.0.70(0x1800463a) NetType/WIFI Language/zh_CN miniProgram/${APPID}`;
}

function safeJsonParse(text, fallback = null) {
  try { return JSON.parse(text); } catch { return fallback; }
}

function getSetCookieList(headers) {
  if (!headers) return [];
  if (typeof headers.getSetCookie === 'function') return headers.getSetCookie();
  if (typeof headers.raw === 'function') {
    const raw = headers.raw();
    if (raw && Array.isArray(raw['set-cookie'])) return raw['set-cookie'];
  }
  const single = headers.get ? headers.get('set-cookie') : '';
  if (!single) return [];
  return String(single).split(/,(?=[^;,]+=)/g).map(s => s.trim()).filter(Boolean);
}

function setCookieToJar(jar, setCookieLine) {
  const line = String(setCookieLine || '');
  const first = line.split(';')[0] || '';
  const idx = first.indexOf('=');
  if (idx <= 0) return;
  const k = first.slice(0, idx).trim();
  const v = first.slice(idx + 1).trim();
  if (!k) return;
  jar[k] = v;
}

function cookieHeaderFromJar(jar) {
  return Object.entries(jar || {}).map(([k, v]) => `${k}=${v}`).join('; ');
}

function sleep(ms) {
  return new Promise(r => setTimeout(r, ms));
}

function genNonce() {
  return crypto.randomUUID ? crypto.randomUUID() : 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, c => {
    const r = Math.random() * 16 | 0;
    const v = c === 'x' ? r : (r & 0x3 | 0x8);
    return v.toString(16);
  });
}

async function httpJson(url, { method = 'GET', headers = {}, body, timeout = 20000 } = {}) {
  const c = new AbortController();
  const t = setTimeout(() => c.abort(new Error('timeout')), timeout);
  try {
    const resp = await fetchFn(url, {
      method,
      headers,
      body: body !== undefined ? JSON.stringify(body) : undefined,
      signal: c.signal,
    });
    const text = await resp.text();
    let data;
    try { data = text ? JSON.parse(text) : {}; } catch { data = { raw: text }; }
    if (!resp.ok) {
      // 附上状态码与已解析的响应体，便于上层分类失败类型（连接层超时 / 熔断 / 上游拒绝）；
      // 错误文案本身仍不带 URL，避免把服务端地址写进日志。
      const err = new Error(`${method} ${url} -> ${resp.status} ${text.slice(0, 400)}`);
      err.status = resp.status;
      err.body = data;
      throw err;
    }
    return data;
  } finally {
    clearTimeout(t);
  }
}

async function httpText(url, { method = 'GET', headers = {}, body, timeout = 20000, jar = null, redirect = 'manual' } = {}) {
  const c = new AbortController();
  const t = setTimeout(() => c.abort(new Error('timeout')), timeout);
  try {
    const reqHeaders = { ...headers };
    if (jar && Object.keys(jar).length) {
      reqHeaders.Cookie = cookieHeaderFromJar(jar);
    }
    const resp = await fetchFn(url, {
      method,
      headers: reqHeaders,
      body,
      redirect,
      signal: c.signal,
    });

    if (jar) {
      const setCookies = getSetCookieList(resp.headers);
      for (const line of setCookies) setCookieToJar(jar, line);
    }

    const text = await resp.text();
    const isRedirect = resp.status >= 300 && resp.status < 400;
    if (!resp.ok && !isRedirect) {
      throw new Error(`${method} ${url} -> ${resp.status} ${text.slice(0, 400)}`);
    }
    return { status: resp.status, headers: resp.headers, text, finalUrl: resp.url };
  } finally {
    clearTimeout(t);
  }
}

// 从 YYB-GO 错误响应体里提取可安全展示的原因摘要。
// 只输出固定措辞，绝不回显服务端原文（可能含 URL、凭据或个人信息）。
function safeErrorDetail(status, body) {
  const msg = String(body?.message || body?.msg || '');
  const low = msg.toLowerCase();
  const data = (body?.data && typeof body.data === 'object') ? body.data : {};
  const upstream = data.upstream_errors || data.rejections;

  // 上游明确拒绝（有结构化错误码）—— 重试无用
  if (Array.isArray(upstream) && upstream.length) {
    const codes = upstream
      .filter(it => it && it.code !== undefined && it.code !== null)
      .map(it => String(it.code));
    return codes.length ? `上游拒绝（错误码 ${codes.join('/')}）` : '上游拒绝';
  }
  // 本地熔断 / 限速
  if (data.circuit_open || low.includes('circuit') || msg.includes('熔断')) {
    return '服务端熔断中';
  }
  // 连接层超时（无上游错误码 + 8 秒 deadline）
  if (low.includes('context deadline exceeded')
      || low.includes('shortlink')
      || low.includes('timeout')
      || low.includes('i/o timeout')) {
    return '上游连接超时';
  }
  if (low.includes('not found')) return '账号不存在';
  return '';
}

// 取 code（带超时与重试）
// 仅「上游连接超时」才重试：这类失败是纯连接层的（上游长连接节点偶发不响应，
// 8 秒 deadline 后返回 502，响应体无上游错误码）。YYB-GO 的编队转移清单不含
// 连接层超时，不会自动换成员；但它每次新请求会把请求轮转到编队的下一个成员，
// 重试正好能命中可用成员。
//
// 熔断（429）、账号失效（404）、上游拒绝（-14 等）一律不重试。
async function getWxCode(server, ref, appid = APPID) {
  const url = `${server}/wxapp/getCode`;
  const attempts = Math.max(1, Number(YYB_RETRY) || 1);
  let lastErr = null;
  let lastDetail = '';
  for (let attempt = 1; attempt <= attempts; attempt++) {
    try {
      const ret = await httpJson(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: { ref, app_id: appid },
        timeout: YYB_TIMEOUT,
      });
      const code = ret?.data?.result?.code || '';
      if (Number(ret?.code) === 0 && code) return code;
      throw new Error(`YYB-Go-Enhanced取code失败: ${brief(ret?.message || ret?.msg || ret, 160)}`);
    } catch (e) {
      lastErr = e;
      lastDetail = classifyYybError(e);
      // 只有「上游连接超时」可重试；其余（熔断/失效/上游拒绝/其它）立即上抛
      if (lastDetail !== '上游连接超时' || attempt >= attempts) break;
      // 4~7 秒随机：必须大于 YYB 取码最小间隔（3s），否则会被限速
      const wait = 4000 + Math.floor(Math.random() * 3001);
      logWarn(`⚠️ 取code第 ${attempt}/${attempts} 次失败（${lastDetail}），${(wait / 1000).toFixed(1)}s 后重试`);
      await sleep(wait);
    }
  }
  // 报错不含 URL，避免把服务端地址写进日志
  const tail = lastDetail ? `（${lastDetail}）` : '';
  throw new Error(`YYB getCode 取code失败（已尝试 ${attempts} 次，单次超时 ${Math.round(YYB_TIMEOUT / 1000)}s）${tail}：${truncText(lastErr?.message || lastErr, 120)}`);
}

// 从异常里判断失败类型：优先用 httpJson 附带的结构化信息，退回按文案匹配。
function classifyYybError(e) {
  const status = e?.status;
  const body = e?.body;
  if (status) {
    const d = safeErrorDetail(status, body);
    if (d) return d;
  }
  const text = String(e?.message || e || '');
  const low = text.toLowerCase();
  if (low.includes('context deadline exceeded')
      || low.includes('shortlink')
      || low.includes('timeout')
      || low.includes('i/o timeout')) {
    return '上游连接超时';
  }
  if (low.includes('circuit') || text.includes('熔断')) return '服务端熔断中';
  if (low.includes('not found')) return '账号不存在';
  return '';
}

function scoreSnapshot(profile) {
  const base = profile?.data?.baseInfo || {};
  const current = Number(base.userScore);
  const today = Number(base.todayScore);
  return {
    current: Number.isFinite(current) ? current : null,
    today: Number.isFinite(today) ? today : null,
  };
}

function scoreSummary(before, after) {
  const delta = before.current !== null && after.current !== null ? after.current - before.current : null;
  const deltaText = delta === null ? '接口未返回' : `${delta >= 0 ? '+' : ''}${delta}`;
  const todayText = after.today === null ? '接口未返回' : String(after.today);
  const currentText = after.current === null ? '接口未返回' : String(after.current);
  return `💰 本次增加 ${deltaText}｜今日累计 ${todayText}｜当前积分 ${currentText}`;
}

async function exchangeByCode(code) {
  const ret = await httpJson(`${UC_API}/v2/user/exchange`, {
    method: 'POST',
    headers: commonHeaders(),
    body: { code },
  });
  const ticket = ret?.data?.ticket || (typeof ret?.data === 'string' ? ret.data : '');
  if ((ret?.code === 0 || ret?.code === '0') && ticket) return ticket;
  throw new Error(`exchange失败(code=${ret?.code ?? '-'}): ${truncText(ret?.msg || '未知错误', 100)}`);
}

async function signinByTicket(ticket) {
  const ret = await httpJson(`${UC_API}/v2/user/signin`, {
    method: 'POST',
    headers: commonHeaders(),
    body: { ticket },
  });
  const token = ret?.data?.token || '';
  if ((ret?.code === 0 || ret?.code === '0') && token) return token;
  const error = new Error(`signin失败(code=${ret?.code ?? '-'}): ${truncText(ret?.msg || '未知错误', 100)}`);
  if (Number(ret?.code) === 19998 && String(ret?.msg || '').includes('用户不存在')) error.accountSkipped = true;
  throw error;
}

async function getUserByToken(token) {
  return httpJson(`${UC_API}/v1/get-user`, {
    method: 'GET',
    headers: commonHeaders({ Authorization: `Bearer ${token}` }),
  });
}

// ========== 积分过期提醒（2026-11-16a）==========
// 规则（用户给定）：从账号注册当天起，每 365 天清理一次积分；按各账号自己的注册时间算清算日。
//   快过期分 = 本周期（清算日前 365 天）的收入 − 支出
//   清算日前 7 天（含当天）起提醒，命中即每轮强推一次汇总通知。
// 注册日从 score-log/all 里 changeDesc 含「注册」的最早一笔 createdAt 识别，结果落盘缓存；
// 窗口外不发任何额外请求。查询异常只打 WARN，不影响签到结果与通知判定。

function parseBjTime(value) {
  if (!value) return null;
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? null : d;
}

function bjDateText(d) {
  // 中国无夏令时：UTC 时间戳 +8h 后取 ISO 前 10 位即北京时间日期
  return new Date(d.getTime() + 8 * 3600 * 1000).toISOString().slice(0, 10);
}

function addDays(d, n) {
  return new Date(d.getTime() + n * 24 * 3600 * 1000);
}

function dayNumber(dateText) {
  const [y, m, d] = String(dateText || '').split('-').map(Number);
  if (!y || !m || !d) return NaN;
  return Math.floor(Date.UTC(y, m - 1, d) / (24 * 3600 * 1000));
}

function loadExpireCache() {
  try {
    if (_qfs.existsSync(EXPIRE_CACHE_FILE)) {
      const data = JSON.parse(_qfs.readFileSync(EXPIRE_CACHE_FILE, 'utf8'));
      if (data && typeof data === 'object' && !Array.isArray(data)) return data;
    }
  } catch (e) {
    logWarn(`⚠️ [积分过期] 读取注册日缓存失败: ${e.message || e}`);
  }
  return {};
}

function saveExpireCache(cache) {
  try {
    const tmp = `${EXPIRE_CACHE_FILE}.tmp`;
    _qfs.writeFileSync(tmp, JSON.stringify(cache, null, 2), 'utf8');
    _qfs.renameSync(tmp, EXPIRE_CACHE_FILE);
  } catch (e) {
    logWarn(`⚠️ [积分过期] 保存注册日缓存失败: ${e.message || e}`);
  }
}

function nextClearDate(registerAt, now) {
  // 注册日 + N×365 天，取「下一次」清算；已过清算时刻则顺延到下个周期
  const elapsed = Math.floor((now.getTime() - registerAt.getTime()) / (24 * 3600 * 1000));
  if (elapsed < 0) return null;
  const n = Math.floor(elapsed / EXPIRE_CYCLE_DAYS) + 1;
  return addDays(registerAt, n * EXPIRE_CYCLE_DAYS);
}

async function fetchScoreLogs(token, page) {
  const url = `${EXPIRE_SCORE_URL}?${new URLSearchParams({
    type: 'all', page: String(page), pageSize: String(SCORE_LOG_PAGE_SIZE),
  }).toString()}`;
  const ret = await httpJson(url, {
    method: 'GET',
    headers: commonHeaders({
      Authorization: `Bearer ${token}`,
      'App-Path': '/pages-user/user-score/user-score',
    }),
    timeout: 20000,
  });
  if (!(Number(ret?.code) === 0 || String(ret?.code) === '0')) {
    throw new Error(`score-log业务码异常(code=${ret?.code ?? '-'})`);
  }
  const data = ret?.data || {};
  return { rows: Array.isArray(data.list) ? data.list : [], total: Number(data.total) || 0 };
}

async function checkExpiringPoints(token, accountKey) {
  // 返回 null（不提醒）或 { expire_date, amount, gained, consumed, register_at }
  const now = new Date();
  const cache = loadExpireCache();
  let registerAt = parseBjTime(cache?.[accountKey]?.register_at);
  let rows = null;

  if (!registerAt) {
    // 首次：翻全部流水识别注册日，顺带拿到本周期明细
    rows = [];
    let total = 0;
    let page = 1;
    do {
      const part = await fetchScoreLogs(token, page);
      rows.push(...part.rows);
      total = part.total;
      if (!part.rows.length) break;
      page++;
    } while (page <= Math.min(SCORE_LOG_MAX_PAGES, Math.ceil(total / SCORE_LOG_PAGE_SIZE)));

    let regRow = null;
    for (const r of rows) {
      if (!r || typeof r !== 'object') continue;
      if (!String(r.changeDesc || '').includes('注册')) continue;
      const t = parseBjTime(r.createdAt);
      if (t && (!regRow || t < parseBjTime(regRow.createdAt))) regRow = r;
    }
    registerAt = regRow ? parseBjTime(regRow.createdAt) : null;
    if (!registerAt) {
      // 兜底：取全量最早一笔流水时间
      for (const r of rows) {
        const t = parseBjTime(r?.createdAt);
        if (t && (!registerAt || t < registerAt)) registerAt = t;
      }
    }
    if (!registerAt) {
      logWarn('⚠️ [积分过期] 未找到注册时间，跳过本次提醒（流水需含「注册」记录）');
      return null;
    }
    cache[accountKey] = { register_at: registerAt.toISOString() };
    saveExpireCache(cache);
  }

  const clearDate = nextClearDate(registerAt, now);
  if (!clearDate) return null;
  const expireDate = bjDateText(clearDate);
  // 按「日期」而非精确时刻比较，避免清算时刻未知导致 D-7 / D 当天漏报
  const daysLeft = dayNumber(expireDate) - dayNumber(bjDateText(now));
  if (daysLeft < 0 || daysLeft > EXPIRE_WARN_DAYS) return null;

  const cycleStart = addDays(clearDate, -EXPIRE_CYCLE_DAYS);
  if (!rows) {
    // 已有注册日缓存：只翻到本周期起点之前的那一页即停，避免每次翻 8 页
    rows = [];
    let total = 0;
    let page = 1;
    let reachedStart = false;
    do {
      const part = await fetchScoreLogs(token, page);
      total = part.total;
      if (!part.rows.length) break;
      for (const r of part.rows) {
        const t = parseBjTime(r?.createdAt);
        if (t && t < cycleStart) reachedStart = true;
        rows.push(r);
      }
      if (reachedStart) break;
      page++;
    } while (page <= Math.min(SCORE_LOG_MAX_PAGES, Math.ceil(total / SCORE_LOG_PAGE_SIZE)));
  }

  let gained = 0, consumed = 0;
  for (const r of rows) {
    if (!r || typeof r !== 'object') continue;
    const t = parseBjTime(r.createdAt);
    if (!t || t < cycleStart || t >= clearDate) continue;
    const v = Number(r.changeValue);
    if (!Number.isFinite(v)) continue;
    if (v > 0) gained += v;
    else consumed += Math.abs(v);
  }
  const amount = Math.max(0, gained - consumed);
  if (amount <= 0) return null;
  return { expire_date: expireDate, amount, gained, consumed, register_at: registerAt.toISOString() };
}

function expireAlertLine(info, idx) {
  return `💰 积分(${Number(info?.amount || 0).toFixed(2)})即将过期： ${info?.expire_date || ''}（账号${idx}）`;
}

async function getWdStatus(token) {
  const ret = await httpJson(`${UC_ADMIN}/userEquityScoreLog/miniProgram/userScoreStatusInfo`, {
    method: 'GET',
    headers: commonHeaders({
      Authorization: `Bearer ${token}`,
      isNoToken: '1',
      'App-Path': '/pages-user/get-wd/get-wd',
    }),
  });
  const arr = Array.isArray(ret?.data) ? ret.data : [];
  const map = {};
  for (const x of arr) map[x.taskLabel] = Number(x.isComplete || 0);
  return { raw: ret, map };
}

async function getIndexNav(token) {
  return httpJson(`${UC_API}/v1/index-nav`, {
    method: 'GET',
    headers: commonHeaders({
      Authorization: `Bearer ${token}`,
      'App-Path': '/pages/user/user',
    }),
  });
}

async function getDuibaAutoLoginUrl(token) {
  const nav = await getIndexNav(token);
  const tokenUrl = nav?.data?.register || '';
  if (!tokenUrl) {
    throw new Error('index-nav未返回register签到入口');
  }
  const ret = await httpJson(tokenUrl, {
    method: 'GET',
    headers: commonHeaders({
      Authorization: `Bearer ${token}`,
      'App-Path': '/pages/user/user',
    }),
  });
  const autoLoginUrl = typeof ret?.data === 'string' ? ret.data : '';
  if (!autoLoginUrl) throw new Error(`duiba-nologin失败: ${JSON.stringify(ret)}`);
  return { tokenUrl, autoLoginUrl };
}

function extractScriptBlocks(html) {
  const out = [];
  const re = /<script[^>]*>([\s\S]*?)<\/script>/gi;
  let m;
  while ((m = re.exec(String(html || '')))) {
    out.push(m[1] || '');
  }
  return out;
}

function decodeObfuscatedEvalScript(scriptCode, context = {}) {
  let generated = '';
  const sandbox = {
    window: {},
    location: { host: '74367-1-activity.m.dexfu.cn' },
    XMLHttpRequest: function () {},
    eval: (s) => {
      generated = String(s || '');
      return s;
    },
    ...context,
  };
  vm.createContext(sandbox);
  vm.runInContext(String(scriptCode || ''), sandbox, { timeout: 2500 });
  return generated;
}

function extractDuibaKeyFromPageHtml(pageHtml) {
  try {
    const scripts = extractScriptBlocks(pageHtml);
    const target = scripts.find(s => s.includes('获取token') || s.includes('/chw/ctoken/getToken'));
    if (!target) return 'e7a76d7t';
    const generated = decodeObfuscatedEvalScript(target);
    const m = generated.match(/var\s+key\s*=\s*['"]([^'"]+)['"]/);
    return m?.[1] || 'e7a76d7t';
  } catch {
    return 'e7a76d7t';
  }
}

function extractWindowAssignments(code) {
  const map = {};
  const re = /window\[['"]([^'"]+)['"]\]\s*=\s*['"]([^'"]*)['"]/g;
  let m;
  while ((m = re.exec(String(code || '')))) {
    map[m[1]] = m[2];
  }
  return map;
}

function resolveDuibaCtoken(tokenScript, keyHint = '') {
  let generated = '';
  try {
    generated = decodeObfuscatedEvalScript(String(tokenScript || ''), { window: {} });
  } catch {
    generated = '';
  }

  if (generated) {
    try {
      const sandbox = { window: {} };
      vm.createContext(sandbox);
      vm.runInContext(generated, sandbox, { timeout: 1500 });
      const win = sandbox.window || {};
      if (keyHint && typeof win[keyHint] === 'string') return win[keyHint];
      for (const v of Object.values(win)) {
        if (typeof v === 'string' && /^[a-zA-Z0-9]{4,24}$/.test(v)) return v;
      }
    } catch {
      // ignore
    }
  }

  const map = extractWindowAssignments(generated || tokenScript);
  if (keyHint && typeof map[keyHint] === 'string') return map[keyHint];
  for (const v of Object.values(map)) {
    if (typeof v === 'string' && /^[a-zA-Z0-9]{4,24}$/.test(v)) return v;
  }
  return '';
}

function getRedirectLocation(headers, baseUrl) {
  const loc = headers?.get ? headers.get('location') : '';
  if (!loc) return '';
  try { return new URL(loc, baseUrl).toString(); } catch { return loc; }
}

async function getDuibaIndexInfo(origin, signOperatingId, pageUrl, jar, ua) {
  const url = `${origin}/sign/component/index?${new URLSearchParams({
    signOperatingId: String(signOperatingId),
    preview: 'false',
    _: String(Date.now()),
  }).toString()}`;
  const resp = await httpText(url, {
    method: 'GET',
    jar,
    redirect: 'manual',
    headers: {
      Accept: 'application/json, text/plain, */*',
      Referer: pageUrl,
      'User-Agent': ua,
    },
  });
  return safeJsonParse(resp.text, {});
}

async function getDuibaCtoken(origin, pageUrl, jar, ua, keyHint) {
  const resp = await httpText(`${origin}/chw/ctoken/getToken`, {
    method: 'POST',
    jar,
    redirect: 'manual',
    headers: {
      Accept: '*/*',
      'Content-Type': 'application/x-www-form-urlencoded',
      Origin: origin,
      Referer: pageUrl,
      'User-Agent': ua,
    },
    body: `timestamp=${Date.now()}`,
  });
  const data = safeJsonParse(resp.text, {});
  if (!data?.success || !data?.token) {
    throw new Error(`ctoken接口失败: ${String(resp.text).slice(0, 200)}`);
  }
  const token = resolveDuibaCtoken(data.token, keyHint);
  if (!token) throw new Error('ctoken解析失败');
  return token;
}

async function doDuibaSign(origin, signOperatingId, signToken, pageUrl, jar, ua) {
  const resp = await httpText(`${origin}/sign/component/doSign?_=${Date.now()}`, {
    method: 'POST',
    jar,
    redirect: 'manual',
    headers: {
      Accept: 'application/json, text/plain, */*',
      'Content-Type': 'application/x-www-form-urlencoded',
      Origin: origin,
      Referer: pageUrl,
      'User-Agent': ua,
    },
    body: new URLSearchParams({
      signOperatingId: String(signOperatingId),
      token: String(signToken),
    }).toString(),
  });
  return safeJsonParse(resp.text, {});
}

async function getDuibaSignResult(origin, orderNum, pageUrl, jar, ua) {
  const url = `${origin}/sign/component/signResult?orderNum=${encodeURIComponent(String(orderNum))}&_=${Date.now()}`;
  const resp = await httpText(url, {
    method: 'GET',
    jar,
    redirect: 'manual',
    headers: {
      Accept: 'application/json, text/plain, */*',
      Referer: pageUrl,
      'User-Agent': ua,
    },
  });
  return safeJsonParse(resp.text, {});
}

function calcTaskSign(taskCode, nonce, timestamp, snCode = '') {
  const data = { taskCode, nonce, timestamp: String(timestamp) };
  if (snCode) data.snCode = snCode;

  const keys = Object.keys(data).sort((a, b) => (a > b ? 1 : -1));
  let preSign = '';
  for (const k of keys) {
    if (k !== 'taskCode' && k !== 'snCode') preSign += `${k}=${data[k]}&`;
  }

  const signer = crypto.createSign('RSA-SHA256');
  signer.update(preSign);
  signer.end();
  const sign = signer.sign(TASK_SIGN_PRIVATE_KEY, 'base64');

  return { preSign, sign };
}

async function completeTask(token, taskCode) {
  const nonce = genNonce();
  const timestamp = String(Date.now());
  const { preSign, sign } = calcTaskSign(taskCode, nonce, timestamp);

  const qs = new URLSearchParams({ taskCode, nonce, timestamp }).toString();
  const url = `${UC_API}/v1/complete-task/${taskCode}?${qs}`;

  const appPathMap = {
    TS00210: '/pages-cwshop/goods/detail/detail?wd=2',
    TS00016: '/pages/webview/webview?taskCode=TS00016',
    TS00203: '/pages-cwshop/goods/detail/detail?czz=1',
    TS00211: '/pages-cwshop/goods/detail/detail?czz=2',
    TS00213: '/pages-picture/picture/page-picture-detail',
  };

  const ret = await httpJson(url, {
    method: 'GET',
    headers: commonHeaders({
      Authorization: `Bearer ${token}`,
      nonce,
      timestamp,
      sign,
      'App-Path': appPathMap[taskCode] || APP_PATH,
    }),
  });

  return { ret, preSign };
}

async function runTasks(token) {
  section('任务中心');
  const before = await getWdStatus(token);
  logInfo(`固定任务(${TASK_CODES.length})：${TASK_CODES.map(c => `${c}(${taskName(c)})`).join('，')}`);

  let execCount = 0;
  let successCount = 0;

  for (let i = 0; i < TASK_CODES.length; i++) {
    const taskCode = TASK_CODES[i];
    const name = taskName(taskCode);
    const b = before.map[taskCode];
    if (Number(b) === 1) {
      logWarn(`[${i + 1}/${TASK_CODES.length}] ${taskCode} ${name}：已完成，跳过`);
      continue;
    }

    execCount++;
    logInfo(`[${i + 1}/${TASK_CODES.length}] 开始上报 ${taskCode} ${name}`);
    try {
      const { ret } = await completeTask(token, taskCode);
      const msg = brief(ret?.data || ret?.msg || ret);
      if (Number(ret?.code) === 0 || String(ret?.code) === '0') {
        successCount++;
        logOk(`${taskCode} ${name}：${msg || '成功'}`);
      } else {
        logWarn(`${taskCode} ${name}：${msg || '失败'}`);
      }
    } catch (e) {
      logErr(`${taskCode} ${name}：${e.message || e}`);
    }
    await sleep(800);
  }

  const after = await getWdStatus(token);
  logInfo('任务状态对比（执行前 -> 执行后）');
  for (const taskCode of TASK_CODES) {
    const name = taskName(taskCode);
    const b = taskStateText(before.map[taskCode]);
    const a = taskStateText(after.map[taskCode]);
    const icon = a === '已完成' ? '✅' : '▫️';
    logInfo(`${icon} ${taskCode} ${name}: ${b} -> ${a}`);
  }
  logOk(`任务执行结束：尝试 ${execCount} 个，成功 ${successCount} 个，跳过 ${TASK_CODES.length - execCount} 个`);
  return `固定任务：尝试${execCount}，成功${successCount}，跳过${TASK_CODES.length - execCount}`;
}

async function runDuibaExtraSign(token) {
  section('额外签到（兑吧连续签到）');
  try {
    const { autoLoginUrl } = await getDuibaAutoLoginUrl(token);
    logOk('签到入口获取成功');

    const jar = {};
    const ua = miniH5UA();

    const autoResp = await httpText(autoLoginUrl, {
      method: 'GET',
      jar,
      redirect: 'manual',
      headers: {
        Accept: 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'User-Agent': ua,
      },
    });

    const signPageUrl = getRedirectLocation(autoResp.headers, autoLoginUrl) || autoLoginUrl;
    const pageResp = await httpText(signPageUrl, {
      method: 'GET',
      jar,
      redirect: 'follow',
      headers: {
        Accept: 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        Referer: autoLoginUrl,
        'User-Agent': ua,
      },
    });

    const finalPageUrl = pageResp.finalUrl || signPageUrl;
    const u = new URL(finalPageUrl);
    const origin = `${u.protocol}//${u.host}`;
    const signOperatingId = u.searchParams.get('signOperatingId');
    if (!signOperatingId) throw new Error(`未找到signOperatingId: ${finalPageUrl}`);

    const keyHint = extractDuibaKeyFromPageHtml(pageResp.text);
    logInfo('已进入签到页');

    const before = await getDuibaIndexInfo(origin, signOperatingId, finalPageUrl, jar, ua);
    const bCon = Number(before?.data?.consecutiveCount || 0);
    const bTot = Number(before?.data?.totalCount || 0);
    const beforeSigned = Boolean(before?.data?.signResult);
    logInfo(`签到前：连签${bCon}天，总签到${bTot}天`);
    logInfo(`签到前状态：${beforeSigned ? '今日已签到' : '今日未签到'}`);

    const signToken = await getDuibaCtoken(origin, finalPageUrl, jar, ua, keyHint);
    logOk('ctoken解析成功');

    const signRet = await doDuibaSign(origin, signOperatingId, signToken, finalPageUrl, jar, ua);
    const signResult = Number(signRet?.data?.signResult ?? -1);
    const orderNum = signRet?.data?.orderNum || '';
    const signTextMap = {
      100: '签到请求已受理',
      2: '今日已签到',
      1: '签到成功',
    };
    logInfo(`doSign：${signTextMap[signResult] || `状态码 ${signResult}`}${orderNum ? `（单号${orderNum}）` : ''}`);

    let finalResult = null;
    if (orderNum) {
      for (let i = 0; i < 3; i++) {
        await sleep(350 + i * 350);
        finalResult = await getDuibaSignResult(origin, orderNum, finalPageUrl, jar, ua);
        if (Number(finalResult?.data?.signResult || 0) === 2) break;
      }
    }
    let finalSignCode = NaN;
    if (finalResult?.data) {
      const sr = Number(finalResult?.data?.signResult ?? -1);
      finalSignCode = sr;
      const credits = finalResult?.data?.credits;
      logInfo(`结果确认：状态码 ${sr}${credits !== null && credits !== undefined ? `，奖励 ${credits} 维豆` : ''}`);
    }

    const after = await getDuibaIndexInfo(origin, signOperatingId, finalPageUrl, jar, ua);
    const aCon = Number(after?.data?.consecutiveCount || 0);
    const aTot = Number(after?.data?.totalCount || 0);
    const afterSigned = Boolean(after?.data?.signResult);
    logInfo(`状态对比: 连签 ${bCon} -> ${aCon}，总签到 ${bTot} -> ${aTot}`);
    logInfo(`签到后状态：${afterSigned ? '今日已签到' : '今日未签到'}`);

    const progressChanged = aCon > bCon || aTot > bTot;
    const signedNow = !beforeSigned && afterSigned;
    const alreadySigned = beforeSigned && afterSigned && !progressChanged;

    if (progressChanged || signedNow || finalSignCode === 1) {
      logOk('兑吧连续签到成功');
      return '兑吧签到：成功';
    } else if (alreadySigned || signResult === 2 || finalSignCode === 2) {
      logInfo('今日已签到（可能是你手动签过）');
      return '兑吧签到：今日已签到';
    } else {
      logWarn('签到状态未变化（可能已签/风控）');
      return '兑吧签到：状态未变化';
    }
  } catch (e) {
    logErr(`兑吧签到失败: ${e.message || e}`);
    return `兑吧签到：失败（${truncText(e.message || e, 60)}）`;
  }
}

async function runOne(account, idx, state = {}) {
  const { server, ref, note = '' } = account;
  section(`账号${idx}${note ? ` (${note})` : ''}`);
  const code = await getWxCode(server, ref, APPID);
  logOk('YYB-Go-Enhanced获取wx.login code成功');

  const ticket = await exchangeByCode(code);
  logOk('exchange成功');

  const token = await signinByTicket(ticket);
  logOk('signin成功');
  printTokenInfo(token);

  // 过期提醒独立于任务流程：即使后续任务失败也要能提醒；查询异常只 WARN，不外抛
  try {
    state.expireInfo = await checkExpiringPoints(token, `${server}@${ref}`);
  } catch (e) {
    logWarn(`⚠️ [积分过期] 查询失败（不影响签到任务）: ${e.message || e}`);
  }

  const profile = await getUserByToken(token);
  if (!(Number(profile?.code) === 0 || String(profile?.code) === '0')) {
    throw new Error(`用户状态验证失败(code=${profile?.code ?? '-'})`);
  }
  const beforeScore = scoreSnapshot(profile);
  logOk('/v1/get-user 成功');
  logInfo(`执行前积分：${beforeScore.current ?? '接口未返回'}，今日累计：${beforeScore.today ?? '接口未返回'}`);
  state.totalPoints = beforeScore.current;

  if (RUN_TASKS) {
    const taskSummary = await runTasks(token);
    const signSummary = await runDuibaExtraSign(token);
    await sleep(1000);
    const afterProfile = await getUserByToken(token);
    const afterScore = scoreSnapshot(afterProfile);
    const points = scoreSummary(beforeScore, afterScore);
    logOk(points.replace('💰 ', ''));
    // 总积分以签到后余额为准
    state.totalPoints = afterScore.current !== null ? afterScore.current : beforeScore.current;
    return [points, `✅ ${taskSummary}`, `📅 ${signSummary}`].filter(Boolean).join('\n');
  } else {
    logWarn('CHUANGW_RUN_TASKS=0，已跳过全部任务（含兑吧签到）');
    return [scoreSummary(beforeScore, beforeScore), '⚠️ 已按配置跳过任务'].join('\n');
  }
}

(async () => {
  logStartup();
  const raw = process.env.YYB_SERVER || '';
  if (!raw.trim()) {
    logErr('❌ 未设置环境变量 YYB_SERVER；格式：YYB-Go-Enhanced地址@账号标识，多账号每行一条');
    process.exitCode = 1;
    return;
  }

  let accounts;
  try {
    accounts = parseAccounts(raw);
  } catch (e) {
    logErr(e.message || e);
    process.exitCode = 1;
    return;
  }
  logInfo(`🚀 创维 YYB-Go-Enhanced 任务启动 | 共 ${accounts.length} 个账号`);

  let failed = 0;
  let skipped = 0;
  const expire_alerts = [];   // [[账号序号, 过期提醒信息], ...]
  const points_rows = [];     // [[账号序号, 总积分], ...]
  for (let i = 0; i < accounts.length; i++) {
    const acc = accounts[i] || {};
    // 今天已经跑完的账号直接跳过，不再消耗 YYB 取码/取号配额（并入 skipped 统计）
    const doneKey = `${acc.server}@${acc.ref}`;
    if (is_done_today(doneKey)) {
      logInfo(`⏭️ [打标] 账号 ${i + 1}/${accounts.length} 今日任务已完成，跳过`);
      skipped++;
      // 当天第二轮被跳过时，从打标缓存读回上午查到的提醒，不重新登录取码
      const cached = load_daily_done()[doneKey];
      const info = cached?.expireInfo;
      if (info && Number(info.amount) > 0) expire_alerts.push([i + 1, info]);
      // 被跳过也要在汇总里显示总积分：从打标缓存读回，不重新登录取码
      const cachedTotal = cached?.totalPoints;
      if (cachedTotal !== null && cachedTotal !== undefined && Number.isFinite(Number(cachedTotal))) {
        points_rows.push([i + 1, Number(cachedTotal), doneKey]);
      }
      continue;
    }
    accBanner(i + 1, accounts.length,
      acc.note ? `备注：${acc.note}` : (acc.ref ? `标识：${acc.ref}` : ''));
    const state = {};
    try {
      await runOne(accounts[i], i + 1, state);
      // 整个流程走完（未抛异常）且确实执行了任务，才算今日完成
      if (RUN_TASKS) mark_done_today(doneKey, '任务完成', state.expireInfo, state.totalPoints);
    } catch (e) {
      if (e?.accountSkipped) {
        logWarn(`⚠️ 账号${i + 1}未注册创维小程序，已跳过`);
        skipped++;
      } else {
        logErr(`❌ 账号${i + 1}失败: ${e.message || e}`);
        failed++;
      }
    }
    if (state.expireInfo && Number(state.expireInfo.amount) > 0) {
      expire_alerts.push([i + 1, state.expireInfo]);
    }
    if (state.totalPoints !== null && state.totalPoints !== undefined
        && Number.isFinite(Number(state.totalPoints))) {
      points_rows.push([i + 1, Number(state.totalPoints), doneKey]);
      record_points_snapshot(doneKey, state.totalPoints);
    }
    accFooter(i + 1, accounts.length);
  }
  const _now = new Date();
  const _p = (n) => String(n).padStart(2, '0');
  const _wk = '日一二三四五六'[_now.getDay()];
  const _date = `${_now.getFullYear()}-${_p(_now.getMonth()+1)}-${_p(_now.getDate())}`;
  logInfo(SUMMARY_SEP);
  logInfo('📂 汇总日志');
  logInfo('📃 脚本名称：创维会员中心');
  logInfo(`📅 运行日期：${_date}  周${_wk}`);
  logInfo(`📊 运行结果：✔️ 成功：${accounts.length - failed - skipped} 个；❌ 失败：${failed} 个；⏭️ 跳过：${skipped} 个`);
  const _cells = [];
  for (const [idx, total, akey] of points_rows) {
    const _delta = points_delta(akey, Number(total));
    const _dt = _delta === null ? '' : `(${_delta >= 0 ? '+' : ''}${_delta.toFixed(2)})`;
    _cells.push(`账号${idx}：${Number(total).toFixed(2)}${_dt}`);
  }
  if (_cells.length) logInfo('💰 总积分：' + _cells.join('  |  '));
  if (expire_alerts.length) {
    logInfo('⏳  即将过期积分：');
    expire_alerts.forEach(([idx, info], j) => {
      const _br = j === expire_alerts.length - 1 ? '└─' : '├─';
      logInfo(`${_br} 账号${idx} ${'·'.repeat(20)} ${Number(info.amount || 0).toFixed(2)}（${info.date || info.expireDate || ''}到期）`);
    });
  }
  logInfo(SUMMARY_SEP);
  let notifySummary = `账号 ${accounts.length} ｜ 成功 ${accounts.length - failed - skipped} ｜ 失败 ${failed} ｜ 跳过 ${skipped}`;
  if (points_rows.length) {
    notifySummary += '\n' + points_rows.map(([idx, total]) => `账号${idx} 总积分 ${total}`).join(' ｜ ');
  }
  if (expire_alerts.length) {
    notifySummary += '\n' + expire_alerts.map(([idx, info]) => expireAlertLine(info, idx)).join('\n');
  }
  // 有 ERROR 或有过期提醒都推送；未配置通知渠道则只提示一行
  await flushNotify('创维', notifySummary, logInfo, Boolean(expire_alerts.length));
  if (failed > 0) process.exitCode = 1;
})();
