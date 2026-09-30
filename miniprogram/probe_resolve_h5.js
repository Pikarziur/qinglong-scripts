'use strict';
// 独立探针：在不改动 hshj.js 主脚本的前提下，验证 resolveH5Openid 桥接兜底逻辑。
//   - 不带参数运行：只做"配置硬前提"诊断（不联网，秒出），告诉你桥接能不能跑得起来。
//   - 带 --live 运行：对每个 YYB 账号真正调用 resolveH5Openid（需要 YYB-Go-Enhanced 在线 + 本机可联网 + 该微信已授权 mktzb 公众号）。
// 用法（在能访问 hshj.js 的环境，如青龙面板容器内）：
//   node probe_resolve_h5.js
//   node probe_resolve_h5.js --live

const fs = require('fs');
const path = require('path');
const vm = require('vm');

// ==================== 统一日志 ====================
// 格式：[LEVEL] [H5PROBE] emoji message；行首自带 emoji 时沿用，否则按级别补默认 emoji
const SRC = 'H5PROBE';
const _LEVEL_EMOJI = { INFO: 'ℹ️', WARN: '⚠️', ERROR: '❌' };
const _EMOJI_HEAD = /^(?:[\u2600-\u27BF]|[\u2B00-\u2BFF]|\u2139|\uFE0F|\uD83C[\uDC00-\uDFFF]|\uD83D[\uDC00-\uDFFF]|\uD83E[\uDC00-\uDFFF]|\d\uFE0F?\u20E3)/;
function _levelOfLine(line) {
  if (/^(?:\u274C|\uD83D\uDCA5)/.test(line)) return 'ERROR'; // ❌ 💥
  if (/^\u26A0/.test(line)) return 'WARN';                   // ⚠️
  return 'INFO';
}
function emit(level, text) {
  const s = String(text === undefined || text === null ? '' : text);
  for (const raw of s.split('\n')) {
    const line = raw.trim();
    if (!line) continue;
    const lv = level || _levelOfLine(line);
    console.log(`[${lv}] [${SRC}] ${_EMOJI_HEAD.test(line) ? line : _LEVEL_EMOJI[lv] + ' ' + line}`);
  }
}
function log(text) { emit(null, text); }
function warn(text) { emit('WARN', text); }
function err(text) { emit('ERROR', text); }

const hshjCandidates = [path.join(__dirname, 'hshj.js')];
try {
  const { execSync } = require('child_process');
  const found = execSync('find /ql -name hshj.js 2>/dev/null', { encoding: 'utf8' }).trim();
  if (found) found.split('\n').forEach(p => { p = p.trim(); if (p) hshjCandidates.push(p); });
} catch (e) { /* Windows/非容器环境忽略 */ }
let HSHJ = hshjCandidates.find(p => { try { return fs.existsSync(p); } catch (e) { return false; } });
if (!HSHJ) HSHJ = hshjCandidates[0];
log('📂 加载 hshj.js: ' + HSHJ);
let src = fs.readFileSync(HSHJ, 'utf8');

// 剥离末尾 main() 自执行副作用，改为导出我们需要的函数
const exportLine = 'module.exports = { resolveH5Openid, getSingleCode, YYB_ENTRIES, getYybEntries, selectYybEntry };';
const mainIdx = src.lastIndexOf('main().catch');
if (mainIdx >= 0) {
  src = src.slice(0, mainIdx) + exportLine + '\n';
} else if (!/module\.exports/.test(src)) {
  src = src.replace(/main\(\)\s*\.\s*catch[\s\S]*?\}?\s*\);\s*$/m, exportLine + '\n');
}

// ---- 极简 axios polyfill（基于 Node 原生 https，仅在环境没有真实 axios 时兜底） ----
function makeAxiosPolyfill() {
  const http = require('http');
  const https = require('https');
  const { URL } = require('url');
  function mergeCookie(oldC, setCs) {
    const map = {};
    if (oldC) oldC.split(';').forEach(s => { const i = s.indexOf('='); if (i > 0) map[s.slice(0, i).trim()] = s.slice(i + 1).trim(); });
    const arr = Array.isArray(setCs) ? setCs : [setCs];
    arr.forEach(c => { const i = c.indexOf('='); if (i > 0) map[c.slice(0, i).trim()] = c.slice(i + 1, c.indexOf(';') > 0 ? c.indexOf(';') : c.length).trim(); });
    return Object.entries(map).map(([k, v]) => k + '=' + v).join('; ');
  }
  function request(method, urlStr, data, opts) {
    return new Promise((resolve, reject) => {
      const start = new URL(urlStr);
      const timeout = (opts && opts.timeout) || 30000;
      const maxRedirects = (opts && opts.maxRedirects) || 0;
      const baseHeaders = Object.assign({}, (opts && opts.headers) || {});
      let body = null;
      if (data != null) {
        if (typeof data === 'string') body = data;
        else if (data instanceof URLSearchParams) body = data.toString();
        else if (typeof data === 'object') body = Object.entries(data).map(([k, v]) => encodeURIComponent(k) + '=' + encodeURIComponent(v)).join('&');
        baseHeaders['Content-Type'] = baseHeaders['Content-Type'] || 'application/x-www-form-urlencoded';
      }
      function doReq(redirectsLeft, cookie, curUrl) {
        const lib = curUrl.protocol === 'https:' ? https : http;
        const h = Object.assign({}, baseHeaders);
        if (cookie) h['Cookie'] = cookie;
        const reqOpts = {
          method, hostname: curUrl.hostname, port: curUrl.port || (curUrl.protocol === 'https:' ? 443 : 80),
          path: curUrl.pathname + curUrl.search, headers: h,
        };
        const req = lib.request(reqOpts, (res) => {
          const chunks = [];
          res.on('data', c => chunks.push(c));
          res.on('end', () => {
            const respHeaders = res.headers;
            if ([301, 302, 303, 307, 308].includes(res.statusCode) && redirectsLeft > 0 && respHeaders.location) {
              const next = new URL(respHeaders.location, curUrl);
              const ck = respHeaders['set-cookie'] ? mergeCookie(cookie, respHeaders['set-cookie']) : cookie;
              return doReq(redirectsLeft - 1, ck, next);
            }
            resolve({ data: Buffer.concat(chunks).toString('utf8'), status: res.statusCode, headers: respHeaders, config: { url: urlStr } });
          });
        });
        req.on('error', reject);
        req.setTimeout(timeout, () => req.destroy(new Error('timeout')));
        if (body) req.write(body);
        req.end();
      }
      doReq(maxRedirects, null, start);
    });
  }
  return {
    get: (u, o) => request('GET', u, null, o),
    post: (u, d, o) => request('POST', u, d, o),
    create: (c) => ({ get: (u, o) => request('GET', u, null, Object.assign({}, c, o)), post: (u, d, o) => request('POST', u, d, Object.assign({}, c, o)) }),
  };
}

const sandbox = {
  console, process, setTimeout, clearTimeout, setInterval, clearInterval, Buffer, URL, URLSearchParams,
  require: function (name) {
    if (name === 'axios') { try { return require('axios'); } catch (e) { return makeAxiosPolyfill(); } }
    return require(name);
  },
  module: { exports: {} },
};
sandbox.global = sandbox; sandbox.exports = sandbox.module.exports;
vm.createContext(sandbox);
vm.runInContext(src, sandbox, { filename: 'hshj.js' });
const mod = sandbox.module.exports;

// ---- 诊断（不联网） ----
log('🔎 桥接兜底配置诊断');
log('注意: ticketCode 来自红包数据包(packet.ticketCode)，脚本并不读取 HSJJ_H5_TICKETCODE 环境变量，无需手动配置');
const entries = mod.YYB_ENTRIES || [];
log('YYB_SERVER 已设置         : ' + !!process.env.YYB_SERVER);
log('YYB_ENTRIES 数量          : ' + entries.length);
log('账号 ref 列表             : ' + (entries.map(e => e.ref).join(', ') || '(空)'));
log('AUTO_CLAIM_H5_RED_PACKET  : ' + (process.env.AUTO_CLAIM_H5_RED_PACKET !== '0') + ' (默认开，控制是否走桥接)');
log('HSJJ_MKTZB_OPENID 已设置  : ' + !!process.env.HSJJ_MKTZB_OPENID + ' (写死会全局串号，不建议)');

const prereqOk = entries.length > 0;
const prereqText = '硬前提是否满足（可进入桥接）: ' + (prereqOk ? '✅ 满足（另需：该微信已授权mktzb公众号 + YYB在线）' : '❌ 不满足——YYB_SERVER 未配/为空');
(prereqOk ? log : err)(prereqText);

const diagOnly = process.env.PROBE_DIAG_ONLY === '1';
if (diagOnly) {
  log('（PROBE_DIAG_ONLY=1：仅做配置诊断，未跑联网实测）');
  process.exit(0);
}
log('▶ 默认联网实测 resolveH5Openid（仅看诊断请设 PROBE_DIAG_ONLY=1）...');

// ---- 联网实测（--live） ----
(async () => {
  log('🚀 联网实测 resolveH5Openid');
  // ticketCode 在这里仅用作 OAuth state/prjCode 以触发桥接链路；用任一有效的 mktzb 活动码即可（默认取之前抓包里的 WV4OZBUWGP202609）
  const testCode = process.env.HSJJ_H5_PRJCODE || 'WV4OZBUWGP202609';
  for (const e of entries) {
    const ref = String(e.ref).split('#')[0].trim();
    log(`[账号 ${ref}] 调用 resolveH5Openid(testCode=${testCode}) ...`);
    try {
      // 真实签名: resolveH5Openid(wxid, ticketCode, cache, cacheKey)
      const o = await mod.resolveH5Openid(ref, testCode, null, null);
      if (o) log(`✅ 成功拿到 openid: ${o.substring(0, 8)}*** (完整: ${o})`);
      else warn('未拿到 openid（返回空）——检查 YYB-Go-Enhanced 是否在线 / 该微信是否登录并授权 mktzb 公众号 / WX_ID 对应微信是否已在 YYB 登录');
    } catch (error) {
      err('调用异常: ' + error.message);
    }
  }
  log('实测结束。若全部为空/异常，多半是网络不通或 YYB 服务/微信授权问题，与本次代码改动无关。');
})();
