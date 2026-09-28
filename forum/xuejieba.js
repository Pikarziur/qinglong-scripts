/**
 * 学姐吧 - 签到
 * 环境变量：
 *   XJB_TOKEN  - 单个 JWT Token (即 Cookie 里的 b2_token)
 *   XJB_TOKENS - 多个 Token，用 @ 分隔，如 token1@token2@token3
 *
 * Token 获取：登录后浏览器 DevTools → Application → Cookies → b2_token
 * 或 Console 执行: document.cookie.match(/b2_token=([^;]+)/)?.[1]
 *
 * API 来源：https://xuejieba2026.com/mission/today → 点击"立刻签到"
 *
 * 青龙面板配置：
 *   cron: 10 0,6 * * *
 *   依赖：Node.js >= 18 (原生 fetch)
 */

const SCRIPT_NAME = '学姐吧签到';
const SCRIPT_VERSION = '1.0.0';
const BASE_URL = 'https://xuejieba2026.com';
const UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36';

// ---------- 工具函数 ----------
function log(msg) {
  const t = new Date().toLocaleTimeString('zh-CN', { hour12: false });
  console.log(`[${t}] ${msg}`);
}

function sleep(ms) {
  return new Promise(r => setTimeout(r, ms));
}

function splitTokens(value) {
  if (!value) return [];
  return value.split(/@/).map(s => s.trim()).filter(s => s.startsWith('eyJ'));
}

// 解码 JWT payload，提取用户信息
function parseJWT(token) {
  try {
    const parts = token.split('.');
    if (parts.length !== 3) return null;
    const payload = JSON.parse(Buffer.from(parts[1].replace(/-/g, '+').replace(/_/g, '/'), 'base64').toString());
    return {
      userId: payload?.data?.user?.id || payload?.user?.id || '未知',
      exp: payload.exp || 0,
      expDate: payload.exp ? new Date(payload.exp * 1000).toLocaleString('zh-CN') : '未知'
    };
  } catch (_) {
    return null;
  }
}

async function apiPost(path, token, body = null, contentType = null) {
  const headers = {
    'Authorization': `Bearer ${token}`,
    'Accept': 'application/json, text/plain, */*',
    'User-Agent': UA,
    'Origin': BASE_URL,
    'Referer': `${BASE_URL}/mission/today`
  };
  if (contentType) headers['Content-Type'] = contentType;

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 15000);

  try {
    const resp = await fetch(BASE_URL + path, {
      method: 'POST',
      headers,
      body: body ?? undefined,
      signal: controller.signal
    });
    const text = await resp.text();
    let data;
    try { data = JSON.parse(text); } catch (_) { data = text; }
    return { status: resp.status, data };
  } catch (err) {
    return { status: 0, error: err.message };
  } finally {
    clearTimeout(timer);
  }
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
  if (d?.mission) {
    log(`  ✅ 签到成功！+${d.credit}积分 | 连续签到:${d.mission.tk?.days || 0}天 | 当前积分:${d.mission.my_credit || '?'}`);
    return true;
  }
  log(`  ⚠️ 签到返回异常: ${JSON.stringify(d).slice(0, 200)}`);
  return false;
}

// 获取任务状态（可选）
async function getMission(token) {
  const res = await apiPost(
    '/wp-json/b2/v1/getUserMission',
    token,
    'count=10&paged=1',
    'application/x-www-form-urlencoded'
  );
  if (res.status === 200 && res.data?.mission) {
    const m = res.data.mission;
    log(`  📊 任务状态: 今日${m.date} | 今日积分+${m.credit} | 当前积分${m.my_credit}`);
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
    log(`  ❌ Token 已过期 (${info.expDate})，请登录后重新获取`);
    return false;
  }
  log(`  👤 用户ID: ${info.userId} | 剩余${remainDays}天 | 过期:${info.expDate}`);
  if (remainDays >= 0 && remainDays <= 3) {
    log(`  🔔 Token 即将过期(${remainDays}天)，建议尽快更新！`);
  }

  // 用 unread-count 接口验证
  const res = await apiPost('/wp-json/b2-me/v1/unread-count', token);
  if (res.status === 200 && res.data?.msg !== undefined) {
    log(`  ✅ Token 有效`);
    return true;
  }
  log(`  ⚠️ Token 验证失败: ${JSON.stringify(res.data).slice(0, 100)}`);
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

  // 优先多账号变量，回退单账号
  let tokens = splitTokens(process.env.XJB_TOKENS);
  if (tokens.length === 0) tokens = splitTokens(process.env.XJB_TOKEN);

  if (tokens.length === 0) {
    console.log('❌ 未检测到 Token');
    console.log('   请在青龙面板配置环境变量 XJB_TOKEN 或 XJB_TOKENS');
    console.log('   Token 获取：登录后从 Cookie 的 b2_token 字段复制');
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

main().catch(e => {
  console.error('💥 脚本崩溃:', e);
  process.exit(1);
});
