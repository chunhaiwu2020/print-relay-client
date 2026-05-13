# Print Relay — WooCommerce 门店自动打印

## 架构

```
WooCommerce 新订单
    │ webhook POST
    ▼
┌──────────────────┐
│  VPS relay-server │  ← Docker (Python 3.11, 零依赖)
│  TCP :51900       │  ← 客户端长连接
│  HTTP :51901      │  ← WooCommerce webhook 入口
└──────────────────┘
    │ TCP (outbound, 零端口转发)
    ▼
┌──────────────────────┐
│ 店里 Windows 打印机    │
│ printer-client.exe    │
│  → 接收 ESC/POS       │
│  → 转发本地打印机      │
└──────────────────────┘
```

---

## 1. VPS 部署 relay server

```bash
cd /opt/data/workspace/print-relay

# 设置 Token（安全）
export RELAY_TOKEN="your-secret-token-here"

# 构建镜像
docker build -t print-relay .

# 启动
docker run -d --name print-relay \
  --restart unless-stopped \
  -p 51900:51900 \
  -p 51901:51901 \
  -e RELAY_TOKEN="$RELAY_TOKEN" \
  print-relay
```

验证:
```bash
curl http://localhost:51901/health        # → OK
curl http://localhost:51901/clients       # → {"clients": []}
```

---

## 2. 店里 Windows 部署客户端

### 2.1 打包 EXE
在 Windows 开发机上:
```cmd
pip install -r requirements.txt
python build.py
```
产物: `dist/PrintRelay-Client.exe` (~15MB)

### 2.2 安装到店里 PC
- 复制 EXE 到 `C:\PrintRelay\`
- 双击运行
- 设置:
  - **VPS Adresse**: 你的 VPS IP 或域名
  - **Port**: 51900
  - **Client Name**: kitchen (或其他名字)
  - **Token**: 同 VPS 端 REPAY_TOKEN
  - **Drucker**: 下拉选择热敏打印机
  - **Papierbreite**: 80mm

点 `Speichern` → `Verbinden`，看到绿色指示灯即完成。

### 2.3 开机自启 (可选)
按 Win+R → `shell:startup` → 放 `PrintRelay-Client.exe` 快捷方式进去。

---

## 3. WooCommerce Webhook 配置

WooCommerce 后台 → **Einstellungen → Erweitert → Webhooks**

| 字段 | 值 |
|------|---|
| Name | Print Relay - Neue Bestellung |
| Status | Aktiv |
| Thema | Bestellung erstellt (Order Created) |
| Auslieferungs-URL | `http://VPS_IP:51901/wc?token=你的TOKEN` |
| Secret | (留空，用 token 就行) |
| API-Version | WP REST API Integration v3 |

---

## 4. 测试打印

```bash
# 模拟新订单
curl -X POST http://VPS_IP:51901/wc?token=TOKEN \
  -H 'Content-Type: application/json' \
  -H 'X-Print-Client: kitchen' \
  -H 'X-Paper-Width: 80' \
  -d '{
    "number": "9999",
    "date_created": "2026-05-13T14:30:00",
    "payment_method_title": "Barzahlung",
    "line_items": [
      {"name": "Peking Suppe", "quantity": 2, "price": 3.50},
      {"name": "Frühlingsrolle", "quantity": 1, "price": 4.50}
    ],
    "shipping_total": "0.00",
    "customer_note": "Bitte scharf"
  }'
```

---

## 5. 安全

- `RELAY_TOKEN` 必须设置
- VPS 防火墙只开放 51900 给客户端 IP（可选但推荐）
- HTTP 接口 51901 建议仅内网访问，WooCommerce 和 VPS 同机则用 `127.0.0.1:51901`
