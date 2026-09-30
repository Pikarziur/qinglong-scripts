/*
# name:福利吧 - 签到（Cookie 缓存为主 + 账号密码登录兜底）· 测试版
*/

// ────────────────────────────────────────────
// 【测试版】与 forum/wnflb2023.js 的差异：
//   · Cookie 两级兜底：本地缓存（主） → 账号密码登录（辅），不使用任何 Cookie 环境变量
//   · 无代理（N1 容器实测直连 200 / 1.12s；走 mihomo 7890 反而 6.38s）
//   · 无通知（notify 已在全仓移除）
//   · 响应按声明的 charset 解码（本站实测为 utf-8，保留兼容以防站点换编码）
//   ⚠️ 出网说明（2026-09-30 22:40 在 N1 青龙容器内实测）：
//      · 直连首页/登录页的 4 种组合（family=4 / auto × 两个 URL）**全部超时**
//      · 走 mihomo 代理 `MY_PROXY=http://192.168.31.233:7890` **全部 200**（首页 28976B / 登录页 25014B）
//      ⇒ 该容器**必须配代理**。（更早 curl 直连首页曾 200/1.12s，说明该站直连时通时不通，以预检实测为准）
//   ⚠️ 已显式发送 ALPN(http/1.1)：curl 默认发 ALPN 而 Node 默认不发，部分 CDN/WAF 会丢弃「无 ALPN 的
//      ClientHello」，典型表现就是「curl 秒回 200，Node 一直卡到超时」。
//   ⚠️ 地址族策略：默认**强制 IPv4**。家宽 / 容器里 IPv6 常是「解析得到地址但没有出口」的黑洞，
//      典型症状就是「curl 能通、Node 一直连到超时」；失败重试会自动换回系统默认（auto）双向兜底。
//
// 任务流程：
//   1. 取 Cookie：本地缓存为主；无缓存（或缓存失效）时用账号密码登录自动获取并写回缓存
//   2. 访问论坛首页，正则提取签到所需的 formhash
//   3. 请求 fx_checkin 签到接口完成每日签到
//   4. 判断成功 / 已签 / Cookie 过期；Cookie 失效时自动登录并续签一次
//
// 可控参数：
//   WNFLB_ACCOUNT       建议必配。账号密码，格式「账号#密码」或「账号:密码」；无缓存时靠它自动登录
//   WNFLB_COOKIE_CACHE  可选。缓存文件路径，默认 /ql/data/wnflb2023.cookie（该目录不可写则回退到脚本同目录）
//   WNFLB_FORCE_LOGIN   可选。=1 忽略缓存，强制走账号密码登录并覆盖缓存（调试用）
//   WNFLB_PROBE         可选。=1 仅预检：网络诊断（DNS + 4 种地址族/URL 组合）+ 登录页 formhash，不登录、不签到
//   WNFLB_DUMP_SIGN     可选。=1 签到成功/已签到时也把服务器响应原文打出来（默认关，排查用）
//   WNFLB_EXPIRE        可选。Cookie 预期过期日 YYYY-MM-DD，设了会提前 3 天提醒
//   WNFLB_TIMEOUT       可选。单次请求超时毫秒，默认 20000
//   WNFLB_IPV6          可选。=1 不强制 IPv4，完全按系统默认地址族走（默认优先 IPv4）
//   MY_PROXY            可选。http 代理（CONNECT 隧道），如 http://192.168.31.233:7890；直连不通时靠它。
//                       与仓库其他脚本（southplus.py / xsijishe.py）统一用这个变量名
//   WNFLB_PROXY         可选。同 MY_PROXY，仅本脚本的覆盖别名；两者都配时以它为准
//   WNFLB_NO_PROXY      可选。=1 忽略上述代理强制直连（面板的 MY_PROXY 是给别站用时可用）
//   WNFLB_SITE          可选。站点地址，默认 https://www.wnflb2023.com；仅供本地 mock 回归测试
//
// 日志规范：[LEVEL] [WNFLB] emoji message   （LEVEL: INFO / WARN / ERROR）
// 报错输出：所有失败路径都会打印「HTTP 状态 + 错误码 + 关键响应头 + 完整响应原文 + 异常堆栈」，
//           原文默认不截断（超过 64KB 才截断并注明剩余量）；成功路径不打印任何响应内容。
// ────────────────────────────────────────────

const https = require('https');
const http = require('http');
const tls = require('tls');
const fs = require('fs');
const path = require('path');
const dns = require('dns');
const { URL } = require('url');

// ========== 配置 ==========
// 版本标识：每次实质性改动 +1。启动日志会带上它，用来确认「容器里跑的到底是哪一版」
// （踩过坑：本机改了、容器没同步，日志看着像"修复没生效"，实际是跑着旧文件）。
const SCRIPT_VER = '2026-09-30c';
// 站点地址：默认福利吧。WNFLB_SITE 仅用于本地 mock 回归测试（默认不影响线上行为）。
const SITE = (process.env.WNFLB_SITE || 'https://www.wnflb2023.com').replace(/\/+$/, '');
const UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36';
const AUTH_KEY = 'S5r8_2132_auth';        // Discuz 登录凭证（本站在用前缀）
const SALT_KEY = 'S5r8_2132_saltkey';      // Discuz 盐值
const MAX_RETRY = 3;
const truthy = (v) => ['1', 'true', 'yes', 'on'].includes(String(v || '').trim().toLowerCase());
const SITE_HOST = new URL(SITE).hostname;
const TIMEOUT_MS = Math.max(3000, parseInt(process.env.WNFLB_TIMEOUT || '20000', 10) || 20000);

// 代理地址：优先 WNFLB_PROXY（本脚本专用覆盖），其次 MY_PROXY（与仓库其他脚本统一：
// southplus.py / xsijishe.py 都用 MY_PROXY）。两者任配其一即可；都配时 WNFLB_PROXY 生效。
// 若面板里的 MY_PROXY 是给别的站点用的、而本站直连也通，可用 WNFLB_NO_PROXY=1 让本脚本忽略它。
const NO_PROXY = truthy(process.env.WNFLB_NO_PROXY);
const PROXY = NO_PROXY ? '' : (process.env.WNFLB_PROXY || process.env.MY_PROXY || '').trim();
const PROXY_SRC = (process.env.WNFLB_PROXY || '').trim() ? 'WNFLB_PROXY'
    : ((process.env.MY_PROXY || '').trim() ? 'MY_PROXY' : '');
const PROXY_DESC = NO_PROXY ? '已按 WNFLB_NO_PROXY=1 强制直连'
    : (PROXY ? `${PROXY}（来源 ${PROXY_SRC}）` : '未配置（纯直连）');

// 地址族策略：**默认强制 IPv4**。
// 原因：家宽 / Docker 容器里 IPv6 常见「解析得到地址但没有出口路由」的黑洞，
// 典型表现就是「curl 能通（happy-eyeballs 会快速回落 IPv4），Node 却一直连到超时」。
// 本站 CDN 有 IPv4，故优先打 IPv4；第 2 次重试换回系统默认（auto），两个方向都留兜底。
// 设 WNFLB_IPV6=1 则不强制，完全按系统默认走。
const FAMILY_SEQ = truthy(process.env.WNFLB_IPV6) ? [0] : [4, 0];

let COOKIE = '';                            // 当前生效的 Cookie（发送时使用）

// ========== 统一日志 ==========
function emit(level, msg) {
    console.log(`[${level}] [WNFLB] ${msg}`);
}
function log(msg)  { emit('INFO', msg); }
function warn(msg) { emit('WARN', msg); }
function err(msg)  { emit('ERROR', msg); }

// ========== 报错详情（仅在**出错路径**调用，成功路径不打印）==========
// 目的：报错时一次打全「HTTP 状态 / 错误码 / 关键响应头 / 完整响应原文 / 异常堆栈」，
//       不用再猜是网络、Cookie 还是接口返回了别的东西。
// 说明：响应原文默认完整打印（不截断）；仅在超过 DUMP_LIMIT 时截断并注明剩余量，防止日志被撑爆。
const DUMP_LIMIT = 65536;

function headBrief(headers) {
    const h = headers || {};
    const sc = h['set-cookie'];
    const names = (Array.isArray(sc) ? sc : [sc])
        .filter(Boolean)
        .map(c => String(c).split('=')[0].trim())
        .filter(Boolean);
    return {
        ct: h['content-type'] || '-',
        loc: h['location'] || '-',
        cookies: names.length ? names.join(', ') : '-'
    };
}

function dumpResp(tag, resp) {
    if (!resp) { warn(`⚠️ [${tag}] 无响应对象（请求未成功返回）`); return; }
    const text = textOf(resp);
    const b = headBrief(resp.headers);
    warn(`⚠️ [${tag}] HTTP ${resp.status}｜原始 ${resp.raw ? resp.raw.length : 0} 字节｜解码后 ${text.length} 字符`);
    warn(`⚠️ [${tag}] content-type: ${b.ct}`);
    if (b.loc !== '-') warn(`⚠️ [${tag}] location: ${b.loc}`);
    warn(`⚠️ [${tag}] set-cookie: ${b.cookies}`);
    let bodyOut = text || '(空响应)';
    if (text.length > DUMP_LIMIT) {
        bodyOut = text.substring(0, DUMP_LIMIT) + `\n...(响应过长，已截断，剩余 ${text.length - DUMP_LIMIT} 字符)`;
    }
    warn(`⚠️ [${tag}] 响应原文:\n${bodyOut}`);
}

function dumpErr(tag, e) {
    const code = (e && e.code) ? ` code=${e.code}` : '';
    const eno = (e && e.errno) ? ` errno=${e.errno}` : '';
    const url = (e && e.url) ? ` url=${e.url}` : '';
    const msg = (e && e.message) ? e.message : String(e);
    warn(`⚠️ [${tag}] 异常: ${msg}${code}${eno}${url}`);
    const st = (e && e.stack) ? String(e.stack) : '';
    if (st) warn(`⚠️ [${tag}] 异常堆栈:\n${st}`);
}

// ========== 极简 Cookie Jar ==========
// 登录过程中需要跨请求累积 Cookie（saltkey 在 GET 登录页下发，auth 在 POST 登录后下发）
class CookieJar {
    constructor() { this.map = new Map(); }
    absorb(headers) {
        const sc = (headers && headers['set-cookie']) || [];
        const list = Array.isArray(sc) ? sc : [sc];
        for (const line of list) {
            if (!line) continue;
            const kv = String(line).split(';')[0];
            const i = kv.indexOf('=');
            if (i <= 0) continue;
            const name = kv.slice(0, i).trim();
            const value = kv.slice(i + 1).trim();
            if (name) this.map.set(name, value);
        }
    }
    get(name) { return this.map.get(name) || ''; }
    size() { return this.map.size; }
    toString() { return [...this.map].map(([k, v]) => `${k}=${v}`).join('; '); }
}

// ========== 响应解码（按声明 charset 自适应）==========
// 本站实测 HTTP 头与 meta 均声明 charset=utf-8；此处仍做兼容，防止站点换编码后中文关键字全部匹配失败。
function tryDecode(buf, label) {
    try { return new TextDecoder(label, { fatal: false }).decode(buf); }
    catch (e) { return ''; }
}
function isStrictUtf8(buf) {
    // GBK 的中文双字节序列几乎不可能通过 UTF-8 严格校验，因此这是比「比中文个数」可靠得多的判据。
    // （反例：把本站 utf-8 字节按 GBK 硬解能解出 1140 个「假中文」，比正确的 889 还多。）
    try { new TextDecoder('utf-8', { fatal: true }).decode(buf); return true; }
    catch (e) { return false; }
}
function textOf(resp) {
    const u = (resp && resp.body) || '';
    if (!resp || !resp.raw || !resp.raw.length) return u;

    // 1) 优先按声明的 charset 解码：HTTP 头 > HTML meta
    const ct = (resp.headers && resp.headers['content-type']) || '';
    const m = ct.match(/charset=["']?([\w-]+)/i) || u.match(/charset=["']?([\w-]+)/i);
    const label = m ? m[1].toLowerCase() : '';
    if (['utf-8', 'utf8', 'us-ascii', 'ascii'].includes(label)) return u;
    if (label) {
        const t = tryDecode(resp.raw, label);
        if (t) return t;
    }

    // 2) 未声明 charset：先验 UTF-8 合法性，不合法再按 GBK 解
    if (isStrictUtf8(resp.raw)) return u;
    return tryDecode(resp.raw, 'gbk') || u;
}

// ========== Cookie 缓存 ==========
function resolveCachePath() {
    if (process.env.WNFLB_COOKIE_CACHE) return process.env.WNFLB_COOKIE_CACHE;
    const primary = '/ql/data/wnflb2023.cookie';
    if (fs.existsSync(primary)) return primary;
    try { fs.accessSync(path.dirname(primary), fs.constants.W_OK); return primary; }
    catch (e) { /* 青龙持久目录不可用（如本机调试）→ 回退脚本同目录 */ }
    return path.join(__dirname, '.wnflb2023.cookie');
}
const CACHE_FILE = resolveCachePath();

function readCache() {
    try {
        if (!fs.existsSync(CACHE_FILE)) return null;
        const txt = fs.readFileSync(CACHE_FILE, 'utf8').trim();
        if (!txt) return null;
        if (txt.startsWith('{')) {
            try {
                const o = JSON.parse(txt);
                const c = (o && o.cookie) ? String(o.cookie).trim() : '';
                return c ? { cookie: c, source: o.source || '-', savedAt: o.savedAt || '-' } : null;
            } catch (e) {
                warn('⚠️ 缓存文件 JSON 解析失败（' + (e.message || e) + '），按纯文本 Cookie 处理');
                warn('⚠️ 缓存文件内容前 200 字符: ' + txt.substring(0, 200));
            }
        }
        return { cookie: txt, source: 'legacy-plain', savedAt: '-' };  // 兼容旧版纯文本缓存
    } catch (e) {
        warn('⚠️ 读取 Cookie 缓存失败: ' + (e.message || e));
        return null;
    }
}

function writeCache(cookie, source) {
    if (!cookie) return;
    try {
        fs.mkdirSync(path.dirname(CACHE_FILE), { recursive: true });
        const payload = JSON.stringify({
            v: 1,
            source: source || '-',
            savedAt: new Date().toISOString(),
            cookie
        }, null, 2);
        fs.writeFileSync(CACHE_FILE, payload, { mode: 0o600 });
        log(`💾 Cookie 已写入缓存（${source || '-'}）: ${CACHE_FILE}`);
    } catch (e) {
        warn('⚠️ Cookie 缓存写入失败: ' + (e.message || e));
    }
}

// ========== 过期日提醒（不阻断）==========
function checkCookieExpire(expireStr) {
    if (!expireStr) {
        log('💡 未配置 WNFLB_EXPIRE，仅在线检测生效（设 YYYY-MM-DD 开启日期提醒）');
        return;
    }
    const exp = new Date(expireStr + 'T23:59:59');
    if (isNaN(exp.getTime())) {
        warn('📝 WNFLB_EXPIRE 格式错误，应为 YYYY-MM-DD');
        return;
    }
    const remainDays = Math.ceil((exp - new Date()) / 86400000);
    const status = remainDays >= 0 ? `剩余${remainDays}天` : `已过期${-remainDays}天`;
    log(`🍪 Cookie ${status} | 过期: ${expireStr}`);
    if (remainDays < 0) {
        err('❌ Cookie 已过期，将尝试自动重新登录');
    } else if (remainDays <= 3) {
        warn(`⏰ Cookie 即将过期（${remainDays}天），建议尽快更新！`);
    }
}

// ========== HTTP 代理（CONNECT 隧道）==========
// 直连不通时的兜底，例如 MY_PROXY=http://192.168.31.233:7890（N1 上 mihomo 的 mixed 端口）。
// 走标准 CONNECT 隧道：代理只做 TCP 转发，到目标站的 TLS 依然端到端加密。
function proxyTunnel(proxyUrl, targetHost, targetPort, timeoutMs) {
    return new Promise((resolve, reject) => {
        let p;
        try { p = new URL(proxyUrl); } catch (e) {
            reject(new Error(`代理地址格式错误（${PROXY_SRC || 'MY_PROXY'}，应形如 http://host:port）: ${proxyUrl}`));
            return;
        }
        if (p.protocol !== 'http:' && p.protocol !== 'https:') {
            reject(new Error(`暂不支持 ${p.protocol} 代理（仅支持 http:// / https:// 的 CONNECT 隧道；`
                + 'socks5:// 请改用 mihomo 的 mixed 或 http 端口）: ' + proxyUrl));
            return;
        }
        const secure = p.protocol === 'https:';
        const lib = secure ? https : http;
        const proxyPort = p.port || (secure ? 443 : 80);
        const auth = p.username
            ? 'Basic ' + Buffer.from(`${decodeURIComponent(p.username)}:${decodeURIComponent(p.password || '')}`).toString('base64')
            : '';
        const req = lib.request({
            host: p.hostname,
            port: proxyPort,
            method: 'CONNECT',
            path: `${targetHost}:${targetPort}`,
            headers: Object.assign({ 'Host': `${targetHost}:${targetPort}` }, auth ? { 'Proxy-Authorization': auth } : {}),
            timeout: timeoutMs
        });
        req.on('connect', (res, socket) => {
            if (res.statusCode !== 200) {
                socket.destroy();
                const e = new Error(`代理 CONNECT ${targetHost}:${targetPort} 被拒绝: HTTP ${res.statusCode} ${res.statusMessage || ''}`);
                e.code = 'EPROXYCONNECT';
                reject(e);
                return;
            }
            resolve(socket);
        });
        req.on('error', reject);
        req.on('timeout', () => {
            req.destroy();
            const e = new Error(`代理 CONNECT 超时（${p.hostname}:${proxyPort} 超过 ${timeoutMs}ms）`);
            e.code = 'ETIMEDOUT';
            reject(e);
        });
        req.end();
    });
}

// 在隧道 socket 上完成 TLS 握手（SNI 仍是目标域名），握手失败可在请求前就精确定位
function wrapTls(socket, hostname, timeoutMs) {
    return new Promise((resolve, reject) => {
        const t = tls.connect({ socket, servername: hostname, ALPNProtocols: ['http/1.1'] });
        t.setTimeout(timeoutMs, () => {
            t.destroy();
            const e = new Error(`代理隧道 TLS 握手超时（${hostname} 超过 ${timeoutMs}ms）`);
            e.code = 'ETIMEDOUT';
            reject(e);
        });
        t.once('secureConnect', () => { t.setTimeout(0); resolve(t); });
        t.once('error', reject);
    });
}

// 把隧道 socket 交给请求使用。
// ⚠️ 不能用「agent:false + options.createConnection」：Node 在 agent:false 时会忽略该回调，
//    请求会退回直连 —— 实测现象就是「CONNECT 隧道都建好了，却仍去连真实 IP 然后 ETIMEDOUT」。必须用自定义 Agent。
function makeTunnelAgent(tunnel, secure) {
    if (secure) {
        class TunnelAgent extends https.Agent {
            createConnection() { return tunnel; }
        }
        return new TunnelAgent({ keepAlive: false, maxSockets: 1 });
    }
    class PlainTunnelAgent extends http.Agent {
        createConnection() { return tunnel; }
    }
    return new PlainTunnelAgent({ keepAlive: false, maxSockets: 1 });
}

// ========== HTTP（直连 or 走代理隧道，代理取 MY_PROXY / WNFLB_PROXY）==========
async function requestOnce(url, options, headers, family) {
    const u = new URL(url);
    const timeoutMs = options.timeoutMs || TIMEOUT_MS;
    const isHttps = u.protocol === 'https:';
    const port = u.port || (isHttps ? 443 : 80);
    const proxy = (options.proxy !== undefined ? options.proxy : PROXY).trim();

    const opts = {
        method: options.method || 'GET',
        hostname: u.hostname,
        port,
        path: u.pathname + u.search,
        headers,
        timeout: timeoutMs,
        // curl 默认会发 ALPN 扩展，Node 默认不发。部分 CDN/WAF 对「无 ALPN 的 ClientHello」直接丢弃连接，
        // 表现就是「curl 秒回 200，Node 一直卡到超时」。这里只声明 http/1.1（不能声明 h2，
        // 否则服务端会切到 HTTP/2 而 Node 的 http 模块解析不了）。
        ALPNProtocols: ['http/1.1']
    };
    // family=4/6 时只解析并使用该地址族；不指定（0/undefined）则交给系统 autoSelectFamily
    if (family) opts.family = family;

    if (proxy) {
        let tunnel;
        try {
            tunnel = await proxyTunnel(proxy, u.hostname, port, timeoutMs);
            if (isHttps) tunnel = await wrapTls(tunnel, u.hostname, timeoutMs);
        } catch (e) {
            if (!e.url) e.url = `${opts.method} ${url} (via ${proxy})`;
            throw e;
        }
        if (opts.family) delete opts.family;   // 隧道由代理建立，地址族不再适用
        opts.agent = makeTunnelAgent(tunnel, isHttps);
    }

    return new Promise((resolve, reject) => {
        const req = (isHttps ? https : http).request(opts, (res) => {
            const chunks = [];
            res.on('data', c => chunks.push(c));           // chunk 为 Buffer，保留原始字节便于 GBK 解码
            res.on('end', () => {
                const raw = Buffer.concat(chunks);
                resolve({ status: res.statusCode, headers: res.headers, body: raw.toString('utf8'), raw });
            });
        });
        req.on('error', reject);
        req.setTimeout(timeoutMs, () => {
            req.destroy();
            const via = proxy ? ` via ${proxy}` : '';
            const famTag = proxy ? 'proxy' : (family || 'auto');
            const te = new Error(`请求超时（${opts.method} ${u.hostname}${u.pathname} 超过 ${timeoutMs}ms，family=${famTag}${via}）`);
            te.code = 'ETIMEDOUT';
            te.url = url;
            reject(te);
        });
        if (options.body) req.write(options.body);
        req.end();
    });
}

async function request(url, options = {}) {
    const headers = Object.assign({
        'User-Agent': UA,
        'Accept': '*/*',
        'Accept-Language': 'zh-CN,zh;q=0.9',
        'Referer': SITE + '/',
        'Cookie': options.cookie !== undefined ? options.cookie : COOKIE
    }, options.headers || {});
    // 显式给 Content-Length：否则 Node 对 write() 的数据走 Transfer-Encoding: chunked。
    // 标准 HTTP 实践如此，也能规避部分 WAF/nginx 对 chunked POST 的不友好处理（未在本站证实）。
    if (options.body && headers['Content-Length'] === undefined) {
        headers['Content-Length'] = Buffer.byteLength(options.body);
    }
    let lastErr = null;
    const maxTry = options.noRetry ? 1 : MAX_RETRY;   // 登录 POST 不重试：避免把失败次数打满触发账号锁定
    const method = options.method || 'GET';
    const proxyOn = !!PROXY;
    const seq = proxyOn ? [0] : FAMILY_SEQ;   // 走代理隧道时地址族无意义，不做切换
    const famAt = (i) => seq[Math.min(i, seq.length - 1)];
    for (let i = 0; i < maxTry; i++) {
        const family = famAt(i);
        try {
            return await requestOnce(url, options, headers, family);
        } catch (e) {
            lastErr = e;
            if (e && !e.url) e.url = `${method} ${url}`;
            if (i < maxTry - 1) {
                const next = famAt(i + 1);
                warn(`🔁 请求失败（第${i + 1}/${maxTry}次，family=${family || 'auto'} → 换 family=${next || 'auto'} 重试）: ${e.message}` + (e.code ? ` code=${e.code}` : ''));
            }
        }
    }
    if (lastErr && lastErr.code === 'ETIMEDOUT' && !proxyOn) {
        warn('💡 直连超时且未配置代理：若这台机器无法直连本站，可设 MY_PROXY=http://<mihomo地址>:7890 走代理');
    }
    throw lastErr || new Error('请求失败: ' + url);
}

function getCookieVal(name, cookieStr) {
    // 兼容 Cookie 串用 "; " 或 ";" 分隔（Cookie-Editor 等工具导出常为无空格分隔）
    const m = String(cookieStr || '').match(new RegExp('(?:^|;\\s*)' + name + '=([^;]*)'));
    if (!m) return '';
    // 仅用于「字段是否存在」判断；畸形 % 序列不能让脚本崩，解不开就退回原值
    try { return decodeURIComponent(m[1]); } catch (e) { return m[1]; }
}

// ⚠️ Discuz 的 `dsetcookie()` 在「删除该 cookie」时下发的值是 `deleted`（不是空串）。
//    所以 `auth=deleted` 表示**已清除登录态**，绝不能当成有效凭证 —— 实测踩过：
//    首次登录后 auth 被清为 deleted，脚本却判为登录成功、写入缓存，导致紧接着的签到返回「未登录」。
const isDeletedCookieVal = (v) => /^deleted$/i.test(String(v || '').trim());

// 真正判断「Cookie 里有没有可用的登录态」——存在 auth 且不是 deleted 才算。
function isAuthValid(cookieStr) {
    const v = getCookieVal(AUTH_KEY, cookieStr);
    return !!v && !isDeletedCookieVal(v);
}

// ========== 账号密码登录（兜底）==========
// 单次登录尝试。返回 { state, cookie }，state 含义：
//   'ok'      拿到有效 auth（非 deleted）+ saltkey
//   'deleted' 服务端把 auth 置为 deleted —— 这是「清除登录态」而非「凭据错误」，**可安全重试一次**
//   'cred'    响应明确是「登录失败…还可以尝试 N 次」→ 凭据问题，绝不重试（Discuz 会累计失败次数）
//   'nomark'  没拿到凭证、也没有明确失败原因（风控 / 验证码 / 响应不是登录结果页）
async function loginAttempt(user, pass, attemptNo) {
    const jar = new CookieJar();
    const loginPageUrl = SITE + '/member.php?mod=logging&action=login';
    try {
        // 1) 匿名访问登录页，取 formhash（清空 Cookie，避免带失效态）
        log('🌐 访问登录页取 formhash...');
        const page = await request(loginPageUrl, { cookie: '', headers: { 'Referer': SITE + '/' } });
        jar.absorb(page.headers);
        const pageText = textOf(page);
        const lfm = pageText.match(/formhash=([a-f0-9]{8})/)
                 || pageText.match(/name=["']formhash["'][^>]*value=["']([a-f0-9]{8})["']/i);
        if (!lfm) {
            warn('⚠️ 登录页未取到 formhash，无法构造登录请求 → 登录链路不可用');
            dumpResp('登录页', page);
            return null;
        }
        const formhash = lfm[1];

        // 2) 提交登录（该站登录页无验证码，纯账号密码即可）
        const loginUrl = SITE + '/member.php?mod=logging&action=login&loginsubmit=yes&infloat=yes&lssubmit=yes&inajax=1';
        const bodyStr = [
            'formhash=' + formhash,
            'username=' + encodeURIComponent(user),
            'password=' + encodeURIComponent(pass),
            'cookietime=2592000',
            'questionid=0',
            'answer=',
            'loginsubmit=yes'
        ].join('&');
        log(`📤 提交登录${attemptNo > 1 ? `...（第 ${attemptNo} 次尝试）` : '...'}`);
        const r = await request(loginUrl, {
            method: 'POST',
            cookie: jar.toString(),
            noRetry: true,          // 登录失败会被 Discuz 计数，重试会加速触发账号/IP 锁定
            headers: {
                'Content-Type': 'application/x-www-form-urlencoded',
                'Referer': loginPageUrl,
                'X-Requested-With': 'XMLHttpRequest'
            },
            body: bodyStr
        });
        jar.absorb(r.headers);
        const rText = textOf(r);

        // 3) 判定：必须拿到**有效** auth（deleted 不算）+ saltkey
        const auth = jar.get(AUTH_KEY);
        const saltkey = jar.get(SALT_KEY);
        const authOk = !!auth && !isDeletedCookieVal(auth);
        if (authOk && saltkey) {
            log(`🔑 登录成功（auth+saltkey，共 ${jar.size()} 个 Cookie 字段）`);
            log(`🍪 ${AUTH_KEY}=${auth.substring(0, 12)}***`);
            return { state: 'ok', cookie: jar.toString() };
        }

        // auth=deleted：Discuz 的清除标记。登录流程本身没报错，只是会话被清 —— 可安全重试一次。
        if (isDeletedCookieVal(auth)) {
            warn(`⚠️ 服务端把 ${AUTH_KEY} 置为 deleted（Discuz 的「清除登录态」标记），本次登录态无效`);
            // 打印本次响应下发的全部 Set-Cookie（含先后顺序）：用来判断是「只发了 deleted」，
            // 还是「真值在前、被后面的 deleted 覆盖」—— 这两种情况的处理方向不一样。
            const scList = [].concat(r.headers['set-cookie'] || []).filter(Boolean).map(c => {
                const kv = String(c).split(';')[0].trim();
                const i = kv.indexOf('=');
                if (i <= 0) return kv;
                const n = kv.slice(0, i), v = kv.slice(i + 1);
                return (n === AUTH_KEY && v && !isDeletedCookieVal(v)) ? `${n}=${v.slice(0, 8)}***` : kv;
            });
            warn(`🍪 本次 Set-Cookie（${scList.length} 条，按先后顺序）: ${scList.join(' | ') || '(无)'}`);
            return { state: 'deleted', resp: r };
        }

        warn(`⚠️ 登录失败（HTTP ${r.status}）：未拿到登录凭证`);
        // Discuz 会把失败原因塞在 XML CDATA 里，例如：
        //   <![CDATA[登录失败，您还可以尝试 4 次 ... {'loginperm':'4'}]]>
        const perm = rText.match(/还可以尝试\s*(\d+)\s*次/) || rText.match(/['"]loginperm['"]\s*:\s*['"](\d+)['"]/);
        if (/登录失败/.test(rText)) {
            warn(`🚫 账号或密码不正确${perm ? `（短时间内还可尝试 ${perm[1]} 次）` : ''}：请核对 WNFLB_ACCOUNT 的「账号#密码」，不要连续重试以免被临时禁止登录`);
            if (user.length <= 2) warn(`🚫 另外注意：当前账号只有 ${user.length} 个字符（${user}），像是占位符没被替换成真实账号`);
            dumpResp('登录POST', r);
            return { state: 'cred', resp: r };
        }
        if (/验证码|seccode|secqaa|安全提问/.test(rText)) {
            warn('🚫 站点要求验证码/安全提问：本次登录已被风控，建议先用浏览器登录一次或稍后再试');
        } else if (!auth) {
            warn(`⚠️ 未拿到 ${AUTH_KEY}，常见原因：账号密码错误 / 需安全提问 / 触发登录频率限制 / 响应不是登录结果页`);
        } else {
            warn(`⚠️ 已拿到 ${AUTH_KEY} 但缺少 ${SALT_KEY}（登录态不完整）`);
        }
        dumpResp('登录POST', r);
        return { state: 'nomark', resp: r };
    } catch (e) {
        dumpErr('登录流程', e);
        return { state: 'nomark' };
    }
}

// 外层封装：先试一次；只有「auth 被清成 deleted」这种**非凭据问题**才会重试一次
// （实测该站首次登录常返回 auth=deleted，第二次即正常；凭据错误则绝不重试，避免打满 Discuz 的失败计数）
const MAX_LOGIN_ATTEMPT = 2;
async function loginAndGetCookie(user, pass) {
    if (!user || !pass) {
        warn('⚠️ 未配置账号密码（WNFLB_ACCOUNT=账号#密码），无法自动登录');
        return null;
    }
    for (let i = 1; i <= MAX_LOGIN_ATTEMPT; i++) {
        const r = await loginAttempt(user, pass, i);
        if (r.state === 'ok') return r.cookie;
        if (r.state === 'deleted' && i < MAX_LOGIN_ATTEMPT) {
            warn(`🔁 登录态被服务端清空（非账号密码问题），重试登录（第 ${i + 1}/${MAX_LOGIN_ATTEMPT} 次）...`);
            continue;
        }
        if (r.state === 'deleted') warn('🚫 连续两次都被清成 deleted：站点可能在风控 / 要求安全提问，建议先用浏览器登录一次');
        return null;
    }
    return null;
}

// ========== Cookie 解析：缓存（主） → 账号密码登录（辅）==========
async function resolveCookie(user, pass) {
    const forceLogin = truthy(process.env.WNFLB_FORCE_LOGIN);

    // 1) 本地缓存为主
    if (!forceLogin) {
        const cached = readCache();
        if (cached && isAuthValid(cached.cookie)) {
            COOKIE = cached.cookie;
            log(`📦 使用本地缓存 Cookie（来源: ${cached.source} | 写入: ${cached.savedAt}）`);
            return true;
        }
        if (cached) {
            const names = String(cached.cookie || '').split(';').map(kv => kv.split('=')[0].trim()).filter(Boolean);
            if (isDeletedCookieVal(getCookieVal(AUTH_KEY, cached.cookie))) {
                warn(`⚠️ 缓存里的 ${AUTH_KEY} 是 deleted（Discuz 的「已登出」标记），视为无效，改走账号密码登录`);
            } else {
                warn(`⚠️ 缓存存在但缺少 ${AUTH_KEY}，视为无效，改走账号密码登录（缓存内 ${names.length} 个字段: ${names.join(', ') || '(空)'}）`);
            }
        }
    } else {
        warn('🔁 WNFLB_FORCE_LOGIN 已开启：忽略缓存，强制账号密码登录');
    }

    // 2) 账号密码登录兜底：无缓存 / 缓存无效时自动登录，成功后把新 Cookie 写回缓存
    if (user && pass) {
        log('🔐 无可用缓存，尝试账号密码登录获取 Cookie...');
        const fresh = await loginAndGetCookie(user, pass);
        if (fresh) {
            COOKIE = fresh;
            writeCache(fresh, 'login');
            return true;
        }
        return false;
    }

    if (forceLogin) warn('⚠️ 已强制登录但未配置 WNFLB_ACCOUNT，回退尝试缓存');
    return false;
}

// ========== 签到响应分类 ==========
// 顺序很重要：先看最强的成功信号（creditrule Set-Cookie / 明确的「签到成功」），
// 再判「已签」，最后才用宽泛的「成功」兜底，避免把已签误判成成功。
// ⚠️ 注意 `已签` 并不是 `已经签到` 的子串（已/经/签/到），所以两种写法都要列。
// Discuz 弹出框模板里的固定文案，本身不含业务信息（"提示信息 / 关闭 / 确定"）。
// 摘要里出现这些词说明真正的那句话在别处（通常在 <script> 里），需要走下面的抢救逻辑。
const UI_NOISE = ['提示信息', '关闭', '确定', '返回上一页', '返回', '点击这里', '点击此处', '继续访问'];

// 把响应正文压成一行便于阅读的摘要（去 script/style / 去标签 / 压空白 / 截断）。
// 只用于结果那一行日志，避免 dump 整个响应造成日志臃肿。
//
// ⚠️ 踩过两个坑，都在这里收口：
//   1) 写成 `<[^>]+>` 会把 Discuz 的 `<![CDATA[...]]>` 整段正文当标签吞掉 → 摘要恒为空。
//      所以标签正则必须以字母或 / 开头，并且先把 CDATA 的括号剥掉。
//   2) Discuz 的 showmessage/showDialog/errorhandle_ 是把提示语**写在 <script> 的字符串里**，
//      HTML 骨架只剩"提示信息 / 关闭 / 确定"这类模板词。直接删掉 script 就只剩噪声，
//      所以删之前先把 script 里的中文字符串抢救出来当候选。
function brief(text, n) {
    let s = String(text || '');
    if (!s.trim()) return '';

    // 1) XML 声明 / DOCTYPE（`<?...?>` 不以字母开头，标签正则吃不到，得单独处理）
    s = s.replace(/<\?[\s\S]*?\?>/g, ' ').replace(/<!DOCTYPE[^>]*>/gi, ' ');

    // 2) 从 <script> 里抢救中文字符串提示语（抢救完再丢弃整个 script 块）
    const salvaged = [];
    s = s.replace(/<script[\s\S]*?<\/script>/gi, (blk) => {
        const re = /['"]([^'"]{1,120})['"]/g;
        let m;
        while ((m = re.exec(blk)) !== null) salvaged.push(m[1]);
        return ' ';
    });

    // 3) 去 style / 剥 CDATA 括号 / 去真标签 / 解实体 / 压空白
    s = s.replace(/<style[\s\S]*?<\/style>/gi, ' ')
        .replace(/<!\[CDATA\[/g, ' ')
        .replace(/\]\]>/g, ' ')
        .replace(/<[a-zA-Z/][^>]*>/g, ' ')
        .replace(/&nbsp;/gi, ' ')
        .replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&quot;/g, '"').replace(/&amp;/g, '&')
        .replace(/\s+/g, ' ')
        .trim();

    // 4) 可见正文若只剩模板词，等于没信息 → 改用 script 里抢救出来的那句话
    const strip = (t) => {
        let x = String(t || '');
        for (const w of UI_NOISE) x = x.split(w).join(' ');
        return x.replace(/\s+/g, ' ').trim();
    };
    let out = strip(s);
    if (!out) {
        const cand = [];
        for (let t of salvaged) {
            t = strip(t);
            if (!t || !/[\u4e00-\u9fa5]/.test(t) || cand.includes(t)) continue;
            cand.push(t);
        }
        out = cand.join(' / ');
    }

    const lim = n || 80;
    return out.length > lim ? out.slice(0, lim) + '…' : out;
}

function classifySign(signResp) {
    const text = textOf(signResp);
    const setCookie = signResp.headers['set-cookie'] || [];
    const scList = Array.isArray(setCookie) ? setCookie : [setCookie];

    if (scList.some(c => c && c.includes('creditrule'))) return 'success';   // 签到成功会下发 credit* Cookie
    if (/签到成功|已领取|获得/.test(text)) return 'success';
    if (/已经签到|已签|重复|明天再来|下次再来/.test(text)) return 'already';
    if (/成功/.test(text)) return 'success';
    if (/请先登录|未登录|需要登录|login/.test(text) || signResp.status === 302) return 'expired';
    return 'unknown';
}

// ========== 预检专用：网络层诊断 ==========
// 直连报错时最难判断的是「整站连不上」还是「某个地址族 / 某条 URL 有问题」。这里一次测全：
//   · DNS 解析出的全部 IPv4 / IPv6 地址
//   · 「首页 / 登录页」×「强制 IPv4 / 系统默认 auto」共 4 种组合，各自的耗时与结果
async function netDiag(timeoutMs) {
    const t = timeoutMs || 10000;
    try {
        const addrs = await dns.promises.lookup(SITE_HOST, { all: true, verbatim: true });
        const v4 = addrs.filter(a => a.family === 4).map(a => a.address);
        const v6 = addrs.filter(a => a.family === 6).map(a => a.address);
        log(`🧭 DNS ${SITE_HOST} → IPv4 [${v4.join(', ') || '无'}]｜IPv6 [${v6.join(', ') || '无'}]`);
        if (v6.length && !v4.length) warn('⚠️ 只解析出 IPv6：容器若没有 IPv6 出口会全部超时');
    } catch (e) {
        warn(`🧭 DNS 解析 ${SITE_HOST} 失败: ${e.message}${e.code ? ' code=' + e.code : ''}`);
    }
    const targets = [
        ['首页  ', SITE + '/'],
        ['登录页', SITE + '/member.php?mod=logging&action=login']
    ];
    const baseHeaders = {
        'User-Agent': UA,
        'Accept': '*/*',
        'Accept-Language': 'zh-CN,zh;q=0.9',
        'Referer': SITE + '/',
        'Cookie': ''
    };
    let directOk = false, proxyOk = false;
    const probeOne = async (label, url, opts, fam, mark) => {
        const t0 = Date.now();
        try {
            const r = await requestOnce(url, opts, baseHeaders, fam);
            log(`📡 ${label} → HTTP ${r.status}｜${r.raw ? r.raw.length : 0} 字节｜${Date.now() - t0}ms`);
            if (mark === 'direct') directOk = true;
            if (mark === 'proxy') proxyOk = true;
        } catch (e) {
            warn(`📡 ${label} → 失败 ${e.message}${e.code ? ' code=' + e.code : ''}（${Date.now() - t0}ms）`);
        }
    };
    // 1) 直连：分别测「强制 IPv4」与「系统默认」，用来区分是 IPv6 黑洞还是整站不可达
    for (const [name, url] of targets) {
        for (const fam of [4, 0]) {
            await probeOne(`${name} 直连 family=${fam || 'auto'}`, url, { method: 'GET', timeoutMs: t, proxy: '' }, fam, 'direct');
        }
    }
    // 2) 代理：配了 MY_PROXY / WNFLB_PROXY 才测
    if (PROXY) {
        for (const [name, url] of targets) {
            await probeOne(`${name} 代理 ${PROXY}`, url, { method: 'GET', timeoutMs: t }, 0, 'proxy');
        }
    } else {
        log('🔌 未配置代理（MY_PROXY），跳过代理测试（直连不通时可用它走 mihomo）');
    }
    return { directOk, proxyOk };
}

// ========== 预检：只探测，不登录、不签到 ==========
async function runProbe(user, pass) {
    log(`🔎 预检模式（WNFLB_PROBE=1）：只探测，不登录、不签到｜脚本 v${SCRIPT_VER}`);
    log(`📁 Cookie 缓存路径: ${CACHE_FILE}`);
    log(`⏱️ 单次超时 ${TIMEOUT_MS}ms｜地址族顺序 [${FAMILY_SEQ.map(f => f || 'auto').join(' → ')}]`);
    log(`🔌 代理: ${PROXY_DESC}`);
    const diag = await netDiag(Math.min(10000, TIMEOUT_MS));
    if (!diag.directOk && !PROXY) {
        warn('⏭️ 直连全部失败且未配代理：后面的探测必然失败，提前结束');
        warn('⏭️ 请在青龙「环境变量」里加 MY_PROXY=http://192.168.31.233:7890（N1 上 mihomo 的 mixed 端口）后重跑');
        log('🏁 预检结束（未做任何登录/签到动作）');
        return;
    }
    if (!diag.directOk && !diag.proxyOk) {
        warn('⏭️ 直连与代理都不通：先修网络（确认 mihomo 在跑、7890 端口可达）再回来');
        log('🏁 预检结束（未做任何登录/签到动作）');
        return;
    }
    const cached = readCache();
    if (cached) log(`📦 缓存存在：来源 ${cached.source}｜写入 ${cached.savedAt}｜${AUTH_KEY} ${isAuthValid(cached.cookie) ? '✓' : (isDeletedCookieVal(getCookieVal(AUTH_KEY, cached.cookie)) ? '✗(deleted)' : '✗')}`);
    else log('📦 缓存不存在（首次运行会走账号密码登录）');
    log(`👤 WNFLB_ACCOUNT ${user && pass ? '已配置' : '未配置'}`);

    // 1) 首页连通性（带当前 Cookie，若为空则匿名）
    COOKIE = cached ? cached.cookie : '';
    try {
        const t0 = Date.now();
        const home = await request(SITE + '/', { noRetry: true, timeoutMs: 15000 });
        const ms = Date.now() - t0;
        const homeText = textOf(home);
        const fh = homeText.match(/formhash=([a-f0-9]{8})/);
        log(`🌐 首页 HTTP ${home.status}｜${home.raw ? home.raw.length : 0} 字节｜${ms}ms`);
        if (fh) {
            log(`🔑 首页 formhash 提取成功: ${fh[1]}` +
                (COOKIE ? '（当前 Cookie 有效）' : '（匿名请求同样有 formhash，不能据此判断 Cookie 有效性）'));
        } else {
            warn('⚠️ 首页未提取到 formhash（Cookie 为空或已失效）');
            dumpResp('预检-首页', home);
        }
    } catch (e) {
        err(`❌ 首页请求失败: ${e.message}`);
        dumpErr('预检-首页', e);
    }

    // 2) 登录页可达性 + formhash（这是账号密码登录能否成功的关键）
    try {
        const t0 = Date.now();
        const page = await request(SITE + '/member.php?mod=logging&action=login', { cookie: '', noRetry: true, timeoutMs: 15000 });
        const ms = Date.now() - t0;
        const pageText = textOf(page);
        const lfm = pageText.match(/formhash=([a-f0-9]{8})/)
                 || pageText.match(/name=["']formhash["'][^>]*value=["']([a-f0-9]{8})["']/i);
        log(`🌐 登录页 HTTP ${page.status}｜${page.raw ? page.raw.length : 0} 字节｜${ms}ms`);
        if (lfm) log(`🔑 登录页 formhash 提取成功: ${lfm[1]} → 账号密码登录链路可用`);
        else {
            warn('⚠️ 登录页未提取到 formhash → 账号密码登录大概率不可用');
            dumpResp('预检-登录页', page);
        }
    } catch (e) {
        err(`❌ 登录页请求失败: ${e.message}`);
        dumpErr('预检-登录页', e);
    }

    log('🏁 预检结束（未做任何登录/签到动作）');
}

// ========== 主流程 ==========
async function main() {
    const summaryLines = [];
    const failLog = (m) => { emit('ERROR', m); summaryLines.push(m); };
    const okLog = (m) => { emit('INFO', m); summaryLines.push(m); };

    log(`🚀 福利吧 签到开始（脚本 v${SCRIPT_VER}）`);

    // 账号密码：单变量 WNFLB_ACCOUNT（账号#密码 / 账号:密码）
    let USER = '';
    let PASS = '';
    const ACCOUNT = (process.env.WNFLB_ACCOUNT || '').trim();
    if (ACCOUNT) {
        const sep = ACCOUNT.includes('#') ? '#' : (ACCOUNT.includes(':') ? ':' : '');
        if (sep) {
            const idx = ACCOUNT.indexOf(sep);
            USER = ACCOUNT.substring(0, idx).trim();
            PASS = ACCOUNT.substring(idx + 1).trim();
        }
        if (USER && PASS) {
            const mask = USER.length <= 1 ? '*' : USER[0] + '*'.repeat(Math.min(USER.length - 1, 3));
            log(`👤 账号: ${mask}（账号 ${USER.length} 字符｜密码 ${PASS.length} 位）`);
            if (USER.length <= 2) warn(`⚠️ 账号只有 ${USER.length} 个字符，像是「账号#密码」这类占位符没被替换成真实值`);
        } else {
            warn('⚠️ WNFLB_ACCOUNT 格式应为「账号#密码」或「账号:密码」，当前无法解析');
        }
    }

    // 0. 预检模式：只探测，不登录、不签到
    if (truthy(process.env.WNFLB_PROBE)) {
        await runProbe(USER, PASS);
        return;
    }

    // 1. 取 Cookie
    await resolveCookie(USER, PASS);
    if (!isAuthValid(COOKIE)) {
        failLog(`❌ 无法获取 Cookie（请配置 WNFLB_ACCOUNT=账号#密码；缓存路径: ${CACHE_FILE}）`);
        return;
    }
    checkCookieExpire(process.env.WNFLB_EXPIRE);

    // 2. 首页取 formhash → 签到；Cookie 失效时自动登录并续签一次
    let signResult = '';
    let signEcho = '';          // 服务器对签到请求的回复摘要（用于确认「已签到」到底算不算签上）
    let triedRelogin = false;
    for (let attempt = 0; attempt < 2; attempt++) {
        log('🌐 访问首页...');
        let resp;
        try {
            resp = await request(SITE + '/');
        } catch (e) {
            failLog(`❌ 请求首页失败: ${e.message}${e.code ? ' (code=' + e.code + ')' : ''}`);
            dumpErr('首页请求', e);
            break;
        }

        const homeText = textOf(resp);
        const fhMatch = homeText.match(/formhash=([a-f0-9]{8})/);
        if (!fhMatch) {
            warn(`⚠️ 首页响应中未匹配到 formhash（HTTP ${resp.status}，${resp.raw ? resp.raw.length : 0} 字节）`);
            dumpResp('首页', resp);
            if (USER && PASS && !triedRelogin) {
                triedRelogin = true;
                warn('⚠️ 首页未取到 formhash（Cookie 可能已失效），尝试账号密码重新登录...');
                const fresh = await loginAndGetCookie(USER, PASS);
                if (fresh) {
                    COOKIE = fresh;
                    writeCache(fresh, 'login-relogin');
                    log('✅ Cookie 已刷新，重试签到');
                    continue;
                }
                failLog('🚫 Cookie 已失效且登录兜底失败，请检查 WNFLB_ACCOUNT');
                break;
            }
            failLog('❌ 未找到 formhash，Cookie 可能已过期');
            break;
        }
        const formhash = fhMatch[1];
        log(`🔑 formhash: ${formhash}`);

        // 签到（fx_checkin 的 URL 里 formhash 出现两次，注意格式）
        const signUrl = `${SITE}/plugin.php?id=fx_checkin:checkin&formhash=${formhash}&${formhash}&infloat=yes&handlekey=fx_checkin&inajax=1&ajaxtarget=fwin_content_fx_checkin`;
        log('📤 发送签到请求...');
        let signResp;
        try {
            signResp = await request(signUrl, { headers: { 'X-Requested-With': 'XMLHttpRequest' } });
        } catch (e) {
            failLog(`❌ 签到请求失败: ${e.message}${e.code ? ' (code=' + e.code + ')' : ''}`);
            dumpErr('签到请求', e);
            break;
        }

        const kind = classifySign(signResp);
        if (kind === 'success' || kind === 'already') {
            signResult = kind;
            signEcho = brief(textOf(signResp), 80);
            // 想核对"服务器到底回了什么"时打开（默认关，避免日志臃肿）
            if (truthy(process.env.WNFLB_DUMP_SIGN)) dumpResp('签到响应', signResp);
            break;
        }
        if (kind === 'expired') {
            if (USER && PASS && !triedRelogin) {
                triedRelogin = true;
                warn('⚠️ 签到返回未登录，尝试账号密码重新登录...');
                const fresh = await loginAndGetCookie(USER, PASS);
                if (fresh) {
                    COOKIE = fresh;
                    writeCache(fresh, 'login-relogin');
                    log('✅ Cookie 已刷新，重试签到');
                    continue;
                }
                failLog('🚫 Cookie 已过期且登录兜底失败');
                break;
            }
            failLog('🚫 Cookie 已过期或未登录，请检查账号密码 / Cookie');
            break;
        }
        failLog(`❌ 签到响应无法识别（HTTP ${signResp.status}，${signResp.raw ? signResp.raw.length : 0} 字节）`);
        dumpResp('签到响应', signResp);
        break;
    }

    if (signResult === 'success') okLog(`🎉 签到成功！${signEcho ? `（服务器：${signEcho}）` : ''}`);
    else if (signResult === 'already') okLog(`✅ 今天已签到${signEcho ? `（服务器：${signEcho}）` : ''}`);

    // 3. 汇总
    const wnOk = summaryLines.some(l => /签到成功|已签到/.test(l));
    const lastLine = summaryLines.filter(l => l.trim()).slice(-1)[0] || '';
    const summary = wnOk
        ? '✔️ 签到成功'
        : '❌ 签到失败' + (lastLine ? '：' + lastLine.replace(/^[^\s]*\s/, '').slice(0, 50) : '');
    log(summary);
}

if (require.main === module) {
    main().catch(e => {
        err(`💥 脚本异常: ${e.message}`);
        dumpErr('脚本异常', e);
    });
}

// 便于单测（青龙以 `node 脚本.js` 主模块方式运行，不影响实际执行）
module.exports = { textOf, isStrictUtf8, tryDecode, CookieJar, getCookieVal, isDeletedCookieVal, isAuthValid, brief, classifySign, resolveCachePath, resolveCookie, dumpResp, dumpErr, DUMP_LIMIT, netDiag, FAMILY_SEQ, TIMEOUT_MS, proxyTunnel, PROXY, PROXY_SRC, PROXY_DESC };
