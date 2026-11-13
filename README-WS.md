# RustDesk 服务端一键安装（WebSocket 补丁版）

这是 [rustdesk/rustdesk-server](https://github.com/rustdesk/rustdesk-server) 的 fork，
在官方 **1.1.16** 基础上给 `hbbs` 打了补丁（分支 `ws-1.1.16`），并提供一键安装脚本。

## 为什么打补丁

开源版 hbbs 的 TCP/WebSocket 通道对注册请求（RegisterPk）写死返回 `NOT_SUPPORT`：
客户端只要在设置里开启「使用 WebSocket」（或禁用 UDP），就永远注册不上，
表现为一直停在「正在接入 RustDesk 网络」。这是官方有意的功能划分
（WebSocket 注册是 Pro 版功能）。本补丁把这条路在开源版里补齐，
做到**开不开 WebSocket 都能注册、都能连接**。

补丁只改一个文件 `src/rendezvous_server.rs`，内容（`hbbs-websocket-register.patch`）：

1. `handle_tcp` 内实现完整 RegisterPk 逻辑（与 UDP 通道同一套校验与写库）
2. 新增 `ws_peers` 注册表 + `send_to_peer`：打洞 / 中继信令可沿常驻连接投递
3. 监听循环改 select 双路（收消息 / 推消息），连接可长期保持
4. 每条注册连接 20 秒空帧保活 + 刷新在线时间，注册响应带 `keep_alive=30`
5. 每次 TCP/WS（重）注册无条件刷新设备地址——否则地址残留旧值或在 hbbs
   重启后变成 `0.0.0.0:0`，打洞投递失败，客户端报「无法通过注册服务器建立
   连接」（2026-11-13 实测踩中并修复）
6. `send_to_peer` 对端口为 0 的地址不再走 UDP 发送（必然 `EINVAL`）

## 一键安装

在目标服务器（Ubuntu / Debian，x86_64）上以 root 运行：

```bash
curl -fsSL https://raw.githubusercontent.com/JingWei2020/rustdesk-server/ws-1.1.16/install.sh | bash
```

带 API 服务（lejianwen/rustdesk-api v2.7，管理后台 `/_admin/`）一起装：

```bash
curl -fsSL https://raw.githubusercontent.com/JingWei2020/rustdesk-server/ws-1.1.16/install.sh | \
  WITH_API=1 API_DOMAIN=rd.example.com bash
```

`API_DOMAIN` 需先解析到本机；脚本会自动配 Nginx 反代并申请 Let's Encrypt 证书。
不填域名则 API 地址为 `http://<公网IP>:21114`。更多环境变量见 `install.sh` 头部注释。

安装完成后脚本会打印客户端要填的四项：ID 服务器、中继服务器、API 服务器、Key。

安装行为：

- 二进制优先从本仓库 Release（`v1.1.16-ws2`）下载预编译包（musl 静态，
  不挑 glibc 版本）；下载失败或 `SKIP_DOWNLOAD=1` 时自动改为源码编译。
- systemd 服务 `rustdesk-hbbs`、`rustdesk-hbbr`（及可选的 `rustdesk-api`），
  开机自启；hbbs / hbbr 强制 Key 校验（`-k _`）。
- 已存在的 `/var/lib/rustdesk-server`（密钥与设备库）绝不覆盖，重装后
  客户端 Key 不变；旧二进制会备份为 `/usr/bin/hbbs.bak-<时间戳>`。

## 自己编译 / 部署

```bash
./build-hbbs-ws.sh                          # 编译补丁版 hbbs（产物 dist/）
./build-hbbs-ws.sh 1.1.16 --deploy root@1.2.3.4   # 编译并部署到目标机
```

脚本可重复运行；官方发布新版后若补丁打不上（相关代码被改动），会明确
报错，需要人工移植补丁并用 `git diff` 重新生成 `.patch`。

## 回滚

- 换回自己部署前的二进制：`systemctl stop rustdesk-hbbs && cp /usr/bin/hbbs.bak-<时间戳> /usr/bin/hbbs && systemctl start rustdesk-hbbs`
- 换回官方原版：用官方 `.deb` 重装即可（会覆盖补丁版 hbbs）。

## 说明

- 服务端程序是 Linux 二进制，没有 Windows 的 EXE 安装包；本仓库的
  Release 预编译包 + `install.sh` 一行命令就是对应的「一键安装」形态。
- 与现有部署（洛杉矶 DC-02，`rd.ljwc.eu.cc`）保持同一套参数：
  ID `21116`、中继 `21117`、API `21114`，强制 Key。
