# 策略下发服务（Pro Strategy 开源平替）

独立服务 `strategy_service.py`（Python 标准库，零依赖），实现与 RustDesk Server Pro
相同的策略下发机制——协议依据是开源客户端自身的代码（`src/hbbs_http/sync.rs`）：

- 客户端周期向 `{api-server}/api/heartbeat` 发送心跳（带设备 ID 与已存的策略时间戳）；
- 本服务把心跳原样转发给 lejianwen API，再把该设备被分配到的策略以
  `{"strategy": {"config_options": {...}}, "modified_at": ...}` 注入响应；
- 客户端收到后把 `config_options` 合并进本机配置（客户端内置黑名单，
  服务器地址/Key/WebSocket 等关键项无法被策略覆盖）；
- 取消分配时自动回送空值，清除此前下发的选项。

## 管理 API（对齐官方 res/strategies.py 的调用形状）

均需 `Authorization: Bearer <STRATEGY_ADMIN_TOKEN>`：

- `GET /api/strategies` 列表
- `POST /api/strategies` 新建 `{"name": "...", "config_options": {"disable-audio": "Y"}}`
- `GET|PUT|DELETE /api/strategies/{guid}`
- `PUT /api/strategies/{guid}/status`（body 为 `true`/`false`）
- `POST /api/strategies/assign` `{"strategy": "<guid或名称>", "peers": ["设备ID"], "users": [...], "groups": [...]}`
  （`strategy` 传 `null` 为取消分配；当前 peers 按设备 ID 生效，users/groups 已入库待解析补强）

## 部署

- 文件放 `/opt/rustdesk-strategy/`，环境变量在同目录 `env`（含管理令牌，权限 600）；
- systemd 单元 `rustdesk-strategy.service`，监听 `127.0.0.1:21214`，上游 `http://127.0.0.1:21114`；
- Nginx 在 rd 域名 vhost 中加两条路由：`location = /api/heartbeat` 与
  `location ^~ /api/strategies` 均 `proxy_pass http://127.0.0.1:21214;`，其余流量仍直达 API 服务。

2026-11-13 已在 DC-02 部署并完成端到端验证（下发→客户端应用→撤销→自动清除）。
