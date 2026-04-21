# HeyunIDC Restock Monitor

一个带 WebUI 的库存监控服务，默认适配核云 `product-card` 页面，也可以在页面里配置其他网站的 CSS 选择器。检测到补货后会向 Telegram 推送商品卡片，并附带包含 AFF 的购买链接按钮。

## 功能

- 多监控网页配置
- 每个监控项独立设置检测间隔、CSS 选择器、库存正则和标题过滤
- 每个监控项独立配置 AFF 前缀/模板
- Telegram 推送 HTML 商品卡片和购买按钮
- SQLite 保存配置和库存快照
- Docker / docker compose 部署

## Docker 部署

```bash
git clone https://github.com/fengbule/heyunidc.git
cd heyunidc

cp docker-compose.yml docker-compose.override.yml
```

编辑 `docker-compose.override.yml`，至少修改：

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
