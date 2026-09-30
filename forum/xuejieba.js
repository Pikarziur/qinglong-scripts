/**
 * name: 学姐吧 - 每日签到
 * cron: 10 0,12 * * *
 *
 * 环境变量：
 *   XJB_ACCOUNT   账号密码：「账号#密码」或「账号:密码」（必填）
 *   XJB_TOKEN_CACHE  可选。Token 缓存文件路径，默认 /ql/data/xuejieba.tokens
 *                   （青龙持久目录，订阅更新不受影响；可覆盖）
 *   XJB_NOTIFY   可选，通知开关，默认开启；填 0/false/off/no 关闭
 *
 * Token 获取逻辑（优先级）：
 *   1. 本地缓存文件 .xuejieba.tokens（登录成功后自动写入，格式：账号#token）
 *   2. 账号密码 XJB_ACCOUNT 登录获取并写缓存
 *   3. 都没有 → 报错退出
 *
 * 登录接口：POST /wp-json/jwt-auth/v1/token（标准 JWT Auth，无验证码），返回 {token}
 *   token 即原 b2_token，既作 Cookie 又作 Bearer。
 *
 * 🚀 配置：只需填 XJB_ACCOUNT=你的账号#你的密码，首次运行自动登录+缓存，之后全自动。
 *   若自动登录失败，可手动把 token 写入 XJB_TOKEN_CACHE 指向的文件救急（格式：账号#token）。
 *
 * 认证说明（必须同时带 Cookie + Bearer，两个都不能少）：
 *   学姐吧 B2 主题 API 很奇葩：
 *   - unread-count 只认 Cookie 或 Bearer（GET）
 *   - userMission   签到接口只认 Bearer
 *   - getUserMission 认 Cookie 或 Bearer（POST form）
 *   所以保险做法是 Cookie + Bearer 双带
 */

const fs = require('fs');
const path = require('path');

const SCRIPT_NAME = '学姐吧签到';
const SCRIPT_VERSION = '1.2.0';
const XJB_NOTIFY = !['0', 'false', 'off', 'no'].includes((process.env.XJB_NOTIFY || '1').trim().toLowerCase());
const BASE_URL = 'https://xuejieba2026.com';
const UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36';

const XJB_TOKEN_CACHE = (process.env.XJB_TOKEN_CACHE || '/ql/data/xuejieba.tokens');

// ---------- 工具函数 ----------
function log(msg) {
  const t = new Date().toLocaleTimeString('zh-CN', { hour12: false });
  console.log(`[${t}] ${msg}`);
}

function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

// 解析账号密码：XJB_ACCOUNT（账号#密码 / 账号:密码）
function parseAccount() {
  const raw = (process.env.XJB_ACCOUNT || '').trim();
  if (!raw) return null;
  const sep = raw.includes('#') ? '#' : (raw.includes(':') ? ':' : '#');
  const i = raw.indexOf(sep);
  const user = raw.slice(0, i).trim();
  const pass = raw.slice(i + 1).trim();
  return (user && pass) ? { user, pass } : null;
}

// 读取缓存的 Token：格式 "账号#token"
function loadCachedToken() {
  try {
    if (fs.existsSync(XJB_TOKEN_CACHE)) {
      const txt = fs.readFileSync(XJB_TOKEN_CACHE, 'utf8').trim();
      const i = txt.indexOf('#');
      if (i >= 0) {
        return { user: txt.slice(0, i).trim(), token: txt.slice(i + 1).trim() };
      }
    }
  } catch (e) { /* 忽略 */ }
  return null;
}

// 写回缓存（权限 0600）
function saveCachedToken(user, token) {
  try {
    fs.mkdirSync(path.dirname(XJB_TOKEN_CACHE), { recursive: true });
    fs.writeFileSync(XJB_TOKEN_CACHE, user + '#' + token, { mode: 0o600 });
    log('💾 已把 Token 缓存到本地文件');
  } catch (e) {
    log('⚠️ Token 缓存写入失败: ' + (e.message || e));
  }
}

// 解码 JWT，提取用户信息
function parseJWT(token) {
  try {
    const parts = token.split('.');
    if (parts.length !== 3) return null;
    const payload = JSON.parse(
      Buffer.from(parts[1].replace(/-/g, '+').replace(/_/g, '/'), 'base64').toString()
    );
    return {
      userId: payload?.data?.user?.id || payload?.user?.id || '未知',
      exp: payload.exp || 0,
      expDate: payload.exp ? new Date(payload.exp * 1000).toLocaleString('zh-CN') : '未知',
    };
  } catch (_) { return null; }
}

// 账号密码登录兜底：POST jwt-auth/v1/token 获取 JWT（即 b2_token）
async function loginAndGetToken(user, pass) {
  if (!user || !pass) return null;
  try {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 15000);
    const resp = await fetch(BASE_URL + '/wp-json/jwt-auth/v1/token', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/x-www-form-urlencoded',
        'Accept': 'application/json',
        'User-Agent': UA,
        'Referer': BASE_URL + '/',
      },
      body: 'username=' + encodeURIComponent(user) + '&password=' + encodeURIComponent(pass),
      signal: controller.signal,
    });
    clearTimeout(timer);
    const text = await resp.text();
    let d;
    try { d = JSON.parse(text); } catch (_) { d = {}; }
    if (d && d.token) {
      log('🔑 账号密码登录成功，已刷新 Token');
      return d.token;
    }
    log('⚠️ 登录失败: ' + (d.message || d.code || ('HTTP ' + resp.status)));
    return null;
  } catch (err) {
    log('⚠️ 登录异常: ' + err.message);
    return null;
  }
}

// 构造双认证 headers（Cookie + Bearer 都带）
function buildHeaders(token, contentType = null) {
  const h = {
    'Cookie': `b2_token=${token}`,
    'Authorization': `Bearer ${token}`,
    'Accept': 'application/json, text/plain, */*',
    'User-Agent': UA,
    'Origin': BASE_URL,
    'Referer': `${BASE_URL}/mission/today`,
  };
  if (contentType) h['Content-Type'] = contentType;
  return h;
}

async function apiGet(path, token) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 15000);
  try {
    const resp = await fetch(BASE_URL + path, {
      method: 'GET',
      headers: buildHeaders(token),
      signal: controller.signal,
    });
    const text = await resp.text();
    let data;
    try { data = JSON.parse(text); } catch (_) { data = text; }
    return { status: resp.status, data };
  } catch (err) {
    return { status: 0, error: err.message };
  } finally { clearTimeout(timer); }
}

async function apiPost(path, token, body = null, contentType = null) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 15000);
  try {
    const resp = await fetch(BASE_URL + path, {
      method: 'POST',
      headers: buildHeaders(token, contentType),
      body: body ?? undefined,
      signal: controller.signal,
    });
    const text = await resp.text();
    let data;
    try { data = JSON.parse(text); } catch (_) { data = text; }
    return { status: resp.status, data };
  } catch (err) {
    return { status: 0, error: err.message };
  } finally { clearTimeout(timer); }
}

// ---------- 核心功能 ----------

// 签到
async function doCheckin(token) {
  const res = await apiPost('/wp-json/b2/v1/userMission', token);
  if (res.status === 0) {
    log(`  ⚠️ 请求失败: ${res.error}`);
    return false;
  }
  if (res.status !== 200) {
    log(`  ⚠️ HTTP ${res.status}: ${JSON.stringify(res.data).slice(0, 200)}`);
    return false;
  }
  const d = res.data;
  // 返回可能是 JSON 也可能是纯字符串
  if (typeof d === 'string') {
    // "10" = 已签到 / 重复
    if (d === '10') {
      log(`  ⚠️ 您已经签到过了`);
      return true; // 算成功（不报错）
    }
    log(`  ⚠️ 签到返回: ${d}`);
    return false;
  }
  if (d?.mission) {
    const signDays = d.mission.tk?.days || 0;
    let msg = `  ✅ 签到成功！+${d.credit || ''}积分`;
    if (signDays) msg += ` | 连续签到:${signDays}天`;
    msg += ` | 当前积分:${d.mission.my_credit || '?'}`;
    log(msg);
    return true;
  }
  // 可能是 {"code":"invitation_error","message":"点太快啦！"}
  if (d?.code) {
    log(`  ⚠️ ${d.message || d.code}`);
    return false;
  }
  log(`  ⚠️ 签到返回异常: ${JSON.stringify(d).slice(0, 200)}`);
  return false;
}

// 获取任务状态
async function getMission(token) {
  const res = await apiPost(
    '/wp-json/b2/v1/getUserMission',
    token,
    'count=10&paged=1',
    'application/x-www-form-urlencoded'
  );
  if (res.status === 200 && res.data?.mission) {
    const m = res.data.mission;
    log(`  📊 今日签到日期: ${m.date || '未签到'} | 当前积分: ${m.my_credit}`);
    return true;
  }
  return false;
}

// 验证 token 有效性
async function verifyToken(token) {
  const info = parseJWT(token);
  if (!info) {
    log('  ⚠️ Token 格式无效');
    return false;
  }
  const now = Date.now() / 1000;
  const remainDays = info.exp ? Math.floor((info.exp - now) / 86400) : -1;
  if (info.exp && info.exp < now) {
    log(`  ❌ Token 已过期 (${info.expDate})，将尝试重新登录`);
    return false;
  }
  log(`  👤 用户ID: ${info.userId} | 剩余${remainDays}天 | 过期: ${info.expDate}`);
  if (remainDays >= 0 && remainDays <= 3) {
    log(`  🔔 Token 即将过期(${remainDays}天)，建议尽快更新！`);
  }

  // unread-count 接口验证（GET 方法）
  const res = await apiGet('/wp-json/b2-me/v1/unread-count', token);
  if (res.status === 200 && res.data?.msg !== undefined) {
    log(`  ✅ Token 验证通过`);
    return true;
  }
  if (res.status === 0) {
    log(`  ⚠️ 验证请求失败: ${res.error}`);
  } else {
    log(`  ⚠️ Token 验证失败: ${JSON.stringify(res.data).slice(0, 100)}`);
  }
  return false;
}

// ---------- 单账号入口 ----------
async function runOne(token) {
  log(`\n${'─'.repeat(30)}`);

  if (!await verifyToken(token)) return false;

  await sleep(500);
  const ok = await doCheckin(token);

  await sleep(500);
  await getMission(token);

  return ok;
}

// ---------- 主入口 ----------
async function main() {
  console.log(`\n🚀 ${SCRIPT_NAME} v${SCRIPT_VERSION}`);
  console.log(`📅 ${new Date().toLocaleString('zh-CN')}`);
  console.log('='.repeat(42));

  const acct = parseAccount();
  if (!acct) {
    console.log('❌ 未配置账号密码');
    console.log('   请配置环境变量 XJB_ACCOUNT=账号#密码');
    process.exit(1);
  }

  const cached = loadCachedToken();
  let token = (cached && cached.user === acct.user) ? cached.token : null;
  if (!token) {
    log(`账号 ${acct.user} 无有效缓存，尝试登录获取...`);
    token = await loginAndGetToken(acct.user, acct.pass);
    if (token) saveCachedToken(acct.user, token);
  }
  if (!token) {
    console.log('❌ 无可用的 Token（登录失败，请检查账号密码或手动写入缓存文件）');
    process.exit(1);
  }

  let ok = await runOne(token);
  // 若失败（Token 失效）且有密码，重新登录刷新一次
  if (!ok) {
    log(`🔄 签到失败，尝试重新登录刷新 Token...`);
    const nt = await loginAndGetToken(acct.user, acct.pass);
    if (nt) {
      saveCachedToken(acct.user, nt);
      ok = await runOne(nt);
    }
  }

  console.log(`\n${'='.repeat(42)}`);
  console.log(`🏁 完成：账号 ${acct.user} ${ok ? '签到成功' : '签到失败'}`);
}

main().catch((e) => {
  console.error('💥 脚本崩溃:', e);
  process.exit(1);
});
