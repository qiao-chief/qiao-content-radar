// 内容雷达 · 灵感速记写入代理
// 路由：POST radar.qiao-flow.cn/api/capture
// 密钥只存在 Worker 环境变量（XIAOK_APP_ID / XIAOK_APP_SECRET），浏览器从不接触。
// 访问控制依赖 Cloudflare Access（整个 radar.qiao-flow.cn 域名挂密码门），本 Worker 不写独立认证。
//
// 部署：
//   cd cloud/worker
//   npx wrangler deploy
//   npx wrangler secret put XIAOK_APP_ID
//   npx wrangler secret put XIAOK_APP_SECRET

const FEISHU = "https://open.feishu.cn/open-apis";
const BASE_TOKEN = "UoUlb5rcca4QN1s8e5UcY7OVn8f";
const TABLE_ID = "tblixXr2RAgrcYCW"; // 网站速记

const CORS_HEADERS = {
  "Access-Control-Allow-Origin": "https://radar.qiao-flow.cn",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type",
};

function jsonResp(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json; charset=utf-8", ...CORS_HEADERS },
  });
}

async function getTenantToken(env) {
  const r = await fetch(`${FEISHU}/auth/v3/tenant_access_token/internal`, {
    method: "POST",
    headers: { "Content-Type": "application/json; charset=utf-8" },
    body: JSON.stringify({ app_id: env.XIAOK_APP_ID, app_secret: env.XIAOK_APP_SECRET }),
  });
  const data = await r.json();
  if (data.code !== 0) throw new Error(`飞书令牌失败：${data.msg}`);
  return data.tenant_access_token;
}

async function createRecord(token, content, link) {
  const fields = {
    "内容": content,
    "状态": "待分诊",
    "时间": Date.now(), // 毫秒时间戳，datetime 字段
  };
  if (link) fields["链接"] = link;
  const r = await fetch(
    `${FEISHU}/bitable/v1/apps/${BASE_TOKEN}/tables/${TABLE_ID}/records`,
    {
      method: "POST",
      headers: {
        "Content-Type": "application/json; charset=utf-8",
        Authorization: `Bearer ${token}`,
      },
      body: JSON.stringify({ fields }),
    }
  );
  const data = await r.json();
  if (data.code !== 0) throw new Error(`写表失败：${data.code} ${data.msg}`);
  return data.data.record.record_id;
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    if (url.pathname !== "/api/capture") {
      return jsonResp({ ok: false, error: "not found" }, 404);
    }
    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: CORS_HEADERS });
    }
    if (request.method !== "POST") {
      return jsonResp({ ok: false, error: "method not allowed" }, 405);
    }

    let body;
    try {
      body = await request.json();
    } catch {
      return jsonResp({ ok: false, error: "invalid json" }, 400);
    }

    const content = String(body.content || "").trim().slice(0, 500);
    const link = String(body.link || "").trim().slice(0, 500);
    if (!content) {
      return jsonResp({ ok: false, error: "内容不能为空" }, 400);
    }
    if (link && !/^https?:\/\//i.test(link)) {
      return jsonResp({ ok: false, error: "链接要以 http(s):// 开头" }, 400);
    }

    try {
      const token = await getTenantToken(env);
      const recordId = await createRecord(token, content, link);
      return jsonResp({ ok: true, record_id: recordId });
    } catch (err) {
      return jsonResp({ ok: false, error: String(err.message || err) }, 500);
    }
  },
};
