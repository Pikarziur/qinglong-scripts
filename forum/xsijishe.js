/*
 * 绅士之社 (xsijishe.com) 青龙面板签到脚本
 * ------------------------------------------
 * 使用方法：
 * 1. 青龙面板 添加环境变量 xsijishe_cookie，值为 Cookie 字符串
 *    或直接把 cookie 写在下方 DEFAULT_COOKIE 中
 * 2. 添加定时任务：task xsijishe_qinglong.js
 * 3. 定时表达式：30 5 * * * （每天 08:30 执行）
 *
 * 注意：
 * - 需要青龙网络能访问 xsijishe.com
 * - cf_clearance 有效期有限，建议定期更新 Cookie
 */

const https = require('https');
const http = require('http');
const { URL } = require('url');

// 不使用青龙环境变量时，把 Cookie 粘贴到这里
const DEFAULT_COOKIE = '';

const COOKIE = process.env.xsijishe_cookie || DEFAULT_COOKIE;
const SITE = 'https://xsijishe.com';

function log(msg) { console.log(`[绅士签到] ${msg}`); }

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

function isSignedToday(cookieStr) {
    const misigntime = getCookieVal('SgL6_2132_misigntime', cookieStr);
    if (!misigntime) return false;
    const signedDate = new Date(parseInt(misigntime) * 1000);
    return signedDate.toDateString() === new Date().toDateString();
}

async function main() {
    log('========== 绅士之社签到 ==========');

    if (!COOKIE) { log('未配置 Cookie'); return; }
    if (!getCookieVal('SgL6_2132_auth', COOKIE)) { log('Cookie 中缺少 SgL6_2132_auth'); return; }

    if (isSignedToday(COOKIE)) { log('今天已经签过啦'); return; }

    log('正在访问首页...');
    let resp;
    try { resp = await request(SITE + '/'); }
    catch (e) { log(`网络请求失败: ${e.message}`); return; }

    if (resp.status === 403 || resp.body.includes('cloudflare')) {
        log('触发 Cloudflare 验证，青龙无法自动绕过');
        log('建议使用油猴脚本签到，或手动更新 cf_clearance');
        return;
    }

    const fhMatch = resp.body.match(/formhash=([a-f0-9]{8,})/);
    if (!fhMatch) { log('未找到 formhash'); return; }
    const formhash = fhMatch[1];
    log(`formhash: ${formhash}`);

    const signUrl = `${SITE}/k_misign-sign.html?operation=qiandao&format=global_usernav_extra&formhash=${formhash}&inajax=1&ajaxtarget=k_misign_topb`;
    log('发送签到请求...');
    const signResp = await request(signUrl, {
        headers: { 'X-Requested-With': 'XMLHttpRequest' }
    });

    const body = signResp.body || '';
    if (body.includes('已签到') || body.includes('misigntime')) log('签到成功!');
    else if (body.includes('已签')) log('今天已签到');
    else log(`响应: ${body.substring(0, 200)}`);

    log('========== 签到结束 ==========');
}

main().catch(e => log(`脚本异常: ${e.message}`));
