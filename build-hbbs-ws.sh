#!/usr/bin/env bash
# =============================================================================
# build-hbbs-ws.sh — 一键：给 rustdesk-server 的 hbbs 打 WebSocket 注册补丁并编译
#
# 背景：开源版 hbbs 的 TCP/WebSocket 通道对 RegisterPk 写死回 NOT_SUPPORT，
#       客户端只要开了 WebSocket（allow-websocket）就永远注册不上。
#       本补丁（hbbs-websocket-register.patch，仅改 src/rendezvous_server.rs）：
#         1. handle_tcp 内实现完整 RegisterPk 逻辑（与 UDP 通道同一套校验/写库）
#         2. 新增 ws_peers 注册表 + send_to_peer，打洞/中继信令可沿常驻连接投递
#         3. 监听循环改 select 双路（收消息 / 推消息），连接可长期保持
#         4. 每条注册连接 20 秒空帧保活 + 刷新 last_reg_time，响应带 keep_alive=30
#         5. （v2）TCP/WS 每次（重）注册都无条件刷新 peer 的 socket_addr 与
#            last_reg_time——否则地址会残留旧 UDP 地址或在 hbbs 重启后变成
#            0.0.0.0:0，打洞请求投递失败，客户端报「无法通过注册服务器建立
#            连接」（2026-11-13 实测踩中并修复）
#         6. （v2）send_to_peer 对端口为 0 的地址不再走 UDP 发送（必然 EINVAL）
#       编译为 x86_64-unknown-linux-musl 静态二进制，可直接替换 .deb 装的 hbbs。
#       脚本可重复运行：已存在的源码树会先重置为官方版本再重新打补丁。
#
# 用法：
#   ./build-hbbs-ws.sh                 # 默认版本 1.1.16，只编译
#   ./build-hbbs-ws.sh 1.1.16          # 指定版本（须与补丁兼容的 tag）
#   ./build-hbbs-ws.sh 1.1.16 --deploy root@1.2.3.4
#                                      # 编译完通过 ssh 部署到目标机（带备份；
#                                      #  非 22 端口用 SSH_PORT=<端口> 指定）
#   ./build-hbbs-ws.sh --skip-deps     # 跳过工具链检查/安装
#
# 产物： dist/hbbs-<版本>-ws/hbbs
# 注意：官方版本若改动了 rendezvous_server.rs 相关代码，git apply 会失败，
#       需先人工把补丁移植到新版，再用 git diff 重新生成 .patch 文件。
#       在 DC-02 上用 .deb 升级官方版会覆盖补丁版，升级后重跑本脚本即可。
# =============================================================================
set -euo pipefail

VERSION="1.1.16"
DEPLOY=""
SKIP_DEPS=0
while [ $# -gt 0 ]; do
  case "$1" in
    --deploy)    DEPLOY="${2:?--deploy 需要目标，如 root@1.2.3.4}"; shift 2 ;;
    --skip-deps) SKIP_DEPS=1; shift ;;
    -*)          echo "未知参数: $1" >&2; exit 2 ;;
    *)           VERSION="$1"; shift ;;
  esac
done

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PATCH="$ROOT/hbbs-websocket-register.patch"
SRC="$ROOT/src-$VERSION"
OUT="$ROOT/dist/hbbs-$VERSION-ws"
TARGET="x86_64-unknown-linux-musl"

log() { echo "[$(date +%H:%M:%S)] $*"; }

[ -f "$PATCH" ] || { echo "找不到补丁文件: $PATCH" >&2; exit 1; }

# ---------------------------------------------------------------- 工具链 ----
if [ "$SKIP_DEPS" -eq 0 ]; then
  if ! command -v cargo >/dev/null 2>&1; then
    log "未找到 cargo，安装 rustup（minimal + musl 目标）..."
    curl -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal --target "$TARGET"
  fi
  export PATH="$HOME/.cargo/bin:$PATH"
  rustup target list --installed | grep -qx "$TARGET" || rustup target add "$TARGET"
  if ! command -v musl-gcc >/dev/null 2>&1 || ! command -v protoc >/dev/null 2>&1; then
    log "安装 musl-tools / protobuf-compiler ..."
    sudo apt-get update -q && sudo apt-get install -y -q musl-tools protobuf-compiler
  fi
else
  export PATH="$HOME/.cargo/bin:$PATH"
fi

# ---------------------------------------------------------------- 源码 ------
if [ ! -d "$SRC/.git" ]; then
  log "克隆 rustdesk-server $VERSION ..."
  git clone --depth 1 --branch "$VERSION" https://github.com/rustdesk/rustdesk-server.git "$SRC"
  git -C "$SRC" submodule update --init --depth 1
fi

# ---------------------------------------------------------------- 补丁 ------
cd "$SRC"
# 已存在的源码树可能打过旧版补丁：先把已跟踪文件重置为官方版本，
# 保证当前补丁总能干净地应用（未跟踪的 target/ 等构建缓存保留，编译更快）。
git checkout -- . 2>/dev/null || true
if git apply --check "$PATCH" 2>/dev/null; then
  git apply "$PATCH"
  log "补丁已应用"
elif git apply --reverse --check "$PATCH" 2>/dev/null; then
  log "补丁已在源码中，跳过"
else
  echo "补丁无法应用到 $VERSION（官方可能改动了 rendezvous_server.rs），请人工移植" >&2
  exit 1
fi

# ---------------------------------------------------------------- 编译 ------
log "开始编译（release / musl 静态，JOBS=${CARGO_BUILD_JOBS:-2}）..."
export CC_x86_64_unknown_linux_musl=/usr/bin/musl-gcc
export CXX_x86_64_unknown_linux_musl=/usr/bin/musl-gcc
export AR_x86_64_unknown_linux_musl=/usr/bin/ar
export TARGET_CC=/usr/bin/musl-gcc
CARGO_BUILD_JOBS="${CARGO_BUILD_JOBS:-2}" cargo build --release --target "$TARGET" --bin hbbs

mkdir -p "$OUT"
cp "target/$TARGET/release/hbbs" "$OUT/hbbs"
chmod 755 "$OUT/hbbs"
log "编译完成: $OUT/hbbs"
"$OUT/hbbs" --version
sha256sum "$OUT/hbbs"

# ---------------------------------------------------------------- 部署 ------
if [ -n "$DEPLOY" ]; then
  SSH_PORT="${SSH_PORT:-22}"
  BAK="/usr/bin/hbbs.bak-$(date +%Y%m%d%H%M%S)"
  log "上传到 $DEPLOY ..."
  scp -P "$SSH_PORT" "$OUT/hbbs" "$DEPLOY:/tmp/hbbs.new"
  ssh -p "$SSH_PORT" "$DEPLOY" "chmod 755 /tmp/hbbs.new && /tmp/hbbs.new --version"
  log "备份原版到 $BAK 并替换 ..."
  ssh -p "$SSH_PORT" "$DEPLOY" "cp -a /usr/bin/hbbs '$BAK' && systemctl stop rustdesk-hbbs && cp /tmp/hbbs.new /usr/bin/hbbs && chmod 755 /usr/bin/hbbs && systemctl start rustdesk-hbbs && sleep 3 && systemctl is-active rustdesk-hbbs"
  log "部署完成。回滚命令: ssh -p $SSH_PORT $DEPLOY 'systemctl stop rustdesk-hbbs && cp $BAK /usr/bin/hbbs && systemctl start rustdesk-hbbs'"
fi

log "全部完成"
