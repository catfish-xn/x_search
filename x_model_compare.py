#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
x_model_compare.py —— 用同一个问题对比中转站上不同 Grok 型号的搜索效果

它直接调用 x_search_mcp.py 里的搜索逻辑，所以对比结果和 MCP 实际使用时一致。
请把本文件和 x_search_mcp.py 放在同一个文件夹里。

先设置环境变量（和 MCP 用的一样）：
    RELAY_BASE_URL   中转站地址，以 /v1 结尾
    RELAY_API_KEY    中转站的 key

用法（型号名按中转站面板里的写法）：
    python3 x_model_compare.py "Gemini 4 Carbon 使用体验" grok-4.5 grok-4.6 grok-4.7

每个型号之间默认等 60 秒，避免被中转站限流。可用环境变量 X_COMPARE_GAP 改秒数。
每个型号的完整结果会存成 compare_型号名.json，不含 key。
"""

import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import x_search_mcp as mcp  # noqa: E402


def width(s):
    """按显示宽度算长度：中文字符算两格。"""
    return sum(2 if ord(c) > 0x2E7F else 1 for c in str(s))


def pad(s, w):
    s = str(s)
    return s + " " * max(0, w - width(s))


def run_one(model, query, hours, max_posts):
    os.environ["RELAY_MODEL"] = model
    started = time.time()
    is_error, text = mcp.search_x({"query": query, "hours": hours, "max_posts": max_posts, "coverage": "fast"})
    elapsed = round(time.time() - started, 1)
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", model)
    path = "compare_%s.json" % safe
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    if is_error:
        return {"model": model, "ok": False, "error": text.split("\n")[0][:60], "elapsed": elapsed, "ids": set(), "file": path}
    r = json.loads(text)
    posts = r.get("posts", [])
    texts = [p.get("text", "") for p in posts]
    with_text = [t for t in texts if t]
    return {
        "model": model,
        "ok": True,
        "search_ran": r.get("search_ran"),
        "calls": r.get("stats", {}).get("x_search_calls", "-"),
        "fetched": r.get("stats", {}).get("x_posts_fetched", "-"),
        "tokens": r.get("stats", {}).get("total_tokens", "-"),
        "count": len(posts),
        "with_text": len(with_text),
        "avg_len": round(sum(len(t) for t in with_text) / len(with_text)) if with_text else 0,
        "in_window": sum(1 for p in posts if not p.get("flags")),
        "suspect": sum(1 for p in posts if any("编造" in f for f in p.get("flags", []))),
        "structured": "raw_answer" not in r,
        "elapsed": elapsed,
        "warnings": r.get("warnings", []),
        "ids": set(p["url"].rsplit("/", 1)[-1] for p in posts),
        "file": path,
    }


def main():
    args = sys.argv[1:]
    if len(args) < 2:
        print(__doc__)
        sys.exit(2)
    query, models = args[0], args[1:]
    for name in ("RELAY_BASE_URL", "RELAY_API_KEY"):
        if not os.environ.get(name, "").strip():
            print("缺少环境变量 %s，请先设置。" % name)
            sys.exit(2)
    try:
        gap = max(0.0, float(os.environ.get("X_COMPARE_GAP", "60")))
    except ValueError:
        gap = 60.0
    hours, max_posts = 24, 20

    print("问题：%s    范围：最近 %d 小时    每个型号最多 %d 条\n" % (query, hours, max_posts))
    rows = []
    for i, model in enumerate(models):
        if i and gap:
            print("等待 %.0f 秒，避免限流……" % gap)
            time.sleep(gap)
        print("正在测试 %s（可能需要一两分钟）" % model)
        rows.append(run_one(model, query, hours, max_posts))

    cols = [
        ("型号", lambda r: r["model"]),
        ("真的搜了", lambda r: "是" if r.get("search_ran") else "否"),
        ("搜索轮数", lambda r: r.get("calls", "-")),
        ("抓取帖数", lambda r: r.get("fetched", "-")),
        ("返回帖数", lambda r: r.get("count", "-")),
        ("带原文", lambda r: r.get("with_text", "-")),
        ("原文均长", lambda r: r.get("avg_len", "-")),
        ("时间合格", lambda r: r.get("in_window", "-")),
        ("疑似编造", lambda r: r.get("suspect", "-")),
        ("按格式输出", lambda r: "是" if r.get("structured") else "否"),
        ("耗时秒", lambda r: r.get("elapsed", "-")),
        ("总token", lambda r: r.get("tokens", "-")),
    ]
    print("\n=== 对比表 ===")
    table = [[h for h, _ in cols]]
    for r in rows:
        if r["ok"]:
            table.append([str(fn(r)) for _, fn in cols])
    widths = [max(width(row[i]) for row in table) for i in range(len(cols))]
    for row in table:
        print("  ".join(pad(c, widths[i]) for i, c in enumerate(row)))
    for r in rows:
        if not r["ok"]:
            print("%s  失败：%s" % (r["model"], r["error"]))

    ok_rows = [r for r in rows if r["ok"] and r["ids"]]
    if len(ok_rows) > 1:
        print("\n=== 结果重合度（相同的帖子有几条）===")
        for i in range(len(ok_rows)):
            for j in range(i + 1, len(ok_rows)):
                a, b = ok_rows[i], ok_rows[j]
                print("  %s 与 %s：%d 条相同" % (a["model"], b["model"], len(a["ids"] & b["ids"])))
        union = set().union(*(r["ids"] for r in ok_rows))
        print("  几个型号合起来共 %d 条不同的帖子" % len(union))

    for r in rows:
        if r["ok"] and r["warnings"]:
            print("\n%s 的提示：" % r["model"])
            for w in r["warnings"]:
                print("  - " + w)

    print("\n完整结果已保存：" + "、".join(r["file"] for r in rows))
    print("把上面的对比表整段贴给 Claude 即可。")


if __name__ == "__main__":
    main()
