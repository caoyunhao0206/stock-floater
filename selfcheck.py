# -*- coding: utf-8 -*-
"""
自检脚本：语法 / 导入 / 边界情况 / 数据一致性
用法：python selfcheck.py
"""
import importlib
import json
import os
import py_compile
import sys
import traceback

BASE = os.path.dirname(os.path.abspath(__file__))
PY_FILES = ["app.py", "sources.py", "engine.py", "indicators.py", "market.py",
            "recommender.py", "backtest.py", "ai.py", "pushers.py", "tracking.py",
            "build_static.py"]

results = {"pass": 0, "fail": 0, "warn": 0, "items": []}


def ok(name, detail=""):
    results["pass"] += 1
    results["items"].append(("PASS", name, detail))


def bad(name, detail=""):
    results["fail"] += 1
    results["items"].append(("FAIL", name, detail))


def warn(name, detail=""):
    results["warn"] += 1
    results["items"].append(("WARN", name, detail))


# ---------- 1. 语法 ----------
print("== 语法检查 ==")
for f in PY_FILES:
    p = os.path.join(BASE, f)
    if not os.path.exists(p):
        warn(f, "文件不存在")
        continue
    try:
        py_compile.compile(p, doraise=True)
        ok(f, "语法通过")
    except py_compile.PyCompileError as e:
        bad(f, str(e)[:120])

# ---------- 2. 导入 ----------
print("== 模块导入 ==")
for m in [x[:-3] for x in PY_FILES]:
    try:
        importlib.import_module(m)
        ok(m, "导入成功")
    except Exception as e:  # noqa: BLE001
        bad(m, "%s: %s" % (type(e).__name__, str(e)[:110]))

# ---------- 3. 指标边界 ----------
print("== 指标边界 ==")
try:
    import indicators as I

    cases = {
        "空序列": lambda: (I.price_position([]), I.vol_ma_ratio([]), I.shrink_days([])),
        "单点": lambda: (I.price_position([1.0]), I.vol_ma_ratio([1.0])),
        "零价": lambda: (I.price_position([0, 0, 0]), I.max_drawdown([0, 0])),
        "全等": lambda: (I.price_position([5, 5, 5]), I.boll_width([5] * 20)),
    }
    for name, fn in cases.items():
        try:
            fn()
            ok("指标-" + name)
        except Exception as e:  # noqa: BLE001
            bad("指标-" + name, "%s: %s" % (type(e).__name__, str(e)[:80]))
except Exception as e:  # noqa: BLE001
    bad("indicators 导入", str(e)[:100])

# ---------- 4. 回测边界 ----------
print("== 回测边界 ==")
try:
    import backtest as B

    try:
        r = B.equity_curve([], 5, 65)
        ok("回测-空输入", "返回 %s" % (r,))
    except Exception as e:  # noqa: BLE001
        bad("回测-空输入", "%s: %s" % (type(e).__name__, str(e)[:80]))

    try:
        r = B.equity_curve([{"_kl": [], "code": "x"}], 5, 65)
        ok("回测-空K线", "返回 %s" % (r,))
    except Exception as e:  # noqa: BLE001
        bad("回测-空K线", "%s: %s" % (type(e).__name__, str(e)[:80]))
except Exception as e:  # noqa: BLE001
    bad("backtest 导入", str(e)[:100])

# ---------- 5. 跟踪边界 ----------
print("== 跟踪边界 ==")
try:
    import tracking as T

    try:
        st = T.summarize([])
        assert st["total"] == 0
        ok("跟踪-空汇总", "total=0")
    except Exception as e:  # noqa: BLE001
        bad("跟踪-空汇总", "%s: %s" % (type(e).__name__, str(e)[:80]))

    try:
        w, info = T.suggest_weights()
        ok("跟踪-无样本校准", "返回 %s" % (w is None))
    except Exception as e:  # noqa: BLE001
        bad("跟踪-无样本校准", "%s: %s" % (type(e).__name__, str(e)[:80]))
except Exception as e:  # noqa: BLE001
    bad("tracking 导入", str(e)[:100])

# ---------- 6. 可转债计算 ----------
print("== 可转债计算 ==")
try:
    import sources as S

    try:
        cp = S._parse_coupons("第一年0.40%、第二年0.60%")
        assert len(cp) == 2, cp
        ok("债底-票息解析", str(cp))
    except Exception as e:  # noqa: BLE001
        bad("债底-票息解析", "%s: %s" % (type(e).__name__, str(e)[:80]))

    try:
        v = S.bond_floor_value([], 108, 3)
        ok("债底-空票息", "返回 %s" % v)
    except Exception as e:  # noqa: BLE001
        bad("债底-空票息", "%s: %s" % (type(e).__name__, str(e)[:80]))

    try:
        cb = {"price": 100, "premium": 10, "double_low": 110, "bond_premium": 5,
              "stock": {"trend": "上升"}, "rating": "AAA", "years": 3, "amount": 5000}
        s1 = S.score_cb(cb)
        assert 0 <= s1 <= 100, s1
        ok("转债-评分区间", "%.1f" % s1)
    except Exception as e:  # noqa: BLE001
        bad("转债-评分区间", "%s: %s" % (type(e).__name__, str(e)[:80]))

    try:
        cb2 = {"price": 0, "premium": 0, "double_low": 0, "bond_premium": None,
               "stock": {}, "rating": "", "years": None, "amount": 0}
        s2 = S.score_cb(cb2)
        assert 0 <= s2 <= 100, s2
        ok("转债-残缺数据评分", "%.1f" % s2)
    except Exception as e:  # noqa: BLE001
        bad("转债-残缺数据评分", "%s: %s" % (type(e).__name__, str(e)[:80]))
except Exception as e:  # noqa: BLE001
    bad("sources 导入", str(e)[:100])

# ---------- 7. 输出数据一致性 ----------
print("== 数据一致性 ==")
dp = os.path.join(BASE, "docs", "data", "all.json")
if os.path.exists(dp):
    try:
        d = json.load(open(dp, encoding="utf-8"))
        need = ["updated", "updated_ts", "market", "recommend", "quotes",
                "sectors", "limit_up", "lof", "cb", "backtest", "tracking"]
        miss = [k for k in need if k not in d]
        if miss:
            warn("all.json 字段", "缺少 " + ", ".join(miss))
        else:
            ok("all.json 字段", "齐全")
        # 推荐股字段完整性
        recs = (d.get("recommend", {}).get("steady") or []) + \
               (d.get("recommend", {}).get("aggressive") or [])
        if recs:
            keys = ["code", "name", "price", "buy_price", "target", "stop", "score", "dims"]
            missing = [k for k in keys if k not in recs[0]]
            if missing:
                warn("推荐字段", "缺少 " + ", ".join(missing))
            else:
                ok("推荐字段", "齐全（%d 只）" % len(recs))
        # 转债字段
        cbs = (d.get("cb") or {}).get("items") or []
        if cbs:
            miss2 = [k for k in ("code", "price", "premium", "bond_floor", "stock")
                     if k not in cbs[0]]
            if miss2:
                warn("转债字段", "缺少 " + ", ".join(miss2))
            else:
                ok("转债字段", "齐全（%d 只）" % len(cbs))
    except Exception as e:  # noqa: BLE001
        bad("all.json 解析", "%s: %s" % (type(e).__name__, str(e)[:80]))
else:
    warn("all.json", "文件不存在，跳过")

# ---------- 8. 权重文件 ----------
print("== 权重 ==")
wp = os.path.join(BASE, "weights.json")
if os.path.exists(wp):
    try:
        w = json.load(open(wp, encoding="utf-8"))
        ws = w.get("weights") or {}
        bad_v = [k for k, v in ws.items() if not (0.5 <= float(v) <= 1.8)]
        if bad_v:
            bad("权重范围", "越界: " + str(bad_v))
        else:
            ok("权重范围", "全部在 0.5~1.8")
    except Exception as e:  # noqa: BLE001
        bad("权重解析", str(e)[:80])
else:
    warn("weights.json", "尚未生成（首次校准后出现）")

# ---------- 汇总 ----------
print("\n" + "=" * 52)
for tag, name, detail in results["items"]:
    if tag != "PASS":
        print("[%s] %-22s %s" % (tag, name, detail))
print("=" * 52)
print("通过 %d / 警告 %d / 失败 %d" % (results["pass"], results["warn"], results["fail"]))
sys.exit(1 if results["fail"] else 0)
