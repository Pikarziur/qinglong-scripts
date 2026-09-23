
/*
# name:福利吧 - 签到
# cron: 10 7,16 * * *
*/


// ────────────────────────────────────────────
// 任务流程：
//   1. 读取 wnflb2023_cookie，校验含 S5r8_2132_auth 字段
//   2. 访问论坛首页，正则提取签到所需的 formhash
//   3. 请求 fx_checkin 签到接口完成每日签到
//   4. 根据响应 / Set-Cookie 判断成功 / 已签 / Cookie 过期，输出汇总并通知
// 可控参数：
//   wnflb2023_cookie   必填。论坛登录 Cookie，需含 S5r8_2132_auth
//   WNFLB_NOTIFY        通知开关，默认开启；填 0/false/off/no 关闭
// ────────────────────────────────────────────

const https = require('https');
const http = require('http');
const { URL } = require('url');
const fs = require('fs');
const path = require('path');

// ========== 配置 ==========
const DEFAULT_COOKIE = ''; // 不使用环境变量时粘贴到这里
// =========================

const COOKIE = process.env.wnflb2023_cookie || DEFAULT_COOKIE;
const SITE = 'https://www.wnflb2023.com';

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
    const summaryLines = [];
    const slog = (m) => { log(m); summaryLines.push(m); };
    log('========== WN2023 签到 ==========');

    if (!COOKIE) { slog('未配置 Cookie'); return; }
    if (!getCookieVal('S5r8_2132_auth', COOKIE)) { slog('Cookie 缺少 S5r8_2132_auth'); return; }

    // 1. 访问首页，拿 formhash
    log('访问首页...');
    let resp;
    try { resp = await request(SITE + '/'); }
    catch (e) { slog(`请求失败: ${e.message}`); return; }

    const fhMatch = resp.body.match(/formhash=([a-f0-9]{8})/);
    if (!fhMatch) { slog('未找到 formhash，Cookie 可能已过期'); return; }
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
        slog('🎉 签到成功！');
    } else if (body.includes('成功') || body.includes('每日签到')) {
        slog('🎉 签到成功！');
    } else if (body.includes('已签') || body.includes('重复')) {
        slog('✅ 今天已签到');
    } else if (body.includes('login') || signResp.status === 302) {
        slog('❌ Cookie 已过期，请重新登录');
    } else {
        slog(`⚠️ 响应片段: ${body.substring(0, 200)}`);
    }

    log('========== 签到结束 ==========');
    log('──── 福利吧 执行汇总 ────');
    log(summaryLines.join('\n') || '无结果（可能未配置 Cookie）');
    log('────────────────────────');
    await sendQingLongNotify('福利吧签到 执行汇总', summaryLines.join('\n') || '未配置 Cookie 或无签到结果');
}

main().catch(e => log(`脚本异常: ${e.message}`));
