# -*- coding: utf-8 -*-
"""
免费公开数据源（无需 API Key）
- 实时行情：腾讯财经 qt.gtimg.cn
- 前复权日K：腾讯 web.ifzq.gtimg.cn
- 主力资金流：东方财富 push2
- 个股新闻：新浪财经
"""
import json
import re
import time
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import requests

log = logging.getLogger("floater.sources")

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
TIMEOUT = (5, 8)


def normalize_code(code):
    """用户输入 -> (symbol, secid, market)。当前版本仅支持沪深 A 股。"""
    code = re.sub(r"\D", "", str(code or ""))
    if not code:
        raise ValueError("代码不能为空")
    if len(code) == 6 and code[0] in "69":
        return "sh" + code, "1." + code, "SH"
    if len(code) == 6 and code[0] in "023":
        return "sz" + code, "0." + code, "SZ"
    raise ValueError("仅支持 6 位沪深股票代码（如 600519 / 000001）")


def _get(url, params=None, encoding="utf-8", retry=1):
    last = None
    for _ in range(retry + 1):
        try:
            r = requests.get(url, params=params, headers=UA, timeout=TIMEOUT)
            r.raise_for_status()
            r.encoding = encoding
            return r
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(0.6)
    raise last


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def fetch_quotes(symbols):
    """批量实时行情。symbols: ['sh600519', 'sz000001']"""
    if not symbols:
        return {}
    url = "https://qt.gtimg.cn/q=" + ",".join(symbols)
    text = _get(url, encoding="gbk").text
    out = {}
    for m in re.finditer(r'v_([\w]+)="([^"]*)"', text):
        sym = m.group(1)
        f = m.group(2).split("~")
        if len(f) < 50:
            continue
        out[sym] = {
            "name": f[1],
            "code": f[2],
            "price": _num(f[3]),
            "prev_close": _num(f[4]),
            "open": _num(f[5]),
            "volume": _num(f[6]),          # 手
            "time": f[30],
            "change": _num(f[31]),
            "change_pct": _num(f[32]),
            "high": _num(f[33]),
            "low": _num(f[34]),
            "amount": _num(f[37]),         # 万元
            "turnover": _num(f[38]),       # 换手率 %
            "pe": _num(f[39]),
            "amplitude": _num(f[43]),
            "circ_mv": _num(f[44]),        # 流通市值（亿）
            "total_mv": _num(f[45]),       # 总市值（亿）
            "pb": _num(f[46]),
            "limit_up": _num(f[47]),
            "limit_down": _num(f[48]),
            "volume_ratio": _num(f[49]),   # 量比
        }
    return out


def fetch_kline(symbol, count=320):
    """日K（新浪财经，未复权）-> [{date, open, close, high, low, volume}]"""
    url = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           "CN_MarketData.getKLineData")
    r = _get(url, params={"symbol": symbol, "scale": "240",
                          "ma": "no", "datalen": count})
    rows = r.json()
    kl = []
    for row in rows:
        try:
            kl.append({
                "date": row["day"],
                "open": float(row["open"]),
                "close": float(row["close"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "volume": float(row.get("volume") or 0),
            })
        except (TypeError, ValueError, KeyError):
            continue
    return kl


# 资金流接口一旦连续失败就整轮停用，避免每只股票都白等一次超时（每轮省几十秒）
_FF_FAIL = {"n": 0, "off": False}


def fetch_fund_flow(secid, days=5):
    """东方财富主力资金流（近 N 日）；接口不可用时返回 []，由引擎降级为量价评分"""
    if _FF_FAIL["off"]:
        return []
    url = "https://push2.eastmoney.com/api/qt/stock/fflow/daykline/get"
    params = {
        "secid": secid, "lmt": days, "klt": 101,
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65",
    }
    try:
        r = _get(url, params=params)
        klines = (r.json().get("data") or {}).get("klines") or []
    except Exception:  # noqa: BLE001
        _FF_FAIL["n"] += 1
        if _FF_FAIL["n"] >= 3:
            _FF_FAIL["off"] = True
        return []
    _FF_FAIL["n"] = 0
    out = []
    for row in klines:
        f = row.split(",")
        if len(f) < 6:
            continue
        out.append({
            "date": f[0],
            "main": _num(f[1]) or 0.0,    # 主力净流入（元）
            "small": _num(f[2]) or 0.0,
            "mid": _num(f[3]) or 0.0,
            "big": _num(f[4]) or 0.0,
            "xbig": _num(f[5]) or 0.0,
            "main_pct": _num(f[6]) or 0.0,
        })
    return out


NEWS_PAT = re.compile(
    r"(\d{4}-\d{2}-\d{2})(?:&nbsp;|\s)*\d{2}:\d{2}(?:&nbsp;|\s)*"
    r"<a[^>]*href=['\"]([^'\"]+)['\"][^>]*>([^<]{6,})</a>")


def fetch_news(symbol, limit=8, name=""):
    """新浪个股新闻：只取 datelist 容器，并按公司名过滤无关泛财经"""
    url = ("https://vip.stock.finance.sina.com.cn/corp/go.php/"
           "vCB_AllNewsStock/symbol/%s.phtml" % symbol)
    try:
        r = _get(url, encoding="gb18030")
    except Exception:  # noqa: BLE001
        return []
    h = r.text
    i = h.find("datelist")
    seg = h[i:i + 40000] if i > 0 else h
    short = ""
    if name:
        short = name.replace("股份", "").replace("集团", "").replace("科技", "").replace("控股", "")[:4]
    items = []
    pat = re.compile(r"(\d{4}-\d{2}-\d{2})&nbsp;(\d{2}:\d{2})&nbsp;&nbsp;"
                     r"<a[^>]+href=['\"]([^'\"]+)['\"][^>]*>([^<]{6,})</a>")
    for mm in pat.finditer(seg):
        date, _, href, title = mm.groups()
        title = title.strip()
        if name and (name not in title) and (not short or short not in title):
            continue          # 与该公司无关，丢弃
        if href.startswith("/"):
            href = "https://vip.stock.finance.sina.com.cn" + href
        items.append({"title": title, "url": href, "date": date})
        if len(items) >= limit:
            break
    return items


# ================= 多源新闻 + 交叉验证 =================

ANN_URL = "https://np-anotice-stock.eastmoney.com/api/security/ann"
ROLL_URL = "https://feed.mix.sina.com.cn/api/roll/get"


def fetch_announcements(code, limit=5):
    """东方财富·上市公司公告（官方披露，最高可信度）"""
    params = {
        "sr": "-1", "page_size": str(limit), "page_index": "1",
        "ann_type": "A", "client_source": "web", "stock_list": code,
        "f_node": "0", "s_node": "0",
    }
    try:
        r = _get(ANN_URL, params=params)
        items = ((r.json().get("data") or {}).get("list")) or []
    except Exception:  # noqa: BLE001
        return []
    out = []
    for it in items:
        art = it.get("art_code", "")
        out.append({
            "title": it.get("title", ""),
            "url": "https://data.eastmoney.com/notices/detail/%s/%s.html" % (code, art),
            "date": (it.get("notice_date") or "")[:10],
            "source": "公告",
            "weight": 2.0,
        })
    return out


def fetch_roll_news(keyword, limit=6):
    """新浪财经 7x24 快讯，按公司名过滤（时效性最强）"""
    try:
        r = _get(ROLL_URL, params={"pageid": "153", "lid": "2516",
                                   "num": "40", "page": "1"})
        data = ((r.json().get("result") or {}).get("data")) or []
    except Exception:  # noqa: BLE001
        return []
    out = []
    key = keyword.strip()
    for it in data:
        title = it.get("title", "")
        if key and key not in title and len(key) > 2 and key[1:] not in title:
            continue
        out.append({
            "title": title,
            "url": it.get("url", ""),
            "date": (it.get("ctime") or "")[:10],
            "source": "7x24",
            "weight": 1.2,
        })
        if len(out) >= limit:
            break
    return out


def _sim(a, b):
    """标题相似度（字符 bigram 重合率），用于多源交叉验证"""
    a = re.sub(r"[^\u4e00-\u9fa5A-Za-z0-9]", "", a)
    b = re.sub(r"[^\u4e00-\u9fa5A-Za-z0-9]", "", b)
    if len(a) < 4 or len(b) < 4:
        return 0.0
    sa = {a[i:i + 2] for i in range(len(a) - 1)}
    sb = {b[i:i + 2] for i in range(len(b) - 1)}
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / min(len(sa), len(sb))


def fetch_news_multi(symbol, code, name, limit=8):
    """兼容入口：实际走五源合并版本"""
    return fetch_news_multi_v2(symbol, code, name, limit)


# ================= 机构面数据：研报 / 龙虎榜 / 业绩预告 / 指数 =================

REPORT_URL = "https://reportapi.eastmoney.com/report/list"
DC_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"


def fetch_reports(code, days=90):
    """近 N 天机构研报：数量、评级分布、目标价"""
    import datetime as _dt
    end = _dt.date.today()
    begin = end - _dt.timedelta(days=days)
    params = {
        "pageSize": "30", "pageNo": "1", "qType": "0", "code": code,
        "industryCode": "*", "industry": "*", "rating": "", "ratingChange": "",
        "beginTime": begin.isoformat(), "endTime": end.isoformat(),
        "fields": "", "p": "1", "pageNum": "1",
    }
    try:
        r = _get(REPORT_URL, params=params)
        data = r.json().get("data") or []
    except Exception:  # noqa: BLE001
        return {}
    if not data:
        return {}
    ratings, targets, items = {}, [], []
    for it in data[:20]:
        rating = (it.get("emRatingName") or it.get("sRatingName") or "").strip()
        if rating:
            ratings[rating] = ratings.get(rating, 0) + 1
        tp = it.get("indvAimPriceT") or it.get("indvAimPriceL")
        try:
            if tp:
                targets.append(float(tp))
        except (TypeError, ValueError):
            pass
        items.append({
            "title": it.get("title", ""),
            "org": it.get("orgSName", ""),
            "rating": rating,
            "date": (it.get("publishDate") or "")[:10],
            "url": "https://data.eastmoney.com/report/info/%s.html" % it.get("infoCode", ""),
        })
    return {
        "count": len(data),
        "ratings": ratings,
        "max_target": round(max(targets), 2) if targets else None,
        "avg_target": round(sum(targets) / len(targets), 2) if targets else None,
        "items": items[:5],
    }


def fetch_billboard(code, days=30):
    """近 N 天龙虎榜上榜情况"""
    import datetime as _dt
    begin = (_dt.date.today() - _dt.timedelta(days=days)).isoformat()
    flt = '(SECURITY_CODE="%s")(TRADE_DATE>=\'%s\')' % (code, begin)
    params = {
        "reportName": "RPT_DAILYBILLBOARD_DETAILSNEW", "columns": "ALL",
        "pageNumber": "1", "pageSize": "20", "source": "WEB", "client": "WEB",
        "filter": flt, "sortColumns": "TRADE_DATE", "sortTypes": "-1",
    }
    try:
        r = _get(DC_URL, params=params)
        data = ((r.json().get("result") or {}).get("data")) or []
    except Exception:  # noqa: BLE001
        return {}
    if not data:
        return {"count": 0, "net": 0.0, "items": []}
    net = 0.0
    items = []
    for it in data:
        v = it.get("NET_BUY_AMT") or it.get("BILLBOARD_NET_AMT") or 0
        try:
            net += float(v)
        except (TypeError, ValueError):
            pass
        items.append({
            "date": (it.get("TRADE_DATE") or "")[:10],
            "explain": it.get("EXPLAIN", ""),
            "amount": it.get("BILLBOARD_DEAL_AMT"),
        })
    return {"count": len(data), "net": round(net / 1e8, 2), "items": items[:3]}


def fetch_forecast(code):
    """最新业绩预告"""
    params = {
        "reportName": "RPT_PUBLIC_OP_NEWPREDICT", "columns": "ALL",
        "pageNumber": "1", "pageSize": "5", "source": "WEB", "client": "WEB",
        "filter": '(SECURITY_CODE="%s")' % code,
        "sortColumns": "NOTICE_DATE", "sortTypes": "-1",
    }
    try:
        r = _get(DC_URL, params=params)
        data = ((r.json().get("result") or {}).get("data")) or []
    except Exception:  # noqa: BLE001
        return {}
    if not data:
        return {}
    it = data[0]
    kind = (it.get("PREDICT_TYPE") or "")
    low, up = it.get("ADD_AMP_LOWER"), it.get("ADD_AMP_UPPER")
    content = (it.get("PREDICT_CONTENT") or "").replace("\r", "").replace("\n", "")
    txt = kind or "业绩预告"
    if low is not None and up is not None:
        txt += " %.0f%%~%.0f%%" % (low, up)
    elif low is not None:
        txt += " %.0f%%以上" % low
    positive = any(k in content for k in ("增长", "预增", "扭亏", "盈利", "上升", "提升"))
    negative = any(k in content for k in ("下降", "亏损", "预减", "下滑", "减少"))
    return {
        "date": (it.get("NOTICE_DATE") or "")[:10],
        "summary": txt[:80],
        "content": content[:160],
        "type": "pos" if positive and not negative else ("neg" if negative else "neutral"),
    }


def fetch_index_kline(symbol="sh000001", count=60):
    """指数日K（计算相对强度用）"""
    try:
        return fetch_kline(symbol, count)
    except Exception:  # noqa: BLE001
        return []


# ================= 新闻源扩容：证券时报 / 新浪多频道 / 板块 =================

STCN_URL = "https://www.stcn.com/article/list/yw.html"
SINA_ROLL = "https://feed.mix.sina.com.cn/api/roll/get"
SECTOR_URL = "https://proxy.finance.qq.com/cgi/cgi-bin/rank/pt/getRank"


def fetch_stcn(limit=40):
    """证券时报·要闻（权威财经媒体）"""
    try:
        r = _get(STCN_URL, encoding="utf-8")
    except Exception:  # noqa: BLE001
        return []
    out = []
    for href, title in re.findall(
            r'<a[^>]+href="(/article/detail/\d+\.html)"[^>]*>([^<]{8,70})</a>', r.text):
        t = title.strip()
        if len(t) < 8:
            continue
        out.append({
            "title": t,
            "url": "https://www.stcn.com" + href,
            "date": "",
            "source": "证券时报",
            "weight": 1.5,
        })
        if len(out) >= limit:
            break
    return out


def fetch_sina_roll_multi(limit=40):
    """新浪滚动新闻（股市 2517 / 公司 2511 / 宏观 2510）"""
    out = []
    for lid in ("2517", "2511", "2510"):
        try:
            r = _get(SINA_ROLL, params={"pageid": "153", "lid": lid, "num": "30", "page": "1"})
            data = ((r.json().get("result") or {}).get("data")) or []
        except Exception:  # noqa: BLE001
            continue
        for it in data:
            title = (it.get("title") or "").strip()
            if len(title) < 8:
                continue
            out.append({
                "title": title,
                "url": it.get("url", ""),
                "date": (it.get("ctime") or "")[:10],
                "source": "新浪滚动",
                "weight": 1.1,
            })
    return out[:limit]


def fetch_sectors(sort="price", count=60):
    """
    腾讯行业板块（申万一级共 31 个），拉全量后本地排序
    sort: price 涨跌幅 / zljlr 主力净流入 / turnover 换手
    金额单位：接口返回万元，统一换算为亿元
    """
    try:
        r = _get(SECTOR_URL, params={
            "board_type": "hy", "sort_type": "price", "direct": "up",
            "offset": "0", "count": str(count),
        })
        data = (r.json().get("data") or {}).get("rank_list") or []
    except Exception:  # noqa: BLE001
        return []
    out = []
    for x in data:
        try:
            out.append({
                "name": x.get("name", ""),
                "code": x.get("code", ""),
                "change_pct": round(float(x.get("zdf") or 0), 2),
                "turnover": round(float(x.get("hsl") or 0), 2),
                "amount": round(float(x.get("turnover") or 0) / 1e8, 1),
                "main_in": round(float(x.get("zljlr") or 0) / 1e4, 2),
                "main_in5": round(float(x.get("zljlr_d5") or 0) / 1e4, 2),
                "chg5": round(float(x.get("zdf_d5") or 0), 2),
                "chg20": round(float(x.get("zdf_d20") or 0), 2),
                "vol_ratio": round(float(x.get("lb") or 0), 2),
                "lead": (x.get("lzg") or {}).get("name", "") if isinstance(x.get("lzg"), dict) else "",
                "lead_pct": round(float(((x.get("lzg") or {}).get("zdf")) or 0), 2)
                             if isinstance(x.get("lzg"), dict) else 0,
            })
        except (TypeError, ValueError):
            continue
    if sort == "zljlr":
        out.sort(key=lambda a: -a["main_in"])
    elif sort == "turnover":
        out.sort(key=lambda a: -a["turnover"])
    else:
        out.sort(key=lambda a: -a["change_pct"])
    return out


# 证券时报 / 新浪滚动是「全市场列表」，不是按股票拉的。
# 68 只候选逐只拉一遍 = 白跑 270 次跨境请求，所以按 2 分钟做一次全局缓存。
_GLOBAL_NEWS = {"t": 0.0, "stcn": [], "sina": []}
_GLOBAL_NEWS_LOCK = threading.Lock()


def _global_news():
    now = time.time()
    if now - _GLOBAL_NEWS["t"] < 120:
        return _GLOBAL_NEWS["stcn"], _GLOBAL_NEWS["sina"]
    with _GLOBAL_NEWS_LOCK:
        if time.time() - _GLOBAL_NEWS["t"] < 120:
            return _GLOBAL_NEWS["stcn"], _GLOBAL_NEWS["sina"]
        with ThreadPoolExecutor(max_workers=2) as ex:
            f1 = ex.submit(fetch_stcn)
            f2 = ex.submit(fetch_sina_roll_multi)
            try:
                _GLOBAL_NEWS["stcn"] = f1.result() or []
            except Exception:  # noqa: BLE001
                _GLOBAL_NEWS["stcn"] = []
            try:
                _GLOBAL_NEWS["sina"] = f2.result() or []
            except Exception:  # noqa: BLE001
                _GLOBAL_NEWS["sina"] = []
        _GLOBAL_NEWS["t"] = time.time()
    return _GLOBAL_NEWS["stcn"], _GLOBAL_NEWS["sina"]


def fetch_news_multi_v2(symbol, code, name, limit=10):
    """
    五源合并：公告(2.0) + 证券时报(1.5) + 7x24(1.2) + 新浪滚动(1.1) + 新浪个股(1.0)
    同一事件跨源命中 -> verified
    """
    pool = []
    pool += fetch_announcements(code, 5)
    for it in fetch_news(symbol, 10, name):
        it = dict(it)
        it["source"] = "新浪个股"
        it["weight"] = 1.0
        pool.append(it)
    pool += fetch_roll_news(name, 8)
    short = name.replace("股份", "").replace("集团", "").replace("科技", "")[:4] if name else ""

    stcn_items, sina_items = _global_news()
    for items, key in ((stcn_items, name), (sina_items, name)):
        for it in items:
            title = it.get("title", "")
            hit = (key and key in title) or (short and len(short) >= 2 and short in title)
            if hit:
                it = dict(it)          # 拷贝，别把 source/weight 写进缓存
                pool.append(it)

    merged = []
    for it in pool:
        if not it.get("title"):
            continue
        hit = None
        for m in merged:
            if _sim(it["title"], m["title"]) >= 0.5:
                hit = m
                break
        if hit:
            srcs = set(hit.get("sources") or [hit.get("source")])
            srcs.add(it.get("source"))
            hit["sources"] = list(srcs)
            hit["verified"] = len(srcs) >= 2
            hit["weight"] = max(hit.get("weight", 1.0), it.get("weight", 1.0)) + 0.3
            if len(it["title"]) > len(hit["title"]):
                hit["title"] = it["title"]
        else:
            it["sources"] = [it.get("source")]
            it["verified"] = False
            merged.append(it)

    merged.sort(key=lambda x: (x.get("date", ""),
                               1 if x.get("weight", 0) >= 2 else 0,
                               1 if x.get("verified") else 0), reverse=True)
    merged = merged[:limit]
    senti, pos, neg = __import__("engine").news_sentiment(merged)
    return {
        "items": merged,
        "score": round(senti, 1),
        "pos": pos,
        "neg": neg,
        "verified": sum(1 for x in merged if x.get("verified")),
        "sources": sorted({s for x in merged for s in (x.get("sources") or [])}),
    }


# ================= 筹码与事件数据 =================

def fetch_holder_num(code):
    """股东户数（季度披露）：户数下降=筹码集中"""
    params = {
        "reportName": "RPT_HOLDERNUMLATEST", "columns": "ALL",
        "pageNumber": "1", "pageSize": "3", "source": "WEB", "client": "WEB",
        "filter": '(SECURITY_CODE="%s")' % code,
        "sortColumns": "END_DATE", "sortTypes": "-1",
    }
    try:
        r = _get(DC_URL, params=params)
        data = ((r.json().get("result") or {}).get("data")) or []
    except Exception:  # noqa: BLE001
        return {}
    if not data:
        return {}
    it = data[0]
    try:
        num = int(it.get("HOLDER_NUM") or 0)
        prev = int(it.get("PRE_HOLDER_NUM") or 0)
        ratio = float(it.get("HOLDER_NUM_RATIO") or 0)
    except (TypeError, ValueError):
        return {}
    return {
        "date": (it.get("END_DATE") or "")[:10],
        "num": num,
        "prev": prev,
        "ratio": round(ratio, 2),          # 户数变化率，负=集中
        "avg_hold": round(it.get("AVG_HOLD_NUM") or 0, 0) if it.get("AVG_HOLD_NUM") else None,
        "signal": ("筹码集中" if ratio <= -5 else
                   "筹码分散" if ratio >= 5 else "基本稳定"),
    }


def fetch_block_trade(code, days=60):
    """近 N 日大宗交易：溢价成交偏多说明机构愿意接"""
    import datetime as _dt
    begin = (_dt.date.today() - _dt.timedelta(days=days)).isoformat()
    params = {
        "reportName": "RPT_DATA_BLOCKTRADE", "columns": "ALL",
        "pageNumber": "1", "pageSize": "20", "source": "WEB", "client": "WEB",
        "filter": '(SECURITY_CODE="%s")(TRADE_DATE>=\'%s\')' % (code, begin),
        "sortColumns": "TRADE_DATE", "sortTypes": "-1",
    }
    try:
        r = _get(DC_URL, params=params)
        data = ((r.json().get("result") or {}).get("data")) or []
    except Exception:  # noqa: BLE001
        return {}
    if not data:
        return {"count": 0}
    prem, items, vol = [], [], 0.0
    for it in data:
        try:
            p = float(it.get("PREMIUM_RATIO") or 0)
            prem.append(p)
            vol += float(it.get("DEAL_VOLUME") or 0)
        except (TypeError, ValueError):
            pass
        if len(items) < 3:
            items.append({
                "date": (it.get("TRADE_DATE") or "")[:10],
                "price": it.get("DEAL_PRICE"),
                "premium": it.get("PREMIUM_RATIO"),
                "volume": it.get("DEAL_VOLUME"),
            })
    avg = round(sum(prem) / len(prem), 2) if prem else 0
    return {
        "count": len(data),
        "avg_premium": avg,
        "total_vol": round(vol, 0),
        "items": items,
        "signal": ("溢价成交" if avg >= 2 else "折价成交" if avg <= -5 else "平价成交"),
    }


# ================= LOF 套利 =================

FUND_LIST_URL = "http://fund.eastmoney.com/js/fundcode_search.js"
FUND_NAV_URL = "https://api.fund.eastmoney.com/f10/lsjz"
_FUND_POOL = {"ts": 0, "codes": []}


def _fund_codes():
    """全量基金代码表中的场内候选（16/50 开头），缓存 12 小时"""
    if _FUND_POOL["codes"] and time.time() - _FUND_POOL["ts"] < 43200:
        return _FUND_POOL["codes"]
    try:
        r = _get(FUND_LIST_URL, encoding="utf-8")
        m = re.search(r"\[\[.*\]\]", r.text, re.S)
        if not m:
            return _FUND_POOL["codes"]
        arr = json.loads(m.group(0))
    except Exception:  # noqa: BLE001
        return _FUND_POOL["codes"]
    codes = [x[0] for x in arr
             if isinstance(x, list) and x and re.match(r"^(16|50)\d{4}$", str(x[0]))]
    _FUND_POOL["codes"] = codes
    _FUND_POOL["ts"] = time.time()
    return codes


def fetch_lof_spot(min_amount=50.0):
    """
    LOF 场内行情（成交额 ≥ min_amount 万元）
    返回 [{code, name, price, change_pct, amount(万), volume}]
    """
    codes = _fund_codes()
    if not codes:
        return []
    syms = [("sh" if c.startswith("5") else "sz") + c for c in codes]
    out = []
    for i in range(0, len(syms), 60):
        batch = syms[i:i + 60]
        try:
            r = _get("https://qt.gtimg.cn/q=" + ",".join(batch), encoding="gbk")
        except Exception:  # noqa: BLE001
            continue
        for mm in re.finditer(r'v_(\w+)="([^"]*)"', r.text):
            f = mm.group(2).split("~")
            if len(f) < 40 or not f[1]:
                continue
            name = f[1]
            is_fund = (f[0] == "51") or ("LOF" in name) or ("基金" in name)
            if not is_fund:
                continue
            try:
                price = float(f[3])
                pct = float(f[32]) if f[32] else 0.0
                amount = float(f[37]) if f[37] else 0.0
            except (TypeError, ValueError):
                continue
            if price <= 0 or amount < min_amount:
                continue
            out.append({
                "code": mm.group(1)[2:],
                "name": name,
                "price": price,
                "change_pct": round(pct, 2),
                "amount": round(amount, 0),      # 万元
            })
    out.sort(key=lambda x: -x["amount"])
    return out


def fetch_fund_nav(code):
    """
    最新单位净值 + 申购/赎回状态
    返回 {date, nav, nav_chg, sg, sh, sg_ok}
    """
    h = dict(UA)
    h["Referer"] = "https://fund.eastmoney.com/"
    try:
        r = requests.get(FUND_NAV_URL, params={"fundCode": code, "pageIndex": "1", "pageSize": "1"},
                         headers=h, timeout=TIMEOUT)
        j = json.loads(r.text)
        data = ((j.get("Data") or {}).get("LSJZList")) or []
    except Exception as e:  # noqa: BLE001
        log.debug("净值获取失败 %s: %s", code, e)
        return {}
    if not data:
        return {}
    d = data[0]
    try:
        nav = float(d.get("DWJZ") or 0)
    except (TypeError, ValueError):
        return {}
    if nav <= 0:
        return {}
    sg = (d.get("SGZT") or "").strip()
    sh = (d.get("SHZT") or "").strip()
    return {
        "date": (d.get("FSRQ") or "")[:10],
        "nav": nav,
        "nav_chg": round(float(d.get("JZZZL") or 0), 2),
        "sg": sg,
        "sh": sh,
        "sg_ok": ("暂停" not in sg) and bool(sg),
        "sh_ok": ("暂停" not in sh) and bool(sh),
    }


def build_lof_arbitrage(min_amount=100.0, max_check=80, workers=6):
    """
    LOF 套利扫描：溢价率 = 场内价 / 最新净值 - 1
    仅对成交额达标的标的查净值
    """
    spots = fetch_lof_spot(min_amount)
    if not spots:
        return {"items": [], "updated": "", "scanned": 0}
    targets = spots[:max_check]

    def _one(s):
        nav = fetch_fund_nav(s["code"])
        if not nav:
            return None
        premium = (s["price"] / nav["nav"] - 1) * 100
        return {
            "code": s["code"], "name": s["name"],
            "price": s["price"], "change_pct": s["change_pct"],
            "amount": s["amount"],
            "nav": nav["nav"], "nav_date": nav["date"], "nav_chg": nav["nav_chg"],
            "premium": round(premium, 2),
            "sg": nav["sg"], "sh": nav["sh"],
            "sg_ok": nav["sg_ok"], "sh_ok": nav["sh_ok"],
        }

    items = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for it in ex.map(_one, targets):
            if it:
                items.append(it)
    items.sort(key=lambda x: -x["premium"])
    return {
        "items": items,
        "scanned": len(spots),
        "updated": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }


# ================= 可转债 =================

CB_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
CB_QUOTE = ("f2~01~CONVERT_STOCK_CODE~CONVERT_STOCK_PRICE,"
            "f235~10~SECURITY_CODE~TRANSFER_PRICE,"
            "f236~10~SECURITY_CODE~TRANSFER_VALUE,"
            "f237~10~SECURITY_CODE~TRANSFER_PREMIUM_RATIO")

RATING_SCORE = {
    "AAA": 10, "AA+": 9, "AA": 7, "AA-": 5,
    "A+": 3, "A": 2, "A-": 1,
    "BBB": 1, "BB": 0, "B": 0, "CCC": 0, "CC": 0, "C": 0, "D": 0,
}
LOW_RATING = {"BB", "B", "CCC", "CC", "C", "D"}


def _cb_rows():
    """东财可转债基础表（含转股价/正股价/转股价值），返回未退市的"""
    out = []
    for page in (1, 2, 3):
        p = {"reportName": "RPT_BOND_CB_LIST", "columns": "ALL",
             "pageNumber": page, "pageSize": 500, "source": "WEB", "client": "WEB",
             "sortColumns": "LISTING_DATE", "sortTypes": "-1",
             "quoteColumns": CB_QUOTE, "quoteType": "1", "quote": "1"}
        try:
            r = requests.get(CB_URL, params=p,
                             headers={**UA, "Referer": "https://data.eastmoney.com/kzz/"},
                             timeout=(5, 15))
            data = ((r.json().get("result") or {}).get("data")) or []
        except Exception as e:  # noqa: BLE001
            log.debug("可转债列表失败: %s", e)
            break
        out += data
        if len(data) < 500:
            break
    return [x for x in out if not x.get("DELIST_DATE")]


def fetch_cb_list(min_amount=100.0):
    """
    可转债实时列表
    转债价格取腾讯行情，溢价率用「转债价 / 转股价值 - 1」重算（东财的转股价值是实时的）
    """
    rows = _cb_rows()
    if not rows:
        return []
    codes = [str(x.get("SECURITY_CODE")) for x in rows if x.get("SECURITY_CODE")]
    syms = [("sh" if c.startswith("11") else "sz") + c for c in codes]
    quotes = {}
    for i in range(0, len(syms), 60):
        try:
            r = _get("https://qt.gtimg.cn/q=" + ",".join(syms[i:i + 60]), encoding="gbk")
        except Exception:  # noqa: BLE001
            continue
        for mm in re.finditer(r'v_(\w+)="([^"]*)"', r.text):
            f = mm.group(2).split("~")
            if len(f) < 40 or not f[1]:
                continue
            try:
                quotes[mm.group(1)[2:]] = {
                    "price": float(f[3]),
                    "pct": float(f[32]) if f[32] else 0.0,
                    "amount": float(f[37]) if f[37] else 0.0,
                }
            except (TypeError, ValueError):
                continue

    out = []
    for x in rows:
        code = str(x.get("SECURITY_CODE"))
        q = quotes.get(code)
        if not q or q["price"] <= 0 or q["amount"] < min_amount:
            continue
        tv = x.get("TRANSFER_VALUE")
        tp = x.get("TRANSFER_PRICE")
        sp = x.get("CONVERT_STOCK_PRICE")
        try:
            tv = float(tv) if tv not in (None, "-", "") else 0.0
            tp = float(tp) if tp not in (None, "-", "") else 0.0
            sp = float(sp) if sp not in (None, "-", "") else 0.0
        except (TypeError, ValueError):
            continue
        if tv <= 0 or tp <= 0:
            continue
        premium = (q["price"] / tv - 1) * 100
        # 强赎触发线：正股价 ≥ 转股价 × 1.3
        redeem_ratio = (sp / tp) if tp > 0 else 0
        expire = (x.get("EXPIRE_DATE") or "")[:10]
        years = None
        try:
            if expire:
                import datetime as _dt
                d = _dt.date.fromisoformat(expire)
                years = round((d - _dt.date.today()).days / 365.0, 2)
        except Exception:  # noqa: BLE001
            pass
        coupons = _parse_coupons(x.get("INTEREST_RATE_EXPLAIN"))
        redeem_px = _parse_redeem_price(x.get("REDEEM_CLAUSE"))
        floor = bond_floor_value(coupons, redeem_px, years)
        out.append({
            "code": code,
            "name": x.get("SECURITY_NAME_ABBR") or "",
            "bond_floor": floor,
            "bond_premium": round((q["price"] / floor - 1) * 100, 2) if floor else None,
            "price": round(q["price"], 2),
            "change_pct": round(q["pct"], 2),
            "amount": round(q["amount"], 0),
            "stock_code": str(x.get("CONVERT_STOCK_CODE") or ""),
            "stock_price": round(sp, 2),
            "transfer_price": round(tp, 2),
            "transfer_value": round(tv, 2),
            "premium": round(premium, 2),
            "double_low": round(q["price"] + premium, 1),
            "redeem_ratio": round(redeem_ratio, 3),
            "rating": (x.get("RATING") or "").strip(),
            "scale": round(float(x.get("ACTUAL_ISSUE_SCALE") or 0), 2),
            "expire": expire,
            "years": years,
        })
    out.sort(key=lambda a: -a["amount"])
    return out


def score_cb(cb):
    """
    可转债评分 0~100
    价格 25 + 转股溢价 25 + 双低 15 + 债底保护 10 + 正股趋势 10 + 评级 10 + 年限 3 + 流动 2
    """
    s = 0.0
    p = cb["price"]
    if p <= 105:
        s += 25
    elif p <= 115:
        s += 20
    elif p <= 125:
        s += 13
    elif p <= 140:
        s += 6
    prem = cb["premium"]
    if prem <= 5:
        s += 25
    elif prem <= 15:
        s += 20
    elif prem <= 30:
        s += 13
    elif prem <= 50:
        s += 6
    else:
        s += 2
    dl = cb["double_low"]
    if dl <= 130:
        s += 15
    elif dl <= 150:
        s += 10
    elif dl <= 170:
        s += 5
    else:
        s += 1
    # 债底保护：离债底越远，下跌保护越弱
    bp = cb.get("bond_premium")
    if bp is None:
        s += 4
    elif bp <= 10:
        s += 10
    elif bp <= 25:
        s += 7
    elif bp <= 50:
        s += 4
    else:
        s += 1
    # 正股趋势
    trend = ((cb.get("stock") or {}).get("trend") or "")
    s += {"上升": 10, "震荡": 5, "下降": 1}.get(trend, 3)
    s += RATING_SCORE.get((cb.get("rating") or "").upper(), 4)
    y = cb.get("years")
    if y is not None:
        s += 3 if y >= 2 else (2 if y >= 1 else 0)
    else:
        s += 1
    if cb["amount"] >= 5000:
        s += 2
    elif cb["amount"] >= 1000:
        s += 1
    s -= cb.get("penalty", 0)
    return round(max(0, min(100, s)), 1)


def build_cb_scan(min_amount=100.0, top=40):
    """可转债扫描 + 评分 + 分组"""
    items = fetch_cb_list(min_amount)
    if not items:
        return {"items": [], "groups": {}, "scanned": 0}

    for it in items:
        pen = 0
        reasons = []
        if it["redeem_ratio"] >= 1.3:
            pen += 12
            reasons.append("已触发强赎线（正股/转股价 %.2f），随时可能公告强赎" % it["redeem_ratio"])
        elif it["redeem_ratio"] >= 1.2:
            pen += 5
            reasons.append("接近强赎线（%.2f），上行空间受限" % it["redeem_ratio"])
        if it["years"] is not None and it["years"] < 1:
            pen += 8
            reasons.append("剩余期限不足 1 年，面临到期赎回")
        if it["premium"] > 60:
            pen += 5
            reasons.append("转股溢价率 %.0f%%，股性弱" % it["premium"])
        rk = (it.get("rating") or "").upper()
        if rk in LOW_RATING:
            pen += 20
            reasons.append("评级 %s，信用风险高，低价低溢价不代表便宜" % rk)
        elif rk == "":
            pen += 3
            reasons.append("无评级，信息不足")
        it["penalty"] = pen
        it["warnings"] = reasons
        it["score"] = score_cb(it)

    # 先按基础分粗排，前 60 只补正股数据（控制请求量）
    items.sort(key=lambda a: -a["score"])
    enrich_n = min(60, len(items))
    def _enrich(cb):
        try:
            ex = fetch_cb_extra(cb, 2 if cb in items[:top] else 1)
            cb.update(ex)
        except Exception:  # noqa: BLE001
            cb["stock"] = {}
        return cb
    with ThreadPoolExecutor(max_workers=8) as ex:
        items[:enrich_n] = list(ex.map(_enrich, items[:enrich_n]))
    # 补完正股后重算分数
    for it in items[:enrich_n]:
        it["score"] = score_cb(it)
    items.sort(key=lambda a: -a["score"])
    picked = items[:top]

    groups = {
        # 双低：价格 + 溢价率 双低的经典防守策略
        "double_low": sorted(
            [x for x in items if x["double_low"] <= 150 and x["redeem_ratio"] < 1.25
             and (x.get("rating") or "").upper() not in LOW_RATING],
            key=lambda a: a["double_low"])[:10],
        # 折价：转股溢价率为负，可转股套利
        "discount": sorted(
            [x for x in items if x["premium"] < 0],
            key=lambda a: a["premium"])[:10],
        # 进攻：正股强（接近强赎线）但价格不算离谱
        "attack": sorted(
            [x for x in items if 1.0 <= x["redeem_ratio"] < 1.28 and x["price"] <= 145],
            key=lambda a: -a["redeem_ratio"])[:10],
    }
    return {
        "items": picked,
        "groups": groups,
        "scanned": len(items),
        "updated": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }


# ================= 可转债增强：正股 + 债底 + 新闻 =================

def _parse_coupons(text):
    """从「第一年0.40%、第二年0.60%…」解析各年票息"""
    if not text:
        return []
    nums = re.findall(r"第[一二三四五六七八九十]年\s*([\d.]+)\s*%", text)
    if not nums:
        nums = re.findall(r"([\d.]+)\s*%", text)
    out = []
    for x in nums:
        try:
            out.append(float(x) / 100)
        except ValueError:
            continue
    return out


def _parse_redeem_price(text, default=108.0):
    """从赎回条款里解析到期赎回价（面值的百分比）"""
    if not text:
        return default
    m = re.search(r"面值的\s*([\d.]+)\s*%", text)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            pass
    return default


def bond_floor_value(coupons, redeem_price=108.0, years=None, discount=0.04):
    """
    纯债价值：未来票息 + 到期赎回价，按 discount 折现
    这是债底，转债价格跌破它通常意味着信用风险被定价
    """
    if not coupons:
        return None
    n = int(round(years)) if years else len(coupons)
    n = max(1, min(n, len(coupons)))
    pv = 0.0
    for i in range(n):
        c = coupons[i] if i < len(coupons) else (coupons[-1] if coupons else 0)
        pv += (100 * c) / ((1 + discount) ** (i + 1))
    pv += redeem_price / ((1 + discount) ** n)
    return round(pv, 2)


def fetch_stock_brief(code, days=70):
    """正股简报：近月走势 + 均线位置 + 量能（供转债 AI 判断用）"""
    if not code or len(str(code)) != 6:
        return {}
    sym = ("sh" if str(code)[0] == "6" else "sz") + str(code)
    try:
        kl = fetch_kline(sym, days)
    except Exception:  # noqa: BLE001
        return {}
    if len(kl) < 25:
        return {}
    closes = [x["close"] for x in kl]
    vols = [x.get("volume") or 0 for x in kl]
    now = closes[-1]
    ma5 = sum(closes[-5:]) / 5
    ma20 = sum(closes[-20:]) / 20
    ma60 = sum(closes[-60:]) / 60 if len(closes) >= 60 else None

    def _chg(n):
        if len(closes) <= n or closes[-1 - n] <= 0:
            return None
        return round((now / closes[-1 - n] - 1) * 100, 2)

    v5 = sum(vols[-5:]) / 5 if len(vols) >= 5 else 0
    v20 = sum(vols[-20:]) / 20 if len(vols) >= 20 else 0
    trend = "震荡"
    if ma60 and now > ma20 > ma60:
        trend = "上升"
    elif ma60 and now < ma20 < ma60:
        trend = "下降"
    return {
        "code": str(code),
        "price": round(now, 2),
        "chg5": _chg(5),
        "chg20": _chg(20),
        "chg60": _chg(60),
        "ma5": round(ma5, 2),
        "ma20": round(ma20, 2),
        "above_ma20": now > ma20,
        "vol_ratio": round(v5 / v20, 2) if v20 > 0 else None,
        "trend": trend,
        "from_high60": round((now / max(closes[-60:]) - 1) * 100, 2)
                       if len(closes) >= 20 else None,
    }


def fetch_cb_extra(cb, news_limit=3):
    """
    为单只转债补充：正股走势 / 正股新闻 / 纯债价值
    cb 需含 stock_code、price、years
    """
    out = {}
    try:
        out["stock"] = fetch_stock_brief(cb.get("stock_code"), 70)
    except Exception:  # noqa: BLE001
        out["stock"] = {}
    try:
        sc = str(cb.get("stock_code") or "")
        if sc:
            sym = ("sh" if sc[0] == "6" else "sz") + sc
            nm = ""
            q = fetch_quotes([sym]).get(sym) or {}
            nm = q.get("name") or ""
            ns = fetch_news_multi(sym, sc, nm, news_limit + 2)
            out["news"] = (ns.get("items") or [])[:news_limit]
            out["news_score"] = ns.get("score")
            out["sources"] = ns.get("sources") or []
    except Exception:  # noqa: BLE001
        out["news"] = []
    return out


# ================= 事件数据：解禁 / 增减持 =================

def fetch_lift_schedule(days=30, min_cap=10000):
    """
    未来 N 日限售解禁（LIFT_MARKET_CAP 单位：万元）
    min_cap: 最小解禁市值（万元），过滤小额解禁
    """
    import datetime as _dt
    today = _dt.date.today()
    end = today + _dt.timedelta(days=days)
    params = {
        "reportName": "RPT_LIFT_STAGE", "columns": "ALL",
        "pageNumber": "1", "pageSize": "100", "source": "WEB", "client": "WEB",
        "filter": "(FREE_DATE>='%s')(FREE_DATE<='%s')" % (today.isoformat(), end.isoformat()),
        "sortColumns": "FREE_DATE", "sortTypes": "1",
    }
    try:
        r = requests.get(DC_URL, params=params,
                         headers={**UA, "Referer": "https://data.eastmoney.com/"}, timeout=(5, 15))
        data = ((r.json().get("result") or {}).get("data")) or []
    except Exception as e:  # noqa: BLE001
        log.debug("解禁数据失败: %s", e)
        return []
    out = []
    for x in data:
        try:
            cap = float(x.get("LIFT_MARKET_CAP") or 0)
        except (TypeError, ValueError):
            continue
        if cap < min_cap:
            continue
        try:
            shares = float(x.get("CURRENT_FREE_SHARES") or 0)
        except (TypeError, ValueError):
            shares = 0
        out.append({
            "code": str(x.get("SECURITY_CODE") or ""),
            "date": (x.get("FREE_DATE") or "")[:10],
            "shares": round(shares, 2),          # 万股
            "cap": round(cap / 10000, 2),        # 亿元
            "type": x.get("FREE_SHARES_TYPE") or "",
            "holders": x.get("BATCH_HOLDER_NUM") or 0,
        })
    out.sort(key=lambda a: a["date"])
    return out


def fetch_holder_changes(days=7, min_rate=0.3):
    """
    近 N 日股东增减持（CHANGE_NUM_SYMBOL 负=减持，正=增持）
    min_rate: 最小变动比例（占总股本 %）
    """
    import datetime as _dt
    begin = (_dt.date.today() - _dt.timedelta(days=days)).isoformat()
    params = {
        "reportName": "RPT_SHARE_HOLDER_INCREASE", "columns": "ALL",
        "pageNumber": "1", "pageSize": "100", "source": "WEB", "client": "WEB",
        "filter": "(NOTICE_DATE>='%s')" % begin,
        "sortColumns": "NOTICE_DATE", "sortTypes": "-1",
    }
    try:
        r = requests.get(DC_URL, params=params,
                         headers={**UA, "Referer": "https://data.eastmoney.com/"},
                         timeout=(5, 15))
        data = ((r.json().get("result") or {}).get("data")) or []
    except Exception as e:  # noqa: BLE001
        log.debug("增减持数据失败: %s", e)
        return []
    out = []
    for x in data:
        try:
            rate = abs(float(x.get("CHANGE_RATE") or 0))
        except (TypeError, ValueError):
            continue
        if rate < min_rate:
            continue
        raw = str(x.get("CHANGE_NUM_SYMBOL") or x.get("CHANGE_NUM") or "")
        try:
            num = float(raw)
        except (TypeError, ValueError):
            num = 0.0
        # CHANGE_RATE 是「占其自身持股」的比例，需换算成占总股本的变化幅度
        after = float(x.get("AFTER_CHANGE_RATE") or 0)
        if num < 0 and rate < 100:
            before = after / (1 - rate / 100.0)
        elif num > 0:
            before = after / (1 + rate / 100.0)
        else:
            before = after
        delta = round(after - before, 3)
        out.append({
            "code": str(x.get("SECURITY_CODE") or ""),
            "date": (x.get("NOTICE_DATE") or "")[:10],
            "holder": (x.get("HOLDER_NAME") or "")[:18],
            "num": round(num, 2),                # 万股，负=减持
            "rate": round(rate, 2),              # 占其自身持股 %
            "after": round(after, 2),            # 变动后占总股本 %
            "delta": delta,                      # 占总股本变化（负=减持）
            "dir": "增持" if num > 0 else "减持",
        })
    out.sort(key=lambda a: (-abs(a["delta"]), a["date"]))
    return out


def fetch_pledge_risk(min_ratio=30, limit=20, days=120):
    """
    高质押风险股（中登数据）
    注意：该表按日快照，需先按日期过滤再按代码取最新，否则会取到多年前的极值
    """
    import datetime as _dt
    begin = (_dt.date.today() - _dt.timedelta(days=days)).isoformat()
    params = {
        "reportName": "RPT_CSDC_LIST", "columns": "ALL",
        "pageNumber": "1", "pageSize": "300", "source": "WEB", "client": "WEB",
        "filter": "(TRADE_DATE>='%s')" % begin,
        "sortColumns": "PLEDGE_RATIO", "sortTypes": "-1",
    }
    try:
        r = requests.get(DC_URL, params=params,
                         headers={**UA, "Referer": "https://data.eastmoney.com/"}, timeout=(5, 15))
        data = ((r.json().get("result") or {}).get("data")) or []
    except Exception as e:  # noqa: BLE001
        log.debug("质押数据失败: %s", e)
        return []
    seen, rows = set(), []
    for x in data:                     # 已按比例降序，同代码保留第一条（最高）
        code = str(x.get("SECURITY_CODE") or "")
        if not code or code in seen:
            continue
        try:
            ratio = float(x.get("PLEDGE_RATIO") or 0)
        except (TypeError, ValueError):
            continue
        if ratio < min_ratio:
            continue
        seen.add(code)
        try:
            bal = float(x.get("REPURCHASE_BALANCE") or 0)      # 万元
        except (TypeError, ValueError):
            bal = 0.0
        rows.append({
            "code": code,
            "name": x.get("SECURITY_NAME_ABBR") or "",
            "date": (x.get("TRADE_DATE") or "")[:10],
            "ratio": round(ratio, 2),
            "balance": round(bal / 10000, 2),                  # 亿元
            "industry": x.get("INDUSTRY") or "",
            "level": "极高" if ratio >= 60 else ("高" if ratio >= 45 else "偏高"),
        })
        if len(rows) >= limit:
            break
    return rows
