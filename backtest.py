# -*- coding: utf-8 -*-
"""
策略回测
1. 单股信号回溯：遍历历史每个交易日，用简化六维评分判断“当时是否会被推荐”，
   统计之后 5 日 / 10 日的收益与胜率
2. 策略汇总：把所有推荐股的样本合并，得到整体胜率与平均收益
"""
import math


def _mean(vals):
    return sum(vals) / len(vals) if vals else 0.0


def _ema_last(vals, n):
    if not vals:
        return 0.0
    k = 2.0 / (n + 1)
    e = vals[0]
    for v in vals[1:]:
        e = v * k + e * (1 - k)
    return e


def _rsi(closes, n=6):
    if len(closes) < n + 1:
        return 50.0
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
    return 100 - 100 / (1 + ag / al)


def quick_score(kl):
    """
    简化版六维评分（只依赖日K，用于历史回溯）
    与实盘评分的口径保持一致的核心项：趋势 35 / 动能 25 / 量能 25 / 位置 15
    """
    if len(kl) < 65:
        return 0.0
    closes = [x["close"] for x in kl]
    vols = [x["volume"] for x in kl]
    price = closes[-1]
    ma5 = _mean(closes[-5:])
    ma10 = _mean(closes[-10:])
    ma20 = _mean(closes[-20:])
    ma60 = _mean(closes[-60:])

    s = 0.0
    # 趋势 35
    if ma5 > ma10 > ma20:
        s += 12
    elif ma5 > ma20:
        s += 6
    if ma20 > ma60:
        s += 8
    if price > ma20:
        s += 7
    if price > ma60:
        s += 4
    slope = (ma20 / _mean(closes[-25:-5]) - 1) * 100 if _mean(closes[-25:-5]) else 0
    if slope > 0.5:
        s += 4

    # 动能 25
    dif = _ema_last(closes, 12) - _ema_last(closes, 26)
    dea = _ema_last([_ema_last(closes[:i + 1], 12) - _ema_last(closes[:i + 1], 26)
                     for i in range(35, len(closes))], 9)
    if dif > dea:
        s += 8
    rsi = _rsi(closes, 6)
    if 45 <= rsi <= 68:
        s += 8
    elif 30 <= rsi < 45:
        s += 5
    elif rsi > 78:
        s -= 3
    if len(closes) >= 13:
        roc = (price / closes[-13] - 1) * 100
        s += 5 if roc > 0 else 0
        s += 4 if roc > 6 else 0

    # 量能 25
    v5, v20 = _mean(vols[-5:]), _mean(vols[-20:])
    vr = v5 / v20 if v20 else 1.0
    if 1.1 <= vr <= 2.8:
        s += 10
    elif 2.8 < vr <= 4.5:
        s += 5
    if price > ma20 and vr > 1.2:
        s += 8
    if price >= max(x["high"] for x in kl[-20:]) * 0.995:
        s += 7

    # 位置 15
    hi = max(closes[-20:])
    lo = min(closes[-20:])
    pos = (price - lo) / (hi - lo) if hi > lo else 0.5
    if 0.35 <= pos <= 0.85:
        s += 8
    elif pos > 0.95:
        s += 2
    if rsi < 70:
        s += 4
    return max(0.0, min(100.0, s))


def backtest_stock(kl, horizon=5, threshold=65):
    """单股历史信号回溯"""
    if len(kl) < 80:
        return None
    rets = []
    for i in range(65, len(kl) - horizon):
        sub = kl[:i + 1]
        if quick_score(sub) >= threshold:
            base = kl[i]["close"]
            if base <= 0:
                continue
            rets.append((kl[i + horizon]["close"] / base - 1) * 100)
    if len(rets) < 2:
        return None
    wins = sum(1 for r in rets if r > 0)
    return {
        "samples": len(rets),
        "win_rate": round(wins / len(rets) * 100, 1),
        "avg_ret": round(_mean(rets), 2),
        "best": round(max(rets), 2),
        "worst": round(min(rets), 2),
        "rets": rets,
        "horizon": horizon,
    }


def run(recommendations, horizon=10, threshold=65, tp=0.08, sl=0.05):
    """
    对推荐股逐一回溯，并汇总成策略整体表现
    recommendations 里需带 _kl（日K）
    """
    per = {}
    all_rets = []
    for rec in recommendations:
        kl = rec.get("_kl") or []
        if len(kl) < 80:
            continue
        rets = backtest_stops(kl, horizon, threshold, tp, sl)
        if len(rets) < 2:
            continue
        wins = sum(1 for r in rets if r > 0)
        per[rec["code"]] = {
            "samples": len(rets),
            "win_rate": round(wins / len(rets) * 100, 1),
            "avg_ret": round(_mean(rets), 2),
            "horizon": horizon,
            "tp": round(tp * 100), "sl": round(sl * 100),
        }
        all_rets.extend(rets)

    if not all_rets:
        return {"per_stock": {}, "strategy": None, "horizon": horizon}

    wins = sum(1 for r in all_rets if r > 0)
    wins_big = sum(1 for r in all_rets if r > 3)
    loss_big = sum(1 for r in all_rets if r < -3)
    avg = _mean(all_rets)
    # 简易盈亏比：平均盈利 / 平均亏损
    gains = [r for r in all_rets if r > 0]
    losses = [r for r in all_rets if r <= 0]
    rr = (_mean(gains) / abs(_mean(losses))) if losses and _mean(losses) != 0 else 0

    return {
        "per_stock": per,
        "horizon": horizon,
        "strategy": {
            "samples": len(all_rets),
            "stocks": len(per),
            "win_rate": round(wins / len(all_rets) * 100, 1),
            "avg_ret": round(avg, 2),
            "avg_win": round(_mean(gains), 2) if gains else 0,
            "avg_loss": round(_mean(losses), 2) if losses else 0,
            "rr": round(rr, 2),
            "big_win_rate": round(wins_big / len(all_rets) * 100, 1),
            "big_loss_rate": round(loss_big / len(all_rets) * 100, 1),
            "threshold": threshold,
            "tp": round(tp * 100),
            "sl": round(sl * 100),
        },
    }


def backtest_stops(kl, horizon=10, threshold=65, tp=0.10, sl=0.05):
    """
    带止盈止损的回测：信号点买入后逐日检查，先触及哪个就走
    返回收益列表
    """
    rets = []
    for i in range(65, len(kl) - horizon):
        sub = kl[:i + 1]
        if quick_score(sub) < threshold:
            continue
        buy = kl[i]["close"]
        if buy <= 0:
            continue
        got = None
        for d in range(1, horizon + 1):
            bar = kl[i + d]
            if bar["high"] >= buy * (1 + tp):
                got = tp * 100
                break
            if bar["low"] <= buy * (1 - sl):
                got = -sl * 100
                break
        if got is None:
            got = (kl[i + horizon]["close"] / buy - 1) * 100
        rets.append(got)
    return rets


def equity_curve(bt_recs, horizon=5, threshold=65, max_points=120):
    """
    简化组合净值：每天等权买入当日所有信号，持有 horizon 个交易日
    返回 {dates:[], nav:[], bench:[], stat:{}}
    """
    # 1. 收集信号与价格序列
    signals = {}          # date -> [code]
    prices = {}           # code -> {date: close}
    for rec in bt_recs:
        kl = rec.get("_kl") or []
        code = rec.get("code")
        if len(kl) < 80 or not code:
            continue
        prices[code] = {x["date"]: x["close"] for x in kl}
        for i in range(65, len(kl) - 1):
            if quick_score(kl[:i + 1]) >= threshold:
                signals.setdefault(kl[i]["date"], []).append(code)

    if not signals or not prices:
        return None

    # 2. 日期轴（用出现最多的股票日期集合）
    all_dates = sorted({d for p in prices.values() for d in p})
    if len(all_dates) < 30:
        return None
    dates = all_dates[-max_points:] if len(all_dates) > max_points else all_dates

    # 3. 逐日模拟
    nav = [1.0]
    daily_ret = []
    holdings = []         # [(code, signal_idx)]
    for i in range(1, len(all_dates)):
        d, dprev = all_dates[i], all_dates[i - 1]
        # 当日新信号入场
        held = {c for c, _ in holdings}
        for c in signals.get(dprev, []):
            if c not in held:
                holdings.append((c, i - 1))
        # 到期离场
        holdings = [(c, s) for (c, s) in holdings if i - s <= horizon]
        rets = []
        for c, _ in holdings:
            p0, p1 = prices[c].get(dprev), prices[c].get(d)
            if p0 and p1 and p0 > 0:
                rets.append(p1 / p0 - 1)
        r = sum(rets) / len(rets) if rets else 0.0
        daily_ret.append(r)
        nav.append(nav[-1] * (1 + r))

    # 3b. 基准：等权全样本
    bench = [1.0]
    for i in range(1, len(all_dates)):
        d, dprev = all_dates[i], all_dates[i - 1]
        rr = []
        for c, p in prices.items():
            p0, p1 = p.get(dprev), p.get(d)
            if p0 and p1 and p0 > 0:
                rr.append(p1 / p0 - 1)
        avg_b = (sum(rr) / len(rr)) if rr else 0.0
        bench.append(bench[-1] * (1 + avg_b))

    # 4. 截取展示区间
    lo = len(all_dates) - len(dates)
    out_dates = all_dates[lo:]
    base_nav = nav[lo] or 1.0
    base_bench = bench[lo] or 1.0
    out_nav = [v / base_nav for v in nav[lo:]]
    out_bench = [v / base_bench for v in bench[lo:]]

    # 5. 统计
    total = out_nav[-1] - 1
    bench_total = out_bench[-1] - 1
    peak, mdd = 1.0, 0.0
    for v in out_nav:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)
    dr = daily_ret[lo - 1:] if lo > 0 else daily_ret
    mean = sum(dr) / len(dr) if dr else 0
    var = sum((x - mean) ** 2 for x in dr) / len(dr) if dr else 0
    std = var ** 0.5
    sharpe = (mean / std * (252 ** 0.5)) if std > 0 else 0

    return {
        "dates": out_dates,
        "nav": [round(v, 4) for v in out_nav],
        "bench": [round(v, 4) for v in out_bench],
        "stat": {
            "total": round(total * 100, 2),
            "bench": round(bench_total * 100, 2),
            "excess": round((total - bench_total) * 100, 2),
            "mdd": round(mdd * 100, 2),
            "sharpe": round(sharpe, 2),
            "days": len(out_dates),
        },
    }
