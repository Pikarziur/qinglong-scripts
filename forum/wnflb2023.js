
// name:福利吧 - 签到
// cron: 0 5 * * *


//  环境变量：
//    wnflb2023_cookie         必填，格式：地址@账号ref，多账号换行


const https = require('https');
const http = require('http');
const { URL } = require('url');

// ========== 配置 ==========
const DEFAULT_COOKIE = ''; // 不使用环境变量时粘贴到这里
// =========================

const COOKIE = process.env.wnflb2023_cookie || DEFAULT_COOKIE;
const SITE = 'https://www.wnflb2023.com';

function log(msg) { console.log(`[WN签到] ${msg}`); }

function request(url, options = {}) {
    return new Promise((resolve, reject) => {
        const u = new URL(url);
        const lib = u.protocol === 'https:' ? https : http;
        const opts = {
            hostname: u.hostname,
            port: u.port || (u.protocol === 'https:' ? 443 : 80),
            path: u.pathname + u.search,
            method: options.method || 'GET',
            headers: Object.assign({
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36',
                'Accept': '*/*',
                'Accept-Language': 'zh-CN,zh;q=0.9',
                'Referer': SITE + '/',
                'Cookie': COOKIE
            }, options.headers || {})
        };
        const req = lib.request(opts, (res) => {
            let data = '';
            res.on('data', chunk => data += chunk);
            res.on('end', () => resolve({ status: res.statusCode, headers: res.headers, body: data }));
        });
        req.on('error', reject);
        req.setTimeout(15000, () => { req.destroy(); reject(new Error('请求超时')); });
        if (options.body) req.write(options.body);
        req.end();
    });
}

function getCookieVal(name, cookieStr) {
    const m = cookieStr.match(new RegExp('(?:^|; )' + name + '=([^;]*)'));
    return m ? decodeURIComponent(m[1]) : '';
}

async function main() {
    log('========== WN2023 签到 ==========');

    if (!COOKIE) { log('未配置 Cookie'); return; }
    if (!getCookieVal('S5r8_2132_auth', COOKIE)) { log('Cookie 缺少 S5r8_2132_auth'); return; }

    // 1. 访问首页，拿 formhash
    log('访问首页...');
    let resp;
    try { resp = await request(SITE + '/'); }
    catch (e) { log(`请求失败: ${e.message}`); return; }

    const fhMatch = resp.body.match(/formhash=([a-f0-9]{8})/);
    if (!fhMatch) { log('未找到 formhash，Cookie 可能已过期'); return; }
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
        log('🎉 签到成功！');
    } else if (body.includes('成功') || body.includes('每日签到')) {
        log('🎉 签到成功！');
    } else if (body.includes('已签') || body.includes('重复')) {
        log('✅ 今天已签到');
    } else if (body.includes('login') || signResp.status === 302) {
        log('❌ Cookie 已过期，请重新登录');
    } else {
        log(`⚠️ 响应片段: ${body.substring(0, 200)}`);
    }

    log('========== 签到结束 ==========');
}

main().catch(e => log(`脚本异常: ${e.message}`));
