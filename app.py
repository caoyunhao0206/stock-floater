# -*- coding: utf-8 -*-
"""
盯盘悬浮助手 · 本地后端服务
- 行情/指标/新闻/资金流 -> 建议引擎 -> 信号推送（桌面通知 + 微信）
- REST API + SSE 实时推送
"""
import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
import logging
import os
import threading
import time
from datetime import datetime

from fastapi import FastAPI, Body
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles

import sources
import engine
import market
import recommender
import pushers
import ai

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("floater.app")

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(BASE, "data.json")

SENSITIVE_KEYS = ("ai_api_key", "push_key")


def mask_settings(s):
    """返回给前端的设置：Key 只给掩码和“是否已配置”"""
    d = dict(s)
    for k in SENSITIVE_KEYS:
        v = str(d.get(k) or "")
        d[k + "_set"] = bool(v)
        if len(v) >= 8:
            d[k] = "****" + v[-4:]
        else:
            d[k] = "****" if v else ""
    return d


DEFAULT_SETTINGS = {
    "interval": 5,             # 刷新间隔（秒）
    "auto_hours": True,        # 仅交易时段高频，休市自动降频
    "sound": True,             # 信号提示音（前端播放）
    "push_channel": "serverchan",
    "push_key": "",
    "push_enabled": False,
    "wave_threshold": 5.0,     # 异动提醒涨跌幅阈值 %
    "cooldown": 1800,          # 同代码同类型信号冷却（秒）
    # ---- AI 大模型 ----
    "ai_enabled": False,
    "ai_base_url": "https://api.deepseek.com/v1",
    "ai_api_key": "",
    "ai_model": "deepseek-chat",
    "ai_top_n": 5,
    "ai_style": "balanced",
}

state_lock = threading.Lock()
MARKET = {
    "rows": [], "stats": {}, "gainers": [], "amount_top": [],
    "updated": None, "last_full": 0, "scan_ms": 0,
}
RECO = {
    "steady": [], "aggressive": [], "updated": None,
    "scanned": 0, "pool": 0, "hit": 0, "filtered": 0,
    "ai": {"enabled": False, "ok": None, "msg": "", "calls": 0, "covered": 0},
}

INDEX_CACHE = {"ts": 0, "closes": []}


def get_index_closes():
    """上证指数收盘序列（120 秒缓存）"""
    if time.time() - INDEX_CACHE["ts"] > 120 or not INDEX_CACHE["closes"]:
        try:
            kl = sources.fetch_index_kline("sh000001", 80)
            INDEX_CACHE["closes"] = [x["close"] for x in kl]
            INDEX_CACHE["ts"] = time.time()
        except Exception:  # noqa: BLE001
            pass
    return INDEX_CACHE["closes"]


def ai_review(items):
    """对推荐结果做 AI 复评（失败静默降级），返回覆盖数量"""
    s = ST["settings"]
    if not (s.get("ai_enabled") and s.get("ai_api_key")):
        RECO["ai"] = {"enabled": False, "ok": None, "msg": "未启用", "calls": 0, "covered": 0}
        return 0
    client = ai.AIClient(s["ai_base_url"], s["ai_api_key"], s["ai_model"])
    if not client.ready():
        RECO["ai"] = {"enabled": True, "ok": False, "msg": "配置不完整", "calls": 0, "covered": 0}
        return 0
    top_n = max(1, min(int(s.get("ai_top_n") or 5), 10))
    # 稳健/进攻各取评分最高的若干只
    cand = sorted(items, key=lambda x: -x["score"])[:top_n]
    style = s.get("ai_style") or "balanced"
    ok_cnt, err = 0, ""

    def work(rec):
        ctx = rec.get("_ai_ctx") or {}
        if not ctx:
            return None
        text = ai.build_context(ctx)
        try:
            res = ai.cached_analyze(client, rec["code"], text, style)
            return rec["code"], res
        except Exception as e:  # noqa: BLE001
            return rec["code"], {"_error": str(e)[:200]}

    with ThreadPoolExecutor(max_workers=3) as ex:
        for code, res in ex.map(work, cand):
            if not code or not res:
                continue
            if res.get("_error"):
                err = res["_error"]
                continue
            target = next((r for r in items if r["code"] == code), None)
            if not target:
                continue
            aj = {
                "rating": res.get("rating"),
                "score": res.get("score"),
                "confidence": res.get("confidence"),
                "thesis": (res.get("thesis") or [])[:4],
                "risks": (res.get("risks") or [])[:3],
                "news_check": res.get("news_check"),
                "one_line": res.get("one_line"),
                "buy_zone": res.get("buy_zone"),
                "stop_loss": res.get("stop_loss"),
                "target": res.get("target"),
                "cached": bool(res.get("_cached")),
            }
            target["ai"] = aj
            # AI 评分参与排序：规则 70% + AI 30%
            try:
                ai_norm = (float(res.get("score")) + 100) / 2
                target["final_score"] = round(target["score"] * 0.7 + ai_norm * 0.3, 1)
            except (TypeError, ValueError):
                target["final_score"] = target["score"]
            ok_cnt += 1

    RECO["ai"] = {
        "enabled": True, "ok": ok_cnt > 0,
        "msg": ("已复评 %d 只" % ok_cnt) if ok_cnt else (err or "AI 未返回结果"),
        "calls": len(cand), "covered": ok_cnt,
        "model": s.get("ai_model"),
    }
    return ok_cnt

ST = {
    "watches": [],            # [{code,symbol,secid,name,cost,shares,alerts,added_at}]
    "settings": dict(DEFAULT_SETTINGS),
    "quotes": {},
    "advices": {},
    "news": {},
    "flows": {},
    "signals": [],            # [{ts,code,name,type,title,body}]
    "last_action": {},        # code -> action
    "last_alert": {},         # "code|type" -> ts
    "errors": {},
    "last_refresh": None,
    "trading": False,
}

# pywebview 窗口句柄（start.py 注入；纯浏览器模式下为 None）
WINDOW = None

subscribers = []             # SSE 订阅者队列
DEEP_CACHE = {}              # code -> {ts, kl, flows, news}
DEEP_TTL = 60                # K线/新闻/资金流缓存秒数（行情仍每 5 秒刷新）


# ---------------- 持久化 ----------------
def load_data():
    global ST
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                d = json.load(f)
            ST["watches"] = d.get("watches", [])
            ST["settings"].update(d.get("settings", {}))
            ST["signals"] = d.get("signals", [])[-100:]
            ST["last_action"] = d.get("last_action", {})
            log.info("已加载 %d 条监控", len(ST["watches"]))
        except Exception as e:  # noqa: BLE001
            log.warning("data.json 读取失败: %s", e)


def save_data():
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump({
                "watches": ST["watches"],
                "settings": ST["settings"],
                "signals": ST["signals"][-100:],
                "last_action": ST["last_action"],
            }, f, ensure_ascii=False, indent=1)
    except Exception as e:  # noqa: BLE001
        log.warning("data.json 保存失败: %s", e)


# ---------------- 工具 ----------------
def is_trading_hours(now=None):
    now = now or datetime.now()
    if now.weekday() >= 5:
        return False
    t = now.hour * 60 + now.minute
    return (9 * 60 + 15) <= t <= (11 * 60 + 30) or (13 * 60) <= t <= (15 * 60)


def notify_desktop(title, body):
    try:
        from plyer import notification
        notification.notify(title=title, message=body[:200],
                            app_name="盯盘小助手", timeout=8)
    except Exception as e:  # noqa: BLE001
        log.debug("桌面通知失败: %s", e)


def broadcast(event):
    data = json.dumps(event, ensure_ascii=False)
    for q in list(subscribers):
        try:
            q.put_nowait(data)
        except Exception:  # noqa: BLE001
            pass


# ---------------- 核心刷新 ----------------
def refresh_all_sync():
    watches = list(ST["watches"])
    if not watches:
        ST["last_refresh"] = datetime.now().isoformat(timespec="seconds")
        return
    symbols = [w["symbol"] for w in watches]
    try:
        quotes = sources.fetch_quotes(symbols)
    except Exception as e:  # noqa: BLE001
        log.warning("行情拉取失败: %s", e)
        quotes = {}
        ST["errors"]["quotes"] = str(e)

    # 并发拉取 K线/资金流/新闻（60 秒缓存，避免高频请求被限流）
    results = {}
    now0 = time.time()

    def fetch_extra(w):
        code = w["code"]
        cached = DEEP_CACHE.get(code)
        if cached and now0 - cached["ts"] < DEEP_TTL:
            results[code] = {"kl": cached["kl"], "flows": cached["flows"],
                             "news": cached["news"], "cached": True}
            return
        out = {}
        try:
            out["kl"] = sources.fetch_kline(w["symbol"])
        except Exception as e:  # noqa: BLE001
            out["kl"] = []
            log.warning("K线失败 %s: %s", w["symbol"], e)
        try:
            out["flows"] = sources.fetch_fund_flow(w["secid"])
        except Exception as e:  # noqa: BLE001
            out["flows"] = []
        try:
            out["news"] = sources.fetch_news(w["symbol"])
        except Exception as e:  # noqa: BLE001
            out["news"] = []
        results[code] = out
        DEEP_CACHE[code] = {"ts": time.time(), "kl": out["kl"],
                            "flows": out["flows"], "news": out["news"]}

    threads = [threading.Thread(target=fetch_extra, args=(w,), daemon=True) for w in watches]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)

    settings = ST["settings"]
    now_ts = time.time()

    for w in watches:
        code = w["code"]
        q = quotes.get(w["symbol"])
        if not q or not q.get("price"):
            ST["errors"][code] = "行情暂不可用"
            continue
        ST["errors"].pop(code, None)
        ST["quotes"][code] = q
        extra = results.get(code, {})
        ST["flows"][code] = extra.get("flows") or []
        news_items = extra.get("news") or []
        senti, pos, neg = engine.news_sentiment(news_items)
        ST["news"][code] = {"score": round(senti, 1), "items": news_items,
                            "pos": pos, "neg": neg}

        advice = engine.build_advice(q, extra.get("kl") or [], extra.get("flows") or [],
                                     news_items, w.get("cost", 0), w.get("shares", 0))
        ST["advices"][code] = advice

        # 信号生成与推送
        last_action = ST["last_action"].get(code)
        if w.get("alerts", True):
            for sig in engine.build_signals(code, q.get("name", code), advice, q, last_action):
                key = "%s|%s" % (code, sig["type"])
                if now_ts - ST["last_alert"].get(key, 0) < settings.get("cooldown", 1800):
                    continue
                ST["last_alert"][key] = now_ts
                record = dict(sig)
                record.update({"ts": datetime.now().isoformat(timespec="seconds"),
                               "code": code, "name": q.get("name", code),
                               "price": q.get("price")})
                ST["signals"].append(record)
                ST["signals"] = ST["signals"][-100:]
                log.info("信号 %s %s", code, sig["title"])
                broadcast({"type": "signal", "data": record})
                notify_desktop(sig["title"], sig["body"])
                if settings.get("push_enabled") and settings.get("push_key"):
                    try:
                        pushers.send(settings["push_channel"], settings["push_key"],
                                     sig["title"], sig["body"])
                    except Exception as e:  # noqa: BLE001
                        log.warning("微信推送失败: %s", e)
        ST["last_action"][code] = advice["action"]

    ST["last_refresh"] = datetime.now().isoformat(timespec="seconds")
    save_data()
    broadcast({"type": "state"})


async def market_loop():
    """全市场扫描：轻量榜单 5 秒一轮，全量 40 秒一轮"""
    while True:
        t0 = time.time()
        try:
            if time.time() - MARKET["last_full"] >= 40:
                t1 = time.time()
                rows = await asyncio.to_thread(market.scan_all)
                MARKET["rows"] = rows
                MARKET["stats"] = market.stats(rows)
                MARKET["last_full"] = time.time()
                MARKET["scan_ms"] = int((time.time() - t1) * 1000)
            gainers = await asyncio.to_thread(market.scan_rank, "changepercent", 3)
            amount_top = await asyncio.to_thread(market.scan_rank, "amount", 3)
            MARKET["gainers"] = [r for r in gainers
                                 if not r["name"].startswith(("N", "C"))][:25]
            MARKET["amount_top"] = amount_top[:25]
            MARKET["updated"] = datetime.now().isoformat(timespec="seconds")
            broadcast({"type": "market"})
        except Exception as e:  # noqa: BLE001
            log.warning("全市场扫描异常: %s", e)
        await asyncio.sleep(max(1.0, 5.0 - (time.time() - t0)))


async def reco_loop():
    """推荐榜：每 90 秒基于最新全市场快照重算"""
    await asyncio.sleep(8)   # 等第一轮全市场扫描完成
    while True:
        try:
            rows = MARKET["rows"]
            if not rows:
                await asyncio.sleep(3)
                continue
            if rows:
                idx = await asyncio.to_thread(get_index_closes)
                res = await asyncio.to_thread(recommender.build, rows, idx)
                all_recs = res["aggressive"] + res["steady"]
                await asyncio.to_thread(ai_review, all_recs)
                # 有 AI 复评的按综合分重排
                has_ai = any(r.get("final_score") is not None for r in all_recs)
                if has_ai:
                    res["aggressive"].sort(key=lambda x: (-(x.get("final_score") or x["score"]), -x["rr"]))
                    res["steady"].sort(key=lambda x: (-(x.get("final_score") or x["score"]), -x["rr"]))
                for r in all_recs:
                    r.pop("_ai_ctx", None)
                RECO.update({
                    "steady": res["steady"], "aggressive": res["aggressive"],
                    "pool": res["pool"], "hit": res["hit"],
                    "filtered": res.get("filtered", 0),
                    "scanned": len(rows),
                    "updated": datetime.now().isoformat(timespec="seconds"),
                })
                log.info("推荐榜更新：稳健 %d / 进攻 %d（候选 %d，过滤 %d）",
                         len(res["steady"]), len(res["aggressive"]),
                         res["pool"], res.get("filtered", 0))
                broadcast({"type": "reco"})
        except Exception as e:  # noqa: BLE001
            log.warning("推荐榜计算异常: %s", e)
        await asyncio.sleep(90)


async def monitor_loop():
    while True:
        try:
            await asyncio.to_thread(refresh_all_sync)
        except Exception as e:  # noqa: BLE001
            log.exception("刷新异常: %s", e)
        interval = ST["settings"].get("interval", 15)
        if ST["settings"].get("auto_hours") and not is_trading_hours():
            interval = max(interval * 4, 300)
        await asyncio.sleep(interval)


# ---------------- FastAPI ----------------
app = FastAPI(title="Stock Floater")

ACCESS_PASS = os.environ.get("FLOATER_PASS", "").strip()


@app.middleware("http")
async def auth_guard(request, call_next):
    """设置 FLOATER_PASS 后，所有 /api 请求需带 X-Floater-Pass 头"""
    if ACCESS_PASS and request.url.path.startswith("/api"):
        if request.headers.get("x-floater-pass", "") != ACCESS_PASS:
            from fastapi.responses import JSONResponse
            return JSONResponse({"error": "unauthorized", "need_pass": True}, status_code=401)
    return await call_next(request)
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])


@app.on_event("startup")
async def _startup():
    load_data()
    ST["trading"] = is_trading_hours()
    asyncio.create_task(monitor_loop())
    asyncio.create_task(market_loop())
    asyncio.create_task(reco_loop())
    log.info("盯盘小助手后端已启动，当前%s交易时段", "处于" if ST["trading"] else "不在")


@app.get("/api/state")
def api_state():
    with state_lock:
        watches = []
        for w in ST["watches"]:
            code = w["code"]
            q = ST["quotes"].get(code) or {}
            adv = ST["advices"].get(code) or {}
            item = dict(w)
            item["quote"] = q
            item["advice"] = adv
            item["news"] = ST["news"].get(code) or {}
            item["flows"] = ST["flows"].get(code) or []
            item["error"] = ST["errors"].get(code)
            if q.get("price") and w.get("cost") and w.get("shares"):
                price, cost, sh = q["price"], w["cost"], w["shares"]
                item["pnl"] = round((price - cost) * sh, 2)
                item["pnl_pct"] = round((price / cost - 1) * 100, 2)
                item["mv"] = round(price * sh, 2)
            watches.append(item)
        return {
            "watches": watches,
            "market": {
                "stats": MARKET["stats"],
                "gainers": MARKET["gainers"],
                "amount_top": MARKET["amount_top"],
                "updated": MARKET["updated"],
                "scan_ms": MARKET["scan_ms"],
            },
            "reco_meta": {
                "updated": RECO["updated"], "scanned": RECO["scanned"],
                "pool": RECO["pool"], "hit": RECO["hit"],
                "steady": len(RECO["steady"]), "aggressive": len(RECO["aggressive"]),
                "filtered": RECO.get("filtered", 0),
                "ai": RECO.get("ai", {}),
            },
            "settings": mask_settings(ST["settings"]),
            "signals": list(reversed(ST["signals"]))[:30],
            "last_refresh": ST["last_refresh"],
            "trading": is_trading_hours(),
            "server_time": datetime.now().isoformat(timespec="seconds"),
            "desktop_mode": WINDOW is not None,
        }


@app.post("/api/watch")
def api_add_watch(payload: dict = Body(...)):
    code = str(payload.get("code", "")).strip()
    try:
        symbol, secid, _ = sources.normalize_code(code)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    cost = float(payload.get("cost") or 0)
    shares = int(payload.get("shares") or 0)
    if cost < 0 or shares < 0:
        return {"ok": False, "error": "成本/持仓不能为负"}
    with state_lock:
        for w in ST["watches"]:
            if w["code"] == symbol:
                w.update({"cost": cost, "shares": shares,
                          "alerts": bool(payload.get("alerts", True))})
                save_data()
                return {"ok": True, "code": symbol}
        ST["watches"].append({
            "code": symbol, "symbol": symbol, "secid": secid,
            "name": "", "cost": cost, "shares": shares,
            "alerts": bool(payload.get("alerts", True)),
            "added_at": datetime.now().isoformat(timespec="seconds"),
        })
        save_data()
    return {"ok": True, "code": symbol}


@app.delete("/api/watch/{code}")
def api_del_watch(code: str):
    with state_lock:
        ST["watches"] = [w for w in ST["watches"] if w["code"] != code]
        ST["quotes"].pop(code, None)
        ST["advices"].pop(code, None)
        ST["news"].pop(code, None)
        ST["flows"].pop(code, None)
        ST["last_action"].pop(code, None)
        save_data()
    return {"ok": True}


@app.post("/api/settings")
def api_settings(payload: dict = Body(...)):
    with state_lock:
        for k, v in payload.items():
            if k not in ST["settings"]:
                continue
            if k in SENSITIVE_KEYS:
                # 留空或回传掩码 -> 保持原值不变
                if not v or "****" in str(v):
                    continue
            ST["settings"][k] = v
        save_data()
        return {"ok": True, "settings": mask_settings(ST["settings"])}


@app.post("/api/refresh")
async def api_refresh():
    await asyncio.to_thread(refresh_all_sync)
    return {"ok": True}


@app.post("/api/test-push")
def api_test_push():
    s = ST["settings"]
    if not s.get("push_key"):
        return {"ok": False, "error": "请先填写推送 Key"}
    try:
        pushers.test(s["push_channel"], s["push_key"])
        return {"ok": True}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


@app.post("/api/window")
def api_window(payload: dict = Body(...)):
    if WINDOW is None:
        return {"ok": False, "error": "当前为浏览器模式，置顶请在桌面窗口中使用"}
    try:
        if "on_top" in payload:
            WINDOW.on_top = bool(payload["on_top"])
        return {"ok": True, "on_top": bool(WINDOW.on_top)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


@app.post("/api/test-ai")
def api_test_ai(payload: dict = Body(...)):
    base = (payload.get("base_url") or ST["settings"].get("ai_base_url") or "").strip()
    key = (payload.get("api_key") or ST["settings"].get("ai_api_key") or "").strip()
    model = (payload.get("model") or ST["settings"].get("ai_model") or "").strip()
    if not key:
        return {"ok": False, "error": "请先填写 API Key"}
    if not base.startswith("http"):
        return {"ok": False, "error": "Base URL 需以 http(s):// 开头，例如 https://api.deepseek.com/v1"}
    try:
        client = ai.AIClient(base, key, model, timeout=35)
        client.test()
        return {"ok": True, "model": model}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:300]}


@app.get("/api/recommend")
def api_recommend():
    return RECO


@app.get("/api/market")
def api_market():
    return {
        "stats": MARKET["stats"], "gainers": MARKET["gainers"],
        "amount_top": MARKET["amount_top"], "updated": MARKET["updated"],
        "scan_ms": MARKET["scan_ms"],
    }


@app.get("/api/stream")
async def api_stream():
    q = asyncio.Queue()
    subscribers.append(q)

    async def gen():
        try:
            yield "data: {\"type\": \"hello\"}\n\n"
            while True:
                try:
                    data = await asyncio.wait_for(q.get(), timeout=20)
                    yield "data: %s\n\n" % data
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
        finally:
            if q in subscribers:
                subscribers.remove(q)

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.get("/")
def index():
    return FileResponse(os.path.join(BASE, "static", "index.html"))


app.mount("/static", StaticFiles(directory=os.path.join(BASE, "static")), name="static")
