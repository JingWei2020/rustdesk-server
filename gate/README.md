# 控制台双因素（TOTP）网关（Pro 控制台 2FA 开源平替）

独立服务 `admin_gate.py`（Python 标准库），作为 Nginx `auth_request` 后端，
为 lejianwen Web Admin 的两个面加一层 TOTP 双因素：

- 保护范围：`/_admin/`（含 `/_admin` 本身）与 `/api/admin/`；客户端个人 API 不受影响。
- 未注册状态：`/auth` 一律放行，控制台行为与现状一致（零锁死风险）；
- 管理员经一次性注册链接 `/gate/enroll?setup=<token>`（token 在服务器
  `/opt/rustdesk-gate/setup-token`，600 权限）打开注册页：自设网关口令、
  把 TOTP 密钥录入验证器 App、输入第一枚动态码确认 → 注册完成、链接作废、强制生效；
- 强制模式：无有效签名 Cookie（`rdgate`，12 小时）访问控制台一律跳到 `/gate/login`，
  登录需网关口令 + 6 位 TOTP 动态码（RFC 6238，±30 秒容差，失败 5 次锁定 5 分钟）；
- 通过网关后仍需完成 lejianwen 控制台自身的账号登录（双层防护）。

部署：文件放 `/opt/rustdesk-gate/`，systemd 单元 `rustdesk-gate.service`
（状态目录环境变量 `GATE_STATE_DIR=/opt/rustdesk-gate`，监听 `127.0.0.1:21215`）。
Nginx 侧改动见生产 vhost 备份 `rustdesk-api.bak-gate` 与现行配置中的
`/gate-auth`、`^~ /gate/`、`@gate_login` 与三个带 `auth_request` 的 location。

2026-11-13 已部署 DC-02；注册→登录→鉴权→注销全流程在同构测试实例上验证通过，
生产当前为未注册放行状态，待管理员完成一次性注册后生效。
