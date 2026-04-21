# Hykucun Stock Monitor

一个带 WebUI 的库存监控服务，默认适配核云 `product-card` 页面，也可以在页面里配置其他网站的 CSS 选择器。检测到补货后会向 Telegram 推送商品卡片，并附带包含 AFF 的购买链接按钮。

## 功能

- 多监控网页配置
- 每个监控项独立设置检测间隔、CSS 选择器、库存正则和标题过滤
- 支持轻量 Requests 模式和 Playwright Browser 模式
- 每个监控项独立配置 AFF 前缀/模板
- Telegram 推送 HTML 商品卡片和购买按钮
- SQLite 保存配置和库存快照
- Docker / docker compose 部署

## Docker 部署

```bash
git clone https://github.com/fengbule/hykucun.git
cd hykucun

cp docker-compose.yml docker-compose.override.yml
```

这个仓库是公开仓库，服务器上 `git clone` 不需要 GitHub 账号密码。编辑 `docker-compose.override.yml` 配置 Telegram 和 WebUI 密码：

```yaml
services:
  heyunidc-monitor:
    environment:
      SECRET_KEY: "换成随机字符串"
      WEBUI_PASSWORD: "换成你的WebUI密码"
      TELEGRAM_BOT_TOKEN: "你的BotToken"
      TELEGRAM_CHAT_ID: "你的ChatID"
      TELEGRAM_MESSAGE_THREAD_ID: ""
```

如果只在内网临时使用，也可以把 `WEBUI_PASSWORD` 设为空字符串来关闭 WebUI 登录。

启动：

```bash
docker compose up -d --build
```

访问：

```text
http://服务器IP:8000
```

## AFF 写法

`AFF 前缀/模板` 支持三种方式：

```text
?aff=123
```

追加到购买链接后面。

```text
https://example.com/aff?target={encoded_url}
```

把购买链接 URL 编码后放到模板里。

```text
https://example.com/prefix/
```

直接拼接在购买链接前面。

模板变量：

- `{url}`：原始购买链接
- `{raw_url}`：原始购买链接
- `{encoded_url}`：URL 编码后的购买链接

## 本地运行

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python app.py
```

Windows PowerShell：

```powershell
C:\Users\fengbule\codex\.venv\Scripts\python.exe app.py
```

## CLI 检查

```bash
python restock_monitor.py --once
python restock_monitor.py --once --aff-template "?aff=123"
```

## Cloudflare 403

如果某个网站显示类似：

```text
403 Client Error: Forbidden
Cloudflare challenge returned 403
```

说明目标站拦截了普通后台请求。编辑这个监控项，把 `请求模式` 改为 `Browser 浏览器模式`，并把 `浏览器等待秒数` 调到 `10-20` 秒。Docker 镜像会内置 Playwright Chromium。

如果浏览器模式仍然提示需要真人验证，说明目标站启用了交互式 Cloudflare 验证，这类页面无法用纯后台脚本稳定监控，只能换接口、RSS、官方 API，或让目标站放行你的服务器 IP。
