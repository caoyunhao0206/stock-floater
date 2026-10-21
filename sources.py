# -*- coding: utf-8 -*-
"""
免费公开数据源（无需 API Key）
- 实时行情：腾讯财经 qt.gtimg.cn
- 前复权日K：腾讯 web.ifzq.gtimg.cn
- 主力资金流：东方财富 push2
- 个股新闻：新浪财经
"""
import re
import time
import logging
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


def fetch_fund_flow(secid, days=5):
    """东方财富主力资金流（近 N 日）；接口不可用时返回 []，由引擎降级为量价评分"""
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
        return []
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

    for src_fn, key in ((fetch_stcn, name), (fetch_sina_roll_multi, name)):
        try:
            items = src_fn()
        except Exception:  # noqa: BLE001
            items = []
        for it in items:
            title = it.get("title", "")
            hit = (key and key in title) or (short and len(short) >= 2 and short in title)
            if hit:
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
