# Print Relay — 通用云打印网关

> HTTP POST → 热敏打印机。自托管 · 白名单 · 多行业模板 · 云端渲染。

[![SaaS](https://img.shields.io/badge/status-beta-orange)](https://printrelay.es)
[![Client](https://img.shields.io/badge/client-v6-blue)](https://printrelay.es/download)
[![Templates](https://img.shields.io/badge/templates-31-green)](https://api.printrelay.es/editor)

---

## 是什么

一行代码把订单打印到店里热敏打印机：

```bash
curl -X POST https://api.printrelay.es/wc?token=YOUR_TOKEN \
  -H "Content-Type: application/json" \
  -d '{"number":"88","total":"25.00","line_items":[...]}'
```

打印机出票 ✅。不需要装驱动、配端口、写 ESC/POS。

---

## 架构

```
你的系统 (WooCommerce/POS/Shopify/自定义)
       │
       ▼  POST /wc?token=xxx
┌─────────────────────┐
│  Print Relay (VPS)  │  ← 云端渲染 ESC/POS
│  白名单 · 路由 · 模板  │
└────────┬────────────┘
         │  TCP 推送 ticket_b64
         ▼
┌─────────────────────┐
│  Win 客户端 (店PC)   │  ← 纯通道，解码即打
│  多打印机 · 开机自启  │
└─────────────────────┘
```

**v6 核心:** 模板渲染全在云端，客户端是"傻"通道——收到 base64 解码直接 `send_raw()`，零本地逻辑。

---

## 功能

| 模块 | 说明 |
|------|------|
| 🔐 白名单安全 | Token 注册制，未注册 → 403 |
| 🧭 多路由 | 同一订单 → 厨房/吧台/前台 三路出票 |
| 📋 模板市场 | 31 套内置模板 / 10 行业（餐饮/零售/维修/物流/酒店…） |
| ✏️ 可视化编辑器 | 拖拽排版、实时预览、云端保存 |
| 🧩 WC 插件 | 激活即用，自动出单 + Telegram 通知 |
| 💻 Win 客户端 | 托盘运行、开机自启、多打印机 |
| 🌐 公开首页 | printrelay.es — 定价/下载/文档 |

---

## 仓库结构

```
print-relay-client/
├── relay-server.py         # VPS 端 (HTTP + TCP + 云端渲染 + 面板)
├── printer-client.py       # Win 客户端 v6 (纯通道)
├── woo-print-relay.php     # WooCommerce 插件 v3.1
├── template-editor.html    # 可视化模板编辑器
├── Dockerfile              # relay 容器
├── build.py / build.bat    # Win EXE 打包
├── installer.nsi           # NSIS 安装包
├── API.md                  # 对外 API 对接文档
├── PRINTRELAY-PROJECTMAP.md # 项目地图（完整技术细节）
└── README.md               # 👈 你在这里
```

---

## 快速开始

### 1. 注册账号

```bash
curl -X POST https://api.printrelay.es/api/register \
  -H "Content-Type: application/json" \
  -d '{"email":"you@example.com","password":"your_pass"}'
# → {"token": "tok_xxxxxxxxxxxx"}
```

### 2. 发测试订单

```bash
curl -X POST "https://api.printrelay.es/wc?token=tok_xxx" \
  -H "Content-Type: application/json" \
  -d '{
    "number":"1001",
    "total":"15.50",
    "line_items":[
      {"name":"红烧牛肉面","quantity":2,"price":"7.75","total":"15.50"}
    ]
  }'
```

### 3. 装客户端

下载 [SETUP.exe](https://printrelay.es/download) → 双击安装 → 开机自启。

### 4. 配路由

打开面板 → 添加配对 → 添加路由（来源 Token → 目标客户端 → 打印机 → 模板）→ 完成。

---

## 文档导航

| 文档 | 用途 |
|------|------|
| [API.md](API.md) | 第三方对接 — 端点/请求体/错误码 |
| [PRINTRELAY-PROJECTMAP.md](PRINTRELAY-PROJECTMAP.md) | 项目地图 — 源码清单/状态/部署命令 |
| [DEVELOPER.md](DEVELOPER.md) | WC 插件开发说明 |
| [printrelay.es](https://printrelay.es) | 公开首页 |
| [api.printrelay.es/editor](https://api.printrelay.es/editor) | 模板编辑器 |

---

## 技术栈

- **Relay:** Python 3 (纯 stdlib + Jinja2) · Docker
- **客户端:** Python 3 + tkinter · PyInstaller · NSIS
- **渲染:** JSON 模板 → Jinja2 → ESC/POS → base64
- **协议:** TCP 长连 (4 字节长度头) · HTTP REST
- **部署:** Docker · Caddy · Cloudflare DNS

---

## 路线图

- [x] 云端 ESC/POS 渲染 (v6)
- [x] 31 套行业模板
- [x] 可视化模板编辑器
- [x] WC 插件 + Telegram 通知
- [ ] HTML → A4 PDF 管线
- [ ] QR 码服务器生成
- [ ] 多租户订阅制
- [ ] 移动端管理面板

---

*自托管云打印 · 欧洲数据 · 餐厅起步*
