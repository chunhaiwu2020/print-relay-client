# Print Relay API 文档

> 通用订单打印网关 — HTTP POST → 热敏打印机。  
> 生产地址: `https://api.printrelay.es`  
> TCP 客户端: `printrelay.es:51902`

---

## 1. 概述

Print Relay 是自托管云打印平台。你在面板创建账号 → 配对 Token → 配置路由 → POST 订单 JSON → 打印小票到店 PC 的热敏打印机。

**三步对接：**
1. 获取 API Token（注册/登录）
2. 发送订单（POST `/wc?token=xxx`）
3. 打印完成

---

## 2. 认证

所有 API 通过 URL 参数 `?token=xxx` 认证（`/api/register` 和 `/api/login` 除外）。

| 端点 | 认证方式 |
|------|---------|
| `/api/register` | 无（email + password） |
| `/api/login` | 无（email + password） |
| 其他 `/api/*` | `?token=TOKEN` |
| `/wc` | `?token=TOKEN` |
| `/health` | 无 |

---

## 3. 账户

### 3.1 注册

```
POST /api/register
Content-Type: application/json

{
  "email": "you@example.com",
  "password": "your_password"
}
```

**返回：**
```json
{
  "success": true,
  "token": "tok_xxxxxxxxxxxxxxxx"
}
```

### 3.2 登录

```
POST /api/login
Content-Type: application/json

{
  "email": "you@example.com",
  "password": "your_password"
}
```

**返回：**
```json
{
  "success": true,
  "token": "tok_xxxxxxxxxxxxxxxx"
}
```

---

## 4. 控制面板

```
https://api.printrelay.es/panel?token=tok_xxx
```

面板功能：
- 添加配对（白名单 Token）
- 配置路由（订单来源 → 目标打印机）
- 测试打印
- 模板预览
- 历史记录

---

## 5. 打印订单

### 5.1 端点

```
POST /wc?token=YOUR_TOKEN
Content-Type: application/json
```

### 5.2 请求体

```json
{
  "number": "2676",
  "total": "56.00",
  "currency": "EUR",
  "date_created": "2026-05-16T19:06:00",
  "payment_method": "Barzahlung",
  "status": "processing",
  "customer_note": "Keine Zwiebeln bitte",
  "billing": {
    "first_name": "Lukas",
    "last_name": "Müller",
    "phone": "06802387206",
    "email": "lukas@example.com",
    "address_1": "Hauptstr. 1",
    "city": "Ziersdorf",
    "postcode": "3710",
    "country": "AT"
  },
  "line_items": [
    {
      "id": 90,
      "name": "Gebr. Nudel mit Gemüse",
      "sku": "90",
      "quantity": 1,
      "price": "9.90",
      "total": "9.90"
    },
    {
      "id": 55,
      "name": "Mini Frühlingsrolle (6 Stk.)",
      "sku": "1a",
      "quantity": 2,
      "price": "3.70",
      "total": "7.40"
    }
  ]
}
```

### 5.3 返回

**成功：**
```json
{
  "status": "ok",
  "results": [
    {"client": "餐厅XP80", "printer": "XP-80C", "ok": true},
    {"client": "前台PC", "printer": "Cashier-Printer", "ok": true}
  ]
}
```

**客户端不在线：**
```json
HTTP 503
{"error": "Client offline"}
```

**Token 未配对：**
```json
HTTP 403
{"error": "Unauthorized"}
```

---

## 6. 模板

### 6.1 模板列表

```
GET /api/templates?token=TOKEN
```

**返回：**
```json
{
  "templates": {
    "restaurant/kitchen.json": {
      "name": "厨房出菜单",
      "industry": "餐饮",
      "desc": "逐菜打印，自动切纸"
    },
    "restaurant/cashier.json": {
      "name": "收银小票",
      "industry": "餐饮",
      "desc": "完整订单，价格合计"
    }
  }
}
```

### 6.2 单个模板

```
GET /api/templates/restaurant/kitchen.json?token=TOKEN
```

**返回：** 完整模板 JSON

---

## 7. 快速开始

### cURL 测试

```bash
# 1. 注册
curl -X POST https://api.printrelay.es/api/register \
  -H "Content-Type: application/json" \
  -d '{"email":"test@example.com","password":"test123"}'

# 2. 登录
curl -X POST https://api.printrelay.es/api/login \
  -H "Content-Type: application/json" \
  -d '{"email":"test@example.com","password":"test123"}'

# 3. 发送测试打印（需先在面板配好路由）
curl -X POST "https://api.printrelay.es/wc?token=tok_xxx" \
  -H "Content-Type: application/json" \
  -d '{
    "number":"9999",
    "total":"9.99",
    "billing":{"first_name":"Test"},
    "line_items":[{"name":"Test Item","quantity":1,"total":"9.99"}]
  }'
```

---

## 8. 客户端

Windows 客户端负责连接打印机并接收打印任务。

- **下载:** `SETUP.exe`（安装包，含开机启动）
- **连接:** 自动连 `printrelay.es:51902`（TCP 长连接）
- **配对:** 安装后复制面板显示的配对码，填入面板完成绑定
- **打印机:** 客户端自动扫描 Windows 已装打印机

---

## 9. 架构

```
你的系统                    Print Relay 云端            店内 PC
┌──────────┐    POST /wc    ┌──────────────┐   TCP    ┌──────────┐
│ Woo/API  │ ──────────────→│ relay server │ ───────→│ 客户端   │──→ 打印机
│ 你的 APP │                │ 渲染 ESC/POS │         │ 纯通道   │
└──────────┘                └──────────────┘         └──────────┘
                                   │
                            ┌──────┴──────┐
                            │ 控制面板     │
                            │ 配对/路由/测试│
                            └─────────────┘
```

---

## 10. 字段说明

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `number` | string | 否 | 订单号，模板可用 |
| `total` | string | 否 | 订单总价 |
| `currency` | string | 否 | 货币代码 (EUR/USD) |
| `payment_method` | string | 否 | 支付方式 |
| `status` | string | 否 | 订单状态 |
| `customer_note` | string | 否 | 客户备注 |
| `billing.first_name` | string | 否 | 客户名 |
| `billing.phone` | string | 否 | 电话 |
| `billing.email` | string | 否 | 邮箱 |
| `line_items[].name` | string | 是 | 菜品名 |
| `line_items[].quantity` | int | 是 | 数量 |
| `line_items[].total` | string | 否 | 行总价 |
| `line_items[].sku` | string | 否 | SKU/编码 |
