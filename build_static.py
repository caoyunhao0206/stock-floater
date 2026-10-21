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


AI_STATUS = {"ok": 0, "tried": 0, "error": ""}


def ai_review(items):
    """对评分最高的若干只做 AI 复评（无 Key 则跳过）"""
    if not AI_KEY:
        AI_STATUS["error"] = "未配置"
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

    AI_STATUS["tried"] = len(cand)
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
    AI_STATUS["ok"] = ok
    if ok:
        print("AI 复评完成 %d 只" % ok)
    else:
        AI_STATUS["error"] = "全部调用失败（额度或密钥问题）"
        print("AI 复评失败：0/%d" % len(cand))
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




# ================= 微信推送 =================
PUSH_STATE = os.path.join(OUT_DIR, "push_state.json")
WATCHLIST_FILE = os.path.join(BASE, "watchlist.json")


def _load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return default


def _save_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)


def wechat_send(title, content):
    """Server酱推送，未配置则静默跳过"""
    key = os.environ.get("SERVERCHAN_KEY", "").strip()
    if not key:
        return False
    import requests
    for base in ("https://sctapi.ftqq.com", "https://sctapi.ft08.com"):
        try:
            r = requests.post("%s/%s.send" % (base, key),
                              data={"title": title, "desp": content}, timeout=12)
            j = r.json()
            if j.get("code") == 0:
                return True
            print("推送返回异常:", str(j)[:120])
        except Exception as e:  # noqa: BLE001
            print("推送通道 %s 失败: %s" % (base, str(e)[:80]))
    return False


# 每类消息每日上限，避免一天几十条轰炸
PUSH_CAP = {"new_a": 4, "board": 3, "watch": 8, "daily": 1}


def push_all(data, reco):
    """四类推送：新A级 / 榜单变化 / 自选触发 / 每日复盘（带每日限额）"""
    if not os.environ.get("SERVERCHAN_KEY", "").strip():
        print("未配置 SERVERCHAN_KEY，跳过推送")
        return

    st = _load_json(PUSH_STATE, {"recos": [], "grade_a": [], "last_daily": ""})
    today = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")
    if st.get("date") != today:
        st["date"] = today
        st["counts"] = {}

    def allow(kind):
        return st.get("counts", {}).get(kind, 0) < PUSH_CAP.get(kind, 3)

    def mark(kind):
        c = st.setdefault("counts", {})
        c[kind] = c.get(kind, 0) + 1
    recs = (reco.get("steady") or []) + (reco.get("aggressive") or [])
    cur = {r["code"]: r for r in recs}
    prev_codes = set(st.get("recos") or [])
    prev_a = set(st.get("grade_a") or [])
    sent = 0

    # 1. 新出现的 A/A+ 机会
    new_a = [r for r in recs if r.get("grade") in ("A+", "A") and r["code"] not in prev_a]
    if new_a and allow("new_a"):
        lines = []
        for r in new_a[:3]:
            lines.append("【%s】%s(%s)\n现价 %.2f → 买入 %.2f\n目标 %.2f (+%.1f%%) 止损 %.2f\n%s" % (
                r.get("grade"), r.get("name"), r.get("code"), r.get("price", 0),
                r.get("buy_price", 0), r.get("target", 0), r.get("upside", 0),
                r.get("stop", 0), (r.get("reasons") or ["-"])[0]))
        if wechat_send("【新机会】%d 只 A 级标的" % len(new_a), "\n\n".join(lines)):
            mark("new_a")
            sent += 1

    # 2. 榜单变化
    added = set(cur) - prev_codes
    removed = prev_codes - set(cur)
    if prev_codes and (added or removed) and allow("board"):
        lines = []
        for c in list(added)[:5]:
            r = cur[c]
            lines.append("+ %s(%s) 买入 %.2f  %s" % (r.get("name"), c, r.get("buy_price", 0), r.get("grade", "")))
        for c in list(removed)[:5]:
            lines.append("- %s 已移出榜单" % c)
        if wechat_send("【榜单变化】新增 %d / 移出 %d" % (len(added), len(removed)), "\n".join(lines)):
            mark("board")
            sent += 1

    # 3. 自选股触发
    wl = _load_json(WATCHLIST_FILE, [])
    hits = []
    for w in wl:
        code = str(w.get("code", "")).strip()
        q = (data.get("quotes") or {}).get(code)
        if not q:
            continue
        price = q["p"]
        zone = w.get("buy_zone")
        stop = w.get("stop")
        if zone and len(zone) == 2 and zone[0] <= price <= zone[1]:
            hits.append("【买点】%s(%s) 现价 %.2f 已进入买入区间 %.2f~%.2f" % (
                q.get("n"), code, price, zone[0], zone[1]))
        if stop and price <= float(stop):
            hits.append("【跌破止损】%s(%s) 现价 %.2f 已跌破 %.2f" % (
                q.get("n"), code, price, float(stop)))
    if hits and allow("watch"):
        if wechat_send("【自选提醒】%d 条" % len(hits), "\n".join(hits[:6])):
            mark("watch")
            sent += 1

    # 4. 每日复盘（15:30 后，每天一次）
    now = datetime.now(timezone(timedelta(hours=8)))
    mins = now.hour * 60 + now.minute
    # 14:45 之后的第一轮视为尾盘复盘（15:00 收盘价与 14:45 差异极小）
    if mins >= 14 * 60 + 45 and st.get("last_daily") != today:
        ms = data.get("market", {}).get("stats", {})
        bt = data.get("backtest", {}).get("strategy") or {}
        body = ("大盘：涨 %s / 跌 %s，涨停 %s 家，成交 %.2f 万亿\n"
                "今日推荐 %d 只，其中 A 级及以上 %d 只\n"
                "策略回测：%s 日胜率 %s%%，盈亏比 %s") % (
            ms.get("up", "-"), ms.get("down", "-"), ms.get("limit_up", "-"),
            (ms.get("amount") or 0) / 10000, len(recs),
            len([r for r in recs if r.get("grade") in ("A+", "A")]),
            bt.get("horizon", 10), bt.get("win_rate", "-"), bt.get("rr", "-"))
        if wechat_send("【尾盘复盘】" + today, body):
            st["last_daily"] = today
            mark("daily")
            sent += 1

    st["recos"] = list(cur)
    st["grade_a"] = [r["code"] for r in recs if r.get("grade") in ("A+", "A")]
    _save_json(PUSH_STATE, st)
    print("推送完成：%d 条 | 今日已发 %s" % (sent, st.get("counts", {})))


def check_ai_health(data):
    """AI 连续两轮失败 -> 推送告警（同一天只提醒一次）"""
    st = _load_json(PUSH_STATE, {})
    ai = data.get("ai_status") or {}
    tried, ok = ai.get("tried") or 0, ai.get("ok") or 0
    if tried == 0:
        return
    today = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")
    if ok == 0:
        fails = st.get("ai_fail_streak", 0) + 1
        st["ai_fail_streak"] = fails
        if fails >= 2 and st.get("ai_alert_date") != today:
            if wechat_send("【AI 告警】连续 %d 轮调用失败" % fails,
                           "推荐榜的 AI 复评已连续 %d 轮全部失败。\n"
                           "常见原因：额度用尽、Key 失效、中转站故障。\n"
                           "页面仍可正常使用，规则评分不受影响；也可在网页设置里填自己的 API 直连。" % fails):
                st["ai_alert_date"] = today
                print("AI 告警已推送")
    else:
        if st.get("ai_fail_streak"):
            print("AI 已恢复正常（之前连续失败 %d 轮）" % st["ai_fail_streak"])
        st["ai_fail_streak"] = 0
    _save_json(PUSH_STATE, st)


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

    # ---- 板块热度 ----
    sectors = {}
    try:
        sec_up = sources.fetch_sectors("price", 30)
        sec_money = sources.fetch_sectors("zljlr", 20)
        sectors = {"by_change": sec_up, "by_money": sec_money}
        print("板块：%d 个行业，涨幅第一 %s，资金第一 %s" % (
            len(sec_up), sec_up[0]["name"] if sec_up else "-",
            sec_money[0]["name"] if sec_money else "-"))
    except Exception as e:  # noqa: BLE001
        print("板块数据失败:", e)

    # ---- 涨停复盘 ----
    limit_up = []
    try:
        ups = [r for r in rows if (r["change_pct"] or 0) >= 9.8
               and not r["name"].startswith(("N", "C", "ST"))]
        ups.sort(key=lambda x: -(x["amount"] or 0))
        ups = ups[:40]
        import indicators as _I

        def _boards(r):
            try:
                k = sources.fetch_kline(r["symbol"], 40)
                if len(k) < 6:
                    return 0
                cnt = 0
                for i in range(len(k) - 1, 0, -1):
                    prev = k[i - 1]["close"]
                    if prev > 0 and (k[i]["close"] / prev - 1) * 100 >= 9.5:
                        cnt += 1
                    else:
                        break
                return cnt
            except Exception:  # noqa: BLE001
                return 0

        with ThreadPoolExecutor(max_workers=8) as ex:
            boards = list(ex.map(_boards, ups))
        for r, b in zip(ups, boards):
            prev = r["prev_close"] or 0
            one_word = bool(prev and r["open"] >= prev * 1.095 and r["low"] >= prev * 1.095)
            limit_up.append({
                "code": r["code"], "name": r["name"], "price": r["price"],
                "change_pct": r["change_pct"], "boards": b,
                "one_word": one_word,
                "turnover": r["turnover"], "amount": round((r["amount"] or 0) / 1e8, 2),
                "nmc": round((r["nmc"] or 0) / 10000, 1),
            })
        limit_up.sort(key=lambda x: (-x["boards"], -x["amount"]))
        print("涨停：%d 只，最高 %d 连板" % (
            len(limit_up), limit_up[0]["boards"] if limit_up else 0))
    except Exception as e:  # noqa: BLE001
        print("涨停复盘失败:", e)

    # ---- 自选股深度（用于页面个股详情）----
    watch_detail = {}
    try:
        wl = _load_json(WATCHLIST_FILE, [])
        codes = [str(w.get("code", "")).strip() for w in wl if w.get("code")]
        import engine as _E

        def _deep(code6):
            if len(code6) != 6:
                return None, None
            sym = ("sh" if code6[0] == "6" else "sz") + code6
            try:
                q = sources.fetch_quotes([sym]).get(sym)
                if not q:
                    return None, None
                k = sources.fetch_kline(sym, 260)
                nm = q.get("name") or code6
                ns = sources.fetch_news_multi(sym, code6, nm, 8)
                ex = {"reports": sources.fetch_reports(code6),
                      "forecast": sources.fetch_forecast(code6),
                      "billboard": sources.fetch_billboard(code6)}
                adv = _E.build_advice(q, k, [], ns["items"], 0, 0, extras=ex, index_closes=idx)
                news = {k2: ns[k2] for k2 in ("items", "score", "pos", "neg", "sources", "verified")}
                return q, {"code": code6, "name": nm, "quote": q,
                           "advice": adv, "news": news, "extras": ex}
            except Exception:  # noqa: BLE001
                return None, None

        with ThreadPoolExecutor(max_workers=4) as ex2:
            for q, d in ex2.map(_deep, codes[:12]):
                if d:
                    watch_detail[d["code"]] = d
        print("自选深度：%d 只" % len(watch_detail))
    except Exception as e:  # noqa: BLE001
        print("自选深度失败:", e)

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
        "sectors": sectors,
        "limit_up": limit_up,
        "watch_detail": watch_detail,
        "ai_status": {"ok": AI_STATUS.get("ok", 0), "tried": AI_STATUS.get("tried", 0),
                      "error": AI_STATUS.get("error", "")},
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
    try:
        push_all(out, reco)
    except Exception as e:  # noqa: BLE001
        print("推送流程异常:", e)

    try:
        check_ai_health(out)
    except Exception as e:  # noqa: BLE001
        print("AI 健康检查异常:", e)

    size = os.path.getsize(path) / 1024
    print("已生成 %s（%.0f KB，总耗时 %.1fs）" % (path, size, time.time() - t0))


if __name__ == "__main__":
    main()
