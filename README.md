# x-search

X 搜索 MCP 服务器（stdio，零依赖，只用 Python 自带库）。Grok 只负责搜，返回原始帖子；读和写交给调用它的模型。

## 工具

- `search_x`：搜帖子。默认 `coverage=wide`，grok-4.5（中文社区）和 grok-4.7（英文高互动）同时搜再合并去重；`coverage=fast` 只用主型号。
  每条帖子带 `id_time_utc`（由帖子 ID 反推的真实发帖时间）和 `found_by`（哪个型号找到的）。
- `fetch_x_post`：取回一条帖子的完整内容，包括长文章正文。用于 `is_article` 为 true 的帖子。
- `get_x_result`：取回上面两个工具的结果。

### 为什么要分两步

一次搜索要 2 到 4 分钟，而 Claude 云端会话里一次工具调用最多等 60 秒。所以 `search_x` 和 `fetch_x_post`
最多等 45 秒（`X_WAIT_SECONDS` 可改）：没搜完就先返回 `status: running` 和一个 `job_id`，
搜索在后台继续，之后用 `get_x_result` 传入 `job_id` 取结果。

## 命令行模式

不经过 MCP，直接搜一次，结果是 JSON：

    python3 x_search_mcp.py search "要搜的内容" --hours 72 --coverage fast
    python3 x_search_mcp.py fetch https://x.com/账号/status/数字

命令行没有 60 秒的限制，一条命令等到搜完为止（实测 72 小时范围约 6 分钟）。
Claude 的 x-search skill 用的就是这个入口。不带参数启动时才是 MCP 服务器。

## HTTP 模式（接成 Claude 连接器，任意对话可用）

在一台有公网入口的服务器上常驻运行，然后在 Claude 的连接器设置里添加它的网址。
部署步骤见 `deploy/README.md`。要点：

- 启动：`python3 x_search_mcp.py serve --port 8765`，默认只监听本机 `127.0.0.1`，由前面的 HTTPS 入口转发进来。
- 访问口令 `X_HTTP_TOKEN` 放在服务器的 `.env` 里。服务入口是 `POST /mcp/<口令>`，不带正确口令一律 404。
- 连接器里填的网址形如 `https://你的域名/mcp/<口令>`。**这个网址等同于密码**，不要发到聊天里或提交到仓库。
- 中转站的真 key 也放在服务器的 `.env` 里（`RELAY_API_KEY=真key`），不用 `proxy` 占位。

## 密钥放哪

**本仓库不含任何密钥，也不要往里提交密钥。** `.env` 已在 `.gitignore` 里。

### 在 Claude 云端会话里用

1. 以本仓库开一个云端会话，`.mcp.json` 会自动把 x-search 注册进来。
2. key 存在云端环境的 Network secrets 里（环境设置 → Network secrets → Add secret）：
   - Allowed websites：`www.zzshu.cc`
   - Custom headers：名称 `Authorization`，前缀 `Bearer`，值填中转站的 key
3. `.mcp.json` 里的 `RELAY_API_KEY=proxy` 是占位值，意思是"程序自己不带 key，由代理补上"。

### 在本机用

在脚本同目录建一个 `.env`（不会被提交）：

    RELAY_BASE_URL=https://www.zzshu.cc/v1
    RELAY_API_KEY=你的key
    RELAY_MODEL=grok-4.5
    RELAY_MODEL_ALT=grok-4.7     # 可省略；填 none 关闭双型号

然后注册为全局（Windows 上通常是 `python`）：

    claude mcp add x-search --scope user -- python 完整路径/x_search_mcp.py

注意：本机用时不要走仓库里的 `.mcp.json`，它的占位值会让请求不带 key。

## 文件

- `x_search_mcp.py`：MCP 服务器、命令行和 HTTP 模式（0.6.0）
- `deploy/`：服务器部署说明和 systemd 配置
- `x_model_compare.py`：同一个问题对比不同 Grok 型号的搜索效果
- `.mcp.json`：云端会话用的 MCP 注册

## 实测（2026-11-11，对真实中转站，本机）

- 双型号合并搜索：135 秒，24 条不重复帖子，中英各半，全部落在时间窗口内
- 长文章抓取：能取回全文（约 9500 字），耗时 1 到 3 分钟
- 图片理解：参数能传，但没有返回图片描述，暂不可用
- 搜索工具原始输出（include）：中转站返回 400，不可用
