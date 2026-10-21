/**
 * 行情代理 + 定时触发数据更新（增强版）
 *
 * 作用：
 * 1. /?url=...  代理行情接口（页面 10 秒实时价格）
 * 2. /trigger   手动触发一次 GitHub 数据更新
 * 3. Cron Trigger 每 5 分钟自动触发（GitHub 自带 cron 不稳时的兜底）
 *
 * 部署步骤：
 * 1. Worker 编辑器全选删除，粘贴本文件全部内容 → Deploy
 * 2. Settings → Variables and Secrets → Add：
 *      名称 GH_TOKEN   值 ghp_xxxx（GitHub token，勾 repo + workflow）
 *    类型选 Secret
 * 3. Settings → Triggers → Cron Triggers → Add：
 *      */5 * * * *
 * 4. 再点一次 Deploy
 *
 * 说明：GitHub token 存在 Cloudflare 加密变量里，不会出现在代码和日志中。
 */

const REPO = "caoyunhao0206/stock-floater";
const WORKFLOW = "update.yml";

async function dispatchUpdate(env) {
  if (!env.GH_TOKEN) return 0;
  try {
    const r = await fetch(
      `https://api.github.com/repos/${REPO}/actions/workflows/${WORKFLOW}/dispatches`,
      {
        method: "POST",
        headers: {
          "Authorization": "Bearer " + env.GH_TOKEN,
          "Accept": "application/vnd.github+json",
          "Content-Type": "application/json",
          "User-Agent": "stock-floater-worker"
        },
        body: JSON.stringify({ ref: "main" })
      }
    );
    return r.status;
  } catch (e) {
    return -1;
  }
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    // 手动触发数据更新
    if (url.pathname === "/trigger") {
      const st = await dispatchUpdate(env);
      return new Response("trigger " + st, {
        headers: {
          "Access-Control-Allow-Origin": "*",
          "Content-Type": "text/plain; charset=utf-8"
        }
      });
    }

    // 行情代理
    const u = url.searchParams.get("url");
    if (!u) {
      return new Response("need url", {status: 400,
        headers: {"Access-Control-Allow-Origin": "*"}});
    }
    if (!u.startsWith("https://qt.gtimg.cn/") && !u.startsWith("https://hq.sinajs.cn/")) {
      return new Response("host not allowed", {status: 403,
        headers: {"Access-Control-Allow-Origin": "*"}});
    }
    try {
      const r = await fetch(u, {headers: {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
        "Referer": "https://gu.qq.com/",
        "Accept": "*/*"
      }});
      const t = await r.text();
      return new Response(t, {status: 200, headers: {
        "Access-Control-Allow-Origin": "*",
        "Content-Type": "text/plain; charset=utf-8",
        "Cache-Control": "no-store"
      }});
    } catch (e) {
      return new Response("ERR: " + e.message, {status: 502,
        headers: {"Access-Control-Allow-Origin": "*"}});
    }
  },

  // 每 5 分钟自动触发一次数据更新
  async scheduled(event, env, ctx) {
    ctx.waitUntil(dispatchUpdate(env));
  }
};
