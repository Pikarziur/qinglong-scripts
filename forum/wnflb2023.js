/*
# name:福利吧
# cron: 10 0,12 * * *
*/

// ────────────────────────────────────────────
// 任务流程：
//   1. 从环境变量读取 Cookie（WNFLB_COOKIE / 旧 wnflb2023_cookie）
//   2. 访问论坛首页，正则提取签到所需的 formhash
//   3. 请求 fx_checkin 签到接口完成每日签到
//   4. 根据响应判断成功 / 已签 / Cookie 过期，输出汇总并通知
// 可控参数：
//   WNFLB_COOKIE         必填。直接给 Cookie 字符串（含 Discuz 字段 S5r8_2132_auth; S5r8_2132_saltkey）
//   WNFLB_NOTIFY        通知开关，默认开启；填 0/false/off/no 关闭
//   WNFLB_EXPIRE        可选，Cookie 预期过期日，如 2026-10-15。设了会提前3天提醒
//
// 🚀 Cookie 获取（登录 www.wnflb2023.com 后）：
//   重要：本站 S5r8_2132_auth / S5r8_2132_saltkey 为 HttpOnly，document.cookie 读不到
//         （console 里 document.cookie.indexOf('S5r8_2132_auth') 会返回 -1，属正常），
//         所以下面控制台命令拿不全，请用「方式一」从 Network 拿完整 Cookie。
//
//   方式一（推荐，避开 HttpOnly 限制）：
//     F12 → 网络(Network) → 刷新页面 → 点第一个请求(本页文档) → 标头 → 请求标头 → 复制 Cookie: 整行值
//     或右键该请求 → 复制 → 作为 cURL 复制，再从 -H 'Cookie: ...' 取出整段，填进 WNFLB_COOKIE。
//
//   方式二（仅当字段非 HttpOnly 时可用，本站不适用，保留作参考）：
//     console.log(['S5r8_2132_auth','S5r8_2132_saltkey'].map(k=>{const m=document.cookie.match(new RegExp(k+'=([^;]+)'));return m?k+'='+m[1]:null;}).filter(Boolean).join('; '))
//
// Discuz Cookie 说明：
//   - S5r8_2132_auth    登录凭证（必须，HttpOnly）
//   - S5r8_2132_saltkey 盐值（必须，HttpOnly）
// ────────────────────────────────────────────

const https = require('https');
const { URL } = require('url');
const fs = require('fs');
const path = require('path');

let COOKIE = ''; // 仅从环境变量读取
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
  } else if (remainDays <= 3) {
    log(`🔔 Cookie 即将过期（${remainDays}天），建议尽快更新！`);
  }
}

// 单次请求（直连，无代理）：返回 { status, headers, body }
function requestOnce(url, options = {}) {
    return new Promise((resolve, reject) => {
        const u = new URL(url);
        const headers = Object.assign({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36',
            'Accept': '*/*',
            'Accept-Language': 'zh-CN,zh;q=0.9',
            'Referer': SITE + '/',
            'Cookie': COOKIE
        }, options.headers || {});
        const opts = {
            method: options.method || 'GET',
            hostname: u.hostname,
            port: u.port || (u.protocol === 'https:' ? 443 : 80),
            path: u.pathname + u.search,
            headers,
            timeout: 20000
        };
        const req = https.request(opts, (res) => {
            let data = '';
            res.on('data', chunk => data += chunk);
            res.on('end', () => resolve({ status: res.statusCode, headers: res.headers, body: data }));
        });
        req.on('error', reject);
        req.setTimeout(20000, () => { req.destroy(); reject(new Error('请求超时')); });
        if (options.body) req.write(options.body);
        req.end();
    });
}

// 带重试的请求：网络偶发卡顿时重连（最多 MAX_RETRY 次）
const MAX_RETRY = 3;
async function request(url, options = {}) {
    let lastErr = null;
    for (let i = 0; i < MAX_RETRY; i++) {
        try {
            return await requestOnce(url, options);
        } catch (e) {
            lastErr = e;
            log(`请求失败（第${i + 1}/${MAX_RETRY}次，重试）: ${e.message}`);
        }
    }
    throw lastErr || new Error('请求失败');
}

function getCookieVal(name, cookieStr) {
    // 兼容 Cookie 串用 "; " 或 ";" 分隔（Cookie-Editor 等工具导出常为无空格分隔）
    const m = cookieStr.match(new RegExp('(?:^|;\\s*)' + name + '=([^;]*)'));
    return m ? decodeURIComponent(m[1]) : '';
}

async function main() {
    const summaryLines = [];
    const slog = (m) => { log(m); summaryLines.push(m); };
    log('========== WN2023 签到 ==========');

    // Cookie 仅从环境变量读取（不再有缓存文件 / 账号密码登录 / 代理）
    COOKIE = (process.env.WNFLB_COOKIE || process.env.wnflb2023_cookie || '').trim();
    if (!COOKIE) {
        slog('未配置 Cookie（请设置环境变量 WNFLB_COOKIE）');
        return;
    }
    log('使用环境变量 Cookie');
    if (!getCookieVal('S5r8_2132_auth', COOKIE)) { slog('Cookie 缺少 S5r8_2132_auth'); return; }
    checkCookieExpire(process.env.WNFLB_EXPIRE);

    // 1. 访问首页，拿 formhash
    log('访问首页...');
    let resp;
    try { resp = await request(SITE + '/'); }
    catch (e) { slog(`请求失败: ${e.message}`); return; }

    const fhMatch = resp.body.match(/formhash=([a-f0-9]{8})/);
    if (!fhMatch) {
        slog('未找到 formhash，Cookie 可能已过期，请更新 WNFLB_COOKIE');
        return;
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
    const setCookie = signResp.headers['set-cookie'] || [];
    let signResult = '';
    if (setCookie.some(c => c.includes('creditrule')) || body.includes('成功') || body.includes('每日签到')) {
        signResult = 'success';
    } else if (body.includes('已签') || body.includes('重复')) {
        signResult = 'already';
    } else if (body.includes('login') || signResp.status === 302) {
        slog('❌ Cookie 已过期或未登录，请更新 WNFLB_COOKIE');
        return;
    } else {
        slog(`⚠️ 响应片段: ${body.substring(0, 200)}`);
        return;
    }

    if (signResult === 'success') slog('🎉 签到成功！');
    else if (signResult === 'already') slog('✅ 今天已签到');

    log('========== 签到结束 ==========');
    const wnOk = summaryLines.some(l => l.includes('签到成功') || l.includes('已签到'));
    const summaryContent = [];
    const _lastWn = summaryLines.filter(l => l.trim()).slice(-1)[0] || '';
    if (wnOk) {
        summaryContent.push('✔️ 签到成功');
    } else {
        summaryContent.push('❌ 签到失败' + (_lastWn ? '：' + _lastWn.slice(0, 50) : ''));
    }
    log(summaryContent.join('\n'));
    await sendQingLongNotify('====== 福利吧 汇总日志 ======', summaryContent.join('\n'));
}

main().catch(e => log(`脚本异常: ${e.message}`));
