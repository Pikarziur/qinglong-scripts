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

const HSHJ = path.join(__dirname, 'hshj.js');
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
console.log('========== 桥接兜底配置诊断 ==========');
const entries = mod.YYB_ENTRIES || [];
console.log('YYB_SERVER 已设置           :', !!process.env.YYB_SERVER);
console.log('YYB_ENTRIES 数量           :', entries.length);
console.log('  账号 ref 列表            :', entries.map(e => e.ref).join(', ') || '(空)');
console.log('HSJJ_H5_TICKETCODE 已设置  :', !!process.env.HSJJ_H5_TICKETCODE, process.env.HSJJ_H5_TICKETCODE ? '(非空)' : '(未设→桥接必失败)');
console.log('AUTO_CLAIM_H5_RED_PACKET   :', process.env.HSJJ_AUTO_CLAIM_H5 !== '0', '(默认开)');
console.log('HSJJ_MKTZB_OPENID 已设置   :', !!process.env.HSJJ_MKTZB_OPENID, '(写死会全局串号，不建议)');

const prereqOk = entries.length > 0 && !!process.env.HSJJ_H5_TICKETCODE;
console.log('----------------------------------------');
console.log('硬前提是否满足（可进入桥接）:', prereqOk ? '✅ 满足' : '❌ 不满足——' + (entries.length === 0 ? 'YYB_SERVER 未配/为空;' : '') + (!process.env.HSJJ_H5_TICKETCODE ? ' HSJJ_H5_TICKETCODE 未设' : ''));

if (!process.argv.includes('--live')) {
  console.log('\n提示：配置满足后，在能联网的环境运行 `node probe_resolve_h5.js --live` 才能真正调 resolveH5Openid 验证能否拿到 openid。');
  process.exit(0);
}

// ---- 联网实测（--live） ----
(async () => {
  console.log('\n========== 联网实测 resolveH5Openid ==========');
  const prjCode = process.env.HSJJ_H5_PRJCODE || 'WV4OZBUWGP202609';
  for (const e of entries) {
    const ref = String(e.ref).split('#')[0].trim();
    console.log(`\n[账号 ${ref}] 调用 resolveH5Openid ...`);
    try {
      const o = await mod.resolveH5Openid(ref, prjCode, 'cfyh');
      if (o) console.log(`  ✅ 成功拿到 openid: ${o.substring(0, 8)}*** (完整: ${o})`);
      else console.log('  ⚠️ 未拿到 openid（返回空）——检查 YYB-Go-Enhanced 是否在线 / 该微信是否登录并授权 mktzb 公众号 / HSJJ_H5_TICKETCODE 是否正确');
    } catch (err) {
      console.log('  ❌ 调用异常:', err.message);
    }
  }
  console.log('\n实测结束。若全部为空/异常，多半是网络不通或 YYB 服务/微信授权问题，与本次代码改动无关。');
})();
