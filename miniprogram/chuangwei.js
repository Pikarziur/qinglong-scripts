#!/usr/bin/env node
/*
# name: 创维
# cron: 15 5,15 * * *
*/

// ────────────────────────────────────────────
// 任务流程：
//   1. 读取 YYB_SERVER 账号基座，按 YYB_ONLY_REFS 序号白名单筛选（1 起）
//   2. 调用 YYBGO 的 /wxapp/getCode 获取 wx.login code
//   3. 登录创维小程序，依次执行固定任务（共 5 个）
//   4. 输出汇总（未注册创维的账号自动跳过）
// 可控参数：
//   YYB_SERVER      必填。格式「地址@ref#备注」，多账号换行分隔
//   YYB_ONLY_REFS   账号序号白名单（1 起）。留空 [] 跑全部；填 [1,2] 只跑第 1、2 个账号
//   CHUANGW_APPID / APP_VERSION / SDK_VERSION / APP_PATH / APP_SYSTEM / APP_MODEL
//                  可选。小程序设备指纹参数，不填用内置默认（iPhone 15 Pro Max / iOS 26.1）
//   CHUANGW_RUN_TASKS  任务总开关，默认 1（开）；填 0 跳过所有任务只登录
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

async function flushNotify(_site, summary, _logger) {
    // 收尾调用：本次有 ERROR 才推送；未配置或找不到青龙 sendNotify 时只提示一行。
    // 前两个参数为兼容既有调用点而保留，站点名以 _QN_SITE 为准。
    if (_qnFlushed || !_qnErrors.length) return false;
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
        await _qnNotify('【' + _QN_SITE + '】执行出错', (summary ? summary + '\n\n' : '') + _qnErrors.join('\n'));
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

const ENV_NAME = 'chuangw';
const UC_API = 'https://uc-api.skyallhere.com/miniprogram/api';
const UC_ADMIN = 'https://uc-admin.skyallhere.com/api';

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
  const _line = `[${level}] [CHUANGWEI] ${msg}`;
  console.log(_line);
  if (level === 'ERROR') collectError(_line);
}

function logInfo(msg) { emit('INFO', msg); }
function logOk(msg)   { emit('INFO', msg); }
function logWarn(msg) { emit('WARN', msg); }
function logErr(msg)  { emit('ERROR', msg); }

function section(title) { logInfo(`━━━ ${title} ━━━`); }

// ========== 账号分隔标识：便于多账号日志区分定位 ==========
const ACC_RULE = '='.repeat(70);
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
    if (!resp.ok) throw new Error(`${method} ${url} -> ${resp.status} ${text.slice(0, 400)}`);
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

async function getWxCode(server, ref, appid = APPID) {
  const ret = await httpJson(`${server}/wxapp/getCode`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: { ref, app_id: appid },
  });
  const code = ret?.data?.result?.code || '';
  if (Number(ret?.code) === 0 && code) return code;
  throw new Error(`YYB-Go-Enhanced取code失败: ${brief(ret?.message || ret?.msg || ret, 160)}`);
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

async function runOne(account, idx) {
  const { server, ref, note = '' } = account;
  section(`账号${idx}${note ? ` (${note})` : ''}`);
  const code = await getWxCode(server, ref, APPID);
  logOk('YYB-Go-Enhanced获取wx.login code成功');

  const ticket = await exchangeByCode(code);
  logOk('exchange成功');

  const token = await signinByTicket(ticket);
  logOk('signin成功');
  printTokenInfo(token);

  const profile = await getUserByToken(token);
  if (!(Number(profile?.code) === 0 || String(profile?.code) === '0')) {
    throw new Error(`用户状态验证失败(code=${profile?.code ?? '-'})`);
  }
  const beforeScore = scoreSnapshot(profile);
  logOk('/v1/get-user 成功');
  logInfo(`执行前积分：${beforeScore.current ?? '接口未返回'}，今日累计：${beforeScore.today ?? '接口未返回'}`);

  if (RUN_TASKS) {
    const taskSummary = await runTasks(token);
    const signSummary = await runDuibaExtraSign(token);
    await sleep(1000);
    const afterProfile = await getUserByToken(token);
    const afterScore = scoreSnapshot(afterProfile);
    const points = scoreSummary(beforeScore, afterScore);
    logOk(points.replace('💰 ', ''));
    return [points, `✅ ${taskSummary}`, `📅 ${signSummary}`].filter(Boolean).join('\n');
  } else {
    logWarn('CHUANGW_RUN_TASKS=0，已跳过全部任务（含兑吧签到）');
    return [scoreSummary(beforeScore, beforeScore), '⚠️ 已按配置跳过任务'].join('\n');
  }
}

(async () => {
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
  for (let i = 0; i < accounts.length; i++) {
    const acc = accounts[i] || {};
    accBanner(i + 1, accounts.length,
      acc.note ? `备注：${acc.note}` : (acc.ref ? `标识：${acc.ref}` : ''));
    try {
      await runOne(accounts[i], i + 1);
    } catch (e) {
      if (e?.accountSkipped) {
        logWarn(`⚠️ 账号${i + 1}未注册创维小程序，已跳过`);
        skipped++;
      } else {
        logErr(`❌ 账号${i + 1}失败: ${e.message || e}`);
        failed++;
      }
    }
    accFooter(i + 1, accounts.length);
  }
  logInfo('='.repeat(70));
  logInfo(`📊 [执行汇总] 创维 · 账号 ${accounts.length} ｜ 成功 ${accounts.length - failed - skipped} ｜ 跳过 ${skipped} ｜ 失败 ${failed}`);
  logInfo('='.repeat(70));
  // 错误日志推送：有错误才发；未配置通知渠道则只提示一行
  await flushNotify(
      '创维',
      `账号 ${accounts.length} ｜ 成功 ${accounts.length - failed - skipped} ｜ 跳过 ${skipped} ｜ 失败 ${failed}`,
      logInfo,
  );
  if (failed > 0) process.exitCode = 1;
})();