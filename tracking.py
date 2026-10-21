# -*- coding: utf-8 -*-
"""
推荐跟踪与自校准
1. 记录每天推荐的标的（买入价、目标、止损、六维得分）
2. 每日回填收盘价，算 T+1 / T+3 / T+5 收益，判定目标/止损是否触发
3. 统计各维度的预测力（高分组 vs 低分组的后续收益差）
4. 据此微调六维权重，写入 weights.json，供评分的下一轮使用
"""
import json
import os
import time

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE, "docs", "data")
TRACK_PATH = os.path.join(DATA_DIR, "track_record.json")
WEIGHT_PATH = os.path.join(BASE, "weights.json")

DIM_MAX = {"trend": 25, "momentum": 20, "volume": 20, "fund": 15, "news": 10, "value": 10}
DEFAULT_W = {k: 1.0 for k in DIM_MAX}

MAX_HOLD = 5          # 跟踪 5 个交易日
MIN_SAMPLES = 30      # 少于这个样本数不做校准
MAX_SHIFT = 0.35      # 单次权重最多调整 35%


def _load(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return default


def _save(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)


def load_weights():
    w = _load(WEIGHT_PATH, {})
    out = dict(DEFAULT_W)
    for k, v in (w.get("weights") or {}).items():
        if k in out:
            try:
                out[k] = max(0.5, min(1.8, float(v)))
            except (TypeError, ValueError):
                pass
    return out


def save_weights(w, note=""):
    _save(WEIGHT_PATH, {
        "weights": w,
        "updated": time.strftime("%Y-%m-%d %H:%M"),
        "note": note,
    })


def record(recs, today, quotes):
    """把当日推荐写入跟踪表（同一天同代码只记一次）"""
    db = _load(TRACK_PATH, {"entries": []})
    entries = db.get("entries") or []
    exist = {(e.get("date"), e.get("code")) for e in entries}
    added = 0
    for r in recs:
        key = (today, r.get("code"))
        if key in exist:
            continue
        q = quotes.get(r.get("code")) or {}
        entries.append({
            "date": today,
            "code": r.get("code"),
            "name": r.get("name"),
            "kind": r.get("kind", "steady"),
            "buy": r.get("buy_price"),
            "target": r.get("target"),
            "stop": r.get("stop"),
            "score": r.get("score"),
            "grade": r.get("grade"),
            "dims": r.get("dims") or {},
            "prices": {},
            "base": q.get("p") or r.get("price"),
            "ret": {},
            "status": "holding",
            "days": 0,
        })
        added += 1
    db["entries"] = entries[-1200:]
    db["stats"] = summarize(entries)
    _save(TRACK_PATH, db)
    return added


def update(quotes):
    """用最新行情回填价格，计算收益与状态"""
    db = _load(TRACK_PATH, {"entries": []})
    entries = db.get("entries") or []
    today = time.strftime("%Y-%m-%d")
    changed = 0
    for e in entries:
        if e.get("status") not in ("holding", None):
            continue
        q = quotes.get(e.get("code"))
        if not q or not q.get("p"):
            continue
        price = q.get("p")
        base = e.get("base") or e.get("buy")
        if not base or base <= 0:
            continue
        # 记录当天收盘（同一天覆盖）
        e.setdefault("prices", {})[today] = round(price, 3)
        days = len(e.get("prices") or {})
        e["days"] = days
        e["last"] = round(price, 3)
        e["ret_now"] = round((price / base - 1) * 100, 2)
        if days >= 1:
            e["ret"]["t1"] = e["ret_now"]
        if days >= 3:
            e["ret"]["t3"] = e["ret_now"]
        if days >= MAX_HOLD:
            e["ret"]["t5"] = e["ret_now"]
        # 状态判定
        tgt, stp = e.get("target") or 0, e.get("stop") or 0
        hit = None
        if tgt and price >= tgt:
            hit = "hit_target"
        elif stp and price <= stp:
            hit = "hit_stop"
        elif days >= MAX_HOLD:
            hit = "expired"
        if hit:
            e["status"] = hit
            e["close_day"] = days
            e["final_ret"] = e["ret_now"]
        changed += 1
    db["entries"] = entries
    db["stats"] = summarize(entries)
    db.setdefault("updated", today)
    _save(TRACK_PATH, db)
    return changed


def summarize(entries):
    done = [e for e in entries if e.get("status") in ("hit_target", "hit_stop", "expired")]
    # 已结算用 final_ret，未结算用最新浮盈
    rets = [e.get("final_ret") if e.get("final_ret") is not None else e.get("ret_now")
            for e in done]
    rets = [r for r in rets if isinstance(r, (int, float))]
    win = sum(1 for r in rets if r > 0)
    out = {
        "total": len(entries),
        "settled": len(rets),
        "holding": len([e for e in entries if e.get("status") == "holding"]),
        "win": win,
        "win_rate": round(win / len(rets) * 100, 1) if rets else None,
        "avg_ret": round(sum(rets) / len(rets), 2) if rets else None,
        "best": round(max(rets), 2) if rets else None,
        "worst": round(min(rets), 2) if rets else None,
        "hit_target": len([e for e in entries if e.get("status") == "hit_target"]),
        "hit_stop": len([e for e in entries if e.get("status") == "hit_stop"]),
    }
    # 近 5 个交易日
    recent = [e for e in done if e.get("date")]
    if recent:
        days = sorted({e["date"] for e in recent})[-5:]
        sel = [e for e in recent if e["date"] in days]
        rr = [e.get("final_ret") for e in sel if isinstance(e.get("final_ret"), (int, float))]
        if rr:
            out["recent5"] = {
                "days": len(days),
                "count": len(rr),
                "win_rate": round(sum(1 for x in rr if x > 0) / len(rr) * 100, 1),
                "avg_ret": round(sum(rr) / len(rr), 2),
            }
    return out


def suggest_weights(min_samples=MIN_SAMPLES):
    """
    按维度分组比较后续收益：
    高分组（该维度得分前 50%）平均收益 - 低分组平均收益 = 该维度的超额贡献
    """
    db = _load(TRACK_PATH, {"entries": []})
    all_e = db.get("entries") or []
    # 优先用已结算样本
    entries = [e for e in all_e
               if e.get("status") in ("hit_target", "hit_stop", "expired")
               and isinstance(e.get("final_ret"), (int, float))
               and e.get("dims")]
    # 样本不够时，用"持有满 1 天"的浮动收益补充（标注为含浮盈）
    floating = 0
    if len(entries) < min_samples:
        for e in all_e:
            if e.get("status") == "holding" and (e.get("days") or 0) >= 1 \
                    and isinstance(e.get("ret_now"), (int, float)) and e.get("dims"):
                e2 = dict(e)
                e2["final_ret"] = e.get("ret_now")
                entries.append(e2)
                floating += 1
    if len(entries) < min_samples:
        return None, {"reason": "样本不足（%d/%d）" % (len(entries), min_samples)}

    cur = load_weights()
    new_w, detail = {}, {}
    for dim, cap in DIM_MAX.items():
        vals = []
        for e in entries:
            v = (e.get("dims") or {}).get(dim)
            if v is None:
                continue
            vals.append((float(v) / cap, e["final_ret"]))
        if len(vals) < min_samples:
            new_w[dim] = cur[dim]
            detail[dim] = {"skip": "样本不足"}
            continue
        vals.sort(key=lambda x: -x[0])
        cut = max(1, len(vals) // 2)
        hi = [r for _, r in vals[:cut]]
        lo = [r for _, r in vals[cut:]]
        hi_avg = sum(hi) / len(hi)
        lo_avg = sum(lo) / len(lo)
        edge = hi_avg - lo_avg          # 正数代表该维度有效
        # 映射到权重系数：edge=+2% -> +7%，edge=-2% -> -7%
        factor = 1.0 + max(-MAX_SHIFT, min(MAX_SHIFT, edge * 0.035))
        new_w[dim] = round(cur[dim] * factor, 3)
        detail[dim] = {
            "high_avg": round(hi_avg, 2),
            "low_avg": round(lo_avg, 2),
            "edge": round(edge, 2),
            "weight": new_w[dim],
        }
    return new_w, {"samples": len(entries), "floating": floating, "detail": detail}


def apply_calibration():
    """执行校准：写入 weights.json，返回变更说明"""
    new_w, info = suggest_weights()
    if not new_w:
        return {"applied": False, "info": info}
    old = load_weights()
    changed = {k: [old[k], new_w[k]] for k in new_w if abs(old[k] - new_w[k]) > 0.01}
    if not changed:
        return {"applied": False, "info": {"reason": "权重已在最优区间", **info}}
    save_weights(new_w, "基于 %d 条已结算推荐自动校准" % info.get("samples", 0))
    return {"applied": True, "changed": changed, "info": info}
