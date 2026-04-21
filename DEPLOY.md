# Docker 部署说明

## 1. 启动服务

```bash
git clone https://github.com/fengbule/hykucun.git
cd hykucun
docker compose up -d --build
```

默认监听 `8000` 端口：

```text
http://服务器IP:8000
```

## 2. Telegram 环境变量

这个仓库是公开仓库，服务器上 `git clone` 不需要 GitHub 账号密码。建议创建 `docker-compose.override.yml` 配置 Telegram 和 WebUI 密码：

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

然后重启：

```bash
docker compose up -d --build
```

默认镜像内置 Playwright/Chromium，WebUI 可以直接在 Requests 和 Browser 模式之间切换。

## 3. WebUI 配置

进入 WebUI 后可以配置：

- Telegram Bot Token / Chat ID / Topic ID
- 监控网页 URL
- 检测间隔
- 请求模式：Requests 轻量模式 / Browser 浏览器模式
- 商品卡片、标题、库存、价格、按钮、购买链接 CSS 选择器
- 库存正则
- 有货词和缺货词
- AFF 前缀/模板

默认已经内置核云周年庆页面：

```text
https://www.heyunidc.cn/cart?fid=49&gid=97
```

## 4. AFF 购买链接

Telegram 推送会带 `打开购买链接` 按钮。购买链接会按监控项里的 AFF 配置生成。

示例：

```text
?aff=123
```

表示在购买链接后追加 `aff=123`。

```text
https://example.com/redirect?url={encoded_url}
```

表示把购买链接 URL 编码后放到模板里。

## 5. 查看日志

```bash
docker compose logs -f
```

## 6. 更新

```bash
git pull
docker compose up -d --build
```

## 7. Cloudflare 403

如果监控 VMISS 这类网站出现 `403 Client Error: Forbidden`，通常是 Cloudflare challenge。编辑监控项，把 `请求模式` 改为 `Browser 浏览器模式`，并把 `浏览器等待秒数` 调到 `10-20` 秒。

浏览器模式占用明显更高，建议监控间隔设置为 `300-600` 秒，不要 10 秒或 20 秒跑一次。

如果浏览器模式仍然失败，说明目标站要求交互式真人验证，纯后台脚本无法稳定通过，需要换接口、官方 API，或让目标站放行服务器 IP。

如果浏览器模式能加载页面但显示 `no_products` 或没有商品快照，说明页面结构和 CSS 选择器不匹配。VMISS/WHMCS 页面会自动尝试按 `h3 产品名 + Order Now + 0 Available` 兜底解析；升级后仍无商品时，先清空标题过滤，再点一次立即检查。
