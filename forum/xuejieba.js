/**
 * name: 学姐吧
 * cron: 10 0,12 * * *
 *
 * 环境变量：
 *   XJB_COOKIE    必填。登录后的 b2_token（JWT），既作 Cookie 也作 Bearer
 *
 * 认证说明（B2 主题签到只需 Bearer，参考通用 HAR 模板「通杀B2主题签到」）：
 *   - 签到接口 userMission、读取接口 getUserMission 均只认 Authorization: Bearer <b2_token>
 *   - 不要带 Cookie 头：带上 b2_token Cookie 反而会在部分 B2 站点触发会话校验分支，
 *     返回 403「请先登录」（Bearer 是无状态 JWT 校验，两者逻辑不同）
 *   所以本脚本只发 Bearer，XJB_COOKIE 的值即 b2_token（JWT）。
 *
 * ⚠️ 验证接口坑：/wp-json/b2-me/v1/unread-count 属 b2-me 命名空间，要求 WP 会话 Cookie，
 *    不认 b2_token JWT，哪怕 JWT 合法也会返回 403 noauth「请先登录」——不能用来验证 token。
 *    本脚本改用 b2/v1/getUserMission 在线验证（与签到同命名空间、只认 Bearer），
 *    并以签到接口 userMission 的返回作为最终成败判定。
 *
 * 🚀 Cookie 获取（登录 xuejieba2026.com 后）：
 *   推荐用 Cookie-Editor 扩展：打开本站已登录页 → 导出 → 选 Header 格式 →
 *   把整段串（含 b2_token=...）直接填进 XJB_COOKIE，脚本会自动提取 b2_token。
 *   也可直接复制 b2_token 的 JWT 字符串（eyJ 开头）填进 XJB_COOKIE。
 *
 * 日志规范：[LEVEL] [XJB] message   （LEVEL: INFO / WARN / ERROR）
 */

const BASE_URL = 'https://xuejieba2026.com';
const UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36';

// ========== 统一日志 ==========
function emit(level, msg) {
  console.log(`[${level}] [XJB] ${msg}`);
}
function log(msg)  { emit('INFO', msg); }
function warn(msg) { emit('WARN', msg); }
function err(msg)  { emit('ERROR', msg); }

function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

// 从 XJB_COOKIE 提取 b2_token：支持直接填 JWT，也支持填 Cookie-Editor 导出的整段 cookie 串
function extractB2Token(raw) {
  raw = (raw || '').trim();
  if (!raw) return '';
  const m = raw.match(/b2_token=([^;]+)/);
  if (m) return m[1].trim();
  return raw;
}

// 解码 JWT，提取用户信息（仅用于展示，不做自动登录）
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

// 构造认证 headers（只带 Bearer，不带 Cookie —— 见顶部认证说明）
function buildHeaders(token, contentType = null) {
  const h = {
    'Authorization': `Bearer ${token}`,
    'Accept': 'application/json, text/plain, */*',
    'User-Agent': UA,
    'Origin': BASE_URL,
    'Referer': `${BASE_URL}/mission/today`,
  };
  if (contentType) h['Content-Type'] = contentType;
  return h;
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
    err(`❌ 请求失败: ${res.error}`);
    return false;
  }
  if (res.status !== 200) {
    err(`❌ HTTP ${res.status}: ${JSON.stringify(res.data).slice(0, 200)}`);
    return false;
  }
  const d = res.data;
  // 返回可能是 JSON 也可能是纯字符串
  if (typeof d === 'string') {
    // B2 主题已签到时返回纯数字串（即当日签到记录/积分值，如 "10"、"13"，
    // 不同站点版本数值不固定，无固定 code）。只要返回非空且含数字即视为今日已签到。
    const s = d.trim();
    if (s && /\d/.test(s)) {
      log('♻️ 您今天已经签到过了（重复签到）');
      return true; // 算成功（不报错）
    }
    err(`⚠️ 签到返回: ${d}`);
    return false;
  }
  if (d?.mission) {
    const signDays = d.mission.tk?.days || 0;
    let msg = `🎉 签到成功！+${d.credit || ''}积分`;
    if (signDays) msg += ` | 连续签到:${signDays}天`;
    msg += ` | 当前积分:${d.mission.my_credit || '?'}`;
    log(msg);
    return true;
  }
  // 可能是 {"code":"invitation_error","message":"点太快啦！"}
  if (d?.code) {
    err(`❌ ${d.message || d.code}`);
    return false;
  }
  err(`⚠️ 签到返回异常: ${JSON.stringify(d).slice(0, 200)}`);
  return false;
}

// 验证 token 有效性（本地校验结构/过期 + b2/v1 在线确认，无自动登录）
async function verifyToken(token) {
  const info = parseJWT(token);
  if (!info) {
    err('🚫 Token 格式无效（非合法 JWT，应以 eyJ 开头且含两个"."）。请从 Cookie-Editor 重新复制完整 b2_token');
    return false;
  }
  const now = Date.now() / 1000;
  const remainDays = info.exp ? Math.floor((info.exp - now) / 86400) : -1;
  if (info.exp && info.exp < now) {
    err(`⏰ Token 已过期 (${info.expDate})，请重新获取 XJB_COOKIE`);
    return false;
  }
  log(`👤 用户ID: ${info.userId} | 剩余${remainDays}天 | 过期: ${info.expDate}`);
  if (remainDays >= 0 && remainDays <= 3) {
    warn(`⏰ Token 即将过期(${remainDays}天)，建议尽快更新！`);
  }

  // 用正确的 b2/v1 接口在线确认（与签到同命名空间、只认 Bearer）
  const res = await apiPost(
    '/wp-json/b2/v1/getUserMission',
    token,
    'count=10&paged=1',
    'application/x-www-form-urlencoded'
  );
  if (res.status === 200 && res.data?.mission) {
    const m = res.data.mission;
    log(`🔓 Token 验证通过 | 今日签到日期: ${m.date || '未签到'} | 当前积分: ${m.my_credit}`);
    return true;
  }
  if (res.status === 0) {
    // 网络异常不阻断，交给签到接口最终判定
    warn(`🔁 验证请求失败(${res.error})，继续尝试签到`);
    return true;
  }
  if (res.data?.code === 'noauth' || res.status === 403) {
    err('🚫 Token 被服务端拒绝(noauth)，请重新从 Cookie-Editor 获取 b2_token 填进 XJB_COOKIE');
    return false;
  }
  warn(`⚠️ Token 在线验证异常: ${JSON.stringify(res.data).slice(0, 120)}，继续尝试签到`);
  return true;
}

// ---------- 单账号入口 ----------
async function runOne(token) {
  if (!await verifyToken(token)) return false;
  await sleep(500);
  const ok = await doCheckin(token);
  return ok;
}

// ---------- 主入口 ----------
async function main() {
  log('🚀 学姐吧 签到开始');

  const token = extractB2Token(process.env.XJB_COOKIE);
  if (!token) {
    err('⚠️ 未配置 Cookie（请设置环境变量 XJB_COOKIE，值为登录后的 b2_token；也可直接填 Cookie-Editor 导出的整段串）');
    process.exit(1);
  }

  const ok = await runOne(token);

  log(`🏁 完成：学姐吧 ${ok ? '签到成功' : '签到失败'}`);
  if (!ok) process.exit(1);
}

main().catch((e) => {
  err('💥 脚本崩溃: ' + (e?.message || e));
  process.exit(1);
});
