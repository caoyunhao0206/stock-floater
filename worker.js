/**
 * Cloudflare Worker：行情接口 CORS 代理（免费，10 万请求/天）
 *
 * 部署步骤（手机也能做，约 2 分钟）：
 * 1. 打开 dash.cloudflare.com 注册/登录（免费，不需要信用卡）
 * 2. 左侧菜单 Workers & Pages → Create → Workers → Create Worker
 * 3. 名字随便填（如 stock-proxy），点 Deploy
 * 4. 点 Edit code，把本文件全部内容粘贴进去，覆盖原有代码
 * 5. 右上角 Deploy
 * 6. 复制你的地址，形如 https://stock-proxy.你的子域.workers.dev
 * 7. 打开盯盘页面，点右上角状态文字 → 填入地址 → 保存并连接
 *
 * 安全说明：
 * - 只放行 qt.gtimg.cn 和 hq.sinajs.cn 两个行情域名，不能拿它当通用代理
 * - 不记录、不存储任何请求内容
 */

export default {
  async fetch(request) {
    const url = new URL(request.url);

    // 健康检查
    if (url.pathname === "/" && !url.searchParams.get("url")) {
      return new Response(
        "行情代理已就绪。用法：/ ?url=https://qt.gtimg.cn/q=sh600519",
        {headers: {"Content-Type": "text/plain; charset=utf-8",
                   "Access-Control-Allow-Origin": "*"}}
      );
    }

    const target = url.searchParams.get("url");
    if (!target) {
      return new Response("missing url param", {status: 400,
        headers: {"Access-Control-Allow-Origin": "*"}});
    }

    // 白名单，防止被当成通用代理滥用
    const allowed = [
      "https://qt.gtimg.cn/",
      "https://hq.sinajs.cn/",
      "https://web.ifzq.gtimg.cn/"
    ];
    if (!allowed.some((p) => target.startsWith(p))) {
      return new Response("host not allowed", {status: 403,
        headers: {"Access-Control-Allow-Origin": "*"}});
    }

    try {
      const resp = await fetch(target, {
        headers: {
          "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15",
          "Referer": "https://finance.sina.com.cn/",
          "Accept": "*/*"
        },
        cf: {cacheTtl: 0}
      });
      const body = await resp.arrayBuffer();
      return new Response(body, {
        headers: {
          "Access-Control-Allow-Origin": "*",
          "Access-Control-Allow-Methods": "GET,OPTIONS",
          "Content-Type": "text/plain; charset=utf-8",
          "Cache-Control": "no-store"
        }
      });
    } catch (e) {
      return new Response("upstream error: " + e.message, {status: 502,
        headers: {"Access-Control-Allow-Origin": "*"}});
    }
  }
};
