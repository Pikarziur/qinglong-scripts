/*
# name: 丸丫甄选
# cron: 44 7,16 * * *
*/

// ────────────────────────────────────────────
// 任务流程：
//   1. 读取 YYB_SERVER 账号基座，按 YYB_ONLY_REFS 白名单过滤 ref
//   2. 调用 YYBGO 的 /wxapp/getCode 获取 wx.login code
//   3. 用 code 走有赞 authorize 静默登录，换取 session
//   4. 查询签到活动(check-in-info)，执行签到(checkinV2)，并查询积分
//   5. 输出汇总（含今日领取 / 总积分）并由 $.done 发送通知
// 可控参数：
//   YYB_SERVER      必填。格式「地址@ref#备注」，多账号换行分隔
//   YYB_ONLY_REFS   白名单常量。留空 [] 跑全部；填 ["1","2"] 只跑对应 ref
// ────────────────────────────────────────────

const YYB_ONLY_REFS = [];  // 账号白名单：留空 [] = 跑 YYB_SERVER 里的全部账号；填入 ref（如 "1"）只跑对应账号

// ===== YYB-Go-Enhanced + QingLong standalone adapter =====
function _yybRoutes() {
    const onlyRefs = (YYB_ONLY_REFS || []).map(r => _yybCleanRef(String(r)));
    const routes = String(process.env.YYB_SERVER || '')
        .split(/[\s&]+/).map(v => v.trim()).filter(Boolean)
        .map((line, index) => {
            const at = line.lastIndexOf('@');
            if (at <= 0 || at >= line.length - 1) {
                throw new Error(`YYB_SERVER 第 ${index + 1} 行格式错误，应为 地址@账号标识`);
            }
            let server = line.slice(0, at).trim().replace(/\/+$/, '');
            if (!/^https?:\/\//i.test(server)) server = `http://${server}`;
            return { server, ref: line.slice(at + 1).trim() };
        });
    if (!routes.length) throw new Error('未配置 YYB_SERVER（地址@账号标识，支持换行/空格/& 分隔）');
    if (onlyRefs.length) {
        const filtered = routes.filter(x => onlyRefs.includes(_yybCleanRef(x.ref)));
        console.log(`[账号过滤] YYB_ONLY_REFS=${JSON.stringify(YYB_ONLY_REFS)} 命中 ${filtered.length}/${routes.length} 个账号`);
        return filtered;
    }
    return routes;
}

function _yybCleanRef(value) {
    return String(value || '').split('#')[0].replace(/^(wx|yyb|wmpf|syzs):/i, '').trim();
}

function _yybRouteFor(identifier) {
    const routes = _yybRoutes();
    const wanted = _yybCleanRef(identifier);
    const exact = routes.find(x => _yybCleanRef(x.ref) === wanted);
    if (exact) return exact;
    if (/^\d+$/.test(wanted) && routes[Number(wanted) - 1]) return routes[Number(wanted) - 1];
    if (routes.length === 1) return routes[0];
    throw new Error(`YYB_SERVER 中找不到账号标识：${wanted || '(空)'}`);
}

async function getSingleCode(appId, identifier) {
    const route = _yybRouteFor(identifier);
    const response = await axios.post(`${route.server}/wxapp/getCode`,
        { ref: route.ref, app_id: appId },
        { timeout: 30000, headers: { 'Content-Type': 'application/json' } });
    const body = response.data;
    if (!body || Number(body.code) !== 0) {
        throw new Error(`/wxapp/getCode 返回失败：${body?.msg || body?.message || JSON.stringify(body)}`);
    }
    const code = body.data?.result?.code;
    if (!code) throw new Error('/wxapp/getCode 未返回 data.result.code');
    return code;
}

async function _resolveYybAccounts(envName = '') {
    const structured = new Set(['qmai', 'quncrm']);
    const configured = structured.has(envName) ? String(process.env[envName] || '').trim() : '';
    if (configured) return configured.split(/[\r\n&]+/).map(v => v.trim()).filter(Boolean);
    return _yybRoutes().map(x => x.ref);
}
global.getSingleCode = getSingleCode;
global.resolveAccounts = _resolveYybAccounts;

async function _sendQingLongNotify(title, content) {
    const candidates = ['./sendNotify', '../sendNotify', '/ql/data/scripts/sendNotify', '/ql/scripts/sendNotify'];
    let lastError = null;
    for (const candidate of candidates) {
        try {
            const mod = require(candidate);
            const send = mod?.sendNotify || mod?.send;
            if (typeof send === 'function') {
                await send(title, content);
                return true;
            }
        } catch (error) { lastError = error; }
    }
    console.log(`青龙通知失败（不影响任务结果）：${lastError?.message || '未找到通知模块'}`);
    return false;
}
const qlNotify = { sendNotify: _sendQingLongNotify, send: _sendQingLongNotify };
// ===== adapter end =====

/*
------------------------------------------
@Author: sm
@Date: 2026.08.16
@Description: 丸丫甄选（有赞微商城）每日签到得积分
------------------------------------------
变量名：wyzx
变量值：微信 openid/账号标识，多账号用 & 或换行

变量：
  YYB_SERVER     YYB-Go-Enhanced 路由，每行：地址@账号标识
  账号直接来自 YYB_SERVER；无需配置 WX_ID
------------------------------------------
接口契约（h5.youzan.com，有赞 SaaS 通用签到，与本仓库 yz19.js 同一套）：
  静默登录 POST /wscshop/weapp/authorize.json  {appId, clientBiz, code}
        -> {accessToken|access_token, sessionId, kdtId, ...}
 签到活动 GET  /wscump/checkin/check-in-info.json -> {checkInId}（camelCase）
 活动详情 GET  /wscump/checkin/get_activity_by_yzuid_v2.json?checkinId=<id>
        -> {isCheckin, continuesDay, dailyRewards[{desc}], rewards[{duration,prize[{desc:{middle,right}}}]}
 执行签到 GET  /wscump/checkin/checkinV2.json?checkinId=<id>
        -> {desc, list[{infos:{title}}], success}；今日已签由 get_activity 的 isCheckin 预检
 积分余额 GET  /wscump/integral/user_points.json -> {current_points|real_points}（214027 抓包未含，留作兼容探测）
  公共 query：app_id / kdt_id / access_token；公共头：Extra-Data(sid/version/...)
  统一响应：code==0 成功，否则 msg 为错误原因
------------------------------------------
常量来源（反编译 wx35322299c6492f6e 主包）：
  app-config.json /ext -> kdtId=100939936、userVersion=2.219.11.101
  app.js  authorize.json（静默登录，登录失败码 135000025/160210092 会重试）
  app.js / c.js  wscump/integral/user_points.json（getPoints）
说明：签到页 packages/ump/sign-in/index 等均在分包内，smallcat 的
  /wx/downloadurl 按文档只返回主包（参数仅 openid/appid/version_type），
  分包 js 解包后为空壳。故签到接口不是从该小程序分包里读出来的，而是沿用
  本仓库已验证可用的有赞通用签到契约（wxapp/yz19.js、yz9d.js），并已在主包中
  确认 authorize.json 与 user_points.json 存在，接口族一致，未做接口盲猜。
------------------------------------------
*/

class WeChatServer {
    constructor(config) { this.config = config || {}; }
    async getCode(wxid) {
        try {
            const ref = String(wxid).split('#')[0].trim();
            const code = await getSingleCode(this.config.appid, ref);
            return { data: { status: true, code, data: { code } } };
        } catch (e) {
            return { data: { status: false, message: e.message || String(e) } };
        }
    }
}

class Env {
    constructor(name) { this.name = name; this.userList = []; this.userIdx = 1; this.userCount = 0; this.logs = []; const originalLog = console.log; console.log = (...args) => { this.logs.push(args.join(" ")); originalLog.apply(console, args); }; }
    log(...args) { console.log(...args); }
    async wait(minMs, maxMs) { const ms = maxMs ? Math.floor(minMs + Math.random() * (maxMs - minMs)) : minMs; await new Promise(r => setTimeout(r, ms)); }
    async checkEnv(ckName) {
        const list = await global.resolveAccounts(ckName);
        this.userList = list;
        this.userCount = list.length;
        if (!this.userList.length) console.log('未配置可用的 YYB_SERVER 或脚本专用账号变量');
    }
    async done() {
        try {
            const notify = qlNotify;
            const content = (LAST_RESULTS && buildSummaryContent(LAST_RESULTS)) || this.logs.join('\n');
            await notify.sendNotify("====== 丸丫甄选 汇总日志 ======", content);
        } catch (e) { console.log('通知发送失败', e); }
    }
}

const $ = new Env("丸丫甄选签到");
const axios = Object.assign(async function axios(config = {}) {
    const method = String(config.method || 'GET').toUpperCase();
    let url = String(config.url || '');
    if (config.params && typeof config.params === 'object') {
        const query = new URLSearchParams();
        for (const [key, value] of Object.entries(config.params)) {
            if (value !== undefined && value !== null) query.append(key, String(value));
        }
        const text = query.toString();
        if (text) url += (url.includes('?') ? '&' : '?') + text;
    }
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), Number(config.timeout || 30000));
    const headers = { ...(config.headers || {}) };
    let body;
    if (!['GET', 'HEAD'].includes(method) && config.data !== undefined) {
        const contentType = Object.entries(headers).find(([key]) => key.toLowerCase() === 'content-type')?.[1] || '';
        body = typeof config.data === 'string' || Buffer.isBuffer(config.data)
            ? config.data
            : contentType.includes('application/x-www-form-urlencoded')
                ? new URLSearchParams(config.data).toString()
                : JSON.stringify(config.data);
        if (!contentType && typeof config.data === 'object') headers['Content-Type'] = 'application/json';
    }
    try {
        const response = await fetch(url, { method, headers, body, signal: controller.signal, redirect: 'follow' });
        const raw = await response.text();
        let data = raw;
        try { data = raw ? JSON.parse(raw) : ''; } catch {}
        const responseHeaders = Object.fromEntries(response.headers.entries());
        const setCookies = typeof response.headers.getSetCookie === 'function'
            ? response.headers.getSetCookie()
            : (response.headers.get('set-cookie') ? [response.headers.get('set-cookie')] : []);
        if (setCookies.length) responseHeaders['set-cookie'] = setCookies;
        const result = { status: response.status, statusText: response.statusText,
            headers: responseHeaders, data };
        const accepted = typeof config.validateStatus === 'function'
            ? config.validateStatus(response.status)
            : response.status >= 200 && response.status < 300;
        if (!accepted) {
            const error = new Error(`HTTP ${response.status}`);
            error.response = result;
            throw error;
        }
        return result;
    } finally {
        clearTimeout(timer);
    }
}, {
    request(config) { return axios(config); },
    get(url, config = {}) { return axios({ ...config, method: 'GET', url }); },
    post(url, data, config = {}) { return axios({ ...config, method: 'POST', url, data }); },
});
const fs = require("fs");
const path = require("path");

const MINI_APP_ID = "wx35322299c6492f6e";
const CLIENT_BIZ = "weapp_wsc";
const KDT_ID = "100939936";
const USER_VERSION = "2.219.11.101";
const PAGE_VERSION = "49"; // Referer 里的版本号，仅用于伪装来源，不参与校验（214027 抓包实采 servicewechat.com/wx35322299c6492f6e/49/page-frame.html）
const API_BASE = "https://h5.youzan.com";
const TOKEN_CACHE_FILE = path.join(__dirname, "wanyazhenxuan_token_cache.json");
const USER_AGENT =
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) MicroMessenger/3.9.12 MiniProgramEnv/Windows WindowsWechat/WMPF";

const ckName = "wyzx";
const wechat = new WeChatServer({ appid: MINI_APP_ID });

function short(value, max = 200) {
    if (value === undefined || value === null) return "";
    const text = typeof value === "string" ? value : JSON.stringify(value);
    return text.length > max ? `${text.slice(0, max)}...` : text;
}

function parsePoints(text = "") {
    const nums = String(text).match(/\d+/g);
    if (!nums) return 0;
    return nums.reduce((s, n) => s + Number(n), 0);
}

function readTokenCache() {
    try {
        if (!fs.existsSync(TOKEN_CACHE_FILE)) return {};
        return JSON.parse(fs.readFileSync(TOKEN_CACHE_FILE, "utf8")) || {};
    } catch (e) {
        return {};
    }
}

function writeTokenCache(cache) {
    try {
        fs.writeFileSync(TOKEN_CACHE_FILE, JSON.stringify(cache, null, 2), "utf8");
    } catch (e) {
        $.log(`写入token缓存失败: ${e.message || e}`);
    }
}

function maskPhone(phone = "") {
    return String(phone).replace(/^(\d{3})\d{4}(\d{4})$/, "$1****$2");
}

function pickToken(data = {}) {
    return data.accessToken || data.access_token || "";
}

function isTokenError(message) {
    return /access_token|token|登录|invalid session|session/i.test(String(message || ""));
}

function isRepeatCheckin(message) {
    return /已达最大参与次数|已签到|重复签到|今日已参与|已经签到/.test(String(message || ""));
}

// yyb_go 取码（getSingleCode 内部已带节流与重试），失败时上层登录会记录原因
async function getWxCode(openid) {
    const { data } = await wechat.getCode(openid);
    if (!data || data.status === false) throw new Error(`yyb_go 取code失败: ${data?.message || short(data)}`);
    const code = data?.code || data?.data?.code;
    if (!code || typeof code !== "string") throw new Error(`yyb_go 未返回 code: ${short(data)}`);
    return code;
}

class Task {
    constructor(openid) {
        this.index = $.userIdx++;
        this.openid = String(openid || "").trim();
        this.token = "";
        this.sessionId = "";
        this.cookie = "";
        this.kdtId = KDT_ID;
        this.userInfo = {};
        this.checkinId = "";
        this.isShow = false;
        this.signedBefore = false;
        this.continuesDay = 0;
        this.todayStatus = "未执行";
        this.todayEarned = 0;
        this.totalPoints = null;
    }

    async run() {
        const cached = this.getCachedToken();
        if (cached) {
            this.applyToken(cached);
            $.log(`账号[${this.index}] 使用缓存token`);
            if (!(await this.checkToken())) {
                this.removeCachedToken();
                $.log(`账号[${this.index}] 缓存token失效，重新登录`);
            }
        }

        if (!this.token) {
            await this.loginByWxCode();
            if (!this.token) return;
        }

        await this.showCheckinPage();
        await this.doCheckin();
        await this.getPoints();
    }

    getCachedToken() {
        const cache = readTokenCache();
        return cache[this.openid] || null;
    }

    saveCachedToken() {
        if (!this.token) return;
        const cache = readTokenCache();
        cache[this.openid] = {
            accessToken: this.token,
            sessionId: this.sessionId,
            kdtId: this.kdtId,
            cookie: this.cookie,
            mobile: this.userInfo.mobile || "",
            nickName: this.userInfo.nick_name || this.userInfo.nickName || "",
            updatedAt: new Date().toISOString(),
        };
        writeTokenCache(cache);
    }

    removeCachedToken() {
        const cache = readTokenCache();
        if (cache[this.openid]) {
            delete cache[this.openid];
            writeTokenCache(cache);
        }
        this.token = "";
        this.sessionId = "";
        this.cookie = "";
    }

    applyToken(data = {}) {
        this.token = pickToken(data);
        this.sessionId = data.sessionId || data.session_id || "";
        this.kdtId = String(data.kdtId || data.kdt_id || KDT_ID);
        // 登录响应本身不带 cookie 字段，此时不要覆盖 request() 从 Set-Cookie 抓到的值
        if (data.cookie) this.cookie = data.cookie;
    }

    getHeaders(extra = {}) {
        const headers = {
            "User-Agent": USER_AGENT,
            "Referer": `https://servicewechat.com/${MINI_APP_ID}/${PAGE_VERSION}/page-frame.html`,
            "Accept": "*/*",
            "Extra-Data": JSON.stringify({
                is_weapp: 1,
                sid: this.sessionId || "",
                version: USER_VERSION,
                clientType: "weapp-miniprogram",
                client: "weapp",
                bizEnv: "wsc",
            }),
            ...extra,
        };
        if (this.cookie) headers.Cookie = this.cookie;
        return headers;
    }

    getBaseParams(params = {}) {
        return {
            app_id: MINI_APP_ID,
            kdt_id: this.kdtId,
            access_token: this.token,
            ...params,
        };
    }

    async request({ method = "GET", path: apiPath, params = {}, data = {}, skipToken = false }) {
        const options = {
            method,
            url: `${API_BASE}${apiPath.startsWith("/") ? apiPath : `/${apiPath}`}`,
            headers: this.getHeaders(method === "POST" ? { "Content-Type": "application/json" } : {}),
            timeout: 15000,
            validateStatus: () => true,
        };
        options.params = skipToken ? params : this.getBaseParams(params);
        if (method !== "GET") options.data = data;

        const { data: result, status, headers } = await axios.request(options);
        if (headers && headers["set-cookie"]) {
            this.cookie = headers["set-cookie"].map((item) => item.split(";")[0]).join("; ");
        }
        if (status !== 200) throw new Error(`HTTP ${status}: ${short(result)}`);
        if (!result || result.code !== 0) throw new Error(result?.msg || short(result));
        return result.data;
    }

    async loginByWxCode() {
        try {
            const code = await getWxCode(this.openid);
            const data = await this.request({
                method: "POST",
                path: "/wscshop/weapp/authorize.json",
                skipToken: true,
                data: {
                    appId: MINI_APP_ID,
                    clientBiz: CLIENT_BIZ,
                    code,
                },
            });
            this.applyToken(data);
            this.userInfo = data || {};
            if (!this.token) throw new Error(`登录响应未包含 accessToken: ${short(data)}`);
            this.saveCachedToken();
            $.log(
                `账号[${this.index}] 登录成功: ${data.nick_name || data.nickName || ""} ${maskPhone(data.mobile) || ""}`
            );
        } catch (e) {
            this.todayStatus = "登录失败";
            $.log(`账号[${this.index}] 登录失败: ${e.message || e}`);
        }
    }

    async checkToken() {
        try {
            await this.request({ path: "/wscump/checkin/check-in-info.json" });
            return true;
        } catch (e) {
            return false;
        }
    }

    async showCheckinPage() {
        try {
            const info = await this.request({ path: "/wscump/checkin/check-in-info.json" });
            this.checkinId = info?.checkInId || info?.checkinId || "";
            this.isShow = true;
            $.log(`账号[${this.index}] 签到活动: checkInId=${this.checkinId || "未获取"}`);
        } catch (e) {
            $.log(`账号[${this.index}] 获取签到活动失败: ${e.message || e}`);
            if (isTokenError(e.message || e)) this.removeCachedToken();
            return;
        }
        if (!this.checkinId) {
            $.log(`账号[${this.index}] 未获取到 checkInId，跳过签到`);
            return;
        }
        try {
            const act = await this.request({
                path: "/wscump/checkin/get_activity_by_yzuid_v2.json",
                params: { checkinId: this.checkinId },
            });
            // 幂等预检：服务端明确 isCheckin=true 表示今日已签，跳过提交
            this.signedBefore = !!act?.isCheckin;
            this.continuesDay = Number(act?.continuesDay || 0) || 0;
            if (this.signedBefore) {
                this.todayStatus = "今日已签";
                this.todayEarned = parsePoints(todayReward);
            }
            const todayReward = (act?.dailyRewards || []).map(x => x?.desc).filter(Boolean).join("、");
            const milestones = (act?.rewards || [])
                .map(x => `${x?.duration || "?"}天/${x?.prize?.[0]?.desc?.middle ?? "?"}${x?.prize?.[0]?.desc?.right || ""}`)
                .filter(Boolean)
                .join("、");
            $.log(
                `账号[${this.index}] 今日${this.signedBefore ? "已签" : "未签"}` +
                    (this.continuesDay ? ` 连续${this.continuesDay}天` : "") +
                    (todayReward ? ` 今日奖励:${todayReward}` : "") +
                    (milestones ? ` 连签奖励:${milestones}` : "")
            );
        } catch (e) {
            $.log(`账号[${this.index}] 获取签到状态失败: ${e.message || e}`);
        }
    }

    async doCheckin() {
        if (!this.checkinId) {
            $.log(`账号[${this.index}] 未获取到 checkinId，跳过签到`);
            return;
        }
        if (this.signedBefore) {
            $.log(`账号[${this.index}] 今日已签到，跳过提交`);
            return;
        }
        try {
            const data = await this.request({
                path: "/wscump/checkin/checkinV2.json",
                params: { checkinId: this.checkinId },
            });
            const awards = (data?.list || [])
                .map((item) => item?.infos?.title)
                .filter(Boolean)
                .join(", ");
            this.todayStatus = "签到成功";
            this.todayEarned = parsePoints(awards);
            $.log(`账号[${this.index}] 签到成功: ${data?.desc || ""}${awards ? ` ${awards}` : ""}`);
        } catch (e) {
            const message = String(e.message || e);
            if (isRepeatCheckin(message)) {
                this.todayStatus = "今日已签";
                $.log(`账号[${this.index}] 今日已签到`);
                return;
            }
            this.todayStatus = "签到失败";
            $.log(`账号[${this.index}] 签到失败: ${message}`);
            if (isTokenError(message)) this.removeCachedToken();
        }
    }

    async getPoints() {
        // 抓包(214027)未包含积分余额接口；user_points.json 为有赞通用契约的兼容探测，失败则降级为连签天数
        try {
            const data = await this.request({ path: "/wscump/integral/user_points.json" });
            const pts = data?.current_points ?? data?.real_points;
            if (pts !== undefined && pts !== null) {
                this.totalPoints = Number(pts);
                $.log(`账号[${this.index}] 当前积分: ${pts}`);
                return;
            }
        } catch (e) {
            // 部分店铺无此余额接口，忽略
        }
        if (this.continuesDay) $.log(`账号[${this.index}] 连续签到: ${this.continuesDay} 天`);
    }
}

let LAST_RESULTS = null;
function buildSummaryContent(results) {
    const seq = ['1️⃣', '2️⃣', '3️⃣', '4️⃣', '5️⃣', '6️⃣', '7️⃣', '8️⃣', '9️⃣', '🔟'];
    const lines = [];
    for (let i = 0; i < results.length; i++) {
        const t = results[i];
        const em = seq[i] || `${i + 1}.`;
        const phone = t.userInfo?.mobile ? maskPhone(t.userInfo.mobile) : "";
        const tag = phone ? `（${phone}）` : "";
        let acct = `${em} [账号${t.index}${tag}]`;
        if (t.totalPoints !== null && t.totalPoints !== undefined) {
            acct += ` 总积分${t.totalPoints}`;
            if (t.todayEarned > 0) acct += `(+${t.todayEarned})`;
        }
        lines.push(acct);
        if (t.todayStatus === "签到成功") lines.push("✔️ 签到成功");
        else if (t.todayStatus === "今日已签") lines.push("✔️ 今日已签");
        else lines.push("❌ " + (t.todayStatus || "签到失败"));
    }
    return lines.join("\n");
}
function printSummary(results) {
    if (!results || !results.length) return;
    LAST_RESULTS = results;
    $.log("");
    $.log("──── 丸丫甄选 执行汇总 ────");
    $.log(buildSummaryContent(results));
    $.log("==========================================");
}

!(async () => {
    const results = [];
    await $.checkEnv(ckName);
    for (const openid of $.userList) {
        const task = new Task(openid);
        await task.run();
        results.push(task);
        await $.wait(800);
    }
    printSummary(results);
})()
    .catch((e) => $.log(e.message || e))
    .finally(() => $.done());