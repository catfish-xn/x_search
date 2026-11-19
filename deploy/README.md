# 在服务器上部署 x-search（HTTP 模式）

目标：让 `https://你的域名/mcp/<口令>` 能被 Claude 的连接器访问。
以 Ubuntu 为例，命令都用 root 或 sudo 执行。

## 1. 放代码

    git clone https://github.com/catfish-xn/x_search /opt/x_search
    useradd --system --home /opt/x_search --shell /usr/sbin/nologin xsearch

## 2. 写 .env（密钥只存在这一个文件里）

先生成访问口令：

    python3 -c "import secrets;print(secrets.token_urlsafe(32))"

然后 `nano /opt/x_search/.env`，写入：

    RELAY_BASE_URL=https://www.zzshu.cc/v1
    RELAY_API_KEY=中转站的真key
    RELAY_MODEL=grok-4.5
    RELAY_MODEL_ALT=grok-4.7
    X_HTTP_TOKEN=上一步生成的口令

收紧权限，只有服务账号能读：

    chown -R xsearch:xsearch /opt/x_search
    chmod 600 /opt/x_search/.env

## 3. 装成服务

    cp /opt/x_search/deploy/x-search.service /etc/systemd/system/
    systemctl daemon-reload
    systemctl enable --now x-search
    systemctl status x-search --no-pager

看到 `HTTP 模式已启动：127.0.0.1:8765` 就对了。本机自测（应返回工具列表）：

    TOKEN=$(grep ^X_HTTP_TOKEN= /opt/x_search/.env | cut -d= -f2)
    curl -s -X POST -H 'Content-Type: application/json' \
      -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' \
      http://127.0.0.1:8765/mcp/$TOKEN | head -c 200

## 4. 给它一个 HTTPS 入口

服务只监听本机，需要一个 HTTPS 入口转发到 `127.0.0.1:8765`。域名在 Cloudflare 上时，两种做法任选其一。

### 做法 A：Cloudflare Tunnel（不用开端口，不用管证书）

在 Cloudflare 控制台的 Zero Trust → Networks → Tunnels 里新建一条隧道，按页面提示在服务器上装 `cloudflared`，
然后给隧道加一条 Public Hostname：子域名（例如 `x.你的域名`）指向 `http://127.0.0.1:8765`。

### 做法 B：服务器上已有 Nginx 或 Caddy

加一个子域名的站点，把请求反向代理到 `127.0.0.1:8765`。Caddy 的写法：

    x.你的域名 {
        reverse_proxy 127.0.0.1:8765
    }

Cloudflare 里给这个子域名加一条 A 记录指向服务器 IP。

## 5. 验证公网入口

在任意一台电脑上（把域名和口令换成你的）：

    curl -s -o /dev/null -w "%{http_code}\n" https://x.你的域名/            # 应为 404
    curl -s -X POST -H 'Content-Type: application/json' \
      -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' \
      https://x.你的域名/mcp/口令 | head -c 200                             # 应返回工具列表

## 6. 接到 Claude

Claude 的连接器设置里添加自定义连接器，网址填 `https://x.你的域名/mcp/口令`。

## 更新

    cd /opt/x_search && sudo -u xsearch git pull && systemctl restart x-search

## 换口令或换 key

改 `/opt/x_search/.env`，然后 `systemctl restart x-search`。换口令后记得更新连接器里的网址。
