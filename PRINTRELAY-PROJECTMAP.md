# Print Relay — 项目地图 (Project Map)

> **生成时间:** 2026-05-18  
> **项目名称:** Print Relay — 通用订单打印网关  
> **域名:** printrelay.es · api.printrelay.es  
> **架构:** v6 Cloud-First (云端渲染 ESC/POS → base64 → 客户端纯通道)

---

## 一、项目概览

Print Relay 是一个 SaaS 云打印平台，对标 PrintNode，目前先跑通餐厅 MVP，后续铺开多行业模板市场。

### 核心能力
- HTTP POST → ESC/POS 热敏打印机
- 白名单安全模型（Token 制）
- 云端 Jinja2 + JSON 模板渲染
- TCP 长连客户端（Win EXE，纯通道）
- WooCommerce 插件（自动出单 + Telegram 通知）
- 控制面板（配对/路由/测试/模板市场）
- 可视化模板编辑器
- POS 多 Token 分离路由

---

## 二、部署架构

```
printerelay.es (HTTPS, Caddy)
├── :443 → api.printerelay.es → relay :51901 (面板/API)
├── :443 → printerelay.es  → relay :51901 (公开页面)
└── :51902 TCP → relay :51900 (客户端长连)
```

### 容器
| 容器 | 端口 | 用途 |
|------|------|------|
| print-relay (生产) | 51900:51901 | relay.thecarte.eu |
| print-relay-es (测试) | 51902:51903 | api.printerelay.es |

### 渲染模型 (v6)
```
订单 JSON → relay 加载模板 → Jinja2 渲染 → ESC/POS bytes → base64 编码
→ 推送 {"ticket_b64":"...", "printer":"XP-80C"}
→ 客户端 base64 解码 → send_raw(printer, ticket)
```

---

## 三、源码清单

### 工作区: /opt/data/workspace/print-relay/

| 文件 | 行数 | 说明 |
|------|------|------|
| relay-server.py | 1166 | VPS 端 — HTTP API + 面板 + 云端渲染 + TCP (纯 stdlib) |
| printer-client.py | 683 | Win 客户端 v6 — 纯通道 (base64→send_raw) |
| woo-print-relay.php | 284 | WC 插件 v3.1 — 多 Token + Telegram 通知 |
| template-editor.html | — | 可视化模板编辑器 (单文件，3 栏) |
| Dockerfile | — | relay server 容器化 |
| build.py | — | Win EXE 打包 (PyInstaller) |
| installer.nsi | — | NSIS 安装包 (MUI2 English, v4.4.1) |
| build.bat | — | Windows 一键构建 |
| API.md | — | 对外 API 对接文档 |
| DEVELOPER.md | — | 开发者文档 |
| requirements.txt | — | Python 依赖 |
| logo.png / logo.ico | — | 应用图标 |
| config.ini | — | 客户端配置模板 |
| relay-server-v1-backup.py | — | v1 旧版备份 |

### Docker 构建上下文: /opt/data/adminfiles/print-relay/

| 文件 | 说明 |
|------|------|
| relay-server.py | relay server (部署副本) |
| printer-client.py | Win 客户端 (部署副本) |
| templates/ | 5 套 JSON 模板（旧版） |
| gen_icon.py | 图标生成脚本 |
| backups/ | v1 旧版备份 |

### 模板库: /data/templates/ (31 套 / 10 行业)

| 行业 | 模板 |
|------|------|
| restaurant | kitchen, cashier, bar, dinein, delivery, takeaway |
| retail | receipt, return, price_label |
| logistics | waybill, warehouse, shipping |
| repair | pickup, estimate, workorder |
| medical | prescription, lab, registration |
| hotel | folio, laundry, checkin |
| auto | carwash, parking, maintenance |
| beauty | service, appointment |
| education | attendance, exam_label |
| general | note, qr_ticket, number_queue |

---

## 四、API 端点

| 端点 | Auth | 说明 |
|------|------|------|
| POST /api/register | 无 | 注册账号 → account_token |
| POST /api/login | 无 | 登录 → account_token |
| GET /panel?token=xxx | account | 用户控制面板 |
| GET /admin?token=90edba8f | admin | 管理员面板 |
| POST /wc?token=xxx | account | 推送订单 JSON → 打印 |
| GET /api/templates?token=xxx | account | 模板列表 |
| GET /api/templates/<path>?token=xxx | account | 单个模板 JSON |
| POST /api/pairings?token=xxx | account | 添加配对（白名单）|
| POST /api/routes?token=xxx | account | 配置路由 |
| GET /api/history?token=xxx | account | 打印历史 |
| GET /api/forget?token=xxx | account | 清除配对 |
| GET /editor | 无 | 可视化模板编辑器 |
| GET / | 无 | SaaS 首页 |
| GET /download | 无 | 下载页 |
| GET /pricing | 无 | 定价页 |
| GET /health | 无 | 健康检查 |
| GET /dl/SETUP.exe | 无 | 安装包下载 |

### 管理员 Token: 90edba8f0283b2c1

---

## 五、WooCommerce 插件 (v3.1)

**位置:** woo-print-relay.php → 部署到 /var/www/html/wp-content/plugins/

**功能:**
- 激活自动生成 3 个 Token（All / Kitchen / Bar）
- 下单触发 woocommerce_checkout_order_processed → 推送订单 JSON → 打印
- Telegram 通知（HTML parse_mode，中文/德文/西文/英文自动切换）
- WP 后台设置页（Token 显示 + Telegram 配置）

**Token 格式:**
- 主 Token: 8 位 hex（如 a59fdae6）
- 厨房 Token: k_xxxxxxxx
- 吧台 Token: b_xxxxxxxx

---

## 六、Win 客户端 (v6)

**连接地址:** printerelay.es:51902  
**架构:** 纯通道 — zero local rendering  
**config.ini:** 仅存打印机名映射

```
[printer.kitchen]
name = XP-80C
```

**GUI 功能:** 打印机扫描 · 下拉选择 · 保存配置 · 托盘图标 · 开机自启

---

## 七、当前状态

| 项目 | 状态 |
|------|------|
| printerelay.es 公开页面 | 在线 (首页/下载/定价) |
| api.printerelay.es 面板 | 在线 |
| 模板库 | 31 套 / 10 行业 |
| 可视化模板编辑器 | 在线 |
| WC 插件 v3.1 | test.thecarte.eu 运行 |
| 客户端 v6 EXE | 构建通过 |
| 客户端 v4.4.1 (生产) | 构建通过 |
| GitHub Actions CI | 自动构建 |
| Caddy HTTPS | 正常 |
| Cloudflare DNS | relay.thecarte.eu 灰云 |

---

## 八、Git 仓库

**Remote:** https://github.com/chunhaiwu2020/print-relay-client  
**分支:** main · v4.4.1-build

**最近提交:**
1. 1f85aa2 — v6: add default station, remove 逐菜切纸 GUI (now relay-side)
2. 0df445f — relay: add cloud ESC/POS rendering engine
3. 768fd88 — v6: cloud-first pure channel
4. 081a90d — client: restore v4 multi-station
5. 3d8c187 — v5: cloud-first protocol

---

## 九、修改日志

> 本文档每次功能修改后更新此节。

| 日期 | 改动 | 作者 |
|------|------|------|
| 2026-05-18 | 创建项目地图 | 俊俊 |

---

## 十、关键决策记录

1. **云端渲染 (v6):** 客户端零渲染，模板全在 relay 侧，31 套模板库 / 10 行业
2. **多 Token 分离:** POS 用 3 个独立 Token（All/Kitchen/Bar）控制打印目标
3. **白名单安全:** 未注册 Token → 403，插件激活自动注册
4. **品牌铁律:** 公开页面不出现 PrintNode 名称
5. **命名 Volume:** print-relay-data（生产）/ print-relay-es-data（测试）

---

## 十一、部署命令速查

```bash
# 重建 relay 镜像
cd /opt/data/adminfiles/print-relay
docker build --no-cache -t print-relay .

# 重启测试容器
docker restart print-relay-es

# 部署 relay-server.py 更新
docker cp /opt/data/adminfiles/print-relay/relay-server.py print-relay-es:/app/
docker restart print-relay-es

# 部署模板编辑器
docker cp /opt/data/workspace/print-relay/template-editor.html print-relay-es:/data/
docker restart print-relay-es

# 部署 SETUP.exe
docker cp SETUP.exe print-relay-es:/data/SETUP.exe
docker restart print-relay-es

# 查看状态
curl -s https://api.printerelay.es/health
```

---

*最后更新: 2026-05-18*
