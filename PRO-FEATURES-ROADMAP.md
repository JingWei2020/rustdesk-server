# 开源版补齐 RustDesk Server Pro 功能 — 对照表与实施路线

目标：在开源 rustdesk-server（本仓库）+ 配套 API 层上，逐项复刻 RustDesk Server Pro 的功能与配置，
自行编译、自行部署。Pro 本体闭源，本项目只做功能层面的独立实现，不使用 Pro 的任何代码。

状态图例：✅ 已完成并在生产环境验证 / ◐ 已有组件覆盖（待核对粒度或待集成） / ⏳ 待实现 / ▽ 仅部分可平替

## 功能对照（对照 Pro 1.8.7 与官方定价/文档，2026-11-13 核对；API 层覆盖度已按 lejianwen/rustdesk-api README 审计）

| # | Pro 功能 | 状态 | 说明 |
|---|----------|------|------|
| 1 | WebSocket 客户端接入（ws://） | ✅ | hbbs 补丁（本仓库 ws 分支），生产已验证开/关 WS 均可连接 |
| 2 | 账号体系（用户注册/登录、个人地址簿） | ✅ | lejianwen/rustdesk-api 提供，客户端登录 API 地址即用；地址簿可多本、可共享给其他用户 |
| 3 | Web 管理控制台（设备/用户管理） | ✅ | lejianwen Web Admin：用户、设备、地址簿、标签、群组、OAuth、LDAP 配置与日志管理齐全 |
| 4 | 用户组 / 设备组与按组访问控制 | ◐ | lejianwen 有共享组/普通组与地址簿共享；与 Pro 按组做设备授权的粒度差待补 |
| 5 | 连接 / 文件传输审计日志 | ✅ | lejianwen 有登录日志、链接日志、文件传输日志 |
| 6 | 安全设置同步 / 策略下发 | ✅ | 独立策略服务（本仓库 strategy/）：客户端心跳 /api/heartbeat 协议与 Pro 完全一致（源码依据为开源客户端 src/hbbs_http/sync.rs），管理 API 对齐官方 res/strategies.py（CRUD/启停/按设备分配）；2026-11-13 部署 DC-02 并实机验证：给 win11 下发 disable-audio 后客户端日志 `strategy updated`、配置落盘，撤销后自动清除。用户/组分配的解析待 #4 一并补强 |
| 7 | 控制台双因素认证（2FA） | ◐ | 独立 TOTP 网关（本仓库 gate/）以 Nginx auth_request 保护 /_admin/ 与 /api/admin/：未注册时放行、管理员经一次性链接自设口令+录入 TOTP 后强制生效，与控制台自身登录构成双层防护；2026-11-13 部署 DC-02，全流程在同构测试实例验证通过，生产待管理员完成一次性注册 |
| 8 | OIDC / LDAP 单点登录 | ✅ | lejianwen 支持 GitHub / Google / OIDC 授权登录与 LDAP（AD、OpenLDAP），部署时配置即可 |
| 9 | API 令牌（细粒度权限） | ✅ | 本仓库 strategy/ 服务已实现：Pro 词汇权限表的作用域令牌（签发/列表/吊销/过期，哈希落库），覆盖 strategies/devices/users/user-groups/device-groups/audits 读写校验；官方 res/strategies.py 与 res/devices.py 已用作用域令牌在生产实测通过（2026-11-13） |
| 10 | 多中继节点 + 自动就近调度 | ⏳ | hbbs/hbbr 层实现：多 hbbr 注册、心跳与按延迟/负载下发 |
| 11 | WSS（WebSocket over TLS） | ◐ | Web 客户端可配 wss Host；桌面客户端 WSS 经反向代理终止，待集成验证 |
| 12 | 定制客户端生成 | ◐ | 已用单文件安装包平替（官方 1.5.3 + magic 文件名 + 装后写配置）；控制台一键生成待做 |
| 13 | 自托管 Web 客户端（浏览器远控） | ◐ | lejianwen 自带 Web Client：自动同步 API/ID/Key/地址簿，支持游客临时分享链接；非官方实现 |
| 14 | WebRTC 传输（Pro 1.8.7 新增） | ⏳ | 涉及客户端/服务端协议，单独立项评估 |

## 实施方式

- 每一项的交付标准：在本仓库（或配套 API 仓库）提交代码 → 编译出二进制 → 部署到生产服务器实机验证 → 更新本表状态。
- 客户端无需改动：免费版与 Pro 是同一客户端，Pro 功能均由服务端点亮；仅当某功能缺客户端协议支持时才另议。
- 实施顺序（按审计后的真实缺口）：#6 策略下发 → #7 控制台 2FA → #9 API 令牌细粒度 → #4 授权粒度补强 → #10 多中继 → #11 WSS 集成 → #12 控制台生成客户端；#14 单独评估。

## 审计备注（2026-11-13，lejianwen/rustdesk-api）

- 其配套 fork（lejianwen/rustdesk-server）另有 MUST_LOGIN（强制登录后连接）与 JWT 集成；本仓库 hbbs 为官方 + WS 补丁线，集成该能力时需评估 JWT 校验的引入方式。
- Web Admin 后台即现生产环境 https://rd.ljwc.eu.cc/_admin/ 。

## 已有基础（2026-11-13）

- hbbs WebSocket 补丁 v2（分支 ws-1.1.16，Release v1.1.16-ws2 附预编译 hbbs/hbbr）
- install.sh 一键安装（可选 WITH_API=1 同时部署 lejianwen/rustdesk-api v2.7）
- 生产环境：洛杉矶 DC-02，ID/中继/API 均已上线运行
