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
| 4 | 用户组 / 设备组与按组访问控制 | ◐ | 本仓库 strategy/ 服务已补：用户组/设备组 CRUD（对齐 Pro 的 allowed_incomings/allowed_outgoings 规则模型）、策略按用户/组分配解析（设备>用户>组优先级，名字/GUID 双写规范化）、/api/access-check 授权解析；连接级拦截需 hbbs 感知账号身份（lejianwen 自有 hbbs 的 MUST_LOGIN 路线），不在 API 层范围 |
| 5 | 连接 / 文件传输审计日志 | ✅ | lejianwen 有登录日志、链接日志、文件传输日志 |
| 6 | 安全设置同步 / 策略下发 | ✅ | 独立策略服务（本仓库 strategy/）：客户端心跳 /api/heartbeat 协议与 Pro 完全一致（源码依据为开源客户端 src/hbbs_http/sync.rs），管理 API 对齐官方 res/strategies.py（CRUD/启停/按设备分配）；2026-11-13 部署 DC-02 并实机验证：给 win11 下发 disable-audio 后客户端日志 `strategy updated`、配置落盘，撤销后自动清除。用户/组分配的解析待 #4 一并补强 |
| 7 | 控制台双因素认证（2FA） | ✅ | 独立网关（本仓库 gate/）以 Nginx auth_request 保护 /_admin/ 与 /api/admin/，与控制台自身登录构成双层防护。登录二选一：① 口令 + TOTP 动态码；② WebAuthn 安全密钥免密码登录（手机本机通行密钥或 YubiKey，触摸即可，可绑定多把，/gate/security 自助管理）——WebAuthn 注册走已验证会话、登录断言严格验签，软件模拟认证器在本地与 DC-02 系统 Python 双环境 13 项测试全过。管理员 2026-11-13 完成注册并按其要求重置过一次、已重新绑定生效；安全密钥的三把凭据（手机 + 两把 YubiKey）待管理员在 /gate/security 逐个添加 |
| 8 | OIDC / LDAP 单点登录 | ✅ | lejianwen 支持 GitHub / Google / OIDC 授权登录与 LDAP（AD、OpenLDAP），部署时配置即可 |
| 9 | API 令牌（细粒度权限） | ✅ | 本仓库 strategy/ 服务已实现：Pro 词汇权限表的作用域令牌（签发/列表/吊销/过期，哈希落库），覆盖 strategies/devices/users/user-groups/device-groups/audits 读写校验；官方 res/strategies.py 与 res/devices.py 已用作用域令牌在生产实测通过（2026-11-13） |
| 10 | 多中继节点 + 自动就近调度 | ◐ | 已实现并上线（补丁 v3）：hbbs 支持多中继列表（上游自带健康剔除），选择逻辑由轮换改为按双方 IP 国家/大区分级就近（MMDB 国家库，RELAY_GEO_OVERRIDES 可覆盖），每次选择有日志、可用控制台 tg 命令验证；生产已双中继（DC-02+DC-03，均洛杉矶）并演练过故障剔除/恢复。剩余：亚太中继节点待一台有可用公网端口的亚洲服务器（SG/HK NAT 的端口映射未确认），加上后中国侧连接即可就近走亚太（hbbs 参数加一项，无需改代码） |
| 11 | WSS（WebSocket over TLS） | ✅ | 已上线并完成真机集成验证：客户端按源码规则（域名 rendezvous + https API）在 `wss://<域名>/ws/id`、`/ws/relay` 两个端点走 443，Nginx 反代到 hbbs 21118 / hbbr 21119；win11 客户端临时切域名实测经 Cloudflare→Nginx 注册成功（hbbs 日志 update_pk 来源为 CF 边缘 IP），测后已恢复原配置。备注：经 WSS 注册的设备其 IP 在 hbbs 侧为代理出口 IP，就近选路对这类设备会退化为轮换；现有常驻设备继续走直连 WS（保真实 IP） |
| 12 | 定制客户端生成 | ✅ | 控制台（2FA 网关后 `/gate/client`）提供当前服务器参数展示 + 预置四项参数的单文件安装包下载 + 官方安装器 magic 文件名自助定制法；打包脚本在仓库 client-package/（IExpress：官方安装器 magic 名 + 装后写配置，Windows 上一条命令重打包）。注：EXE 生成依赖 Windows 工具链，故控制台分发的是按生产参数预生成的包，参数变更时在 Windows 上重跑打包脚本 |
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
