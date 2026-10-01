/**
 * name: 学姐吧 签到（测试版：缓存为主 + 账号密码登录 + Cookie 环境变量兜底）
 * cron: 10 0,12 * * *
 *
 * 【测试版】与 forum/xuejieba.js 的差异：
 *   · token 三级获取：① 本地缓存 → ② 账号密码登录 → ③ XJB_COOKIE 环境变量（最后兜底，不写缓存）
 *   · 新增代理支持（MY_PROXY / XJB_PROXY，CONNECT 隧道）与网络预检（XJB_PROBE=1）
 *   · 报错时打印完整上下文（HTTP 状态 + 关键响应头 + 完整响应原文 + 异常堆栈），成功路径不打印响应体
 *   · 启动日志带版本号，用来确认容器里跑的到底是哪一版
 *
 * 认证说明（沿用旧版结论）：
 *   - 签到接口 userMission、读取接口 getUserMission 只认 Authorization: Bearer <b2_token>
 *   - 不要带 Cookie 头：带上 b2_token Cookie 反而可能在部分 B2 站点触发会话校验分支返回 403「请先登录」
 *   - ⚠️ /wp-json/b2-me/v1/unread-count 属 b2-me 命名空间，要求 WP 会话 Cookie，不认 b2_token JWT，
 *     哪怕 JWT 合法也回 403 noauth —— 不能用来验证 token
 *   - ⚠️ /wp-json/b2/v1/getUserMission 对**未认证请求也返回 200**（只是 mission.current_user = 0、date = ''），
 *     所以「有 mission 字段」不能证明 token 有效；真正判据是 mission.current_user > 0（2026-10-01 用伪 Bearer 实测）
 *   - ⚠️ 签到接口 userMission 认证失败时返回 HTTP 403 {"code":"user_error","message":"请先登录"}
 *     → 脚本据此触发「重新登录后重试一次」
 *
 * 🚀 登录接口（2026-10-01 从站点主题 main.js 反查确认，非猜测）：
 *   主题前端 loginSubmit 在 loginType==1（账号密码登录）分支执行：
 *       this.$https.post(rest_url + 'jwt-auth/v1/token', Qs.stringify(this.data))
 *   即 WordPress 插件「JWT Authentication for WP REST API」的端点：
 *       POST {rest_url}jwt-auth/v1/token
 *       Content-Type: application/x-www-form-urlencoded
 *       body: username=<账号>&password=<密码>
 *   · 成功返回 {"token":"<JWT>", ...}；同时服务端会下发 Set-Cookie: b2_token=<同一个 JWT>
 *     （主题 JS 里只有 b2getCookie('b2_token') 与 b2delCookie，从不自己 b2setCookie → cookie 必为服务端下发）
 *   · 该 JWT 的 payload 为 {"data":{"user":{"id":...}}}，与本脚本 parseJWT 解析的 b2_token 结构一致
 *   · 本站登录组件 props 为 check-type='email' → 登录（loginType=1）**不需要**图形验证码/滑块
 *   · 失败时返回 JSON：{"code":"[jwt_auth] invalid_username","message":"...","data":{"status":403}}
 *
 * 可控参数：
 *   XJB_ACCOUNT       建议必配。账号密码，格式「账号#密码」或「账号:密码」；无缓存时靠它自动登录
 *   XJB_COOKIE        可选。b2_token（JWT）或 Cookie-Editor 导出的整段 Cookie 串；
 *                     **最后兜底**：缓存与账号密码登录都拿不到时才用，且**不写回缓存**
 *                     （写回的话下一轮会直接从缓存命中，等于把它抬到登录之前）
 *   XJB_COOKIE_CACHE  可选。缓存文件路径，默认 /ql/data/xuejieba.token（该目录不可写则回退脚本同目录）
 *   XJB_FORCE_LOGIN   可选。=1 忽略缓存强制账号密码登录（调试用；此时也不走 XJB_COOKIE 兜底）
 *   XJB_PROBE         可选。=1 只做网络/接口预检，不登录、不签到
 *   XJB_DUMP_SIGN     可选。=1 签到时也打印服务器响应原文（默认关，排查用）
 *   XJB_TIMEOUT       可选。单次请求超时毫秒，默认 20000
 *   XJB_IPV6          可选。=1 不强制 IPv4，完全按系统默认地址族走（默认优先 IPv4）
 *   MY_PROXY          可选。http 代理（CONNECT 隧道），如 http://192.168.31.233:7890；与仓库其他脚本统一
 *   XJB_PROXY         可选。同 MY_PROXY，仅本脚本的覆盖别名；两者都配时以它为准
 *   XJB_NO_PROXY      可选。=1 忽略上述代理强制直连
 *   XJB_SITE          可选。站点地址，默认 https://xuejieba2026.com；仅供本地 mock 回归测试
 *
 * 日志规范：[LEVEL] [XJB] emoji message   （LEVEL: INFO / WARN / ERROR）
 */

const https = require('https');
const http = require('http');
const tls = require('tls');
const fs = require('fs');
const path = require('path');
const dns = require('dns');
const { URL } = require('url');

// ========== 配置 ==========
// 版本标识：每次实质性改动 +1。启动日志会带上它，用来确认「容器里跑的到底是哪一版」
const SCRIPT_VER = '2026-10-01b';
const BASE_URL = (process.env.XJB_SITE || 'https://xuejieba2026.com').replace(/\/+$/, '');
const REST = BASE_URL + '/wp-json/';
const UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36';
const TOKEN_COOKIE = 'b2_token';        // B2 主题把 JWT 同时放在这个名字的 Cookie 里
const MAX_RETRY = 2;                     // 普通请求的地址族重试次数
const MAX_LOGIN_ATTEMPT = 2;             // 登录尝试上限（凭据错误不重试，只有异常类失败才重试一次）
const truthy = (v) => ['1', 'true', 'yes', 'on'].includes(String(v || '').trim().toLowerCase());
const SITE_HOST = new URL(BASE_URL).hostname;
const TIMEOUT_MS = Math.max(3000, parseInt(process.env.XJB_TIMEOUT || '20000', 10) || 20000);

// 代理：XJB_PROXY（本脚本覆盖别名）> MY_PROXY（与 southplus.py / xsijishe.py 等统一）
const NO_PROXY = truthy(process.env.XJB_NO_PROXY);
const PROXY = NO_PROXY ? '' : (process.env.XJB_PROXY || process.env.MY_PROXY || '').trim();
const PROXY_SRC = (process.env.XJB_PROXY || '').trim() ? 'XJB_PROXY'
    : ((process.env.MY_PROXY || '').trim() ? 'MY_PROXY' : '');
const PROXY_DESC = NO_PROXY ? '已按 XJB_NO_PROXY=1 强制直连'
    : (PROXY ? `${PROXY}（来源 ${PROXY_SRC}）` : '未配置（纯直连）');

// 地址族策略：默认强制 IPv4（家宽/容器的 IPv6 常见「有地址无出口」黑洞，
// 典型症状就是「curl 能通、Node 一直连到超时」）；重试时自动换回系统默认（auto）
const FAMILY_SEQ = truthy(process.env.XJB_IPV6) ? [0] : [4, 0];

let TOKEN = '';                          // 当前生效的 b2_token

// ========== 统一日志 ==========
function emit(level, msg) {
    console.log(`[${level}] [XJB] ${msg}`);
}
function log(msg)  { emit('INFO', msg); }
function warn(msg) { emit('WARN', msg); }
function err(msg)  { emit('ERROR', msg); }

function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

// ========== 报错详情（仅在出错路径调用，成功路径不打印）==========
const DUMP_LIMIT = 64 * 1024;   // 响应原文最多打印 64KB，超过才截断

function headOf(headers, name) {
    const v = headers && headers[name];
    return Array.isArray(v) ? v.join(', ') : (v || '');
}
// 只列出 set-cookie 的「名字」，避免把长 cookie 值刷屏
function cookieNames(headers) {
    const sc = (headers && headers['set-cookie']) || [];
    const list = Array.isArray(sc) ? sc : [sc];
    const names = list.filter(Boolean).map(c => String(c).split(';')[0].split('=')[0].trim());
    return names.join(', ');
}
function dumpResp(tag, r) {
    if (!r) return;
    const bytes = r.raw ? r.raw.length : 0;
    const chars = r.text ? r.text.length : 0;
    warn(`⚠️ [${tag}] HTTP ${r.status}｜原始 ${bytes} 字节｜解码后 ${chars} 字符`);
    const ct = headOf(r.headers, 'content-type');
    if (ct) warn(`⚠️ [${tag}] content-type: ${ct}`);
    const loc = headOf(r.headers, 'location');
    if (loc) warn(`⚠️ [${tag}] location: ${loc}`);
    const ck = cookieNames(r.headers);
    if (ck) warn(`⚠️ [${tag}] set-cookie: ${ck}`);
    let body = r.text || '';
    if (body.length > DUMP_LIMIT) {
        warn(`⚠️ [${tag}] 响应原文（前 ${DUMP_LIMIT} 字符，剩余 ${body.length - DUMP_LIMIT} 字符已省略）:`);
        body = body.slice(0, DUMP_LIMIT);
    } else {
        warn(`⚠️ [${tag}] 响应原文:`);
    }
    console.log(body);
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

// ========== HTTP 代理（CONNECT 隧道）==========
// 走标准 CONNECT：代理只做 TCP 转发，到目标站的 TLS 依然端到端加密
function proxyTunnel(proxyUrl, targetHost, targetPort, timeoutMs) {
    return new Promise((resolve, reject) => {
        let p;
        try { p = new URL(proxyUrl); } catch (_) { return reject(new Error(`代理地址格式错误（应形如 http://host:port）: ${proxyUrl}`)); }
        if (p.protocol !== 'http:' && p.protocol !== 'https:') {
            return reject(new Error(`暂不支持 ${p.protocol}// 代理（仅支持 http:// / https:// 的 CONNECT 隧道；socks5:// 请改用 mihomo 的 mixed 或 http 端口）`));
        }
        const lib = p.protocol === 'https:' ? https : http;
        const req = lib.request({
            host: p.hostname,
            port: p.port || (p.protocol === 'https:' ? 443 : 80),
            method: 'CONNECT',
            path: `${targetHost}:${targetPort}`,
            headers: { Host: `${targetHost}:${targetPort}` },
            timeout: timeoutMs
        });
        req.on('connect', (res, socket) => {
            if (res.statusCode !== 200) {
                socket.destroy();
                return reject(new Error(`代理拒绝 CONNECT（HTTP ${res.statusCode}）: ${proxyUrl}`));
            }
            resolve(socket);
        });
        req.on('error', reject);
        req.setTimeout(timeoutMs, () => { req.destroy(); reject(new Error(`代理 CONNECT 超时（${timeoutMs}ms）: ${proxyUrl}`)); });
        req.end();
    });
}

// ⚠️ 必须用自定义 Agent 把已建好的隧道交给 Node 复用。
//    踩过的坑：`agent:false` + `options.createConnection` 会被 Node **忽略**并退回直连，
//    现象极具误导性 —— 隧道建好了、TLS 也握手成功了，请求却又去连真实 IP 报 ETIMEDOUT。
function makeTunnelAgent(tunnel, isHttps, servername) {
    const Base = isHttps ? https.Agent : http.Agent;
    class TunnelAgent extends Base {
        createConnection(options, callback) {
            if (!isHttps) return callback(null, tunnel);
            const sock = tls.connect({ socket: tunnel, servername, ALPNProtocols: ['http/1.1'] }, () => callback(null, sock));
            sock.on('error', callback);
        }
    }
    return new TunnelAgent({ keepAlive: false });
}

// ========== HTTP ==========
function requestOnce(url, o, headers, family) {
    return new Promise((resolve, reject) => {
        const u = new URL(url);
        const isHttps = u.protocol === 'https:';
        const lib = isHttps ? https : http;
        const port = u.port ? Number(u.port) : (isHttps ? 443 : 80);
        const timeoutMs = o.timeoutMs || TIMEOUT_MS;
        const opts = {
            method: o.method || 'GET',
            hostname: u.hostname,
            port,
            path: u.pathname + u.search,
            headers: Object.assign({}, headers),
            timeout: timeoutMs
        };
        if (isHttps) {
            // ⚠️ 关键：curl 默认发 ALPN，Node 默认**不发**。部分 CDN/WAF 会直接丢弃「无 ALPN 的
            //    ClientHello」，表现就是「curl 秒回 200，Node 卡到超时」。
            opts.ALPNProtocols = ['http/1.1'];   // 别声明 h2，Node 的 http 模块解析不了
            opts.servername = u.hostname;
        }
        const proxyUrl = (o.proxy || '').trim();
        let tunnel = null;

        const send = () => {
            const req = lib.request(opts, (res) => {
                const chunks = [];
                res.on('data', c => chunks.push(c));
                res.on('end', () => {
                    const raw = Buffer.concat(chunks);
                    resolve({ status: res.statusCode, headers: res.headers, raw, text: raw.toString('utf8'), url });
                });
            });
            req.on('error', (e) => { if (tunnel) tunnel.destroy(); reject(e); });
            req.setTimeout(timeoutMs, () => {
                req.destroy();
                const te = new Error(`请求超时（${opts.method} ${u.hostname}${u.pathname} 超过 ${timeoutMs}ms，family=${family || 'auto'}${proxyUrl ? '，via ' + proxyUrl : ''}）`);
                te.code = 'ETIMEDOUT';
                te.url = url;
                reject(te);
            });
            if (o.body !== undefined && o.body !== null && o.body !== '') req.write(o.body);
            req.end();
        };

        if (proxyUrl) {
            delete opts.family;      // 隧道由代理建立，地址族不再适用
            proxyTunnel(proxyUrl, u.hostname, port, timeoutMs).then(sock => {
                tunnel = sock;
                opts.agent = makeTunnelAgent(sock, isHttps, u.hostname);
                send();
            }).catch(e => { if (!e.url) e.url = `${opts.method} ${url} (via ${proxyUrl})`; reject(e); });
        } else {
            if (family) opts.family = family;
            send();
        }
    });
}

// 带重试的请求：默认按 FAMILY_SEQ 依次换地址族；noRetry=true 时只发一次（登录 POST 用）
async function request(url, options = {}) {
    const method = options.method || 'GET';
    const headers = Object.assign({
        'User-Agent': UA,
        'Accept': '*/*',
        'Accept-Language': 'zh-CN,zh;q=0.9'
    }, options.headers || {});
    if (method !== 'GET') {
        headers['Content-Length'] = options.body ? Buffer.byteLength(options.body) : 0;
    }
    const useProxy = options.proxy !== undefined ? options.proxy : PROXY;
    const maxTry = options.noRetry ? 1 : MAX_RETRY;
    let lastErr = null;
    for (let i = 0; i < maxTry; i++) {
        const family = options.family !== undefined ? options.family : FAMILY_SEQ[Math.min(i, FAMILY_SEQ.length - 1)];
        try {
            return await requestOnce(url, {
                method,
                body: options.body,
                timeoutMs: options.timeoutMs || TIMEOUT_MS,
                proxy: useProxy
            }, headers, family);
        } catch (e) {
            lastErr = e;
            if (e && !e.url) e.url = `${method} ${url}`;
            if (i < maxTry - 1) {
                const nextFam = FAMILY_SEQ[Math.min(i + 1, FAMILY_SEQ.length - 1)];
                warn(`🔁 请求失败（第${i + 1}/${maxTry}次，换 family=${nextFam || 'auto'} 重试）: ${e.message}${e.code ? ' code=' + e.code : ''}`);
            }
        }
    }
    if (lastErr && PROXY && /超时|ETIMEDOUT/.test(lastErr.message || '')) {
        warn(`💡 已配置代理仍超时：确认代理可达（${PROXY}）以及它是否放行本站`);
    }
    throw lastErr || new Error('请求失败: ' + url);
}

// ========== 缓存 ==========
function resolveCachePath() {
    const p = (process.env.XJB_COOKIE_CACHE || '').trim();
    if (p) return p;
    try {
        if (fs.existsSync('/ql/data') && fs.statSync('/ql/data').isDirectory()) {
            return path.join('/ql/data', 'xuejieba.token');
        }
    } catch (_) { /* 忽略，回退脚本同目录 */ }
    return path.join(__dirname, '.xuejieba.token');
}
const CACHE_FILE = resolveCachePath();

// 缓存格式：JSON {v, source, savedAt, token}；同时兼容历史遗留的「纯文本 token」文件
function readCache() {
    try {
        if (!fs.existsSync(CACHE_FILE)) return null;
        const raw = fs.readFileSync(CACHE_FILE, 'utf8').trim();
        if (!raw) return null;
        if (raw.startsWith('{')) {
            const j = JSON.parse(raw);
            const token = String(j.token || j.cookie || '').trim();
            if (!token) return null;
            return { v: j.v || 1, source: j.source || 'cache', savedAt: j.savedAt || '未知', token };
        }
        return { v: 0, source: 'plain-text', savedAt: '未知', token: raw };
    } catch (e) {
        warn(`⚠️ 读取缓存失败（${CACHE_FILE}）: ${e.message || e}`);
        return null;
    }
}
function writeCache(token, source) {
    try {
        fs.writeFileSync(CACHE_FILE, JSON.stringify({
            v: 1,
            source,
            savedAt: new Date().toISOString(),
            token
        }, null, 2), { mode: 0o600 });
        log(`💾 token 已写入缓存（${source}）: ${CACHE_FILE}`);
    } catch (e) {
        warn('⚠️ token 缓存写入失败: ' + (e.message || e));
    }
}

// ========== token 工具 ==========
// 从 XJB_COOKIE 提取 b2_token：支持直接填 JWT，也支持 Cookie-Editor 导出的整段 cookie 串
function extractB2Token(raw) {
    const s = String(raw || '').trim();
    if (!s) return '';
    const m = s.match(/b2_token=([^;]+)/);
    return m ? m[1].trim() : s;
}
function getCookieVal(name, cookieStr) {
    const m = String(cookieStr || '').match(new RegExp('(?:^|;\\s*)' + name + '=([^;]*)'));
    if (!m) return '';
    try { return decodeURIComponent(m[1]); } catch (_) { return m[1]; }
}
function parseJWT(token) {
    try {
        const parts = String(token || '').split('.');
        if (parts.length !== 3) return null;
        const payload = JSON.parse(
            Buffer.from(parts[1].replace(/-/g, '+').replace(/_/g, '/'), 'base64').toString('utf8')
        );
        return {
            userId: payload?.data?.user?.id || payload?.user?.id || '未知',
            exp: payload.exp || 0,
            expDate: payload.exp ? new Date(payload.exp * 1000).toLocaleString('zh-CN') : '未知'
        };
    } catch (_) { return null; }
}
function isUsableToken(token) {
    const info = parseJWT(extractB2Token(token));
    if (!info) return false;
    if (info.exp && info.exp < Date.now() / 1000) return false;
    return true;
}

// 把 HTML 压成一行摘要（登录失败时服务端可能回带 <strong> 的富文本 message）
function brief(text, n) {
    const s = String(text || '')
        .replace(/<script[\s\S]*?<\/script>/gi, ' ')
        // 块级标签换成空格（保留分句），行内标签直接删掉 —— 否则 <strong>x</strong>y 会多出一个空格，
        // 中文里看起来就是「错误： 未知用户名」这种夹缝空格
        .replace(/<\/?(?:br|p|div|li|ul|ol|tr|td|th|h[1-6]|table|blockquote)[^>]*>/gi, ' ')
        .replace(/<[a-zA-Z/][^>]*>/g, '')
        .replace(/&nbsp;/gi, ' ')
        .replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&quot;/g, '"').replace(/&amp;/g, '&')
        .replace(/\s+/g, ' ')
        .trim();
    const lim = n || 120;
    return s.length > lim ? s.slice(0, lim) + '…' : s;
}

// ========== 登录：POST /wp-json/jwt-auth/v1/token ==========
// 返回 {state, token, msg}
//   'ok'        拿到可用 token
//   'cred'      凭据问题（用户名/密码错）—— 绝不重试，避免触发风控
//   'captcha'   站点要求验证码/安全校验 —— 重试也没用，提示改用手工 Cookie
//   'transient' 网络异常 / 服务端 5xx / 返回 200 却拿不到 token —— 可重试一次
async function loginAttempt(user, pass, attemptNo) {
    log(`📤 提交登录${attemptNo > 1 ? `...（第 ${attemptNo} 次尝试）` : '...'}`);
    const bodyStr = `username=${encodeURIComponent(user)}&password=${encodeURIComponent(pass)}`;
    let r;
    try {
        r = await request(REST + 'jwt-auth/v1/token', {
            method: 'POST',
            body: bodyStr,
            noRetry: true,
            headers: {
                'Content-Type': 'application/x-www-form-urlencoded',
                'Accept': 'application/json, text/plain, */*',
                'Origin': BASE_URL,
                'Referer': BASE_URL + '/',
                'X-Requested-With': 'XMLHttpRequest'
            }
        });
    } catch (e) {
        dumpErr('登录请求', e);
        return { state: 'transient', msg: e.message };
    }

    let data = null;
    try { data = JSON.parse(r.text); } catch (_) { data = null; }

    // token 可能来自 Set-Cookie: b2_token（主题就是靠它读登录态），也可能在响应体 .token
    const setCookie = [].concat(r.headers['set-cookie'] || []).filter(Boolean).join('; ');
    const cookieToken = getCookieVal(TOKEN_COOKIE, setCookie);
    const bodyToken = (data && typeof data.token === 'string') ? data.token.trim() : '';
    const token = cookieToken || bodyToken;

    if (r.status === 200 && token && isUsableToken(token)) {
        const info = parseJWT(token);
        const remain = info && info.exp ? Math.floor((info.exp - Date.now() / 1000) / 86400) : -1;
        log(`🔑 登录成功（来源: ${cookieToken ? 'Set-Cookie b2_token' : '响应体 token'}｜用户ID: ${info ? info.userId : '未知'}`
            + `${remain >= 0 ? `｜剩余${remain}天` : ''}）`);
        log(`🍪 ${TOKEN_COOKIE}=${String(token).slice(0, 12)}***`);
        return { state: 'ok', token };
    }

    const msg = brief((data && (data.message || data.code)) || r.text, 200);

    if (/验证码|滑动|滑块|recaptcha|security|安全校验|人机/.test(msg)) {
        warn(`🚫 站点要求验证码/安全校验：${msg}`);
        dumpResp('登录POST', r);
        return { state: 'captcha', msg };
    }
    if (r.status === 403 || r.status === 401 || /\[jwt_auth\]/.test(msg) || /用户名|密码/.test(msg)) {
        let reason = '用户名或密码不正确';
        if (/invalid_username|未知用户名|用户不存在/.test(msg)) reason = '用户名不存在（需与注册时一致，本站用邮箱/用户名登录）';
        else if (/incorrect_password|密码错误|密码不正确/.test(msg)) reason = '密码错误';
        else if (/empty_username|empty_password/.test(msg)) reason = '账号或密码为空（检查 XJB_ACCOUNT 的「账号#密码」分隔符）';
        warn(`🚫 登录被拒（HTTP ${r.status}）：${reason}`);
        warn(`🚫 服务端原话: ${msg}`);
        if (user.length <= 2) warn(`🚫 另外注意：当前账号只有 ${user.length} 个字符（${user}），像是占位符没被替换成真实账号`);
        dumpResp('登录POST', r);
        return { state: 'cred', msg };
    }
    warn(`⚠️ 登录未拿到可用 token（HTTP ${r.status}）${data ? '' : '（响应不是 JSON）'}`);
    dumpResp('登录POST', r);
    return { state: 'transient', msg };
}

async function loginAndGetToken(user, pass) {
    if (!user || !pass) {
        warn('⚠️ 未配置账号密码（XJB_ACCOUNT=账号#密码），无法自动登录');
        return null;
    }
    for (let i = 1; i <= MAX_LOGIN_ATTEMPT; i++) {
        const r = await loginAttempt(user, pass, i);
        if (r.state === 'ok') return r.token;
        // 凭据错误 / 验证码：确定性失败，重试无意义且可能触发风控 → 不重试
        if (r.state === 'cred' || r.state === 'captcha') return null;
        if (i < MAX_LOGIN_ATTEMPT) warn(`🔁 登录遇到临时性失败，重试（第 ${i + 1}/${MAX_LOGIN_ATTEMPT} 次）...`);
    }
    return null;
}

// ========== token 三级兜底：缓存 → 账号密码登录 → XJB_COOKIE ==========
// ⚠️ 顺序是有意的：环境变量 token 排在账号密码**之后**，且**不写回缓存**
//    （写回后下一轮会直接从缓存命中，等于把它抬到登录之前，与"最后兜底"矛盾）
async function resolveToken(user, pass) {
    const forceLogin = truthy(process.env.XJB_FORCE_LOGIN);

    // 1) 本地缓存为主
    if (!forceLogin) {
        const cached = readCache();
        if (cached && isUsableToken(cached.token)) {
            TOKEN = extractB2Token(cached.token);
            log(`📦 使用本地缓存 token（来源: ${cached.source} | 写入: ${cached.savedAt}）`);
            return true;
        }
        if (cached) {
            const info = parseJWT(extractB2Token(cached.token));
            const why = !info ? '不是合法 JWT'
                : (info.exp && info.exp < Date.now() / 1000 ? `已过期（${info.expDate}）` : '不可用');
            warn(`⚠️ 缓存里的 token ${why}，视为无效，改走账号密码登录`);
        }
    } else {
        warn('🔁 XJB_FORCE_LOGIN 已开启：忽略缓存，强制账号密码登录');
    }

    // 2) 账号密码登录
    if (user && pass) {
        log('🔐 尝试账号密码登录获取 token...');
        const fresh = await loginAndGetToken(user, pass);
        if (fresh) {
            TOKEN = fresh;
            writeCache(fresh, 'login');
            return true;
        }
        warn('⚠️ 账号密码登录未拿到有效 token，继续尝试 XJB_COOKIE 兜底');
    } else {
        warn('⚠️ XJB_ACCOUNT 未配置或格式不对（应为「账号#密码」），跳过账号密码登录');
    }

    // 3) XJB_COOKIE 环境变量：最后兜底
    if (forceLogin) {
        warn('🔁 XJB_FORCE_LOGIN 已开启：跳过 XJB_COOKIE 兜底（否则会掩盖登录是否真的成功）');
        return false;
    }
    const envRaw = process.env.XJB_COOKIE || process.env.xjb_cookie || '';
    const envToken = extractB2Token(envRaw);
    if (!envToken) return false;

    if (isUsableToken(envToken)) {
        TOKEN = envToken;
        log('🌱 账号密码登录也拿不到 token，改用 XJB_COOKIE 环境变量兜底（本次不写缓存）');
        return true;
    }
    const info = parseJWT(envToken);
    if (!info) warn('⚠️ XJB_COOKIE 里的值不是合法 JWT（应以 eyJ 开头且含两个 "."），不可用');
    else warn(`⚠️ XJB_COOKIE 里的 token 已过期（${info.expDate}），不可用`);
    return false;
}

// ========== 业务接口 ==========
function buildHeaders(token, contentType = null) {
    const h = {
        'Authorization': `Bearer ${token}`,      // 只发 Bearer，不带 Cookie（见文件头认证说明）
        'Accept': 'application/json, text/plain, */*',
        'User-Agent': UA,
        'Origin': BASE_URL,
        'Referer': `${BASE_URL}/mission/today`
    };
    if (contentType) h['Content-Type'] = contentType;
    return h;
}

async function apiPost(apiPath, token, body = null, contentType = null) {
    try {
        const r = await request(REST + apiPath.replace(/^\/+/, ''), {
            method: 'POST',
            body: body === null ? '' : body,
            headers: buildHeaders(token, contentType)
        });
        let data;
        try { data = JSON.parse(r.text); } catch (_) { data = r.text; }
        return { status: r.status, data, resp: r };
    } catch (e) {
        dumpErr(`接口 ${apiPath}`, e);
        return { status: 0, error: e.message, err: e };
    }
}

// 验证 token：本地校验 JWT 结构/过期 + b2/v1 在线确认
// 返回 { ok, authFailed }：authFailed=true 表示「服务端不认这个 token」，值得重新登录一次
async function verifyToken(token) {
    const info = parseJWT(token);
    if (!info) {
        err('🚫 Token 格式无效（非合法 JWT，应以 eyJ 开头且含两个 "."）。请从 Cookie-Editor 重新复制完整 b2_token');
        return { ok: false, authFailed: true };
    }
    const now = Date.now() / 1000;
    const remainDays = info.exp ? Math.floor((info.exp - now) / 86400) : -1;
    if (info.exp && info.exp < now) {
        err(`⏰ Token 已过期 (${info.expDate})，请重新获取或改用账号密码登录`);
        return { ok: false, authFailed: true };
    }
    log(`👤 用户ID: ${info.userId}${info.exp ? ` | 剩余${remainDays}天 | 过期: ${info.expDate}` : ''}`);
    if (remainDays >= 0 && remainDays <= 3) warn(`⏰ Token 即将过期(${remainDays}天)，建议尽快更新！`);

    // 用正确的 b2/v1 接口在线确认（与签到同命名空间、只认 Bearer）
    const res = await apiPost('b2/v1/getUserMission', token, 'count=10&paged=1', 'application/x-www-form-urlencoded');
    if (res.status === 200 && res.data?.mission) {
        const m = res.data.mission;
        // ⚠️ 实测关键点（2026-10-01，用伪 Bearer 打真实站点验证）：
        //    该接口对**未认证请求同样返回 200**，只是 mission.current_user = 0、date = ''。
        //    所以「有 mission 字段」根本不能证明 token 有效 —— 旧版就是在这里误判的。
        //    真正的判据是 mission.current_user（登录用户 ID，匿名时为 0）。
        if (m.current_user != null && Number(m.current_user) === 0) {
            err('🚫 Token 未被服务端认可：getUserMission 返回的是匿名数据（current_user=0），登录态已失效');
            return { ok: false, authFailed: true };
        }
        if (m.current_user == null) warn('⚠️ 响应里没有 current_user 字段，无法据此判断登录态（按可用处理）');
        log(`🔓 Token 验证通过 | 用户ID: ${m.current_user ?? '?'} | 今日签到日期: ${m.date || '未签到'} | 当前积分: ${m.my_credit}`);
        return { ok: true };
    }
    if (res.status === 0) {
        warn(`🔁 验证请求失败(${res.error})，继续尝试签到`);
        return { ok: true };    // 网络异常不阻断，交给签到接口最终判定
    }
    if (res.status === 403 || res.data?.code === 'noauth' || res.data?.code === 'user_error') {
        err(`🚫 Token 被服务端拒绝(${res.data?.code || res.status})：登录态已失效。删掉缓存后重跑会自动重新登录，或重新填 XJB_COOKIE`);
        dumpResp('getUserMission', res.resp);
        return { ok: false, authFailed: true };
    }
    warn(`⚠️ Token 在线验证异常: ${JSON.stringify(res.data).slice(0, 120)}，继续尝试签到`);
    return { ok: true };
}

// 签到
// 返回 { ok, authFailed }：authFailed=true 表示「接口回未登录」→ 值得重新登录后重试一次
async function doCheckin(token) {
    const res = await apiPost('b2/v1/userMission', token);
    if (res.status === 0) {
        err(`❌ 请求失败: ${res.error}`);
        return { ok: false, authFailed: false };
    }
    if (truthy(process.env.XJB_DUMP_SIGN)) dumpResp('签到响应', res.resp);
    const d = res.data;
    // 401/403 且带 user_error / 「请先登录」→ 登录态失效（实测该站返回 {"code":"user_error","message":"请先登录"}）
    const authMsg = typeof d === 'object' && d ? String(d.message || '') : String(d || '');
    if ((res.status === 401 || res.status === 403) && (d?.code === 'user_error' || d?.code === 'noauth' || /请先登录|未登录|需要登录/.test(authMsg))) {
        warn(`⚠️ 签到接口回未登录（${res.status} ${d?.code || ''}）：${authMsg || '(无消息)'}`);
        return { ok: false, authFailed: true };
    }
    if (res.status !== 200) {
        err(`❌ HTTP ${res.status}: ${JSON.stringify(d).slice(0, 200)}`);
        dumpResp('签到响应', res.resp);
        return { ok: false, authFailed: false };
    }
    // 返回可能是 JSON 也可能是纯字符串
    if (typeof d === 'string') {
        // B2 主题已签到时返回纯数字串（当日签到记录/积分值，如 "10"、"13"，各站数值不固定）。
        // 只要返回非空且含数字即视为今日已签到。
        const s = d.trim();
        if (s && /\d/.test(s)) {
            log(`♻️ 您今天已经签到过了（服务器：${s}）`);
            return { ok: true, authFailed: false };
        }
        err(`⚠️ 签到返回异常: ${s || '(空)'}`);
        dumpResp('签到响应', res.resp);
        return { ok: false, authFailed: false };
    }
    if (d?.mission) {
        const signDays = d.mission.tk?.days || 0;
        let msg = `🎉 签到成功！+${d.credit || ''}积分`;
        if (signDays) msg += ` | 连续签到:${signDays}天`;
        msg += ` | 当前积分:${d.mission.my_credit || '?'}`;
        log(msg);
        return { ok: true, authFailed: false };
    }
    // 可能是 {"code":"invitation_error","message":"点太快啦！"}
    if (d?.code) {
        err(`❌ ${d.message || d.code}`);
        dumpResp('签到响应', res.resp);
        return { ok: false, authFailed: false };
    }
    err(`⚠️ 签到返回异常: ${JSON.stringify(d).slice(0, 200)}`);
    dumpResp('签到响应', res.resp);
    return { ok: false, authFailed: false };
}

async function runOne(token) {
    const v = await verifyToken(token);
    if (!v.ok) return { ok: false, authFailed: v.authFailed };
    await sleep(500);
    return await doCheckin(token);
}

// ========== 预检：只探测，不登录、不签到 ==========
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
        ['首页    ', BASE_URL + '/'],
        ['REST 根 ', REST]
    ];
    const hdrs = { 'User-Agent': UA, 'Accept': '*/*', 'Accept-Language': 'zh-CN,zh;q=0.9' };
    let directOk = false, proxyOk = false;
    const probeOne = async (label, url, proxy, family, mark) => {
        const t0 = Date.now();
        try {
            const r = await requestOnce(url, { method: 'GET', timeoutMs: t, proxy }, hdrs, family);
            log(`📡 ${label} ${mark} → HTTP ${r.status}｜${r.raw ? r.raw.length : 0} 字节｜${Date.now() - t0}ms`);
            return true;
        } catch (e) {
            warn(`📡 ${label} ${mark} → 失败 ${e.message}${e.code ? ' code=' + e.code : ''}（${Date.now() - t0}ms）`);
            return false;
        }
    };
    // 1) 直连：先强制 IPv4，失败再换系统默认（能区分「IPv6 黑洞」与「整站不可达」）
    for (const [name, url] of targets) {
        let ok = await probeOne(name, url, '', 4, '直连 family=4');
        if (!ok) {
            warn(`⏭️ ${name} 强制 IPv4 失败，换系统默认（auto）再试一次`);
            ok = await probeOne(name, url, '', 0, '直连 family=auto');
        }
        if (ok) directOk = true;
    }
    // 2) 代理：配了才测
    if (PROXY) {
        for (const [name, url] of targets) {
            const ok = await probeOne(name, url, PROXY, 0, `代理 ${PROXY}`);
            if (ok) proxyOk = true;
        }
    } else {
        log('🔌 未配置 MY_PROXY，跳过代理测试（直连不通时可用它走 mihomo）');
    }
    return { directOk, proxyOk };
}

async function runProbe(user, pass) {
    log(`🔎 预检模式（XJB_PROBE=1）：只探测，不登录、不签到｜脚本 v${SCRIPT_VER}`);
    log(`📁 token 缓存路径: ${CACHE_FILE}`);
    log(`⏱️ 单次超时 ${TIMEOUT_MS}ms｜地址族顺序 [${FAMILY_SEQ.map(f => f || 'auto').join(' → ')}]`);
    log(`🔌 代理: ${PROXY_DESC}`);

    const diag = await netDiag(Math.min(10000, TIMEOUT_MS));
    if (!diag.directOk && !diag.proxyOk) {
        warn('⏭️ 直连与代理都不通：先修网络（确认 mihomo 在跑、端口可达）再回来');
        log('🏁 预检结束（未做任何登录/签到动作）');
        return;
    }

    // 配置体检
    const cached = readCache();
    if (cached) log(`📦 缓存存在：来源 ${cached.source}｜写入 ${cached.savedAt}｜token ${isUsableToken(cached.token) ? '✓ 可用' : '✗ 不可用（过期或非 JWT）'}`);
    else log('📦 缓存不存在（首次运行会走账号密码登录）');
    log(`👤 XJB_ACCOUNT ${user && pass ? `已配置（账号 ${maskUser(user)}）` : '未配置'}`);
    const envToken = extractB2Token(process.env.XJB_COOKIE || '');
    log(`🍪 XJB_COOKIE ${envToken
        ? (isUsableToken(envToken) ? '已配置（token ✓ 可用，作为最后兜底）' : '已配置但 token 不可用（过期或非 JWT）')
        : '未配置（可选，仅在缓存与账号密码登录都失败时兜底）'}`);

    // 接口可用性：直接读 REST 路由表，确认登录/签到端点存在（无副作用，不需要凭据）
    try {
        const r = await request(REST, { noRetry: true, timeoutMs: 15000 });
        if (r.status === 200) {
            let routes = [];
            try { routes = Object.keys(JSON.parse(r.text).routes || {}); } catch (_) { routes = []; }
            log(`🧩 REST 路由表 HTTP 200｜共 ${routes.length} 条`);
            for (const k of ['/jwt-auth/v1/token', '/b2/v1/userMission', '/b2/v1/getUserMission']) {
                const hit = routes.includes(k);
                (hit ? log : warn)(`🧩 ${hit ? '✓' : '✗'} ${k}${hit ? '' : '（路由表里没有，对应功能可能不可用）'}`);
            }
        } else {
            warn(`⚠️ REST 路由表 HTTP ${r.status}，无法确认接口可用性`);
            dumpResp('REST 根', r);
        }
    } catch (e) {
        dumpErr('REST 根', e);
    }
    log('🏁 预检结束（未做任何登录/签到动作）');
}

// ========== 主入口 ==========
function maskUser(u) {
    if (!u) return '(空)';
    return u.length <= 2 ? u[0] + '*' : u.substring(0, 2) + '***';
}
function parseAccount(raw) {
    const s = String(raw || '').trim();
    if (!s) return { user: '', pass: '' };
    const i = s.search(/[#:]/);
    if (i <= 0) return { user: s, pass: '' };
    return { user: s.slice(0, i).trim(), pass: s.slice(i + 1).trim() };
}
function failLog(msg) { err(msg); }

async function main() {
    log(`🚀 学姐吧 签到开始（脚本 v${SCRIPT_VER}）`);

    const { user, pass } = parseAccount(process.env.XJB_ACCOUNT);
    if (user) log(`👤 账号: ${maskUser(user)}（账号 ${user.length} 字符｜密码 ${pass.length} 位）`);

    if (truthy(process.env.XJB_PROBE)) return await runProbe(user, pass);

    // 三级来源全都没有时才提前退出（注意：缓存存在时即使没配账号也必须继续跑）
    if (!user && !(process.env.XJB_COOKIE || '').trim() && !readCache()) {
        failLog('❌ 未配置任何凭证：请设置 XJB_ACCOUNT=账号#密码（推荐），或 XJB_COOKIE 作为兜底');
        process.exit(1);
    }

    const got = await resolveToken(user, pass);
    if (!got) {
        failLog(`❌ 无法获取 token（按「缓存 → 账号密码登录 → XJB_COOKIE」三级都失败；缓存路径: ${CACHE_FILE}）`);
        process.exit(1);
    }

    let res = await runOne(TOKEN);
    // 服务端不认当前 token（例如缓存里的登录态被服务端作废）→ 重新登录一次再试
    // 只有「有账号密码可登」时才值得重试；用 XJB_COOKIE 兜底时重登也没意义
    if (!res.ok && res.authFailed && user && pass) {
        warn('⚠️ 当前 token 已被服务端作废，尝试账号密码重新登录后重试...');
        const fresh = await loginAndGetToken(user, pass);
        if (fresh) {
            TOKEN = fresh;
            writeCache(fresh, 'login-relogin');
            log('✅ token 已刷新，重试签到');
            res = await runOne(TOKEN);
        } else {
            failLog('🚫 token 失效且重新登录失败，本次放弃');
        }
    }

    log(`🏁 完成：学姐吧 ${res.ok ? '签到成功' : '签到失败'}`);
    if (!res.ok) process.exit(1);
}

if (require.main === module) {
    main().catch((e) => {
        dumpErr('脚本', e);
        err('💥 脚本崩溃: ' + (e?.message || e));
        process.exit(1);
    });
}

module.exports = {
    SCRIPT_VER, BASE_URL, REST, CACHE_FILE, brief, extractB2Token, getCookieVal, parseJWT,
    isUsableToken, readCache, writeCache, parseAccount, maskUser, resolveToken,
    dumpResp, dumpErr, request, requestOnce, proxyTunnel, isProxyEnabled: () => !!PROXY
};
