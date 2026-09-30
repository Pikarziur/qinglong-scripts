
/*
# name:福利吧 - 签到
# cron: 10 0,12 * * *
*/


// ────────────────────────────────────────────
// 任务流程：
//   1. 获取 Cookie：本地缓存文件为主；缓存缺失/过期时用账号密码登录自动获取并写缓存
//   2. 访问论坛首页，正则提取签到所需的 formhash
//   3. 请求 fx_checkin 签到接口完成每日签到
//   4. 根据响应 / Set-Cookie 判断成功 / 已签 / Cookie 过期，输出汇总并通知
// 可控参数：
//   WNFLB_COOKIE_CACHE 可选。Cookie 缓存文件路径，默认 /ql/data/wnflb2023.cookie（青龙持久目录，订阅更新不受影响；可覆盖）
//   WNFLB_NOTIFY        通知开关，默认开启；填 0/false/off/no 关闭
//   WNFLB_EXPIRE        可选，Cookie 预期过期日，如 2026-10-15。设了会提前3天提醒
//   MY_PROXY           可选。HTTP 代理地址，如 http://192.168.31.233:7890；福利吧等站点若需代理才能出网请配置，未配则直连
//   WNFLB_ACCOUNT      账号密码（推荐必填）。单变量存放「账号#密码」或「账号:密码」：
//                         配了即启用「登录兜底 + Cookie 缓存」，首次/失效时自动登录并把 Cookie 写缓存文件；
//                         不配则需本地已有缓存文件（.wnflb2023.cookie），否则报错
//
// 🚀 Cookie 一键获取（登录 www.wnflb2023.com 后 F12 → Console 执行）：
//
//   // 方法 A: 复制全部 Cookie（包含所有 Discuz 字段）
//   copy(document.cookie)
//
//   // 方法 B: 只复制关键登录字段（更干净，推荐）
//   copy(['S5r8_2132_auth','S5r8_2132_saltkey'].map(k=>k+'='+document.cookie.match(new RegExp(k+'=([^;]+)'))?.[1]).join('; '))
//
//   // 方法 C: Application 面板 → Cookies → www.wnflb2023.com → 全选 Value 列合并
//
// Discuz 论坛 Cookie 说明：
//   - S5r8_2132_auth    登录凭证（必须）
//   - S5r8_2132_saltkey 盐值（必须）
//   - 前缀 S5r8_2132 是站点唯一标识，不同 Discuz 站点前缀不同
// ────────────────────────────────────────────

const https = require('https');
const http = require('http');
const { URL } = require('url');
const fs = require('fs');
const path = require('path');

// ========== 配置 ==========
const DEFAULT_COOKIE = ''; // 不使用环境变量时粘贴到这里
// =========================

let COOKIE = DEFAULT_COOKIE; // Cookie 不再从环境变量读取，改为「本地缓存文件 → 账号密码登录」自动管理
const SITE = 'https://www.wnflb2023.com';

// Cookie 缓存：登录获取到的新 Cookie 落盘，仅 Cookie 失效时才重新账号密码登录
const COOKIE_CACHE = process.env.WNFLB_COOKIE_CACHE || '/ql/data/wnflb2023.cookie';
function loadCachedCookie() {
    try { if (fs.existsSync(COOKIE_CACHE)) return fs.readFileSync(COOKIE_CACHE, 'utf8').trim(); } catch (e) {}
    return '';
}
function saveCachedCookie(c) {
    if (!c) return;
    try {
        fs.mkdirSync(path.dirname(COOKIE_CACHE), { recursive: true });
        fs.writeFileSync(COOKIE_CACHE, c, { mode: 0o600 }); log('💾 已把新 Cookie 缓存到本地文件');
    }
    catch (e) { log('⚠️ Cookie 缓存写入失败: ' + (e.message || e)); }
}

// ========== 青龙通知（共享仓库根 notify.js，缺失时 CDN 回退；WNFLB_NOTIFY=0 关闭）==========
const WNFLB_NOTIFY = !['0', 'false', 'off', 'no'].includes((process.env.WNFLB_NOTIFY || '1').trim().toLowerCase());
async function loadWnflbNotify() {
    const cands = [path.join(__dirname, '..', 'notify.js'), path.join(__dirname, 'notify.js'), '/ql/data/scripts/notify.js', '/ql/scripts/notify.js'];
    const p = cands.find(f => fs.existsSync(f)) || cands[0];
    if (!fs.existsSync(p)) {
        const urls = [
            'https://cdn.jsdelivr.net/gh/whyour/qinglong@develop/sample/notify.js',
            'https://raw.githubusercontent.com/whyour/qinglong/refs/heads/develop/sample/notify.js',
            'https://ghproxy.net/https://raw.githubusercontent.com/whyour/qinglong/refs/heads/develop/sample/notify.js'
        ];
        for (const u of urls) {
            try {
                const r = await fetch(u, { timeout: 15000 });
                const body = await r.text();
                if (r.status === 200 && body.includes('sendNotify')) { fs.writeFileSync(p, body); break; }
            } catch (e) { /* 忽略，尝试下一个源 */ }
        }
    }
    return fs.existsSync(p) ? require(p) : null;
}
async function sendQingLongNotify(title, content) {
    if (!WNFLB_NOTIFY) { log('WNFLB_NOTIFY=0，已关闭通知'); return; }
    try {
        const notify = await loadWnflbNotify();
        if (!notify) { log('未安装 notify.js，跳过推送'); return; }
        await notify.sendNotify(title, content);
        log('✅ 通知发送成功');
    } catch (e) { log('⚠️ 通知发送失败: ' + (e.message || e)); }
}

function log(msg) { console.log(`[WN签到] ${msg}`); }

// 手动过期日检测（Discuz Cookie 无内置 exp，需用户手动配 WNFLB_EXPIRE）
function checkCookieExpire(expireStr) {
  if (!expireStr) {
    log('📅 未配置 WNFLB_EXPIRE，仅在线检测生效（设 YYYY-MM-DD 开启日期提醒）');
    return;
  }
  const exp = new Date(expireStr + 'T23:59:59');
  if (isNaN(exp.getTime())) {
    log('⚠️ WNFLB_EXPIRE 格式错误，应为 YYYY-MM-DD');
    return;
  }
  const remainDays = Math.ceil((exp - new Date()) / 86400000);
  const status = remainDays >= 0 ? `剩余${remainDays}天` : `已过期${-remainDays}天`;
  log(`📅 Cookie ${status} | 过期: ${expireStr}`);
  if (remainDays < 0) {
    log('❌ Cookie 已过期，请重新登录抓取！');
    return false;
  }
  if (remainDays <= 3) {
    log(`🔔 Cookie 即将过期（${remainDays}天），建议尽快更新！`);
  }
  return true;
}

// 可选代理（MY_PROXY）：默认直连；配了则通过 HTTP CONNECT 隧道转发（零依赖实现）
function getProxySocket(u) {
    const proxy = process.env.MY_PROXY;
    if (!proxy) return Promise.resolve(null);
    const pu = new URL(proxy);
    const targetPort = u.port || (u.protocol === 'https:' ? 443 : 80);
    return new Promise((resolve, reject) => {
        const creq = http.request({
            host: pu.hostname,
            port: pu.port || 80,
            method: 'CONNECT',
            path: `${u.hostname}:${targetPort}`,
            timeout: 15000
        });
        creq.on('connect', (res, socket) => {
            if (res.statusCode !== 200) {
                socket.destroy();
                reject(new Error('代理 CONNECT 失败: ' + res.statusCode));
                return;
            }
            // 返回明文隧道 socket；https 目标交由 https.request 在其上自建一次 TLS，
            // 避免在隧道上重复做 TLS 导致 "wrong version number"
            resolve(socket);
        });
        creq.on('error', reject);
        creq.on('timeout', () => { creq.destroy(); reject(new Error('代理连接超时')); });
        creq.end();
    });
}

function request(url, options = {}) {
    return new Promise((resolve, reject) => {
        const u = new URL(url);
        const lib = u.protocol === 'https:' ? https : http;
        const headers = Object.assign({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36',
            'Accept': '*/*',
            'Accept-Language': 'zh-CN,zh;q=0.9',
            'Referer': SITE + '/',
            'Cookie': COOKIE
        }, options.headers || {});
        getProxySocket(u).then((conn) => {
            const opts = {
                method: options.method || 'GET',
                path: u.pathname + u.search,
                headers,
                timeout: 15000
            };
            if (conn) {
                // 走代理隧道：复用已建立的 TCP 隧道 socket；https 目标由模块自建一次 TLS
                opts.host = u.hostname;
                opts.port = u.port || (u.protocol === 'https:' ? 443 : 80);
                opts.socket = conn;
                opts.agent = false;
                if (u.protocol === 'https:') opts.servername = u.hostname;
            } else {
                opts.hostname = u.hostname;
                opts.port = u.port || (u.protocol === 'https:' ? 443 : 80);
            }
            const req = lib.request(opts, (res) => {
                let data = '';
                res.on('data', chunk => data += chunk);
                res.on('end', () => resolve({ status: res.statusCode, headers: res.headers, body: data }));
            });
            req.on('error', reject);
            req.setTimeout(15000, () => { req.destroy(); reject(new Error('请求超时')); });
            if (options.body) req.write(options.body);
            req.end();
        }).catch(reject);
    });
}

function getCookieVal(name, cookieStr) {
    const m = cookieStr.match(new RegExp('(?:^|; )' + name + '=([^;]*)'));
    return m ? decodeURIComponent(m[1]) : '';
}

// 账号密码登录兜底：Cookie 失效时调用，返回新 Cookie 字符串或 null
async function loginAndGetCookie(user, pass) {
    if (!user || !pass) return null;
    try {
        // 1. 匿名访问登录页取 formhash（清空 Cookie 避免带失效态）
        const loginPage = await request(SITE + '/member.php?mod=logging&action=login', { headers: { 'Cookie': '' } });
        const lfm = loginPage.body.match(/formhash=([a-f0-9]{8})/);
        if (!lfm) { log('登录页未找到 formhash'); return null; }
        const lfh = lfm[1];
        // 2. POST 登录（该站登录页无验证码，纯账号密码即可）
        const loginUrl = SITE + '/member.php?mod=logging&action=login&loginsubmit=yes&infloat=yes&lssubmit=yes&inajax=1';
        const bodyStr = 'formhash=' + lfh + '&username=' + encodeURIComponent(user) + '&password=' + encodeURIComponent(pass) + '&cookietime=2592000&questionid=0&answer=';
        const r = await request(loginUrl, {
            method: 'POST',
            headers: { 'Content-Type': 'application/x-www-form-urlencoded', 'Cookie': '' },
            body: bodyStr
        });
        // 3. 从 Set-Cookie 提取 auth + saltkey 拼成新 Cookie
        const sc = r.headers['set-cookie'] || [];
        const auth = sc.map(c => (c.match(/S5r8_2132_auth=([^;]+)/) || [])[1]).find(Boolean);
        const saltkey = sc.map(c => (c.match(/S5r8_2132_saltkey=([^;]+)/) || [])[1]).find(Boolean);
        if (auth && saltkey) {
            log('🔑 账号密码登录成功，已刷新 Cookie');
            return 'S5r8_2132_auth=' + auth + '; S5r8_2132_saltkey=' + saltkey;
        }
        log('⚠️ 登录失败（账号密码错误或需验证码）');
        return null;
    } catch (e) {
        const tip = process.env.MY_PROXY ? '' : '（若站点需代理才能出网，请配置 MY_PROXY）';
        log('⚠️ 登录异常: ' + (e.message || e) + tip);
        return null;
    }
}

async function main() {
    const summaryLines = [];
    const slog = (m) => { log(m); summaryLines.push(m); };
    log('========== WN2023 签到 ==========');

    // 账号密码：从单变量 WNFLB_ACCOUNT（账号#密码 / 账号:密码）读取
    let USER = '';
    let PASS = '';
    const ACCOUNT = (process.env.WNFLB_ACCOUNT || '').trim();
    if (ACCOUNT) {
        // 以第一个 # 或 : 分割（账号/密码中一般不含这两个字符）
        const sep = ACCOUNT.indexOf('#') >= 0 ? '#' : (ACCOUNT.indexOf(':') >= 0 ? ':' : '#');
        const idx = ACCOUNT.indexOf(sep);
        USER = ACCOUNT.substring(0, idx).trim();
        PASS = ACCOUNT.substring(idx + 1).trim();
    }

    // Cookie 来源：本地缓存文件为主；缓存缺失/过期时用账号密码登录获取并缓存；都没有则报错
    const cached = loadCachedCookie();
    if (cached) { COOKIE = cached; log('使用本地缓存的 Cookie'); }
    if (!COOKIE && USER && PASS) {
        log('无 Cookie 也无缓存，尝试账号密码登录...');
        const nc = await loginAndGetCookie(USER, PASS);
        if (nc) { COOKIE = nc; saveCachedCookie(nc); }
    }
    if (!COOKIE) { slog('无法获取 Cookie（请确认已配置 WNFLB_ACCOUNT，且容器网络/代理可访问站点）'); return; }
    if (!getCookieVal('S5r8_2132_auth', COOKIE)) { slog('Cookie 缺少 S5r8_2132_auth'); return; }
  checkCookieExpire(process.env.WNFLB_EXPIRE); // 手动过期日检测（不阻断，只提醒）

    // 签到执行（Cookie 失效时账号密码兜底重试一次）
    let signResult = '';
    let triedRelogin = false;
    for (let attempt = 0; attempt < 2; attempt++) {
        // 1. 访问首页，拿 formhash
        log('访问首页...');
        let resp;
        try { resp = await request(SITE + '/'); }
        catch (e) { slog(`请求失败: ${e.message}`); return; }

        const fhMatch = resp.body.match(/formhash=([a-f0-9]{8})/);
        if (!fhMatch) {
            if (USER && PASS && !triedRelogin) {
                triedRelogin = true;
                log('Cookie 可能已过期，尝试账号密码重新登录...');
                const nc = await loginAndGetCookie(USER, PASS);
                if (nc) { COOKIE = nc; saveCachedCookie(nc); log('✅ 已刷新 Cookie，重试签到'); continue; }
                slog('Cookie 已过期且登录兜底失败，请检查账号密码'); return;
            }
            slog('未找到 formhash，Cookie 可能已过期'); return;
        }
        const formhash = fhMatch[1];
        log(`formhash: ${formhash}`);

        // 2. 签到（fx_checkin 的 URL 里 formhash 出现两次，注意格式）
        const signUrl = `${SITE}/plugin.php?id=fx_checkin:checkin&formhash=${formhash}&${formhash}&infloat=yes&handlekey=fx_checkin&inajax=1&ajaxtarget=fwin_content_fx_checkin`;
        log('发送签到请求...');
        const signResp = await request(signUrl, {
            headers: { 'X-Requested-With': 'XMLHttpRequest' }
        });

        const body = signResp.body || '';

        // 3. 处理响应头里的 Set-Cookie（签到成功会返回 creditnotice/creditbase/creditrule）
        const setCookie = signResp.headers['set-cookie'] || [];
        if (setCookie.some(c => c.includes('creditrule'))) {
            signResult = 'success'; break;
        } else if (body.includes('成功') || body.includes('每日签到')) {
            signResult = 'success'; break;
        } else if (body.includes('已签') || body.includes('重复')) {
            signResult = 'already'; break;
        } else if (body.includes('login') || signResp.status === 302) {
            if (USER && PASS && !triedRelogin) {
                triedRelogin = true;
                log('Cookie 已过期，尝试账号密码重新登录后续签...');
                const nc = await loginAndGetCookie(USER, PASS);
                if (nc) { COOKIE = nc; saveCachedCookie(nc); log('✅ 已刷新 Cookie，重试签到'); continue; }
                slog('❌ Cookie 已过期，请重新登录'); return;
            }
            slog('❌ Cookie 已过期，请重新登录'); return;
        } else {
            slog(`⚠️ 响应片段: ${body.substring(0, 200)}`); return;
        }
    }

    if (signResult === 'success') slog('🎉 签到成功！');
    else if (signResult === 'already') slog('✅ 今天已签到');

    log('========== 签到结束 ==========');
    const wnOk = summaryLines.some(l => l.includes('签到成功') || l.includes('已签到'));
    log('──── 福利吧 执行汇总 ────');
    // 推送分账号汇总（面向 notify，简洁精要；单账号，纯签到无积分）
    const seqEmoji = ['1️⃣', '2️⃣', '3️⃣', '4️⃣', '5️⃣', '6️⃣', '7️⃣', '8️⃣', '9️⃣', '🔟'];
    const summaryContent = [];
    const _lastWn = summaryLines.filter(l => l.trim()).slice(-1)[0] || '';
    if (wnOk) {
        summaryContent.push('✔️ 签到成功');
    } else {
        summaryContent.push('❌ 签到失败' + (_lastWn ? '：' + _lastWn.slice(0, 50) : ''));
    }
    log(summaryContent.join('\n'));
    log('────────────────────────');
    await sendQingLongNotify('====== 福利吧 汇总日志 ======', summaryContent.join('\n'));
}

main().catch(e => log(`脚本异常: ${e.message}`));
