#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
x_search_mcp.py —— X 搜索 MCP 服务器（本地版，stdio 方式）

只用 Python 自带的库，不需要安装任何东西。

它做的事：把中转站的 Grok X 搜索包装成一个工具 search_x。
Grok 只负责搜，返回帖子列表；读、筛、写报告交给调用它的模型。

环境变量（密钥只放在环境变量里，本程序不会把它写进任何输出）。
没有设置的变量会从本文件同目录下的 .env 里读取，所以换 key 只需要改 .env 一处：
    RELAY_BASE_URL   必填。中转站地址，以 /v1 结尾，例如 https://example.com/v1
    RELAY_API_KEY    必填。中转站的 key。填 proxy 表示本程序不带 key，
                     由外层代理补上（Claude 云端环境的 Network secrets 就是这种用法）
    RELAY_MODEL      选填。主型号，默认 grok-4.5（实测偏中文社区的普通用户）
    RELAY_MODEL_ALT  选填。第二型号，默认 grok-4.7（实测偏英文圈的高互动帖子）。
                     coverage 为 wide 时两个型号同时搜再合并。填 none 则只用主型号
    X_MAX_TURNS      选填。限制 Grok 的搜索轮数，例如 3。不填则不限制
    X_INCLUDE        选填。实验项：请求里附带的 include 值，多个用逗号分隔，
                     例如 x_search_call_output。中转站不支持时会自动去掉重试
    X_STREAM         选填。默认 1，用流式接收，避免中转站前面的 Cloudflare 在 100 秒时掐断（524）。
                     填 0 则关闭
    X_TIMEOUT        选填。两次收到数据之间最长等待秒数，默认 180
    X_RETRY_DELAYS   选填。失败后的重试等待秒数，默认 5,15,30
    X_WAIT_SECONDS   选填。一次工具调用最多等多少秒就先返回，默认 45。
                     很多客户端的工具调用上限是 60 秒，而一次搜索要 2 到 4 分钟，
                     所以到时间没搜完会先返回一个 job_id，再用 get_x_result 取结果
    X_DEBUG_DIR      选填。填一个文件夹路径，每次调用的原始返回会存进去（不含 key）

命令行用法（不经过 MCP，直接搜，结果是 JSON，打印到标准输出）：
    python3 x_search_mcp.py search "要搜的内容" --hours 72 --coverage fast
    python3 x_search_mcp.py fetch https://x.com/账号/status/数字
不带任何参数启动时，才是 MCP 服务器模式。

在 Claude Code 里注册（把路径和三个值换成你自己的）：
    claude mcp add x-search \
      --env RELAY_BASE_URL=https://中转站地址/v1 \
      --env RELAY_API_KEY=你的key \
      --env RELAY_MODEL=grok-4.5 \
      -- python3 /完整路径/x_search_mcp.py
"""

import datetime
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request

SERVER_NAME = "x-search"
SERVER_VERSION = "0.5.0"
DEFAULT_PROTOCOL = "2025-06-18"
UA = "Mozilla/5.0 (x-search-mcp)"
KEY_VIA_PROXY = "proxy"

TWITTER_EPOCH_MS = 1288834974657
POST_URL = re.compile(r"https?://(?:www\.)?(?:x|twitter)\.com/([A-Za-z0-9_]+)/status/(\d+)")
ARTICLE_ONLY = re.compile(r"https?://(?:www\.)?(?:x|twitter)\.com/i/article/\d+/?")
OPTIONAL_FIELDS = ("include", "max_turns")
RETRY_STATUS = (0, 408, 409, 425, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524)

TOOL = {
    "name": "search_x",
    "description": (
        "在 X（推特）上搜索帖子，返回帖子列表：链接、作者、发帖时间、原文。"
        "只返回原始帖子，不做总结或评价；筛选和总结由调用方完成。"
        "每条帖子都带有根据帖子 ID 反推出的真实发帖时间（id_time_utc），可用来核对。"
        "结果里的 search_ran 为 false 时说明搜索没有真正执行，内容不可信，不要引用。"
        "帖子带 is_article 为 true 时，正文是一篇长文章，只有链接；需要内容时用 fetch_x_post 取全文。"
        "搜索本身需要 2 到 6 分钟（时间范围越大越久），所以本工具通常不会直接给出结果："
        "返回里 status 为 running 时，记下 job_id，等 60 秒左右再调用 get_x_result 取结果，没好就继续取。"
        "不要因为 status 是 running 就重新调用 search_x，那样会重复搜索。"
        "关键词搜不准时，换几组不同的词分别调用。"
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "要搜的内容。可以是关键词，也可以是一句话描述，例如“用户对 Gemini 4 Carbon 的实际使用体验”。",
            },
            "hours": {
                "type": "integer",
                "description": "只要最近多少小时内的帖子。默认 24，最大 720。",
                "minimum": 1,
                "maximum": 720,
                "default": 24,
            },
            "max_posts": {
                "type": "integer",
                "description": "最多返回多少条帖子。默认 20，最大 50。",
                "minimum": 1,
                "maximum": 50,
                "default": 20,
            },
            "language": {
                "type": "string",
                "description": "语言偏好：zh（中文）、en（英文）、any（不限）。默认 any。",
                "enum": ["zh", "en", "any"],
                "default": "any",
            },
            "handles": {
                "type": "array",
                "items": {"type": "string"},
                "description": "只搜这些账号发的帖子（不带 @，最多 20 个）。用于白名单来源，例如官方账号和通讯社。",
            },
            "exclude_handles": {
                "type": "array",
                "items": {"type": "string"},
                "description": "排除这些账号（不带 @，最多 20 个）。不能和 handles 同时使用。",
            },
            "coverage": {
                "type": "string",
                "description": "wide（默认）：两个型号同时搜再合并去重，一个偏中文社区、一个偏英文高互动帖，覆盖面最广。fast：只用主型号，更省时。",
                "enum": ["wide", "fast"],
                "default": "wide",
            },
            "include_replies": {
                "type": "boolean",
                "description": "是否包含回复。默认 false，只要原帖，可以避开回复区的广告。",
                "default": False,
            },
        },
        "required": ["query"],
    },
}


FETCH_TOOL = {
    "name": "fetch_x_post",
    "description": (
        "取回一条 X 帖子的完整内容，包括它所在的串和它链接的长文章（article）正文。"
        "用于 search_x 结果里 is_article 为 true 的帖子，或需要逐字核对某条帖子原文时。"
        "got_article 为 false 表示没取到文章正文。抓取需要 1 到 3 分钟："
        "返回里 status 为 running 时，记下 job_id，稍后用 get_x_result 取结果。"
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "帖子链接，形如 https://x.com/账号/status/数字"},
        },
        "required": ["url"],
    },
}

RESULT_TOOL = {
    "name": "get_x_result",
    "description": (
        "取回 search_x 或 fetch_x_post 的结果。传入它们返回的 job_id。"
        "任务还没完成时会等一小段时间；仍未完成则返回 status 为 running，过 30 到 60 秒再调用一次。"
        "status 为 not_found 说明服务器重启过、任务已丢失，需要重新发起搜索。"
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "job_id": {"type": "string", "description": "search_x 或 fetch_x_post 返回的 job_id"},
        },
        "required": ["job_id"],
    },
}

TOOLS = [TOOL, FETCH_TOOL, RESULT_TOOL]


# ---------------------------------------------------------------- 基础工具

def log(msg):
    """日志只写到 stderr。stdout 只能出现 MCP 协议消息。"""
    sys.stderr.write("[x-search] %s\n" % msg)
    sys.stderr.flush()


def load_dotenv():
    """把同目录 .env 里的值补进环境变量（已经设置的不覆盖）。"""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                name, value = line.split("=", 1)
                name = name.strip()
                if name.startswith("export "):
                    name = name[7:].strip()
                value = value.strip().strip('"').strip("'")
                if name and not os.environ.get(name, "").strip():
                    os.environ[name] = value
    except OSError:
        pass


def now_utc():
    return datetime.datetime.now(datetime.timezone.utc)


def snowflake_time(post_id):
    """从帖子 ID 反推发帖时间。无法解析时返回 None。"""
    try:
        ms = (int(post_id) >> 22) + TWITTER_EPOCH_MS
        return datetime.datetime.fromtimestamp(ms / 1000.0, datetime.timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None


def env_int(name, default=None):
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def retry_delays():
    raw = os.environ.get("X_RETRY_DELAYS", "5,15,30")
    out = []
    for part in raw.split(","):
        try:
            out.append(max(0.0, float(part)))
        except ValueError:
            pass
    return out


def clean_handles(value):
    if not isinstance(value, list):
        return []
    out = []
    for item in value:
        if isinstance(item, str):
            h = item.strip().lstrip("@")
            if re.fullmatch(r"[A-Za-z0-9_]{1,15}", h) and h not in out:
                out.append(h)
    return out[:20]


def clamp(value, low, high, default):
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------- 请求上游

def read_sse(resp):
    """读取流式返回，拼回和非流式一样的结构。"""
    final = None
    items = []
    text_parts = []
    plain = []  # 不是流式格式的内容：有的中转站会无视 stream 直接返回普通 JSON
    for raw_line in resp:
        line = raw_line.decode("utf-8", "replace").strip()
        if not line.startswith("data:"):
            if line and not line.startswith(("event:", "id:", "retry:", ":")):
                plain.append(line)
            continue
        chunk = line[5:].strip()
        if not chunk or chunk == "[DONE]":
            continue
        try:
            event = json.loads(chunk)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        etype = event.get("type") or ""
        if etype in ("response.completed", "response.incomplete") and isinstance(event.get("response"), dict):
            final = event["response"]
        elif etype == "response.output_item.done" and isinstance(event.get("item"), dict):
            items.append(event["item"])
        elif etype == "response.output_text.delta" and isinstance(event.get("delta"), str):
            text_parts.append(event["delta"])
        elif etype == "error" or (not etype and "error" in event):
            return 502, json.dumps(event, ensure_ascii=False)
    if final is not None:
        if not final.get("output") and items:
            final["output"] = items
        return 200, json.dumps(final, ensure_ascii=False)
    if items or text_parts:
        # 流中途断了：用已经收到的部分拼一个结果
        if not any(isinstance(i, dict) and i.get("type") == "message" for i in items) and text_parts:
            items.append({"type": "message", "content": [{"type": "output_text", "text": "".join(text_parts)}]})
        return 200, json.dumps({"output": items, "stream_truncated": True}, ensure_ascii=False)
    if plain:
        body = "\n".join(plain)
        try:
            json.loads(body)
            return 200, body
        except ValueError:
            return 502, body[:2000]
    return 0, "网络错误: 流式返回为空"


def http_post(url, key, payload, timeout):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream, application/json",
        "User-Agent": UA,
    }
    # key 填 proxy 时不带 Authorization，由外层代理在请求离开本机后补上
    if key.lower() != KEY_VIA_PROXY:
        headers["Authorization"] = "Bearer " + key
    req = urllib.request.Request(url, data=data, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if payload.get("stream"):
                return read_sse(resp)
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:  # 超时、握手失败、域名解析失败等
        return 0, "网络错误: %s" % type(e).__name__


def call_upstream(base, key, payload, warnings):
    """带重试地调用中转站。返回 (状态码, 原始文本)。"""
    url = base.rstrip("/") + "/responses"
    timeout = env_int("X_TIMEOUT", 180)
    delays = retry_delays()
    body = dict(payload)
    stripped = False
    stream_stripped = False
    attempt = 0
    while True:
        status, raw = http_post(url, key, body, timeout)
        if status == 200:
            return status, raw
        # 可选参数被拒绝：去掉后重试一次，不算在重试次数里
        if status in (400, 422) and not stripped and any(f in body for f in OPTIONAL_FIELDS):
            removed = [f for f in OPTIONAL_FIELDS if f in body]
            for f in removed:
                body.pop(f, None)
            stripped = True
            warnings.append("中转站不接受可选参数 %s，已去掉后重试。" % "、".join(removed))
            continue
        if status in (400, 422) and not stream_stripped and body.get("stream"):
            body.pop("stream", None)
            stream_stripped = True
            warnings.append("中转站不接受流式请求，已改回普通请求。耗时超过 100 秒时可能被掐断。")
            continue
        if status in RETRY_STATUS and attempt < len(delays):
            wait = delays[attempt]
            attempt += 1
            log("请求失败（%s），%.0f 秒后第 %d 次重试" % (status, wait, attempt))
            time.sleep(wait)
            continue
        if attempt:
            warnings.append("重试了 %d 次后仍然失败。" % attempt)
        return status, raw


# ---------------------------------------------------------------- 解析返回

def gather_message_text(data):
    """取出模型的回答文字。优先按 Responses 格式取，取不到再做通用收集。"""
    parts = []
    for item in data.get("output", []) if isinstance(data, dict) else []:
        if isinstance(item, dict) and item.get("type") == "message":
            for c in item.get("content", []) or []:
                if isinstance(c, dict) and isinstance(c.get("text"), str):
                    parts.append(c["text"])
    if parts:
        return "\n".join(parts)
    if isinstance(data, dict) and isinstance(data.get("output_text"), str):
        return data["output_text"]
    # 兼容 chat/completions 风格
    try:
        return data["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError):
        return ""


def gather_tool_calls(data):
    """收集服务端搜索工具的调用记录，用来证明搜索真的执行了。"""
    calls = []
    for item in data.get("output", []) if isinstance(data, dict) else []:
        if not isinstance(item, dict):
            continue
        name = item.get("name") or ""
        typ = item.get("type") or ""
        if name.startswith("x_") or "x_search" in typ:
            entry = {"tool": name or typ}
            arg = item.get("input") or item.get("arguments")
            if isinstance(arg, str) and arg:
                entry["input"] = arg[:300]
            elif isinstance(arg, dict):
                entry["input"] = json.dumps(arg, ensure_ascii=False)[:300]
            calls.append(entry)
    return calls


def gather_tool_outputs(data):
    """实验项：如果中转站透传了搜索工具的原始输出，原样带回（会截断）。"""
    outs = []
    for item in data.get("output", []) if isinstance(data, dict) else []:
        if not isinstance(item, dict):
            continue
        typ = item.get("type") or ""
        if typ.endswith("_output") or (("x_" in typ or str(item.get("name", "")).startswith("x_")) and "output" in item):
            text = item.get("output")
            if not isinstance(text, str):
                text = json.dumps(text, ensure_ascii=False)
            if text:
                outs.append(text[:20000])
    return outs


def gather_citations(data, raw):
    urls = []
    cites = data.get("citations") if isinstance(data, dict) else None
    if isinstance(cites, list):
        for c in cites:
            if isinstance(c, str):
                urls.append(c)
            elif isinstance(c, dict) and isinstance(c.get("url"), str):
                urls.append(c["url"])
    for m in re.finditer(r'"url"\s*:\s*"([^"]+)"', raw):
        urls.append(m.group(1))
    ids = set()
    for u in urls:
        m = POST_URL.search(u)
        if m:
            ids.add(m.group(2))
    return ids


def usage_details(data):
    usage = data.get("usage") if isinstance(data, dict) else None
    if not isinstance(usage, dict):
        return {}
    details = usage.get("server_side_tool_usage_details")
    out = {}
    if isinstance(details, dict):
        for k in ("x_search_calls", "x_posts_fetched", "x_users_fetched"):
            if isinstance(details.get(k), (int, float)):
                out[k] = details[k]
    for k in ("input_tokens", "output_tokens", "total_tokens"):
        if isinstance(usage.get(k), (int, float)):
            out[k] = usage[k]
    return out


def parse_json_array(text):
    """从回答里取出 JSON 数组。失败返回 None。"""
    if not text:
        return None
    cleaned = re.sub(r"```(?:json)?", "", text)
    start = cleaned.find("[")
    end = cleaned.rfind("]")
    if start == -1 or end <= start:
        return None
    try:
        value = json.loads(cleaned[start:end + 1])
    except ValueError:
        return None
    return value if isinstance(value, list) else None


def build_posts(answer, cited_ids, cutoff, max_posts, warnings):
    posts = []
    seen = set()
    structured = parse_json_array(answer)

    def add(url, text=None, claimed_time=None, extra=None):
        m = POST_URL.search(url or "")
        if not m:
            return
        author, pid = m.group(1), m.group(2)
        if pid in seen:
            return
        seen.add(pid)
        post = {"url": "https://x.com/%s/status/%s" % (author, pid), "author": author}
        t = snowflake_time(pid)
        flags = []
        # 近年的帖子 ID 都在 15 位以上；更短的只可能是 2010 年以前的老帖，搜近期内容时不会出现
        if t is None or len(pid) < 15 or t > now_utc() + datetime.timedelta(minutes=10):
            flags.append("ID 无法对应到合理的发帖时间，这条可能是编造的")
        else:
            post["id_time_utc"] = t.strftime("%Y-%m-%d %H:%M:%S")
            if t < cutoff:
                flags.append("早于要求的时间范围")
        if isinstance(claimed_time, str) and claimed_time:
            post["claimed_time"] = claimed_time[:40]
        if isinstance(text, str) and text.strip():
            post["text"] = text.strip()[:2000]
            if ARTICLE_ONLY.fullmatch(text.strip()):
                post["is_article"] = True
        if isinstance(extra, dict):
            for k in ("likes", "reposts", "replies", "views", "is_reply"):
                if k in extra and isinstance(extra[k], (int, float, bool)):
                    post[k] = extra[k]
        post["cited_by_search"] = pid in cited_ids
        if flags:
            post["flags"] = flags
        posts.append(post)

    if structured is not None:
        for obj in structured:
            if isinstance(obj, dict):
                add(obj.get("url") or obj.get("link") or "", obj.get("text"), obj.get("posted_at"), obj)
    else:
        if answer.strip():
            warnings.append("模型没有按要求输出结构化列表，只提取到了链接，原文请看 raw_answer。")
        for m in POST_URL.finditer(answer):
            add(m.group(0))

    return posts[:max_posts], structured is not None


def build_prompt(query, hours, max_posts, language, include_replies, style=""):
    lang = {"zh": "优先中文帖子。", "en": "优先英文帖子。"}.get(language, "语言不限。")
    if style == "popular":
        lang += "优先互动量高、有影响力的账号发的帖子。"
    elif style == "community":
        lang += "优先普通用户的真实使用反馈，不要只看大号。"
    replies = "可以包含回复。" if include_replies else "只要原帖，不要回复。"
    return (
        "你是一个搜索工具，只负责在 X 上搜帖子并原样返回，不做任何总结、评价或筛选性的点评。\n"
        "搜索内容：%s\n"
        "时间范围：最近 %d 小时内。%s%s\n"
        "排除色情、博彩、引流广告和完全重复的帖子。\n"
        "最多返回 %d 条，按互动量从高到低。\n"
        "输出格式：只输出一个 JSON 数组，不要任何其他文字，不要代码块标记。"
        "每个元素包含：url（帖子完整链接）、author（账号名，不带@）、"
        "posted_at（发帖时间，UTC，ISO 格式）、text（帖子原文，不要改写或翻译）、"
        "likes、reposts（数字，不知道就省略）。\n"
        "只能返回你通过搜索工具实际找到的帖子，不许凭记忆或推测补充。搜不到就输出 []。"
    ) % (query, hours, lang, replies, max_posts)


# ---------------------------------------------------------------- 工具实现

def search_x(args):
    base = os.environ.get("RELAY_BASE_URL", "").strip()
    key = os.environ.get("RELAY_API_KEY", "").strip()
    model = os.environ.get("RELAY_MODEL", "").strip() or "grok-4.5"
    missing = [n for n, v in (("RELAY_BASE_URL", base), ("RELAY_API_KEY", key)) if not v]
    if missing:
        return True, "缺少环境变量：%s。请在注册 MCP 服务器时设置。" % "、".join(missing)

    query = args.get("query")
    if not isinstance(query, str) or not query.strip():
        return True, "参数 query 不能为空。"
    query = query.strip()[:500]
    hours = clamp(args.get("hours", 24), 1, 720, 24)
    max_posts = clamp(args.get("max_posts", 20), 1, 50, 20)
    language = args.get("language") if args.get("language") in ("zh", "en", "any") else "any"
    include_replies = bool(args.get("include_replies", False))
    handles = clean_handles(args.get("handles"))
    exclude = clean_handles(args.get("exclude_handles"))

    warnings = []
    if handles and exclude:
        exclude = []
        warnings.append("handles 和 exclude_handles 不能同时使用，已忽略 exclude_handles。")

    now = now_utc()
    cutoff = now - datetime.timedelta(hours=hours)
    tool = {
        "type": "x_search",
        "from_date": cutoff.strftime("%Y-%m-%d"),
        # to_date 是不含当天的截止日，所以取明天
        "to_date": (now + datetime.timedelta(days=1)).strftime("%Y-%m-%d"),
    }
    if handles:
        tool["allowed_x_handles"] = handles
    if exclude:
        tool["excluded_x_handles"] = exclude

    coverage = "fast" if args.get("coverage") == "fast" else "wide"
    alt = os.environ.get("RELAY_MODEL_ALT", "grok-4.7").strip()
    if alt.lower() in ("", "none", "off", "0") or alt == model:
        alt = ""
    plans = [(model, language, "")]
    if coverage == "wide" and alt:
        if language == "any":
            plans = [(model, "zh", "community"), (alt, "en", "popular")]
        else:
            plans = [(model, language, "community"), (alt, language, "popular")]

    runs = [None] * len(plans)

    def work(i, m, lang, style):
        prompt = build_prompt(query, hours, max_posts, lang, include_replies, style)
        try:
            runs[i] = one_search(base, key, m, prompt, tool, cutoff, max_posts, now)
        except Exception as e:  # 一个型号出错不影响另一个
            log("型号 %s 出现意外错误: %r" % (m, e))
            runs[i] = {"ok": False, "model": m, "error": "内部出错：%s" % type(e).__name__, "warnings": []}

    started = time.time()
    threads = []
    for i, (m, lang, style) in enumerate(plans):
        t = threading.Thread(target=work, args=(i, m, lang, style))
        t.start()
        threads.append(t)
        if i + 1 < len(plans):
            time.sleep(3)  # 错开一点，避免同一瞬间打到中转站
    for t in threads:
        t.join()
    elapsed = round(time.time() - started, 1)

    good = [r for r in runs if r and r.get("ok")]
    for r in runs:
        for w in (r or {}).get("warnings", []):
            warnings.append("[%s] %s" % (r.get("model"), w) if len(plans) > 1 else w)
    if not good:
        return True, "\n".join(r.get("error", "未知错误") for r in runs if r)
    for r in runs:
        if r and not r.get("ok"):
            warnings.append("型号 %s 这次失败了，结果只来自另一个型号。原因：%s" % (r.get("model"), r.get("error", "")[:120]))

    posts = []
    index = {}
    for r in good:
        for post in r["posts"]:
            pid = post["url"].rsplit("/", 1)[-1]
            if pid in index:
                merged = index[pid]
                if r["model"] not in merged["found_by"]:
                    merged["found_by"].append(r["model"])
                if len(post.get("text", "")) > len(merged.get("text", "")):
                    merged["text"] = post["text"]
                continue
            post = dict(post)
            post["found_by"] = [r["model"]]
            index[pid] = post
            posts.append(post)
    if len(plans) == 1:
        for post in posts:
            post.pop("found_by", None)
        posts = posts[:max_posts]

    search_ran = all(r["search_ran"] for r in good)
    suspect = sum(1 for p in posts if any("编造" in f for f in p.get("flags", [])))
    if suspect:
        warnings.append("有 %d 条帖子的 ID 对不上合理的发帖时间，已在 flags 里标出。" % suspect)
    articles = sum(1 for p in posts if p.get("is_article"))
    if articles:
        warnings.append("有 %d 条帖子的正文是长文章（is_article），需要内容时用 fetch_x_post 取全文。" % articles)

    stats = {}
    for r in good:
        for k, v in r["stats"].items():
            stats[k] = stats.get(k, 0) + v
    result = {
        "query": query,
        "window": {"hours": hours, "from_utc": cutoff.strftime("%Y-%m-%d %H:%M"), "to_utc": now.strftime("%Y-%m-%d %H:%M")},
        "model": model if len(good) == 1 and len(plans) == 1 else ", ".join(r["model"] for r in good),
        "coverage": coverage if len(plans) > 1 else "fast",
        "search_ran": search_ran,
        "elapsed_seconds": elapsed,
        "stats": stats,
        "search_queries_used": [dict(c, model=r["model"]) if len(plans) > 1 else c for r in good for c in r["tool_calls"]][:30],
        "post_count": len(posts),
        "posts": posts,
        "warnings": warnings,
    }
    unstructured = [r for r in good if not r["structured"] and r["answer"]]
    if unstructured:
        result["raw_answer"] = "\n\n".join(r["answer"] for r in unstructured)[:6000]
    outputs = [o for r in good for o in r["outputs"]]
    if outputs:
        result["raw_tool_outputs"] = outputs[:10]
    return False, json.dumps(result, ensure_ascii=False, indent=1)


def base_payload(model, prompt, tool):
    payload = {"model": model, "input": [{"role": "user", "content": prompt}], "tools": [tool]}
    if os.environ.get("X_STREAM", "1").strip() not in ("0", "false", "no"):
        payload["stream"] = True
    max_turns = env_int("X_MAX_TURNS")
    if max_turns and max_turns > 0:
        payload["max_turns"] = max_turns
    include = [s.strip() for s in os.environ.get("X_INCLUDE", "").split(",") if s.strip()]
    if include:
        payload["include"] = include
    return payload


def save_debug(raw, label, now):
    debug_dir = os.environ.get("X_DEBUG_DIR", "").strip()
    if not debug_dir:
        return
    try:
        os.makedirs(debug_dir, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", label)
        name = "x_%s_%s.json" % (now.strftime("%Y%m%d_%H%M%S"), safe)
        with open(os.path.join(debug_dir, name), "w", encoding="utf-8") as f:
            f.write(raw)
    except OSError as e:
        log("保存调试文件失败: %s" % type(e).__name__)


def upstream_error(status, raw, model):
    if status == 524:
        hint = "中转站等上游超时（Cloudflare 约 100 秒上限）。请确认 X_STREAM 没有被关掉，或减少 max_posts。"
    elif status in (401, 403):
        hint = ("中转站不认这次请求的 key。RELAY_API_KEY 填的是 proxy 时，说明当前环境没有替它补 key："
                "需要在这个会话所用的云端环境里，为中转站域名添加 Network secret。")
    elif status in RETRY_STATUS:
        hint = "中转站可能在限流或暂时不稳定，稍后再试。"
    else:
        hint = "请检查模型名和中转站是否支持 /responses 接口与 x_search 工具。"
    return "搜索失败（型号 %s，状态码 %s）。%s\n返回内容开头：%s" % (model, status, hint, raw[:300].replace("\n", " "))


def one_search(base, key, model, prompt, tool, cutoff, max_posts, now):
    """用一个型号搜一次，返回解析好的结果。"""
    warnings = []
    status, raw = call_upstream(base, key, base_payload(model, prompt, tool), warnings)
    save_debug(raw, model, now)
    if status != 200:
        return {"ok": False, "model": model, "error": upstream_error(status, raw, model), "warnings": warnings}
    try:
        data = json.loads(raw)
    except ValueError:
        return {"ok": False, "model": model, "warnings": warnings,
                "error": "中转站返回的不是 JSON，无法解析。返回内容开头：%s" % raw[:300].replace("\n", " ")}
    if isinstance(data, dict) and data.get("stream_truncated"):
        warnings.append("流式返回中途断开，结果可能不完整。")
    answer = gather_message_text(data)
    tool_calls = gather_tool_calls(data)
    stats = usage_details(data)
    posts, structured = build_posts(answer, gather_citations(data, raw), cutoff, max_posts, warnings)
    search_ran = bool(tool_calls) or bool(stats.get("x_search_calls"))
    if not search_ran:
        warnings.append("返回里没有任何搜索工具的执行痕迹。搜索很可能没有生效，下面的帖子不可信，不要引用。")
    return {"ok": True, "model": model, "posts": posts, "structured": structured, "answer": answer,
            "tool_calls": tool_calls, "stats": stats, "search_ran": search_ran, "warnings": warnings,
            "outputs": gather_tool_outputs(data)}


def parse_json_object(text):
    if not text:
        return None
    cleaned = re.sub(r"```(?:json)?", "", text)
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        value = json.loads(cleaned[start:end + 1])
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def fetch_x_post(args):
    base = os.environ.get("RELAY_BASE_URL", "").strip()
    key = os.environ.get("RELAY_API_KEY", "").strip()
    model = os.environ.get("RELAY_MODEL", "").strip() or "grok-4.5"
    missing = [n for n, v in (("RELAY_BASE_URL", base), ("RELAY_API_KEY", key)) if not v]
    if missing:
        return True, "缺少环境变量：%s。" % "、".join(missing)
    m = POST_URL.search(args.get("url") if isinstance(args.get("url"), str) else "")
    if not m:
        return True, "参数 url 不是有效的帖子链接，应形如 https://x.com/账号/status/数字。"
    author, pid = m.group(1), m.group(2)
    url = "https://x.com/%s/status/%s" % (author, pid)
    prompt = (
        "你是一个抓取工具。请用工具取回这条 X 帖子的完整内容：%s 。\n"
        "如果它是一个串，把同一作者的后续帖子按顺序拼在一起；如果它链接了一篇 X 长文章（article），把文章正文也取回。\n"
        "只输出一个 JSON 对象，不要任何其他文字：{\"post_text\": 帖子原文, \"article_title\": 文章标题或 null, "
        "\"article_text\": 文章正文原文或 null, \"got_article\": true 或 false}。\n"
        "原文不要改写、翻译或总结，尽量完整。取不到就如实填 null 和 false，不许凭记忆或推测补充。"
    ) % url
    warnings = []
    now = now_utc()
    started = time.time()
    status, raw = call_upstream(base, key, base_payload(model, prompt, {"type": "x_search"}), warnings)
    save_debug(raw, "fetch_" + pid, now)
    if status != 200:
        return True, upstream_error(status, raw, model)
    try:
        data = json.loads(raw)
    except ValueError:
        return True, "中转站返回的不是 JSON，无法解析。"
    answer = gather_message_text(data)
    tool_calls = gather_tool_calls(data)
    stats = usage_details(data)
    search_ran = bool(tool_calls) or bool(stats.get("x_search_calls"))
    if not search_ran:
        warnings.append("返回里没有任何抓取工具的执行痕迹，内容不可信，不要引用。")
    if isinstance(data, dict) and data.get("stream_truncated"):
        warnings.append("流式返回中途断开，内容可能不完整。")
    result = {"url": url, "author": author, "model": model, "search_ran": search_ran,
              "elapsed_seconds": round(time.time() - started, 1), "stats": stats}
    t = snowflake_time(pid)
    if t is not None and len(pid) >= 15:
        result["id_time_utc"] = t.strftime("%Y-%m-%d %H:%M:%S")
    obj = parse_json_object(answer)
    if obj is not None:
        for k in ("post_text", "article_title", "article_text"):
            if isinstance(obj.get(k), str) and obj[k].strip():
                result[k] = obj[k].strip()[:60000]
        result["got_article"] = bool(obj.get("got_article")) and bool(result.get("article_text"))
    else:
        warnings.append("模型没有按要求输出结构化内容，原文请看 raw_answer。")
        result["raw_answer"] = answer[:60000]
    result["warnings"] = warnings
    return False, json.dumps(result, ensure_ascii=False, indent=1)


# ---------------------------------------------------------------- 后台任务

JOBS = {}
JOBS_LOCK = threading.Lock()
JOB_SEQ = [0]
MAX_JOBS = 30


def wait_seconds():
    return max(0, min(55, env_int("X_WAIT_SECONDS", 45)))


def start_job(kind, impl, args):
    """在后台线程里跑一个耗时工具，返回 job_id。"""
    with JOBS_LOCK:
        JOB_SEQ[0] += 1
        job_id = "%s-%d-%d" % (kind, int(time.time()) % 100000, JOB_SEQ[0])
        job = {"kind": kind, "started": time.time(), "done": threading.Event(), "is_error": False, "text": ""}
        JOBS[job_id] = job
        # 只保留最近的若干个，先清理已完成的旧任务
        if len(JOBS) > MAX_JOBS:
            for old_id in sorted(JOBS, key=lambda k: JOBS[k]["started"]):
                if len(JOBS) <= MAX_JOBS:
                    break
                if JOBS[old_id]["done"].is_set():
                    del JOBS[old_id]

    def run():
        try:
            job["is_error"], job["text"] = impl(args)
        except Exception as e:  # 任何意外都转成工具错误
            log("%s 出现意外错误: %r" % (kind, e))
            job["is_error"], job["text"] = True, "工具内部出错：%s" % type(e).__name__
        job["finished"] = time.time()
        job["done"].set()

    threading.Thread(target=run, daemon=True).start()
    return job_id


def job_reply(job_id, wait):
    """等待最多 wait 秒。完成则返回真实结果，否则返回进行中的状态。"""
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if job is None:
        return True, json.dumps({"status": "not_found", "job_id": job_id,
                                 "hint": "找不到这个任务，可能是服务器重启过。请重新调用 search_x 或 fetch_x_post。"},
                                ensure_ascii=False, indent=1)
    if job["done"].wait(wait):
        return job["is_error"], job["text"]
    return False, json.dumps({
        "status": "running",
        "job_id": job_id,
        "elapsed_seconds": round(time.time() - job["started"], 1),
        "hint": "还在搜。等 30 到 60 秒后调用 get_x_result 并传入这个 job_id；不要重新发起同样的搜索。",
    }, ensure_ascii=False, indent=1)


def search_x_tool(args):
    return job_reply(start_job("search", search_x, args), wait_seconds())


def fetch_x_post_tool(args):
    return job_reply(start_job("fetch", fetch_x_post, args), wait_seconds())


def get_x_result_tool(args):
    job_id = args.get("job_id")
    if not isinstance(job_id, str) or not job_id.strip():
        return True, "参数 job_id 不能为空。"
    return job_reply(job_id.strip(), wait_seconds())


# ---------------------------------------------------------------- MCP 协议（stdio）

def send(msg):
    sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def reply(msg_id, result):
    send({"jsonrpc": "2.0", "id": msg_id, "result": result})


def reply_error(msg_id, code, message):
    send({"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}})


def handle(msg):
    if not isinstance(msg, dict):
        return
    method = msg.get("method")
    msg_id = msg.get("id")
    is_request = "id" in msg and method is not None
    params = msg.get("params") if isinstance(msg.get("params"), dict) else {}

    if method == "initialize":
        version = params.get("protocolVersion")
        reply(msg_id, {
            "protocolVersion": version if isinstance(version, str) else DEFAULT_PROTOCOL,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        })
    elif method == "ping":
        reply(msg_id, {})
    elif method == "tools/list":
        reply(msg_id, {"tools": TOOLS})
    elif method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
        impl = {"search_x": search_x_tool, "fetch_x_post": fetch_x_post_tool,
                "get_x_result": get_x_result_tool}.get(name)
        if impl is None:
            reply_error(msg_id, -32602, "未知的工具: %s" % name)
            return
        try:
            is_error, text = impl(args)
        except Exception as e:  # 任何意外都转成工具错误，不让服务器崩掉
            log("%s 出现意外错误: %r" % (name, e))
            is_error, text = True, "工具内部出错：%s" % type(e).__name__
        reply(msg_id, {"content": [{"type": "text", "text": text}], "isError": is_error})
    elif is_request:
        reply_error(msg_id, -32601, "不支持的方法: %s" % method)
    # 其余是通知（例如 notifications/initialized），不需要回复


def cli(argv):
    """命令行模式：直接搜一次并打印 JSON。成功返回 0，失败返回 1。"""
    import argparse
    parser = argparse.ArgumentParser(prog="x_search_mcp.py", description="X 搜索（命令行模式）")
    sub = parser.add_subparsers(dest="cmd", required=True)
    ps = sub.add_parser("search", help="搜帖子")
    ps.add_argument("query")
    ps.add_argument("--hours", type=int, default=24)
    ps.add_argument("--max-posts", type=int, default=20)
    ps.add_argument("--language", choices=["zh", "en", "any"], default="any")
    ps.add_argument("--coverage", choices=["wide", "fast"], default="wide")
    ps.add_argument("--handles", default="", help="只搜这些账号，逗号分隔")
    ps.add_argument("--exclude-handles", default="", help="排除这些账号，逗号分隔")
    ps.add_argument("--include-replies", action="store_true")
    pf = sub.add_parser("fetch", help="取回一条帖子的全文")
    pf.add_argument("url")
    ns = parser.parse_args(argv)
    if ns.cmd == "search":
        is_error, text = search_x({
            "query": ns.query, "hours": ns.hours, "max_posts": ns.max_posts,
            "language": ns.language, "coverage": ns.coverage,
            "handles": [h for h in ns.handles.split(",") if h.strip()],
            "exclude_handles": [h for h in ns.exclude_handles.split(",") if h.strip()],
            "include_replies": ns.include_replies,
        })
    else:
        is_error, text = fetch_x_post({"url": ns.url})
    if is_error:
        text = json.dumps({"error": text}, ensure_ascii=False, indent=1)
    sys.stdout.write(text + "\n")
    sys.stdout.flush()
    return 1 if is_error else 0


def main():
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    load_dotenv()
    if len(sys.argv) > 1:
        sys.exit(cli(sys.argv[1:]))
    log("已启动，模型 %s" % (os.environ.get("RELAY_MODEL", "").strip() or "grok-4.5"))
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            reply_error(None, -32700, "无法解析的消息")
            continue
        if isinstance(msg, list):
            for item in msg:
                handle(item)
        else:
            handle(msg)


if __name__ == "__main__":
    main()
