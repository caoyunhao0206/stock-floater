# -*- coding: utf-8 -*-
"""
AI 大模型接入（OpenAI 兼容协议）
支持 DeepSeek / 通义千问 / Kimi / 智谱 / OpenAI 等
用途：
1. 对推荐候选做二次研判（多空逻辑、消息真伪、买卖区间）
2. 对新闻做情绪与可信度判断
全部失败降级为规则引擎，不影响主流程
"""
import json
import logging
import re
import threading
import time

import requests

log = logging.getLogger("floater.ai")

CACHE = {}                 # key -> {"ts":..., "data":...}
CACHE_TTL = 1800           # 30 分钟
_lock = threading.Semaphore(3)
_cache_lock = threading.Lock()


class AIError(Exception):
    pass


def _parse_json(text):
    """从模型输出中稳健提取 JSON"""
    if not text:
        raise AIError("模型返回为空")
    text = text.strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    try:
        return json.loads(text)
    except Exception:  # noqa: BLE001
        m = re.search(r"\{[\s\S]*\}", text)
        if m:
            return json.loads(m.group(0))
        raise AIError("模型输出不是合法 JSON")


class AIClient:
    def __init__(self, base_url, api_key, model, timeout=50):
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = (api_key or "").strip()
        self.model = model or "deepseek-chat"
        self.timeout = timeout

    def ready(self):
        return bool(self.base_url and self.api_key and self.model)

    def chat(self, system, user, json_mode=True, temperature=0.3, max_tokens=1200):
        if not self.ready():
            raise AIError("AI 未配置")
        url = self.base_url + "/chat/completions"
        headers = {"Authorization": "Bearer " + self.api_key,
                   "Content-Type": "application/json"}
        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        last = None
        for attempt in range(2):
            try:
                with _lock:
                    r = requests.post(url, json=payload, headers=headers, timeout=self.timeout)
                if r.status_code == 400 and json_mode and attempt == 0:
                    # 部分模型不支持 response_format，去掉后重试
                    payload.pop("response_format", None)
                    continue
                if r.status_code != 200:
                    raise AIError("HTTP %s: %s" % (r.status_code, r.text[:200]))
                data = r.json()
                content = data["choices"][0]["message"]["content"]
                return content
            except AIError:
                raise
            except Exception as e:  # noqa: BLE001
                last = e
        raise AIError(str(last) if last else "调用失败")

    # ---------- 个股研判 ----------
    def analyze_stock(self, ctx, style="balanced"):
        style_txt = {
            "steady": "偏稳健，优先控制回撤，宁可错过不做错",
            "aggressive": "偏进攻，容忍波动换取弹性，但必须设止损",
            "balanced": "平衡型，兼顾胜率与盈亏比",
        }.get(style, "平衡型")
        system = (
            "你是资深A股量化研究员，擅长技术面、资金面与消息面综合研判。"
            "必须严格基于用户给出的数据推理，不得编造数据或新闻。"
            "只输出JSON对象，不要输出任何其他文字。"
        )
        user = (
            "请研判以下A股标的，风格偏好：%s。\n\n%s\n\n"
            "输出JSON，字段如下：\n"
            '{"rating":"强烈看多/看多/中性/看空/强烈看空",'
            '"score":整数-100到100,'
            '"confidence":整数0到100,'
            '"buy_zone":[建议买入下限,上限],'
            '"stop_loss":止损价,'
            '"target":目标价,'
            '"thesis":["核心理由，最多4条，每条不超过30字"],'
            '"risks":["主要风险，最多3条，每条不超过30字"],'
            '"news_check":"对消息面的独立判断，40字以内",'
            '"one_line":"一句话结论，25字以内"}\n'
            "要求：buy_zone 必须低于或接近现价；stop_loss 低于 buy_zone 下限；"
            "target 高于现价；若数据不足以判断则 rating 给中性、confidence 低于50。"
        ) % (style_txt, ctx)
        t0 = time.time()
        raw = self.chat(system, user)
        data = _parse_json(raw)
        data["_elapsed"] = round(time.time() - t0, 1)
        return data

    # ---------- 新闻研判 ----------
    def check_news(self, name, items):
        system = ("你是财经事实核查员。判断消息对股价的真实影响，"
                  "注意区分官方公告、权威媒体、传闻。只输出JSON。")
        lines = []
        for i, it in enumerate(items[:10]):
            lines.append("%d. [%s|%s] %s" % (
                i + 1, it.get("source", ""), it.get("date", ""), it.get("title", "")))
        user = ("标的：%s\n消息列表：\n%s\n\n"
                '输出JSON：{"score":整数-50到50,'
                '"credible":整数0到100,"summary":"整体消息面判断，50字以内",'
                '"key_news":["最重要的一条，20字以内"]}') % (name, "\n".join(lines))
        return _parse_json(self.chat(system, user, max_tokens=600))

    def test(self):
        raw = self.chat("你是一个测试助手。", '只输出JSON：{"ok":true}', max_tokens=50)
        return _parse_json(raw)


def build_context(stock):
    """把结构化数据压成给模型看的紧凑文本"""
    q = stock.get("quote") or {}
    ind = stock.get("indicators") or {}
    ext = stock.get("ext") or {}
    news = stock.get("news") or []
    extra = stock.get("extras") or {}

    lines = [
        "股票：%s（%s）" % (stock.get("name", ""), stock.get("code", "")),
        "现价：%.2f 元，今日涨跌：%.2f%%，换手率：%.2f%%，成交额：%.2f 亿"
        % (q.get("price", 0), q.get("change_pct", 0), q.get("turnover", 0) or 0,
           (q.get("amount", 0) or 0) / 1e4),
        "市盈率：%s，市净率：%s，流通市值：%s 亿"
        % (_f(q.get("pe")), _f(q.get("pb")), _f((q.get("circ_mv") or 0))),
        "均线：MA5=%s MA10=%s MA20=%s MA60=%s"
        % (_f(ind.get("ma5")), _f(ind.get("ma10")), _f(ind.get("ma20")), _f(ind.get("ma60"))),
        "MACD：DIF=%s DEA=%s 柱=%s" % (_f(ind.get("dif")), _f(ind.get("dea")), _f(ind.get("macd_hist"))),
        "RSI6=%s，KDJ(K/D)=%s/%s，BOLL 上轨=%s 下轨=%s"
        % (_f(ind.get("rsi6")), _f(ind.get("kdj_k")), _f(ind.get("kdj_d")),
           _f(ind.get("boll_up")), _f(ind.get("boll_low"))),
        "扩展指标：ATR%%=%s CCI=%s ADX=%s MFI=%s WR=%s OBV斜率=%s BIAS20=%s 波动率=%s%%"
        % (_f(ext.get("atr_pct")), _f(ext.get("cci")), _f(ext.get("adx")),
           _f(ext.get("mfi")), _f(ext.get("wr")), _f(ext.get("obv_slope")),
           _f(ext.get("bias20")), _f(ext.get("volatility"))),
        "位置：距20日高点 %s%%，20日区间位置 %s%%，连续涨跌 %s 天，相对大盘强度 %s%%"
        % (_f(ext.get("from_high20")),
           _f((ext.get("donchian") or {}).get("pos")),
           ext.get("streak", 0), _f(ext.get("rs20"))),
        "周线趋势：%s" % (_weekly_txt(ext.get("weekly"))),
        "支撑：%s，压力：%s" % (_f(stock.get("support")), _f(stock.get("pressure"))),
    ]
    if extra.get("reports"):
        rp = extra["reports"]
        lines.append("近3月研报：%d 篇，机构评级分布 %s，最高目标价 %s"
                     % (len(rp.get("items", [])), rp.get("ratings", {}), _f(rp.get("max_target"))))
    if extra.get("forecast"):
        lines.append("业绩预告：%s" % extra["forecast"].get("summary", "无"))
    if extra.get("billboard"):
        bb = extra["billboard"]
        lines.append("近30日龙虎榜：上榜 %d 次，累计净买入 %s 亿"
                     % (bb.get("count", 0), _f(bb.get("net", 0))))
    if stock.get("fund_type") == "volume_price":
        lines.append("资金：主力资金数据缺失，以量价动能替代（量比 %s）"
                     % _f((stock.get("vol_info") or {}).get("vol_ratio")))
    else:
        lines.append("资金：近5日主力净流入 %s 亿" % _f(stock.get("main_inflow")))
    if news:
        lines.append("近期消息（按可信度排序）：")
        for it in news[:8]:
            lines.append("- [%s%s] %s" % (
                it.get("source", ""),
                "·已核实" if it.get("verified") else "",
                (it.get("title", "") or "")[:60]))
    return "\n".join(lines)


def _f(x):
    if x is None:
        return "无"
    try:
        return ("%.2f" % float(x)).rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        return str(x)


def _weekly_txt(w):
    if not w:
        return "数据不足"
    parts = []
    parts.append("站上5周线" if w.get("above_ma5") else "低于5周线")
    parts.append("5周线上穿10周线" if w.get("ma5_above_ma10") else "5周线弱于10周线")
    parts.append("方向向上" if w.get("slope_up") else "方向向下")
    return "，".join(parts)


def cached_analyze(client, code, ctx, style, cache_ttl=CACHE_TTL):
    key = "%s|%s|%s" % (code, client.model, style)
    now = time.time()
    with _cache_lock:
        hit = CACHE.get(key)
        if hit and now - hit["ts"] < cache_ttl:
            data = dict(hit["data"])
            data["_cached"] = True
            return data
    data = client.analyze_stock(ctx, style)
    with _cache_lock:
        CACHE[key] = {"ts": now, "data": data}
    return data
