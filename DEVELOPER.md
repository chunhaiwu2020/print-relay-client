# Print Relay — WooCommerce 插件开发说明

> 安装即用的 WooCommerce 打印插件。插件激活时自动注册到 Print Relay 服务器，下单时直接推送订单，不需要手动创建 Webhook。

---

## 1. 配对码生成

插件激活时自动生成 8 位十六进制 Token：

```php
$token = bin2hex(random_bytes(4));  // 例: "bdb3dbcb"
```

- Token 存储在 `wp_options` 表的 `print_relay_token`
- 同一站点多次激活**不换 Token**（已存在则复用）
- Token 是 Print Relay 白名单的**唯一身份标识**

---

## 2. 自动注册到 Print Relay

插件激活时 POST 到 relay 服务器的 `/api/pairings` 端点：

```php
wp_remote_post('http://relay.thecarte.eu:51901/api/pairings?panel=90edba8f0283b2c1', [
    'body'    => json_encode([
        'token' => $token,              // 8位配对码
        'type'  => 'woo',               // 来源类型
        'name'  => get_bloginfo('name') // 站点名称（面板显示用）
    ]),
    'headers' => ['Content-Type' => 'application/json'],
    'timeout' => 10,
]);
```

**关键常量：**

| 常量 | 值 | 说明 |
|------|-----|------|
| `PRINT_RELAY_SERVER` | `http://relay.thecarte.eu:51901` | relay 服务器地址 |
| `PRINT_RELAY_PANEL_TOKEN` | `90edba8f0283b2c1` | 面板管理 Token |

**安全模型：**
- 面板 Token 用于注册配对（插件持有）
- 配对 Token 用于推送订单（每次下单携带）
- relay 只接受已在面板注册过的 Token → 否则 403

注册成功后设置 `print_relay_registered = 1`，后台显示 ✅。

---

## 3. 订单推送

### 触发时机

```php
add_action('woocommerce_checkout_order_processed', 'push_order', 20, 1);
```

任何来源都会触发：网站下单、餐桌 QR、API 创建订单。

### 推送目标

```
POST http://relay.thecarte.eu:51901/wc?token=<配对码>
Content-Type: application/json
```

### 订单 JSON 格式（标准推送载荷）

```json
{
    "number": "2683",
    "total": "55.80",
    "billing": {
        "first_name": "Table #7",
        "last_name": "",
        "phone": "+4912345678"
    },
    "line_items": [
        {
            "name": "Gebr. Nudeln mit knuspriger Ente",
            "quantity": 2,
            "total": "15.60",
            "price": "7.80",
            "sku": "BOX1"
        },
        {
            "name": "Fisch Chop-Suey",
            "quantity": 1,
            "total": "9.90",
            "price": "9.90",
            "sku": "60"
        }
    ],
    "payment_method_title": "Barzahlung",
    "date_created": "2026-05-15 12:34:56"
}
```

### 字段说明

| 字段 | 类型 | 必须 | 说明 |
|------|------|------|------|
| `number` | string/int | ✅ | 订单号，显示在小票头部 |
| `total` | string | ✅ | 订单总额，用于小票合计行 |
| `billing.first_name` | string | - | 客户名/桌号 |
| `billing.last_name` | string | - | 客户姓 |
| `billing.phone` | string | - | 电话 |
| `line_items` | array | ✅ | 菜品列表，为空则不出票 |
| `line_items[].name` | string | ✅ | 菜品名 |
| `line_items[].quantity` | int | ✅ | 数量 |
| `line_items[].total` | string | ✅ | 该行小计金额 |
| `line_items[].price` | string | ✅ | 单价 |
| `line_items[].sku` | string | - | SKU 编码（小票上显示为 `{{item_code}}`） |
| `payment_method_title` | string | - | 支付方式 |
| `date_created` | string | - | 下单时间 |

---

## 4. Print Relay 如何处理订单

```
POST /wc?token=xxx
  → relay 验 Token（查白名单）
  → 匹配路由（woo_token → 客户端 → 打印机）
  → TCP 推送 JSON 给客户端 PC：
       {"type": "print", "order": <上面的订单JSON>, "printer": "Xprinter XP-80"}
  → 客户端加载 JSON 模板 → 渲染 ESC/POS → 出票
```

### JSON 模板中的变量映射

客户端模板通过 `{{变量}}` 引用订单数据：

| 模板变量 | 映射到 |
|----------|--------|
| `{{id}}` | `order.number` |
| `{{total_amount}}` | `order.total` |
| `{{table}}` | `order.billing.first_name` |
| `{{qty}}` | `item.quantity` |
| `{{name}}` | `item.name` |
| `{{item_code}}` | `item.sku` |
| `{{price}}` | `item.price` |
| `{{total}}` | `item.total` |

### 模板示例 (templates/kitchen.json)

```json
{
    "width": 48,
    "lines": [
        {"text": "Table {{table}}  #{{id}}", "align": "center", "bold": true},
        {"hr": "="},
        {"repeat": "items", "text": "{{qty}}x {{item_code}} {{name}}", "size": "wide"},
        {"hr": "="}
    ]
}
```

---

## 5. 接入清单（新站只需 2 步）

### 对于 WordPress 开发者

1. **安装插件** (`woo-print-relay.php`) — 激活即完成注册
2. **复制配对码** 给 Print Relay 管理员，在面板添加路由

### 对于非 WordPress 系统

参照上述 JSON 格式，直接 POST 到：
```
http://relay.thecarte.eu:51901/wc?token=<配对码>
```

Token 需提前在 relay 面板注册（联系 relay 管理员）。

---

## 6. 测试订单

从 WordPress 容器手动推送测试：

```bash
curl -s -X POST "http://172.18.0.1:51901/wc?token=bdb3dbcb" \
  -H "Content-Type: application/json" \
  -d '{
    "number": "TEST-001",
    "total": "19.80",
    "line_items": [
      {"name": "Test Item", "quantity": 2, "total": "12.00", "price": "6.00", "sku": "TEST"}
    ]
  }'
# 期望: {"status":"ok","results":[{"client":"...","printer":"...","ok":true}]}
```

---

## 7. 常量配置

修改 `woo-print-relay.php` 顶部的 define：

```php
define('PRINT_RELAY_SERVER',       'http://relay.thecarte.eu:51901');  // relay 地址
define('PRINT_RELAY_PANEL_TOKEN',  '90edba8f0283b2c1');               // 面板 Token
```

其他 relay 实例部署时只需改这两个值。
