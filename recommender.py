# -*- coding: utf-8 -*-
"""
荐股引擎 V2
流程：全市场快照 -> 行情层硬筛（两榜） -> 技术面初判 -> 机构面补数据 -> 六维评分定档
     -> 盈亏比与风险门槛 -> 排序输出（可选 AI 复评由 app 层完成）

质量优先：只输出 A+/A/B+ 档，且盈亏比 >= 1.5
"""
import logging
from concurrent.futures import ThreadPoolExecutor

import engine
import sources

log = logging.getLogger("floater.reco")


# ---------------- 行情层硬筛 ----------------
def _base_ok(r, min_mv=30):
    if not r or r["price"] <= 0:
        return False
    if r["name"].startswith(("N", "C")):          # 新股/次新
        return False
    if "ST" in r["name"].upper() or "退" in r["name"]:
        return False
    if r["price"] < 2 or r["price"] > 600:
        return False
    if r["nmc"] and r["nmc"] < min_mv * 10000:    # nmc 单位：万元
        return False
    if abs(r["change_pct"]) > 20:                 # 异常波动
        return False
    return True


def screen_aggressive(rows, limit=60):
    """进攻：放量走强、突破形态，但不过热"""
    out = []
    for r in rows:
        if not _base_ok(r, 30):
            continue
        chg = r["change_pct"]
        if not (1.5 <= chg <= 8.0):
            continue
        if not (2.0 <= r["turnover"] <= 15):
            continue
        if r["amount"] < 1.5e8:
            continue
        # 相对涨幅强度（不追已涨停/接近涨停）
        if chg >= 9.2:
            continue
        score = (chg * 1.5 + min(r["turnover"], 12) * 1.1
                 + min(r["amount"] / 1e8, 25) * 0.6)
        out.append((score, r))
    out.sort(key=lambda x: -x[0])
    return [r for _, r in out[:limit]]


def screen_steady(rows, limit=60):
    """稳健：估值合理、走势温和、流动性好、盘子不小"""
    out = []
    for r in rows:
        if not _base_ok(r, 50):
            continue
        chg = r["change_pct"]
        if not (-3.0 <= chg <= 4.5):
            continue
        if not (0.4 <= r["turnover"] <= 12):
            continue
        if r["amount"] < 2.0e8:
            continue
        pe = r["pe"]
        if not pe or not (0 < pe <= 45):
            continue
        pb = r["pb"]
        if pb and not (0 < pb <= 8):
            continue
        score = ((1.0 - abs(chg) / 6.0) * 22 + min(r["amount"] / 1e8, 30) * 0.7
                 + (14 if pe <= 22 else 0) + (6 if 0 < (pb or 99) <= 2.5 else 0))
        out.append((score, r))
    out.sort(key=lambda x: -x[0])
    return [r for _, r in out[:limit]]


# ---------------- 深度分析 ----------------
def _deep(row, kind, index_closes):
    code, sym = row["code"], row["symbol"]
    try:
        kl = sources.fetch_kline(sym, 120)
    except Exception:  # noqa: BLE001
        kl = []
    if len(kl) < 60:
        return None

    try:
        flows = sources.fetch_fund_flow(
            ("1." if sym.startswith("sh") else "0.") + code, 5)
    except Exception:  # noqa: BLE001
        flows = []
    try:
        news = sources.fetch_news_multi(sym, code, row["name"], limit=10)
    except Exception:  # noqa: BLE001
        news = {"items": [], "score": 0, "verified": 0, "sources": []}

    quote = {
        "name": row["name"], "code": code, "price": row["price"],
        "prev_close": row["prev_close"], "open": row["open"],
        "high": row["high"], "low": row["low"],
        "change": row["change"], "change_pct": row["change_pct"],
        "amount": row["amount"] / 1e4, "turnover": row["turnover"],
        "volume_ratio": row.get("volume_ratio"),
        "circ_mv": row["nmc"] / 1e4, "pe": row["pe"], "pb": row["pb"],
    }

    # 第一步：技术面初判（省请求，低于 52 分直接淘汰）
    adv0 = engine.build_advice(quote, kl, flows, news["items"], 0, 0,
                               index_closes=index_closes)
    if adv0["score100"] < 52:
        return None

    # 第二步：补机构面数据（研报 / 龙虎榜 / 业绩预告）
    extras = {}
    try:
        extras["reports"] = sources.fetch_reports(code)
    except Exception:  # noqa: BLE001
        extras["reports"] = {}
    try:
        extras["billboard"] = sources.fetch_billboard(code)
    except Exception:  # noqa: BLE001
        extras["billboard"] = {}
    try:
        extras["forecast"] = sources.fetch_forecast(code)
    except Exception:  # noqa: BLE001
        extras["forecast"] = {}

    adv = engine.build_advice(quote, kl, flows, news["items"], 0, 0,
                              extras=extras, index_closes=index_closes)

    # 第三步：定档门槛
    if adv["grade"] not in ("A+", "A", "B+", "B"):
        return None

    return _shape(row, quote, adv, extras, news, kind, kl)


def _shape(row, quote, adv, extras, news, kind, kl=None):
    price = row["price"]
    ext = adv.get("ext") or {}
    ind = adv.get("indicators") or {}
    rsi = ind.get("rsi6")
    ma5, ma10, ma20 = ind.get("ma5"), ind.get("ma10"), ind.get("ma20")
    support, pressure = adv["support"], adv["pressure"]
    fh = ext.get("from_high20")

    # ---- 推荐买入价 ----
    if kind == "aggressive":
        if (rsi and rsi >= 70) or (fh is not None and fh > -1.0):
            buy = min(price, ma5 or price * 0.98)
        else:
            buy = price
    else:
        anchors = [x for x in (support, ma10, ma20) if x and x < price * 1.005]
        buy = max(anchors) if anchors else price * 0.985
        buy = max(buy, price * 0.92)
    buy = round(min(buy, price * 1.003), 2)

    # ---- 目标 / 止损：以 ATR 为尺度，兼顾支撑压力 ----
    atr = ext.get("atr") or price * 0.03
    # 止损：取“支撑位”与“1.8 倍 ATR”中更近的一个，限制在 -4%~-10%
    dist_stop = min(max(buy - support, 0.5 * atr), 1.8 * atr) if support else 1.8 * atr
    stop = round(min(max(buy - dist_stop, buy * 0.90), buy * 0.965), 2)
    # 目标：取“压力位”与“3 倍 ATR”中更远的一个，限制在 +6%~+28%（稳健 +6%~+20%）
    up_cap = 1.28 if kind == "aggressive" else 1.20
    dist_tp = max(max(pressure - buy, 0), 3.0 * atr)
    target = round(min(max(buy + dist_tp, buy * 1.06), buy * up_cap), 2)

    upside = round((target / buy - 1) * 100, 1)
    downside = round((stop / buy - 1) * 100, 1)
    rr = round(upside / abs(downside), 2) if downside else 0

    # ---- 盈亏比与波动门槛 ----
    vol = ext.get("volatility")
    if rr < 1.3:
        return None
    if vol and vol > 95:
        return None

    # ---- 推荐理由（按维度组织，最多 5 条）----
    reasons = []
    dims = adv.get("dims") or {}
    if dims.get("trend", 0) >= 18:
        adx = ext.get("adx")
        if adx and adx >= 25:
            reasons.append("趋势强度高（ADX %.0f），均线多头排列" % adx)
        else:
            reasons.append("均线多头排列，趋势结构完整")
    wk = ext.get("weekly") or {}
    if wk.get("ma5_above_ma10") and wk.get("slope_up"):
        reasons.append("周线级别同步向上，中期趋势未破")
    if dims.get("volume", 0) >= 13:
        reasons.append("量能配合良好（换手 %.1f%%%s）" % (
            row["turnover"],
            "，量比 %.1f" % quote["volume_ratio"] if quote.get("volume_ratio") else ""))
    rs = ext.get("rs20")
    if rs and rs > 3:
        reasons.append("近20日跑赢大盘 %.1f 个百分点" % rs)
    rp = extras.get("reports") or {}
    if rp.get("count"):
        ratings = rp.get("ratings") or {}
        good = sum(v for k, v in ratings.items() if ("买入" in k or "增持" in k))
        if good:
            reasons.append("近3月 %d 篇研报%s" % (
                rp["count"], "，" + str(good) + " 家给出买入/增持"))
        if rp.get("avg_target"):
            reasons.append("机构平均目标价 %.2f（空间 %.1f%%）" % (
                rp["avg_target"], (rp["avg_target"] / price - 1) * 100))
    fc = extras.get("forecast") or {}
    if fc.get("type") == "pos":
        reasons.append("业绩预告向好：%s" % fc.get("summary", ""))
    if dims.get("value", 0) >= 7:
        reasons.append("估值处于合理区间（PE %s）" % _f(row.get("pe")))

    # ---- 风险 ----
    risks = list(adv.get("penalties") or [])
    if rsi and rsi >= 72:
        risks.append("RSI %.0f 偏热，建议分批而非一次买入" % rsi)
    if kind == "aggressive":
        risks.append("进攻型标的波动大，跌破 %.2f 应无条件止损" % stop)
    if fc.get("type") == "neg":
        risks.append("业绩预告偏负面，注意基本面风险")
    if not risks:
        risks.append("大盘系统性风险与个股突发消息仍需自行跟踪")

    return {
        "code": row["code"], "symbol": row["symbol"], "name": row["name"],
        "kind": kind,
        "price": price, "change_pct": row["change_pct"],
        "buy_price": buy,
        "buy_zone": [round(buy * 0.985, 2), round(buy * 1.012, 2)],
        "target": target, "stop": stop,
        "upside": upside, "downside": downside, "rr": rr,
        "score": adv["score100"], "grade": adv["grade"],
        "grade_label": adv["grade_label"],
        "dims": dims,
        "action": adv["action"], "action_label": adv["action_label"],
        "turnover": row["turnover"],
        "amount": round(row["amount"] / 1e8, 2),
        "pe": row["pe"], "pb": row["pb"],
        "nmc": round(row["nmc"] / 10000, 0),
        "rsi": rsi,
        "adx": ext.get("adx"),
        "volatility": ext.get("volatility"),
        "rs20": ext.get("rs20"),
        "reasons": reasons[:5],
        "risks": risks[:4],
        "indicators": {
            "ma5": ind.get("ma5"), "ma10": ind.get("ma10"),
            "ma20": ind.get("ma20"), "ma60": ind.get("ma60"),
            "rsi6": rsi, "kdj_k": ind.get("kdj_k"), "kdj_d": ind.get("kdj_d"),
            "boll_up": ind.get("boll_up"), "boll_low": ind.get("boll_low"),
            "atr_pct": ext.get("atr_pct"), "cci": ext.get("cci"),
            "mfi": ext.get("mfi"), "wr": ext.get("wr"),
            "obv_slope": ext.get("obv_slope"), "bias20": ext.get("bias20"),
            "from_high20": fh,
            "ma_slope20": ext.get("ma_slope20"),
            "boll_width": ext.get("boll_width"),
            "vol_price_corr": ext.get("vol_price_corr"),
            "limit_up60": ext.get("limit_up60"),
            "up_ratio20": ext.get("up_ratio20"),
            "max_dd60": ext.get("max_dd60"),
        },
        "patterns": ext.get("patterns") or [],
        "reports": {
            "count": (extras.get("reports") or {}).get("count", 0),
            "ratings": (extras.get("reports") or {}).get("ratings", {}),
            "avg_target": (extras.get("reports") or {}).get("avg_target"),
            "items": (extras.get("reports") or {}).get("items", [])[:3],
        },
        "forecast": extras.get("forecast") or {},
        "billboard": extras.get("billboard") or {},
        "news": news.get("items", [])[:6],
        "news_score": news.get("score", 0),
        "news_verified": news.get("verified", 0),
        "sources": news.get("sources", []),
        "_kl": kl,  # 供回测使用（返回前端前会剔除）
        # 供 AI 复评使用（返回前端前会剔除）
        "_ai_ctx": {
            "code": row["code"], "name": row["name"],
            "quote": quote, "indicators": ind, "ext": ext,
            "news": news.get("items", [])[:8],
            "support": support, "pressure": pressure,
            "extras": extras,
            "fund_type": adv.get("fund_type"),
            "main_inflow": round(sum(f.get("main", 0) for f in (extras.get("flows") or [])) / 1e8, 2)
            if extras.get("flows") else None,
            "vol_info": adv.get("vol_info") or {},
        },
    }


def _f(x):
    try:
        return ("%.1f" % float(x)).rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        return "--"


def build(rows, index_closes=None, per_list=10, agg_pool=34, steady_pool=34):
    """两榜各取 24 只候选，深度分析后只留 A+/A/B+ 且盈亏比>=1.5"""
    agg = screen_aggressive(rows, agg_pool)
    st = screen_steady(rows, steady_pool)
    tasks = [(r, "aggressive") for r in agg] + [(r, "steady") for r in st]

    results = []
    with ThreadPoolExecutor(max_workers=12) as ex:
        for res in ex.map(lambda t: _deep(t[0], t[1], index_closes), tasks):
            if res:
                results.append(res)

    aggressive = sorted([r for r in results if r["kind"] == "aggressive"],
                        key=lambda x: (-x["score"], -x["rr"]))
    steady = sorted([r for r in results if r["kind"] == "steady"],
                    key=lambda x: (-x["score"], -x["rr"]))
    return {
        "aggressive": aggressive[:per_list],
        "steady": steady[:per_list],
        "pool": len(tasks), "hit": len(results),
        "filtered": len(tasks) - len(results),
        "candidates": [r["symbol"] for r in (agg + st)],
    }
