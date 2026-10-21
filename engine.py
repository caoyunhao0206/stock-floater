# -*- coding: utf-8 -*-
"""
技术指标 + 新闻情绪 + 多因子建议引擎
评分区间 [-100, 100]，>0 偏多，<0 偏空
"""
import math

import indicators as ext_lib

# ---------------- 情绪词典 ----------------
POS_WORDS = [
    "预增", "预盈", "续盈", "扭亏", "增长", "高增", "超预期", "中标", "签约",
    "回购", "增持", "举牌", "获批", "通过", "突破", "创新高", "新高", "战略",
    "合作", "订单", "扩产", "提价", "涨价", "分红", "送转", "首发", "亮相",
    "大单", "涨停", "大涨", "拉升", "净流入", "看好", "买入评级", "增持评级",
    "目标价", "上调", "加仓", "北向", "主力", "景气", "回暖", "复苏", "盈利",
    "新高", "量产", "投产", "并表", "扭亏为盈", "满产", "爆款", "热销",
]
NEG_WORDS = [
    "预减", "预亏", "亏损", "下滑", "下降", "减持", "质押", "平仓", "违规",
    "处罚", "问询", "立案", "诉讼", "仲裁", "跌停", "大跌", "下跌", "回调",
    "爆雷", "商誉", "减值", "退市", "风险", "下调", "卖出评级", "低于预期",
    "解禁", "冻结", "停牌", "澄清", "否认", "质疑", "曝", "踩雷", "清仓",
    "流出", "被查", "警示", "关注函", "责令", "索赔", "召回", "裁员", "诉讼",
    "立案调查", "面值退市", "ST",
]


def clamp(x, lo, hi):
    return max(lo, min(hi, x))


# ---------------- 基础指标 ----------------
def ma_series(closes, n):
    if len(closes) < n:
        return None
    return sum(closes[-n:]) / n


def macd_series(closes):
    """返回 (dif, dea, hist) 末值及前值"""
    if len(closes) < 35:
        return None
    k12, k26, k9 = 2 / 13.0, 2 / 27.0, 2 / 10.0
    e12 = e26 = closes[0]
    difs = []
    for x in closes:
        e12 = x * k12 + e12 * (1 - k12)
        e26 = x * k26 + e26 * (1 - k26)
        difs.append(e12 - e26)
    dea = difs[0]
    deas = []
    for d in difs:
        dea = d * k9 + dea * (1 - k9)
        deas.append(dea)
    dif, dea_now = difs[-1], deas[-1]
    return {
        "dif": dif, "dea": dea_now, "hist": 2 * (dif - dea_now),
        "hist_prev": 2 * (difs[-2] - deas[-2]),
        "golden": difs[-2] <= deas[-2] and dif > dea_now,
        "dead": difs[-2] >= deas[-2] and dif < dea_now,
    }


def rsi_wilder(closes, n=6):
    if len(closes) < n + 2:
        return None
    gains = losses = 0.0
    for i in range(1, n + 1):
        d = closes[i] - closes[i - 1]
        if d >= 0:
            gains += d
        else:
            losses -= d
    ag, al = gains / n, losses / n
    for i in range(n + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        g = d if d > 0 else 0.0
        l = -d if d < 0 else 0.0
        ag = (ag * (n - 1) + g) / n
        al = (al * (n - 1) + l) / n
    if al == 0:
        return 100.0
    rs = ag / al
    return 100.0 - 100.0 / (1.0 + rs)


def kdj_series(klines, n=9):
    if len(klines) < n + 2:
        return None
    k = d = 50.0
    for i in range(len(klines)):
        lo = min(x["low"] for x in klines[max(0, i - n + 1): i + 1])
        hi = max(x["high"] for x in klines[max(0, i - n + 1): i + 1])
        c = klines[i]["close"]
        rsv = 50.0 if hi == lo else (c - lo) / (hi - lo) * 100.0
        k = 2.0 / 3.0 * k + 1.0 / 3.0 * rsv
        d = 2.0 / 3.0 * d + 1.0 / 3.0 * k
    return {"k": k, "d": d, "j": 3 * k - 2 * d}


def boll(closes, n=20, k=2.0):
    if len(closes) < n:
        return None
    seg = closes[-n:]
    mid = sum(seg) / n
    var = sum((x - mid) ** 2 for x in seg) / n
    std = math.sqrt(var)
    return {"up": mid + k * std, "mid": mid, "low": mid - k * std, "std": std}


def compute_indicators(kl):
    """kl: kline list。返回指标 dict"""
    closes = [x["close"] for x in kl]
    ind = {
        "ma5": ma_series(closes, 5),
        "ma10": ma_series(closes, 10),
        "ma20": ma_series(closes, 20),
        "ma60": ma_series(closes, 60),
        "macd": macd_series(closes),
        "rsi6": rsi_wilder(closes, 6),
        "rsi12": rsi_wilder(closes, 12),
        "kdj": kdj_series(kl),
        "boll": boll(closes),
        "closes": closes[-30:],
    }
    return ind


# ---------------- 支撑压力 ----------------
def support_resistance(kl, ind, price):
    recent = kl[-20:] if len(kl) >= 20 else kl
    lo20 = min(x["low"] for x in recent)
    hi20 = max(x["high"] for x in recent)
    b = ind.get("boll") or {}
    ma60 = ind.get("ma60")
    supports = sorted({round(x, 2) for x in [lo20, b.get("low"), ma60] if x and x < price}, reverse=True)
    pressures = sorted({round(x, 2) for x in [hi20, b.get("up"), ma60] if x and x > price})
    support = supports[0] if supports else round(price * 0.95, 2)
    pressure = pressures[0] if pressures else round(price * 1.05, 2)
    return {"support": support, "pressure": pressure, "low20": round(lo20, 2), "high20": round(hi20, 2)}


# ---------------- 评分 ----------------
def tech_score(quote, ind):
    price = quote.get("price") or 0
    s = 0.0
    ma5, ma10, ma20, ma60 = ind.get("ma5"), ind.get("ma10"), ind.get("ma20"), ind.get("ma60")
    if None not in (ma5, ma10, ma20):
        if ma5 > ma10 > ma20:
            s += 14
        elif ma5 < ma10 < ma20:
            s -= 14
    if ma20 and price:
        s += clamp((price / ma20 - 1.0) * 260, -18, 18)
    if ma60 and ma20:
        s += 8 if ma20 > ma60 else -8

    m = ind.get("macd")
    if m:
        if m["hist"] > 0 and m["hist"] > m["hist_prev"]:
            s += 18
        elif m["hist"] > 0:
            s += 6
        elif m["hist"] < 0 and m["hist"] < m["hist_prev"]:
            s -= 18
        else:
            s -= 6
        if m["golden"]:
            s += 8
        if m["dead"]:
            s -= 8

    rsi6 = ind.get("rsi6")
    if rsi6 is not None:
        if rsi6 < 20:
            s += 22
        elif rsi6 < 30:
            s += 14
        elif rsi6 < 45:
            s += 4
        elif rsi6 <= 55:
            s += 0
        elif rsi6 <= 70:
            s -= 4
        elif rsi6 <= 80:
            s -= 14
        else:
            s -= 22

    kd = ind.get("kdj")
    if kd:
        if kd["k"] > kd["d"] and kd["k"] < 25:
            s += 8
        elif kd["k"] < kd["d"] and kd["k"] > 75:
            s -= 8

    b = ind.get("boll")
    if b and b["up"] > b["low"]:
        pct = (price - b["low"]) / (b["up"] - b["low"])
        if pct < 0:
            s += 16
        elif pct < 0.2:
            s += 8
        elif pct > 1:
            s -= 16
        elif pct > 0.8:
            s -= 8

    chg = quote.get("change_pct") or 0
    vr = quote.get("volume_ratio") or 1
    if vr > 2 and chg > 0:
        s += 5
    elif vr > 2 and chg < 0:
        s -= 5
    return clamp(s, -100, 100)


def news_sentiment(items):
    """返回 (score[-50,50], positives, negatives)"""
    score = 0.0
    pos, neg = [], []
    for i, it in enumerate(items):
        t = it.get("title", "")
        s = 0
        for w in POS_WORDS:
            if w in t:
                s += 1
        for w in NEG_WORDS:
            if w in t:
                s -= 1
        if s > 0:
            pos.append(t)
        elif s < 0:
            neg.append(t)
        w = it.get("weight", 1.0) or 1.0
        score += s * w / (1.0 + 0.18 * i)
    return clamp(score * 12.0, -50, 50), pos[:3], neg[:3]


def fund_score(flows, quote, kl=None):
    """资金面评分：优先东财主力资金流，失败则降级为量价动能（仅用日K量价）"""
    if flows:
        total_main = sum(f["main"] for f in flows)
        circ = quote.get("circ_mv") or 0
        if circ > 0:
            return clamp(total_main / (circ * 1e8) * 400.0, -25, 25)
        return clamp(total_main / 1e8 * 4.0, -25, 25)
    if kl and len(kl) >= 20:
        vols = [x["volume"] for x in kl]
        closes = [x["close"] for x in kl]
        v5 = sum(vols[-5:]) / 5.0
        v20 = sum(vols[-20:]) / 20.0
        vr = (v5 / v20) if v20 else 1.0
        lo20 = min(closes[-20:])
        hi20 = max(closes[-20:])
        pos = (closes[-1] - lo20) / (hi20 - lo20) if hi20 > lo20 else 0.5
        chg = quote.get("change_pct") or 0
        s = clamp((vr - 1.0) * 10.0, -10.0, 10.0)
        if chg > 0:
            s += clamp(chg * 2.0, 0, 8)
        else:
            s -= clamp(-chg * 2.0, 0, 8)
        s += (pos - 0.5) * 16.0
        return clamp(s, -25, 25)
    return 0.0



# ================= V2 六维评分（0~100） =================
GRADE_LABEL = {"A+": "A+ 强", "A": "A 优", "B+": "B+ 良", "B": "B 中", "C": "C 弱", "D": "D 差"}


def score_v2(quote, ind, ext, flows, senti, extras, fund_val):
    """趋势25 动能20 量能20 资金15 消息10 估值10 -> 0~100"""
    price = quote.get("price") or 0
    ma5, ma10, ma20, ma60 = ind.get("ma5"), ind.get("ma10"), ind.get("ma20"), ind.get("ma60")
    dims = {}

    # ---- 趋势 25 ----
    t = 0.0
    if ma5 and ma10 and ma20 and ma5 > ma10 > ma20:
        t += 6
    elif ma5 and ma10 and ma5 > ma10:
        t += 3
    if ma20 and ma60 and ma20 > ma60:
        t += 4
    if ma20 and price > ma20:
        t += 4
    if ma60 and price > ma60:
        t += 2
    adx = ext.get("adx")
    if adx is not None:
        t += 4 if adx >= 25 else (2 if adx >= 18 else 0)
    w = ext.get("weekly") or {}
    t += (2 if w.get("above_ma5") else 0) + (2 if w.get("ma5_above_ma10") else 0) + (1 if w.get("slope_up") else 0)
    dims["trend"] = round(min(25, t), 1)

    # ---- 动能 20 ----
    m = 0.0
    md = ind.get("macd") or {}
    if md.get("hist", 0) > 0:
        m += 5
        if md.get("hist", 0) > md.get("hist_prev", 0):
            m += 3
    if md.get("golden"):
        m += 2
    rsi = ind.get("rsi6")
    if rsi is not None:
        if 45 <= rsi <= 65:
            m += 4
        elif 30 <= rsi < 45:
            m += 3
        elif 20 <= rsi < 30:
            m += 2
        elif 65 < rsi <= 75:
            m += 1
        elif rsi > 78:
            m -= 3
    roc = ext.get("roc12")
    if roc is not None:
        m += 3 if roc > 0 else 0
        m += 2 if roc > 5 else 0
    cci = ext.get("cci")
    if cci is not None:
        if 0 <= cci <= 100:
            m += 3
        elif cci < -100:
            m += 1
        elif cci > 180:
            m -= 1
    dims["momentum"] = round(max(0, min(20, m)), 1)

    # ---- 量能 20 ----
    v = 0.0
    vr = quote.get("volume_ratio")
    if vr:
        if 1.2 <= vr <= 3:
            v += 4
        elif 3 < vr <= 5:
            v += 2
        elif vr > 5:
            v += 1
    to = quote.get("turnover") or 0
    if 2 <= to <= 12:
        v += 4
    elif 1 <= to < 2 or 12 < to <= 20:
        v += 2
    obv = ext.get("obv_slope")
    if obv is not None:
        v += 4 if obv > 0 else 0
        v += 2 if obv > 20 else 0
    mfi = ext.get("mfi")
    if mfi is not None:
        if 50 <= mfi <= 80:
            v += 3
        elif 20 <= mfi < 50:
            v += 2
        elif mfi < 20:
            v += 1
    fh = ext.get("from_high20")
    if fh is not None and fh > -1.5 and vr and vr > 1.3:
        v += 3
    dims["volume"] = round(min(20, v), 1)

    # ---- 资金 15 ----
    dims["fund"] = round(clamp((fund_val + 25) / 50 * 15, 0, 15), 1)

    # ---- 消息 10 ----
    n = clamp((senti + 50) / 100 * 6, 0, 6)
    rp = (extras or {}).get("reports") or {}
    ratings = rp.get("ratings") or {}
    good = sum(v2 for k, v2 in ratings.items() if ("买入" in k or "增持" in k or "推荐" in k))
    bad = sum(v2 for k, v2 in ratings.items() if ("减持" in k or "卖出" in k))
    if good >= 3:
        n += 2.5
    elif good >= 1:
        n += 1.5
    if bad:
        n -= 2
    fc = (extras or {}).get("forecast") or {}
    if fc.get("type") == "pos":
        n += 2
    elif fc.get("type") == "neg":
        n -= 2
    bb = (extras or {}).get("billboard") or {}
    if bb.get("net", 0) > 0.3:
        n += 1
    elif bb.get("net", 0) < -0.3:
        n -= 1
    dims["news"] = round(clamp(n, 0, 10), 1)

    # ---- 估值 10 ----
    e = 0.0
    pe, pb = quote.get("pe"), quote.get("pb")
    if pe and pe > 0:
        if pe <= 20:
            e += 7
        elif pe <= 35:
            e += 5.5
        elif pe <= 50:
            e += 3.5
        elif pe <= 80:
            e += 2
        else:
            e += 1
    else:
        e += 1
    if pb and 0 < pb <= 2:
        e += 2
    elif pb and pb <= 4:
        e += 1.3
    elif pb and pb <= 6:
        e += 0.6
    tp = rp.get("avg_target")
    if tp and price and tp / price - 1 > 0.1:
        e += 1
    dims["value"] = round(min(10, e), 1)

    total = sum(dims.values())

    # ---- 形态加减分 ----
    pats = ext.get("patterns") or []
    bull = sum(1 for x in pats if x.get("dir") == 1)
    bear = sum(1 for x in pats if x.get("dir") == -1)
    if bull:
        total += min(6, bull * 2.5)
    if bear:
        total -= min(8, bear * 3)

    # ---- 风险扣分 ----
    penalties = []
    vol = ext.get("volatility")
    if vol and vol > 70:
        total -= 5
        penalties.append("波动率 %.0f%% 偏高" % vol)
    if fh is not None and fh > -1.5 and rsi and rsi > 78:
        total -= 6
        penalties.append("短线过热且贴近20日高点，追高风险")
    if fc.get("type") == "neg":
        total -= 8
        penalties.append("业绩预告偏负面")
    if bb.get("net", 0) < -1:
        total -= 3
        penalties.append("近30日龙虎榜净卖出")
    if ext.get("streak", 0) >= 5:
        total -= 3
        penalties.append("连续上涨 %d 天，短期透支" % ext.get("streak"))
    if ext.get("vp_div") == -1:
        total -= 4
        penalties.append("量价背离（价新高量萎缩）")
    for x in pats:
        if x.get("dir") == -1:
            penalties.append("形态：" + x.get("desc", x.get("name", "")))

    total = clamp(total, 0, 100)
    if total >= 80:
        grade = "A+"
    elif total >= 72:
        grade = "A"
    elif total >= 62:
        grade = "B+"
    elif total >= 52:
        grade = "B"
    elif total >= 42:
        grade = "C"
    else:
        grade = "D"
    return {"total": round(total, 1), "grade": grade, "dims": dims,
            "penalties": penalties}

# ---------------- 综合建议 ----------------
ACTION_LABEL = {
    "buy": "买入", "add": "加仓", "hold": "持有",
    "reduce": "减仓", "sell": "卖出", "watch": "观望",
}


def build_advice(quote, kl, flows, news_items, cost=0.0, shares=0,
                 extras=None, index_closes=None):
    ind = compute_indicators(kl)
    ext = ext_lib.compute_all(kl, quote, index_closes)
    price = quote.get("price") or 0
    ts_ = tech_score(quote, ind)
    senti, pos, neg = news_sentiment(news_items)
    fs = fund_score(flows, quote, kl)
    sc = score_v2(quote, ind, ext, flows, senti, extras or {}, fs)
    total = sc["total"] * 2 - 100          # 0~100 映射到 -100~100

    holding = bool(cost and cost > 0 and shares and shares > 0)
    sr = support_resistance(kl, ind, price)
    fund_type = "money_flow" if flows else ("volume_price" if len(kl) >= 20 else "none")
    vol_info = {}
    if fund_type == "volume_price":
        vols = [x["volume"] for x in kl]
        v5 = sum(vols[-5:]) / 5.0
        v20 = sum(vols[-20:]) / 20.0
        vol_info = {
            "vol_ratio": round(v5 / v20, 2) if v20 else None,
            "pos20": round((kl[-1]["close"] - min(x["close"] for x in kl[-20:])) /
                           (max(x["close"] for x in kl[-20:]) -
                            min(x["close"] for x in kl[-20:])) * 100, 1),
        }

    if total >= 50:
        action = "add" if holding else "buy"
    elif total >= 20:
        action = "hold"
    elif total > -20:
        action = "hold" if holding else "watch"
    elif total > -50:
        action = "reduce" if holding else "watch"
    else:
        action = "sell" if holding else "watch"

    reasons, warnings = [], []
    ma5, ma10, ma20, ma60 = ind.get("ma5"), ind.get("ma10"), ind.get("ma20"), ind.get("ma60")
    if ma5 and ma10 and ma20 and ma5 > ma10 > ma20:
        reasons.append("短期均线多头排列（MA5>MA10>MA20）")
    if ma5 and ma10 and ma20 and ma5 < ma10 < ma20:
        warnings.append("短期均线空头排列（MA5<MA10<MA20）")
    m = ind.get("macd")
    if m:
        if m["hist"] > 0 and m["hist"] > m["hist_prev"]:
            reasons.append("MACD 红柱放大，动能走强")
        elif m["golden"]:
            reasons.append("MACD 金叉")
        if m["hist"] < 0 and m["hist"] < m["hist_prev"]:
            warnings.append("MACD 绿柱放大，动能走弱")
        elif m["dead"]:
            warnings.append("MACD 死叉")
    rsi6 = ind.get("rsi6")
    if rsi6 is not None:
        if rsi6 < 30:
            reasons.append("RSI6=%.0f 处于超卖区，反弹概率增大" % rsi6)
        elif rsi6 > 70:
            warnings.append("RSI6=%.0f 处于超买区，注意回调" % rsi6)
    b = ind.get("boll")
    if b and price:
        pct = (price - b["low"]) / (b["up"] - b["low"]) if b["up"] > b["low"] else 0.5
        if pct < 0.2:
            reasons.append("股价回落至布林带下轨附近")
        elif pct > 0.8:
            warnings.append("股价触及布林带上轨，追高风险")
    kd = ind.get("kdj")
    if kd and kd["k"] < 20 and kd["k"] > kd["d"]:
        reasons.append("KDJ 低位金叉")
    if kd and kd["k"] > 80 and kd["k"] < kd["d"]:
        warnings.append("KDJ 高位死叉")
    if flows:
        total_main = sum(f["main"] for f in flows)
        if total_main > 0:
            reasons.append("近5日主力资金净流入 %.1f 亿" % (total_main / 1e8))
        elif total_main < 0:
            warnings.append("近5日主力资金净流出 %.1f 亿" % (-total_main / 1e8))
    elif fund_type == "volume_price" and vol_info:
        vr = vol_info.get("vol_ratio")
        p20 = vol_info.get("pos20")
        if vr is not None and vr >= 1.3:
            reasons.append("5日均量为20日 %.1f 倍，放量走强" % vr)
        elif vr is not None and vr <= 0.75:
            warnings.append("5日均量仅为20日 %.1f 倍，缩量观望" % vr)
        if p20 is not None and p20 >= 80:
            reasons.append("股价处于近20日高位区（%s%%）" % p20)
        elif p20 is not None and p20 <= 20:
            warnings.append("股价处于近20日低位区（%s%%）" % p20)
    if senti >= 20:
        reasons.append("新闻面偏暖（%d 条利好）" % len(pos))
    elif senti <= -20:
        warnings.append("新闻面偏冷（%d 条利空）" % len(neg))

    # 价格位
    support, pressure = sr["support"], sr["pressure"]
    buy_zone = stop_loss = take_profit = None
    if holding:
        stop_loss = round(min(support * 0.995, cost * 0.93), 2)
        take_profit = round(max(pressure * 1.01, cost * 1.15), 2)
        if action in ("buy", "add", "hold") and total >= 0:
            buy_zone = (round(support * 0.98, 2), round(support * 1.02, 2))
    else:
        take_profit = round(pressure * 1.05, 2)
        if action == "buy":
            buy_zone = (round(support * 0.97, 2), round(support * 1.03, 2))

    return {
        "action": action,
        "action_label": ACTION_LABEL[action],
        "score": round(total, 1),
        "score100": sc["total"],
        "grade": sc["grade"],
        "grade_label": GRADE_LABEL[sc["grade"]],
        "dims": sc["dims"],
        "penalties": sc["penalties"],
        "ext": ext,
        "tech_score": round(ts_, 1),
        "sentiment": round(senti, 1),
        "fund_score": round(fs, 1),
        "holding": holding,
        "support": support,
        "pressure": pressure,
        "buy_zone": buy_zone,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "fund_type": fund_type,
        "vol_info": vol_info,
        "reasons": reasons[:5],
        "warnings": warnings[:5],
        "news_pos": pos[:3],
        "news_neg": neg[:3],
        "indicators": {
            "ma5": _r(ind.get("ma5")), "ma10": _r(ind.get("ma10")),
            "ma20": _r(ind.get("ma20")), "ma60": _r(ind.get("ma60")),
            "dif": _r((ind.get("macd") or {}).get("dif")),
            "dea": _r((ind.get("macd") or {}).get("dea")),
            "rsi6": _r(ind.get("rsi6")),
            "kdj_k": _r((ind.get("kdj") or {}).get("k")),
            "kdj_d": _r((ind.get("kdj") or {}).get("d")),
            "boll_up": _r((ind.get("boll") or {}).get("up")),
            "boll_low": _r((ind.get("boll") or {}).get("low")),
        },
        "closes": [round(c, 2) for c in ind.get("closes") or []],
    }


def _r(x, n=2):
    return round(x, n) if isinstance(x, (int, float)) else None


# ---------------- 信号生成 ----------------
def build_signals(code, name, advice, quote, last_action):
    """对比上一次建议与关键价位，生成待推送信号列表"""
    sigs = []
    price = quote.get("price") or 0
    chg = quote.get("change_pct") or 0
    action = advice["action"]

    if action in ("buy", "add", "sell", "reduce") and action != last_action:
        level = "强" if abs(advice["score"]) >= 50 else ""
        sigs.append({
            "type": "action_" + action,
            "title": "【%s%s】%s(%s)" % (level, advice["action_label"], name, code),
            "body": "现价 %.2f（%+.2f%%） 综合评分 %.0f\n%s" % (
                price, chg, advice["score"],
                "；".join((advice["reasons"] or advice["warnings"])[:3]) or "无",
            ),
        })

    if advice.get("stop_loss") and price and price <= advice["stop_loss"] * 1.004:
        sigs.append({
            "type": "stop_loss",
            "title": "【触及止损】%s(%s)" % (name, code),
            "body": "现价 %.2f 已跌破止损线 %.2f，如纪律操作可考虑卖出" % (price, advice["stop_loss"]),
        })

    if advice.get("take_profit") and price and price >= advice["take_profit"] * 0.996:
        sigs.append({
            "type": "take_profit",
            "title": "【触及止盈】%s(%s)" % (name, code),
            "body": "现价 %.2f 已到达止盈线 %.2f，可分批落袋为安" % (price, advice["take_profit"]),
        })

    if advice.get("buy_zone") and action in ("buy", "add"):
        lo, hi = advice["buy_zone"]
        if lo <= price <= hi:
            sigs.append({
                "type": "buy_zone",
                "title": "【进入买入区间】%s(%s)" % (name, code),
                "body": "现价 %.2f 位于买入区间 %.2f~%.2f，评分 %.0f，可关注" % (price, lo, hi, advice["score"]),
            })

    if abs(chg) >= 5:
        sigs.append({
            "type": "wave",
            "title": "【异动提醒】%s(%s)" % (name, code),
            "body": "现价 %.2f，当前%s %.2f%%，请留意盘口变化" % (
                price, "上涨" if chg > 0 else "下跌", chg),
        })

    rsi6 = advice["indicators"].get("rsi6")
    if rsi6 is not None and rsi6 <= 20:
        sigs.append({"type": "oversold", "title": "【超卖信号】%s(%s)" % (name, code),
                     "body": "RSI6=%.0f，短期或存反弹机会" % rsi6})
    elif rsi6 is not None and rsi6 >= 80:
        sigs.append({"type": "overbought", "title": "【超买信号】%s(%s)" % (name, code),
                     "body": "RSI6=%.0f，注意回调风险" % rsi6})

    if advice.get("sentiment", 0) <= -25:
        sigs.append({
            "type": "bad_news",
            "title": "【利空消息】%s(%s)" % (name, code),
            "body": "；".join(advice["news_neg"][:2]) or "新闻情绪偏冷",
        })
    return sigs
