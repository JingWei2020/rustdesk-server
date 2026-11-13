# Pro API 层平替（策略下发 + 细粒度 API 令牌 + Pro 形状资源接口）

一个 Python 标准库服务（`strategy_service.py`），在 lejianwen 开源 API 之上
补齐三块 Pro 能力；协议与数据形状对齐开源客户端源码与官方 Pro API 参考脚本
（rustdesk 客户端仓库 `res/*.py`），不使用 Pro 的任何代码。

## 1. 策略下发（Strategy，路线图 #6 ✅）

客户端心跳 `POST /api/heartbeat` 被先透传给上游 lejianwen，再按设备被分配的
策略注入 `{"strategy": {"config_options": {...}}, "modified_at": ts}`，
客户端（`src/hbbs_http/sync.rs`）收到即合并进本机配置；其黑名单保证下发
改不动服务器地址、Key、WebSocket 等关键项。取消分配时自动回送空值清除已下发的键。

管理接口与官方 `res/strategies.py` 完全同形：
`GET/POST /api/strategies`、`GET/PUT/DELETE /api/strategies/{guid}`、
`PUT /api/strategies/{guid}/status`、`POST /api/strategies/assign {strategy, peers, users, groups}`。
当前仅 peers（设备 ID）分配即时生效；users/groups 分配会落库，由授权层解析（路线图 #4）。

## 2. 细粒度 API 令牌（路线图 #9 ✅）

- 令牌权限使用 Pro 词汇（`res/admin-roles.py` 的权限表）：
  `users.view users.create devices.view devices.enable_disable audits.view
  strategies.view strategies.edit user_groups.view device_groups.view …`
- 主令牌（服务器 `env` 中的 `STRATEGY_ADMIN_TOKEN`）= 全权限；
  作用域令牌经 `POST /api/tokens {name, permissions[], expires_days?}` 签发，
  令牌值只在签发时返回一次，库存 SHA-256 哈希；`GET /api/tokens` 列表、
  `DELETE /api/tokens/{id}` 吊销，均仅主令牌可用。
- 校验：`*.edit` 蕴含 `*.view`；过期/吊销即刻失效；记录 last_used_at。

## 3. Pro 形状只读资源接口

数据只读自 lejianwen 的 SQLite（peers/users/groups/device_groups/audit_*），
统一 `{data: [...], total: N}` + `current/pageSize` 分页，对象带确定性 GUID（uuid5）：

| 接口 | 权限 |
| --- | --- |
| `GET /api/devices` | devices.view |
| `GET /api/users` | users.view |
| `GET /api/user-groups` | user_groups.view |
| `GET /api/device-groups` | device_groups.view |
| `GET /api/audits/conns`、`/api/audits/files`、`/api/audits/logins` | audits.view |
| `GET /api/strategies` 等 | strategies.view / strategies.edit |

例外：`/api/users` 在 lejianwen 本有同名路由——请求不带本层令牌时原样透传上游。

## 部署

- 文件：`/opt/rustdesk-strategy/strategy_service.py`，systemd 单元
  `rustdesk-strategy.service`，监听 `127.0.0.1:21214`；
- 环境：`env`（600）提供 `STRATEGY_ADMIN_TOKEN`，可选 `STRATEGY_UPSTREAM`、
  `STRATEGY_DB`、`LEJIANWEN_DB`（默认 `/opt/rustdesk-api/data/rustdeskapi.db`）；
- Nginx 路由：`location = /api/heartbeat`、`^~ /api/strategies`、`^~ /api/tokens`、
  `^~ /api/devices`、` = /api/users`、`^~ /api/user-groups`、`^~ /api/device-groups`、
  `^~ /api/audits/`（vhost 备份见同机 `sites-available/rustdesk-api.bak-*`）。

## 验证记录（2026-11-13，DC-02 生产）

- 策略闭环：`disable-audio` 下发到云电脑，客户端日志 `strategy updated` 且落盘；
  取消分配后自动清除；
- 令牌闭环：官方 `res/strategies.py list` 用 `strategies.view` 令牌返回 `[]`；
  用仅 `devices.view` 的令牌调用同一脚本得到
  `Error: HTTP 403: insufficient permission: strategies.view`；
  官方 `res/devices.py view` 列出全部已注册设备；测试令牌验收后已吊销。
