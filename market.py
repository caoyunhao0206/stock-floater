# -*- coding: utf-8 -*-
"""
全市场扫描（新浪行情中心）
- 单页 80 只 / 0.05s，全市场 5400 只并发约 2.6s
- 支持按涨幅/成交额/换手排序拉榜单
"""
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor

import requests

log = logging.getLogger("floater.market")

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
LIST_URL = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/"
            "json_v2.php/Market_Center.getHQNodeData")
TIMEOUT = 8


def _num(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _page(page, num=80, sort="symbol", asc=1):
    try:
        r = requests.get(LIST_URL, params={
            "page": page, "num": num, "sort": sort, "asc": asc,
            "node": "hs_a", "symbol": "", "_s_r_a": "page",
        }, headers=UA, timeout=TIMEOUT)
        r.encoding = "utf-8"
        data = json.loads(r.text)
        return data or []
    except Exception:  # noqa: BLE001
        return []


def _norm(x):
    sym = x.get("symbol", "")
    if not (sym.startswith("sh") or sym.startswith("sz")):
        return None
    name = x.get("name", "")
    if not name or "ST" in name.upper() or "退" in name:
        return None
    price = _num(x.get("trade"))
    if price <= 0:
        return None
    return {
        "symbol": sym,
        "code": x.get("code", ""),
        "name": name,
        "price": price,
        "change": _num(x.get("pricechange")),
        "change_pct": _num(x.get("changepercent")),
        "open": _num(x.get("open")),
        "high": _num(x.get("high")),
        "low": _num(x.get("low")),
        "prev_close": _num(x.get("settlement")),
        "volume": _num(x.get("volume")),
        "amount": _num(x.get("amount")),
        "turnover": _num(x.get("turnoverratio")),
        "pe": _num(x.get("per")),
        "pb": _num(x.get("pb")),
        "mktcap": _num(x.get("mktcap")),   # 万元
        "nmc": _num(x.get("nmc")),         # 流通市值（万元）
        "time": x.get("ticktime", ""),
    }


def scan_all(max_pages=70, workers=12):
    """全市场快照（约 2-3 秒）"""
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=workers) as ex:
        pages = list(ex.map(lambda p: _page(p), range(1, max_pages + 1)))
    rows, seen = [], set()
    for p in pages:
        for x in p:
            n = _norm(x)
            if n and n["symbol"] not in seen:
                seen.add(n["symbol"])
                rows.append(n)
    log.info("全市场扫描 %d 只，耗时 %.2fs", len(rows), time.time() - t0)
    return rows


def scan_rank(sort="changepercent", pages=4, asc=0):
    """榜单快扫（涨幅/成交额/换手），4 页 = 320 只"""
    with ThreadPoolExecutor(max_workers=pages) as ex:
        data = list(ex.map(lambda p: _page(p, sort=sort, asc=asc), range(1, pages + 1)))
    out, seen = [], set()
    for p in data:
        for x in p:
            n = _norm(x)
            if n and n["symbol"] not in seen:
                seen.add(n["symbol"])
                out.append(n)
    return out


def stats(rows):
    """市场情绪统计"""
    if not rows:
        return {}
    up = sum(1 for r in rows if r["change_pct"] > 0)
    down = sum(1 for r in rows if r["change_pct"] < 0)
    flat = len(rows) - up - down
    limit_up = sum(1 for r in rows if r["change_pct"] >= 9.7)
    limit_down = sum(1 for r in rows if r["change_pct"] <= -9.7)
    amt = sum(r["amount"] for r in rows)
    avg_chg = sum(r["change_pct"] for r in rows) / len(rows)
    return {
        "total": len(rows),
        "up": up, "down": down, "flat": flat,
        "limit_up": limit_up, "limit_down": limit_down,
        "avg_change": round(avg_chg, 2),
        "amount": round(amt / 1e8, 1),          # 亿元
        "breadth": round(up / len(rows) * 100, 1),
    }
