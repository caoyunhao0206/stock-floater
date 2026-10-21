# -*- coding: utf-8 -*-
"""
GitHub Actions 数据构建脚本
全市场扫描 -> 六维评分 -> 推荐榜 -> docs/data/all.json
可选：配置 AI_API_KEY secret 后，对头部候选做 AI 复评
"""
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import backtest
import market
import recommender
import sources

BASE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(BASE, "docs", "data")

AI_BASE = (os.environ.get("AI_BASE_URL") or "https://api.deepseek.com/v1").strip()
AI_KEY = os.environ.get("AI_API_KEY", "").strip()
AI_MODEL = (os.environ.get("AI_MODEL") or "deepseek-chat").strip()
AI_TOP_N = int(os.environ.get("AI_TOP_N", "5") or 5)


def ai_review(items):
    """对评分最高的若干只做 AI 复评（无 Key 则跳过）"""
    if not AI_KEY:
        return 0
    try:
        import ai
    except Exception as e:  # noqa: BLE001
        print("AI 模块不可用:", e)
        return 0
    client = ai.AIClient(AI_BASE, AI_KEY, AI_MODEL)
    if not client.ready():
        return 0
    cand = sorted(items, key=lambda x: -x.get("score", 0))[:AI_TOP_N]
    ok = 0

    def work(rec):
        ctx = rec.get("_ai_ctx")
        if not ctx:
            return rec["code"], None
        try:
            text = ai.build_context(ctx)
            return rec["code"], ai.cached_analyze(client, rec["code"], text, "balanced")
        except Exception as e:  # noqa: BLE001
            print("AI 失败 %s: %s" % (rec.get("code"), e))
            return rec["code"], None

    with ThreadPoolExecutor(max_workers=3) as ex:
        for code, res in ex.map(work, cand):
            if not res:
                continue
            target = next((r for r in items if r["code"] == code), None)
            if not target:
                continue
            target["ai"] = {
                "rating": res.get("rating"),
                "score": res.get("score"),
                "confidence": res.get("confidence"),
                "thesis": (res.get("thesis") or [])[:4],
                "risks": (res.get("risks") or [])[:3],
                "news_check": res.get("news_check"),
                "one_line": res.get("one_line"),
                "cached": bool(res.get("_cached")),
            }
            try:
                ai_norm = (float(res.get("score")) + 100) / 2
                target["final_score"] = round(target["score"] * 0.7 + ai_norm * 0.3, 1)
            except (TypeError, ValueError):
                target["final_score"] = target["score"]
            ok += 1
    print("AI 复评完成 %d 只" % ok)
    return ok


def slim_quote(r):
    """压缩字段体积（5000+ 只需要控制大小）"""
    return {
        "n": r["name"], "p": r["price"], "c": round(r["change_pct"], 2),
        "t": round(r["turnover"], 2) if r.get("turnover") else 0,
        "a": round(r["amount"] / 1e8, 2) if r.get("amount") else 0,
        "pe": round(r["pe"], 1) if r.get("pe") else None,
        "pb": round(r["pb"], 2) if r.get("pb") else None,
        "mv": round(r["nmc"] / 10000, 0) if r.get("nmc") else None,
    }


def main():
    t0 = time.time()
    print("开始全市场扫描…")
    rows = market.scan_all()
    if not rows:
        print("扫描失败，退出")
        sys.exit(1)
    print("扫描完成：%d 只，%.1fs" % (len(rows), time.time() - t0))

    stats = market.stats(rows)
    try:
        idx = [x["close"] for x in sources.fetch_index_kline("sh000001", 80)]
    except Exception:  # noqa: BLE001
        idx = []

    print("开始推荐引擎…")
    reco = recommender.build(rows, idx, per_list=10)
    all_recs = reco["aggressive"] + reco["steady"]
    ai_review(all_recs)
    try:
        # 回测用整个候选池，样本更充分（推荐股本身只有十几只）
        cand_syms = reco.get("candidates") or [r["symbol"] for r in all_recs]
        bt_recs = []
        for sym in cand_syms[:70]:
            try:
                k = sources.fetch_kline(sym, 220)
                if len(k) >= 120:
                    bt_recs.append({"_kl": k, "code": sym[-6:]})
            except Exception:  # noqa: BLE001
                continue
        bt = backtest.run(bt_recs, horizon=10, threshold=65, tp=0.10, sl=0.07)
        print("回测：%d 只股票，%d 个历史信号，10日胜率 %s%%（盈8%%/损5%%）" % (
            bt["strategy"]["stocks"] if bt.get("strategy") else 0,
            bt["strategy"]["samples"] if bt.get("strategy") else 0,
            bt["strategy"]["win_rate"] if bt.get("strategy") else "-"))
    except Exception as e:  # noqa: BLE001
        print("回测失败:", e)
        bt = {"per_stock": {}, "strategy": None, "horizon": 5}
    if any(r.get("final_score") is not None for r in all_recs):
        reco["aggressive"].sort(key=lambda x: (-(x.get("final_score") or x["score"]), -x["rr"]))
        reco["steady"].sort(key=lambda x: (-(x.get("final_score") or x["score"]), -x["rr"]))
    for r in all_recs:
        r.pop("_ai_ctx", None)
        r.pop("_kl", None)
        c = r.get("code")
        if c and bt.get("per_stock", {}).get(c):
            r["hist"] = bt["per_stock"][c]

    print("推荐：稳健 %d / 进攻 %d（候选 %d，过滤 %d）" % (
        len(reco["steady"]), len(reco["aggressive"]),
        reco.get("pool", 0), reco.get("filtered", 0)))

    gainers = [r for r in rows if not r["name"].startswith(("N", "C"))]
    gainers.sort(key=lambda x: -x["change_pct"])
    amount_top = sorted(rows, key=lambda x: -(x["amount"] or 0))

    out = {
        "updated": datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds"),
        "updated_ts": int(time.time()),
        "market": {
            "stats": stats,
            "gainers": [slim_quote(r) | {"code": r["code"]} for r in gainers[:20]],
            "amount_top": [slim_quote(r) | {"code": r["code"]} for r in amount_top[:20]],
            "scan_seconds": round(time.time() - t0, 1),
        },
        "recommend": {
            "steady": reco["steady"],
            "aggressive": reco["aggressive"],
            "meta": {"pool": reco.get("pool", 0), "hit": reco.get("hit", 0),
                     "filtered": reco.get("filtered", 0), "scanned": len(rows)},
        },
        "quotes": {r["code"]: slim_quote(r) for r in rows},
        "backtest": {
            "strategy": bt.get("strategy"),
            "per_stock": bt.get("per_stock", {}),
            "horizon": bt.get("horizon", 5),
        },
    }

    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, "all.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    size = os.path.getsize(path) / 1024
    print("已生成 %s（%.0f KB，总耗时 %.1fs）" % (path, size, time.time() - t0))


if __name__ == "__main__":
    main()
