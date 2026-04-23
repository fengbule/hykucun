# Hykucun Stock Monitor

一个带 WebUI 的库存监控服务，默认适配核云 `product-card` 页面，也可以在页面里配置其他网站的 CSS 选择器。检测到补货后会向 Telegram 推送商品卡片，并附带包含 AFF 的购买链接按钮。

## 功能

- 多监控网页配置
- 每个监控项独立设置检测间隔、CSS 选择器、库存正则和标题过滤
- Docker 镜像内置 Playwright Chromium，可在 WebUI 直接切换 Requests / Browser
- 每个监控项独立配置 AFF 前缀/模板
- 支持“通知通道池”：可预先保存多个 Telegram 机器人/频道配置，再让每个监控项下拉选择通知目标
- 保留全局默认 Telegram，老配置可继续使用
- Telegram 推送 HTML 商品卡片和购买按钮
- 当前可购买商品只要是新出现的，或者相比上一轮库存/可购状态发生变化，就会按补货发送新推送
- 推送后的商品卡片会在库存、价格或状态变化时自动编辑原消息，频道里不刷屏
- SQLite 保存配置和库存快照
- Docker / docker compose 部署

## Docker 部署

```bash
git clone https://github.com/fengbule/hykucun.git
cd hykucun
```

这个仓库是公开仓库，服务器上 `git clone` 不需要 GitHub 账号密码。创建 `.env` 配置默认 Telegram 和 WebUI 密码，不要把真实密码提交到仓库：

```env
SECRET_KEY=换成随机字符串
WEBUI_PASSWORD=换成你的WebUI密码
TELEGRAM_BOT_TOKEN=你的默认BotToken
TELEGRAM_CHAT_ID=你的默认ChatID
TELEGRAM_MESSAGE_THREAD_ID=
```

项目没有默认 WebUI 密码；忘记密码时，直接修改 `.env` 里的 `WEBUI_PASSWORD` 后重启服务。只在可信内网临时使用时，可以把 `WEBUI_PASSWORD` 留空来关闭 WebUI 登录。

如果你从旧版本升级，服务器上可能还留有 `docker-compose.override.yml` 并覆盖 `.env`。更新后如果容器反复重启并提示 `SECRET_KEY is using a public placeholder`，先清掉旧 override 里的占位值：

```bash
cp docker-compose.override.yml docker-compose.override.yml.bak.$(date +%s) 2>/dev/null || true
sed -i '/SECRET_KEY:/d;/WEBUI_PASSWORD:/d' docker-compose.override.yml 2>/dev/null || true
```

如果服务器面板要求使用外部端口，例如 `1457`，把端口写到同一个 `.env`：

```bash
echo "WEB_PORT=1457" >> .env
```

启动：

```bash
docker compose up -d --build
```

访问：

```text
http://服务器IP:8000
```

如果设置了 `WEB_PORT=1457`，访问：

```text
http://服务器IP:1457
```

## 通知通道池怎么用

WebUI 里现在分成两层：

1. **默认 Telegram**
   - 这是全局兜底出口。
   - 监控项没有绑定单独通道时，就用这里。

2. **通知通道**
   - 可以新建多个通道，例如：`主频道`、`测试频道`、`私聊提醒`、`香港库存频道`。
   - 每个通道保存自己的 `Bot Token`、`Chat ID`、`Topic ID`。
   - 监控项里新增了 `通知目标` 下拉框，可直接选择某个通道。

这样多个监控项可以复用同一个通知目标，后续如果换机器人或换频道，只改那一个通道即可。

## Telegram 频道对接

1. 在 Telegram 里用 `@BotFather` 创建一个 Bot，拿到 Bot Token。
2. 创建你的频道，把这个 Bot 拉进频道。
3. 把 Bot 设置成频道管理员，否则 Bot 没法往频道发消息。
4. 在 WebUI 的 **默认 Telegram** 或 **通知通道** 里填写：

```env
TELEGRAM_BOT_TOKEN=123456:ABCDEF_xxx
TELEGRAM_CHAT_ID=@your_channel_name
TELEGRAM_MESSAGE_THREAD_ID=
```

- 公开频道：`TELEGRAM_CHAT_ID` 直接填 `@频道用户名`
- 私有频道：`TELEGRAM_CHAT_ID` 填频道的数字 Chat ID（通常是 `-100` 开头）
- 普通频道：`TELEGRAM_MESSAGE_THREAD_ID` 留空
- 只有带话题的论坛群组，才需要填写 `TELEGRAM_MESSAGE_THREAD_ID`

填好后，在 WebUI 里点一次 `测试默认通道` 或某个通知通道的 `测试`，能收到消息就说明对接成功。

## 常用更新命令（可直接复制）

在服务器项目目录执行：

```bash
cd ~/hykucun/hykucun
git pull --ff-only origin main
docker compose up -d --build
```

如果你实际目录不是 `~/hykucun/hykucun`，先用下面命令找项目目录：

```bash
find ~ -maxdepth 3 -name docker-compose.yml -print
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

## 测试

仓库新增了针对通知通道池的单元测试：

```bash
python -m unittest discover -s tests -v
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

说明目标站拦截了普通后台请求。编辑这个监控项，把 `请求模式` 改为 `Browser 浏览器模式`，并把 `浏览器等待秒数` 调到 `10-20` 秒。Docker 镜像内置 Playwright Chromium，所以 WebUI 可以直接切换模式。

浏览器模式占用明显更高，建议把这类监控间隔调到 `300-600` 秒，不要 10 秒或 20 秒跑一次。

如果浏览器模式仍然提示需要真人验证，说明目标站启用了交互式 Cloudflare 验证，这类页面无法用纯后台脚本稳定监控，只能换接口、RSS、官方 API，或让目标站放行你的服务器 IP。

对于 VMISS 这类站点，可以在监控项里填写 `Cookie（可选，防护站点用）`，格式示例：

```text
cf_clearance=xxxx; __cf_bm=yyyy
```

然后再执行立即检查。若 Cookie 失效会再次出现安全验证提示，需要重新获取。
