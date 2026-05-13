# Print Relay API 文档 v2

> 通用网页订单打印网关 — 任何 HTTP 系统 → ESC/POS 热敏打印机

---

## 1. 架构

```
您的系统                    Print Relay                打印机
┌──────────┐   HTTP POST    ┌──────────────┐   TCP     ┌──────┐
│ 任意系统  │ ─────────────→ │  relay server │ ───────→ │ 热敏  │
│ (Woo/    │  /wc?token=x   │  票据生成      │          │ 打印机 │
│  Odoo/   │                │  路由转发      │          │ 80mm  │
│  POS/等) │                └──────────────┘          └──────┘
└──────────┘
```

**原则：** 您的系统只需发一个 HTTP POST，其余全部自动处理。

---

## 2. 接入步骤

### 第一步：获取 Token

在 **Print Relay 控制面板** 中注册您的系统，获取一个 8 位 `TOKEN`。

或者直接调用注册接口：

```bash
curl -X POST http://relay.thecarte.eu:51901/api/register \
  -H "Content-Type: application/json" \
  -d '{"type": "woo", "name": "我的系统"}'
```

返回：
```json
{"ok": true, "token": "a1b2c3d4"}
```

### 第二步：等待审批

把 Token 告诉 Print Relay 管理员，在控制面板点 ✓ 审批。

### 第三步：发送订单

```
POST http://relay.thecarte.eu:51901/wc?token=您的TOKEN
Content-Type: application/json

{订单 JSON}
```

---

## 3. 订单数据格式

### 标准格式（推荐）

```json
{
  "number":         "ORD-2026-001",
  "date_created":   "2026-05-13T14:30:00",
  "payment_method_title": "Barzahlung",
  "line_items": [
    {
      "name":      "Peking Suppe",
      "quantity":  2,
      "price":     3.50
    },
    {
      "name":      "Frühlingsrolle",
      "quantity":  1,
      "price":     4.50
    }
  ],
  "shipping_total": "2.00",
  "customer_note":  "Bitte extra scharf"
}
```

| 字段 | 必填 | 说明 |
|------|------|------|
| `number` | 推荐 | 订单号，打印在票头 |
| `date_created` | 否 | ISO 8601 时间 |
| `line_items` | ✅ | 菜品列表 |
| `line_items[].name` | ✅ | 品名（支持 äöüß） |
| `line_items[].quantity` | ✅ | 数量 |
| `line_items[].price` | ✅ | 单价（数字） |
| `payment_method_title` | 否 | 支付方式 |
| `shipping_total` | 否 | 配送费 |
| `customer_note` | 否 | 顾客备注 |

### 极简格式

只发最关键字段：

```json
{
  "number": "001",
  "line_items": [
    {"name": "牛肉面", "quantity": 1, "price": 12.00}
  ]
}
```

### 其他系统格式

如果您的系统 JSON 结构不同（比如 Odoo 用 `order_line` 而不是 `line_items`），
可以使用 `?format=odoo` 参数，或联系我们加适配器。

---

## 4. HTTP 请求头

| Header | 必填 | 说明 |
|--------|------|------|
| `Content-Type` | ✅ | `application/json` |
| `X-Print-Client` | 否 | 指定客户端名（如 `kitchen`） |
| `X-Printer-Name` | 否 | 指定打印机名 |
| `X-Paper-Width` | 否 | `80` 或 `58`（默认 80） |

---

## 5. 小票效果

```
*Restaurant Asia Shanghai*
          thecarte.eu
================================
Bestell-Nr: #ORD-2026-001
Datum: 2026-05-13 14:30
Zahlung: Barzahlung
--------------------------------
2x  Peking Suppe                €7,00
1x  Frühlingsrolle              €4,50
--------------------------------
                   Gesamt: €11,50
            inkl. Lieferung: €2,00
--------------------------------
        Hinweis: Bitte extra scharf
================================
          Vielen Dank!
          13.05.2026 14:31
```

---

## 6. 返回码

| 状态码 | 含义 |
|--------|------|
| `200` | ✅ 打印任务已转发 |
| `400` | JSON 格式错误 |
| `403` | Token 无效或未审批 |
| `503` | 店 PC 客户端离线 |
| `504` | 转发超时 |

成功返回：
```json
{"status": "ok", "client": "kitchen", "printer": "EPSON TM-T88V", "order": "ORD-001"}
```

---

## 7. 平台对接示例

### WooCommerce
设置 → 高级 → Webhooks → 新建：
- 主题：订单已创建
- URL：`http://relay.thecarte.eu:51901/wc?token=您的TOKEN`
- 状态：活跃

### Odoo
设置自动动作：
- 触发：创建销售订单
- 动作：执行 Python 代码
```python
import json, requests
order = {
    "number": record.name,
    "line_items": [
        {"name": line.product_id.name, "quantity": int(line.product_uom_qty), "price": line.price_unit}
        for line in record.order_line
    ]
}
requests.post("http://relay.thecarte.eu:51901/wc?token=YOUR_TOKEN", json=order)
```

### 自定义网页 / POS 系统（JavaScript）
```javascript
fetch('http://relay.thecarte.eu:51901/wc?token=YOUR_TOKEN', {
  method: 'POST',
  headers: {'Content-Type': 'application/json'},
  body: JSON.stringify({
    number: orderId,
    line_items: cart.items.map(i => ({
      name: i.name, quantity: i.qty, price: i.price
    }))
  })
})
```

### curl 测试
```bash
curl -X POST http://relay.thecarte.eu:51901/wc?token=YOUR_TOKEN \
  -H "Content-Type: application/json" \
  -d '{
    "number": "TEST-001",
    "date_created": "2026-05-13T14:30:00",
    "payment_method_title": "Barzahlung",
    "line_items": [
      {"name": "Peking Suppe", "quantity": 2, "price": 3.50},
      {"name": "Frühlingsrolle", "quantity": 1, "price": 4.50}
    ],
    "shipping_total": "0.00"
  }'
```

---

## 8. 限制

| 项目 | 限制 |
|------|------|
| 品名长度 | 最长 30 字符 |
| 备注长度 | 最长 40 字符 |
| 每行字符数 (80mm) | 48 |
| 每行字符数 (58mm) | 32 |
| 字符编码 | Latin-1（äöüß 正常，中日韩不支持） |
| 时间格式 | ISO 8601 |

---

## 9. 支持

- 控制面板：`http://relay.thecarte.eu:51901/?panel=...`
- 健康检查：`GET /health`
- 新增格式适配：联系管理员在 relay server 添加解析器
