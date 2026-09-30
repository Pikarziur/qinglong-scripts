/*
# name:福利吧 - 签到（Cookie 缓存为主 + 账号密码登录兜底）· 测试版
*/

// ────────────────────────────────────────────
// 【测试版】与 forum/wnflb2023.js 的差异：
//   · Cookie 两级兜底：本地缓存（主） → 账号密码登录（辅），不使用任何 Cookie 环境变量
//   · 无代理（N1 容器实测直连 200 / 1.12s；走 mihomo 7890 反而 6.38s）
//   · 无通知（notify 已在全仓移除）
//   · 响应按声明的 charset 解码（本站实测为 utf-8，保留兼容以防站点换编码）
//   ⚠️ 本版为**纯直连**：实测 N1 容器直连即可（200 / 1.12s），故未实现代理。
//      若换到无法直连该站的机器上跑，需要把 MY_PROXY 隧道支持加回来（参考 git 提交 a860657）。
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
//   WNFLB_PROBE         可选。=1 仅预检：探测连通性 + 登录页 formhash，不登录、不签到
//   WNFLB_EXPIRE        可选。Cookie 预期过期日 YYYY-MM-DD，设了会提前 3 天提醒
//
// 日志规范：[LEVEL] [WNFLB] emoji message   （LEVEL: INFO / WARN / ERROR）
// 报错输出：所有失败路径都会打印「HTTP 状态 + 错误码 + 关键响应头 + 完整响应原文 + 异常堆栈」，
//           原文默认不截断（超过 64KB 才截断并注明剩余量）；成功路径不打印任何响应内容。
// ────────────────────────────────────────────

const https = require('https');
const fs = require('fs');
const path = require('path');
const { URL } = require('url');

// ========== 配置 ==========
const SITE = 'https://www.wnflb2023.com';
const UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36';
const AUTH_KEY = 'S5r8_2132_auth';        // Discuz 登录凭证（本站在用前缀）
const SALT_KEY = 'S5r8_2132_saltkey';      // Discuz 盐值
const MAX_RETRY = 3;
const truthy = (v) => ['1', 'true', 'yes', 'on'].includes(String(v || '').trim().toLowerCase());

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

// ========== HTTP（直连，无代理）==========
function requestOnce(url, options, headers) {
    return new Promise((resolve, reject) => {
        const u = new URL(url);
        const opts = {
            method: options.method || 'GET',
            hostname: u.hostname,
            port: u.port || (u.protocol === 'https:' ? 443 : 80),
            path: u.pathname + u.search,
            headers,
            timeout: 20000
        };
        const req = https.request(opts, (res) => {
            const chunks = [];
            res.on('data', c => chunks.push(c));           // chunk 为 Buffer，保留原始字节便于 GBK 解码
            res.on('end', () => {
                const raw = Buffer.concat(chunks);
                resolve({ status: res.statusCode, headers: res.headers, body: raw.toString('utf8'), raw });
            });
        });
        req.on('error', reject);
        req.setTimeout(20000, () => {
            req.destroy();
            const te = new Error(`请求超时（${opts.method} ${u.hostname}${u.pathname} 超过 20000ms）`);
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
    for (let i = 0; i < maxTry; i++) {
        try {
            return await requestOnce(url, options, headers);
        } catch (e) {
            lastErr = e;
            if (e && !e.url) e.url = `${method} ${url}`;
            if (i < maxTry - 1) {
                warn(`🔁 请求失败（第${i + 1}/${maxTry}次，重试）: ${e.message}` + (e.code ? ` code=${e.code}` : ''));
            }
        }
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

// ========== 账号密码登录（兜底）==========
async function loginAndGetCookie(user, pass) {
    if (!user || !pass) {
        warn('⚠️ 未配置账号密码（WNFLB_ACCOUNT=账号#密码），无法自动登录');
        return null;
    }
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
        log('📤 提交登录...');
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

        // 3) 判定：拿到 auth + saltkey，或响应里出现 Discuz 的登录成功回调标记
        const auth = jar.get(AUTH_KEY);
        const saltkey = jar.get(SALT_KEY);
        const okMarker = /succeedhandle_login/.test(rText);
        if ((auth && saltkey) || okMarker) {
            log(`🔑 登录成功（${auth && saltkey ? 'auth+saltkey' : 'succeedhandle_login 标记'}，共 ${jar.size()} 个 Cookie 字段）`);
            log(`🍪 ${AUTH_KEY}=${auth ? auth.substring(0, 12) + '***' : '(无)'}`);
            return jar.toString();
        }

        warn(`⚠️ 登录失败（HTTP ${r.status}）：未拿到登录凭证`);
        if (!auth) warn(`⚠️ 未拿到 ${AUTH_KEY}，常见原因：账号密码错误 / 需安全提问 / 触发登录频率限制 / 响应不是登录结果页`);
        else warn(`⚠️ 已拿到 ${AUTH_KEY} 但缺少 ${SALT_KEY}（登录态不完整）`);
        dumpResp('登录POST', r);
        return null;
    } catch (e) {
        dumpErr('登录流程', e);
        return null;
    }
}

// ========== Cookie 解析：缓存（主） → 账号密码登录（辅）==========
async function resolveCookie(user, pass) {
    const forceLogin = truthy(process.env.WNFLB_FORCE_LOGIN);

    // 1) 本地缓存为主
    if (!forceLogin) {
        const cached = readCache();
        if (cached && getCookieVal(AUTH_KEY, cached.cookie)) {
            COOKIE = cached.cookie;
            log(`📦 使用本地缓存 Cookie（来源: ${cached.source} | 写入: ${cached.savedAt}）`);
            return true;
        }
        if (cached) {
            const names = String(cached.cookie || '').split(';').map(kv => kv.split('=')[0].trim()).filter(Boolean);
            warn(`⚠️ 缓存存在但缺少 ${AUTH_KEY}，视为无效，改走账号密码登录（缓存内 ${names.length} 个字段: ${names.join(', ') || '(空)'}）`);
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

// ========== 预检：只探测，不登录、不签到 ==========
async function runProbe(user, pass) {
    log('🔎 预检模式（WNFLB_PROBE=1）：只探测，不登录、不签到');
    log(`📁 Cookie 缓存路径: ${CACHE_FILE}`);
    const cached = readCache();
    if (cached) log(`📦 缓存存在：来源 ${cached.source}｜写入 ${cached.savedAt}｜${AUTH_KEY} ${getCookieVal(AUTH_KEY, cached.cookie) ? '✓' : '✗'}`);
    else log('📦 缓存不存在（首次运行会走账号密码登录）');
    log(`👤 WNFLB_ACCOUNT ${user && pass ? '已配置' : '未配置'}`);

    // 1) 首页连通性（带当前 Cookie，若为空则匿名）
    COOKIE = cached ? cached.cookie : '';
    try {
        const t0 = Date.now();
        const home = await request(SITE + '/');
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
        const page = await request(SITE + '/member.php?mod=logging&action=login', { cookie: '' });
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

    log('🚀 福利吧 签到开始');

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
        if (!USER || !PASS) warn('⚠️ WNFLB_ACCOUNT 格式应为「账号#密码」或「账号:密码」，当前无法解析');
    }

    // 0. 预检模式：只探测，不登录、不签到
    if (truthy(process.env.WNFLB_PROBE)) {
        await runProbe(USER, PASS);
        return;
    }

    // 1. 取 Cookie
    await resolveCookie(USER, PASS);
    if (!getCookieVal(AUTH_KEY, COOKIE)) {
        failLog(`❌ 无法获取 Cookie（请配置 WNFLB_ACCOUNT=账号#密码；缓存路径: ${CACHE_FILE}）`);
        return;
    }
    checkCookieExpire(process.env.WNFLB_EXPIRE);

    // 2. 首页取 formhash → 签到；Cookie 失效时自动登录并续签一次
    let signResult = '';
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

    if (signResult === 'success') okLog('🎉 签到成功！');
    else if (signResult === 'already') okLog('✅ 今天已签到');

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
module.exports = { textOf, isStrictUtf8, tryDecode, CookieJar, getCookieVal, classifySign, resolveCachePath, resolveCookie, dumpResp, dumpErr, DUMP_LIMIT };
