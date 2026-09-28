/**
 * name: 学姐吧 - 每日签到
 * cron: 10 0,12 * * *
 * 环境变量：
 *   XJB_TOKEN  - 单个 JWT Token (即 Cookie 里的 b2_token)
 *   XJB_TOKENS - 多个 Token，用 @ 分隔
 *
 * 🚀 Token 一键获取：学姐吧登录后 F12 → Console 执行:
 *   copy(document.cookie.match(/b2_token=([^;]+)/)[1])
 *
 * 认证说明（必须同时带 Cookie + Bearer，两个都不能少）：
 *   学姐吧 B2 主题 API 很奇葩：
 *   - unread-count 只认 Cookie 或 Bearer（GET）
 *   - userMission   签到接口只认 Bearer
 *   - getUserMission 认 Cookie 或 Bearer（POST form）
 *   所以保险做法是 Cookie + Bearer 双带
 *
 */

const SCRIPT_NAME = '学姐吧签到';
const SCRIPT_VERSION = '1.1.0';
const BASE_URL = 'https://xuejieba2026.com';
const UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36';

// ---------- 工具函数 ----------
function log(msg) {
  const t = new Date().toLocaleTimeString('zh-CN', { hour12: false });
  console.log(`[${t}] ${msg}`);
}

function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

function splitTokens(value) {
  if (!value) return [];
  return value.split(/@/).map(s => s.trim()).filter(s => s.startsWith('eyJ'));
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
    log(`  ✅ 签到成功！+${d.credit || ''}积分 | 连续签到:${d.mission.tk?.days || 0}天 | 当前积分:${d.mission.my_credit || '?'}`);
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
    log(`  ❌ Token 已过期 (${info.expDate})，请重新抓取`);
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
async function runOne(token, idx) {
  log(`\n${'─'.repeat(30)}`);
  log(`🔑 账号 #${idx}`);

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

  let tokens = splitTokens(process.env.XJB_TOKENS);
  if (tokens.length === 0) tokens = splitTokens(process.env.XJB_TOKEN);

  if (tokens.length === 0) {
    console.log('❌ 未检测到 Token');
    console.log('   请配置环境变量 XJB_TOKEN 或 XJB_TOKENS');
    console.log('   Token 获取: 学姐吧登录后 F12 → Console 执行:');
    console.log("     copy(document.cookie.match(/b2_token=([^;]+)/)[1])");
    process.exit(1);
  }

  console.log(`👥 共 ${tokens.length} 个账号待签到`);

  let success = 0;
  for (let i = 0; i < tokens.length; i++) {
    try {
      if (await runOne(tokens[i], i + 1)) success++;
    } catch (e) {
      log(`  💥 异常: ${e.message}`);
    }
    if (i < tokens.length - 1) await sleep(1500);
  }

  console.log(`\n${'='.repeat(42)}`);
  console.log(`🏁 完成：${success}/${tokens.length} 个账号签到成功`);
}

main().catch((e) => {
  console.error('💥 脚本崩溃:', e);
  process.exit(1);
});