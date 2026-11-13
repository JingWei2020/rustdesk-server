#!/usr/bin/env bash
# =============================================================================
# RustDesk 服务端一键安装（hbbs WebSocket 补丁版 + hbbr，可选 API 服务）
#
# 仓库：https://github.com/JingWei2020/rustdesk-server （官方 fork，分支 ws-1.1.16）
# 补丁说明见 README-WS.md：让开源版 hbbs 支持 WebSocket 模式客户端注册与连接。
#
# 在目标服务器（Ubuntu / Debian，x86_64）上以 root 运行：
#   curl -fsSL https://raw.githubusercontent.com/JingWei2020/rustdesk-server/ws-1.1.16/install.sh | bash
# 或克隆本仓库后在仓库根目录运行： ./install.sh
#
# 环境变量：
#   RELAY_IP=1.2.3.4        服务器公网 IP（默认自动探测；hbbs 的 -r 参数与客户端
#                           中继地址都用它）
#   WITH_API=1              同时安装 lejianwen/rustdesk-api v2.7（端口 21114，
#                           管理后台 /_admin/，与 DC-02 现有部署一致）
#   API_DOMAIN=rd.xx.cc     API 域名（需 WITH_API=1；自动装 Nginx 反代并签
#                           Let's Encrypt 证书；不填则 API 地址为
#                           http://<公网IP>:21114）
#   CERTBOT_EMAIL=a@b.c     证书通知邮箱（可选，不填则无邮箱注册证书）
#   SKIP_DOWNLOAD=1         不下载预编译二进制，改为在本机从源码编译
#                           （慢，约 10–15 分钟，需约 2 GB 可用内存）
#   DESTDIR=/tmp/stage      测试模式：只把文件铺设到该目录下，不执行
#                           systemctl / ufw / apt 等系统操作
#
# 行为说明：
#   - 二进制来源优先级：脚本同目录 bin/ 下的现成文件 > 本仓库 Release
#     （v1.1.16-ws2）的预编译包 > 从源码编译。
#   - 已存在的 /var/lib/rustdesk-server（密钥 id_ed25519* 与设备库）绝不
#     覆盖——重装后客户端 Key 不变。已存在的 /usr/bin/hbbs、hbbr 会先
#     备份为 *.bak-<时间戳> 再替换。
# =============================================================================
set -euo pipefail

REPO="JingWei2020/rustdesk-server"
BRANCH="ws-1.1.16"
RELEASE_TAG="v1.1.16-ws2"
RELEASE_URL="https://github.com/$REPO/releases/download/$RELEASE_TAG"
API_TARBALL="https://github.com/lejianwen/rustdesk-api/releases/download/v2.7/linux-amd64.tar.gz"

DESTDIR="${DESTDIR:-}"
WITH_API="${WITH_API:-0}"
API_DOMAIN="${API_DOMAIN:-}"
SKIP_DOWNLOAD="${SKIP_DOWNLOAD:-0}"

log() { echo "[install] $*"; }
die() { echo "[install] 错误：$*" >&2; exit 1; }

if [ -z "$DESTDIR" ]; then
  [ "$(id -u)" = "0" ] || die "请以 root 运行（或用 sudo）"
fi
[ "$(uname -m)" = "x86_64" ] || die "仅支持 x86_64（当前 $(uname -m)）"

# ------------------------------------------------------------ 公网 IP ----
if [ -n "${RELAY_IP:-}" ]; then
  :
elif [ -n "$DESTDIR" ]; then
  RELAY_IP="127.0.0.1"
else
  RELAY_IP="$(curl -fsS --max-time 8 https://api.ipify.org 2>/dev/null || true)"
  [ -n "$RELAY_IP" ] || RELAY_IP="$(hostname -I | awk '{print $1}')"
  [ -n "$RELAY_IP" ] || die "无法自动探测公网 IP，请用 RELAY_IP=<IP> 指定"
fi
log "公网 IP（中继地址）：$RELAY_IP"

# ------------------------------------------------------------ 取二进制 ----
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-.}")" 2>/dev/null && pwd || echo .)"

get_binaries() {
  if [ -f "$SCRIPT_DIR/bin/hbbs" ] && [ -f "$SCRIPT_DIR/bin/hbbr" ]; then
    log "使用脚本同目录 bin/ 下的现成二进制"
    cp "$SCRIPT_DIR/bin/hbbs" "$TMP/hbbs"; cp "$SCRIPT_DIR/bin/hbbr" "$TMP/hbbr"
    return 0
  fi
  if [ "$SKIP_DOWNLOAD" != "1" ]; then
    log "从 GitHub Release $RELEASE_TAG 下载预编译二进制 ..."
    if curl -fsSL --max-time 300 -o "$TMP/hbbs" "$RELEASE_URL/hbbs" \
       && curl -fsSL --max-time 300 -o "$TMP/hbbr" "$RELEASE_URL/hbbr"; then
      return 0
    fi
    log "下载失败，改走源码编译"
  fi
  log "从源码编译（需要 git、curl；会自动装 Rust 工具链）..."
  command -v git >/dev/null 2>&1 || die "源码编译需要 git，请先安装"
  git clone --depth 1 --branch "$BRANCH" "https://github.com/$REPO.git" "$TMP/repo"
  ( cd "$TMP/repo" && ./build-hbbs-ws.sh )
  cp "$TMP/repo/dist/hbbs-1.1.16-ws/hbbs" "$TMP/hbbs"
  ( cd "$TMP/repo/src-1.1.16" && export PATH="$HOME/.cargo/bin:$PATH" \
      CC_x86_64_unknown_linux_musl=/usr/bin/musl-gcc \
      CXX_x86_64_unknown_linux_musl=/usr/bin/musl-gcc \
      AR_x86_64_unknown_linux_musl=/usr/bin/ar TARGET_CC=/usr/bin/musl-gcc \
      && CARGO_BUILD_JOBS="${CARGO_BUILD_JOBS:-2}" cargo build --release \
           --target x86_64-unknown-linux-musl --bin hbbr )
  cp "$TMP/repo/src-1.1.16/target/x86_64-unknown-linux-musl/release/hbbr" "$TMP/hbbr"
}
get_binaries
chmod 755 "$TMP/hbbs" "$TMP/hbbr"
"$TMP/hbbs" --version >/dev/null || die "hbbs 二进制无法运行"
log "二进制就绪：$("$TMP/hbbs" --version) / $("$TMP/hbbr" --version)"

# ------------------------------------------------------------ 铺设文件 ----
BIN_DIR="$DESTDIR/usr/bin"
WORK_DIR="$DESTDIR/var/lib/rustdesk-server"
LOG_DIR="$DESTDIR/var/log/rustdesk-server"
UNIT_DIR="$DESTDIR/etc/systemd/system"
mkdir -p "$BIN_DIR" "$WORK_DIR" "$LOG_DIR" "$UNIT_DIR"
TS="$(date +%Y%m%d%H%M%S)"
for b in hbbs hbbr; do
  if [ -f "$BIN_DIR/$b" ]; then
    cp -a "$BIN_DIR/$b" "$BIN_DIR/$b.bak-$TS"
    log "已备份原 $b 到 $BIN_DIR/$b.bak-$TS"
  fi
  install -m 755 "$TMP/$b" "$BIN_DIR/$b"
done
log "hbbs / hbbr 已安装到 $BIN_DIR（工作目录 $WORK_DIR，密钥与设备库保留不动）"

cat > "$UNIT_DIR/rustdesk-hbbs.service" <<EOF
[Unit]
Description=Rustdesk Signal Server
After=network.target

[Service]
Type=simple
LimitNOFILE=1000000
ExecStart=/usr/bin/hbbs -r $RELAY_IP:21117 -k _
WorkingDirectory=/var/lib/rustdesk-server/
Restart=always
StandardOutput=append:/var/log/rustdesk-server/hbbs.log
StandardError=append:/var/log/rustdesk-server/hbbs.error
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

cat > "$UNIT_DIR/rustdesk-hbbr.service" <<EOF
[Unit]
Description=Rustdesk Relay Server
After=network.target

[Service]
Type=simple
LimitNOFILE=1000000
ExecStart=/usr/bin/hbbr -k _
WorkingDirectory=/var/lib/rustdesk-server/
Restart=always
StandardOutput=append:/var/log/rustdesk-server/hbbr.log
StandardError=append:/var/log/rustdesk-server/hbbr.error
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
log "systemd unit 已写入（rustdesk-hbbs / rustdesk-hbbr）"

# ------------------------------------------------------------ API（可选）----
API_ADDR="http://$RELAY_IP:21114"
if [ "$WITH_API" = "1" ]; then
  API_DIR="$DESTDIR/opt/rustdesk-api"
  mkdir -p "$API_DIR"
  log "下载 rustdesk-api v2.7 ..."
  curl -fsSL --max-time 600 -o "$TMP/api.tar.gz" "$API_TARBALL"
  # 压缩包内有一层 release/ 目录，先定位 apimain 所在目录作为源根
  ( cd "$TMP" && mkdir -p api-x && tar -xzf api.tar.gz -C api-x )
  API_SRC="$(dirname "$(find "$TMP/api-x" -name apimain -type f | head -1)")"
  [ -n "$API_SRC" ] && [ -d "$API_SRC" ] || die "rustdesk-api 压缩包里找不到 apimain"
  # 铺设时不覆盖已存在的 conf/ 与 data/ 文件（保留配置与数据）
  ( cd "$API_SRC" && find . -type f | while read -r f; do
      case "$f" in
        ./conf/*|./data/*) [ -f "$API_DIR/${f#./}" ] || { mkdir -p "$API_DIR/$(dirname "${f#./}")"; cp "$f" "$API_DIR/${f#./}"; } ;;
        *) mkdir -p "$API_DIR/$(dirname "${f#./}")"; cp "$f" "$API_DIR/${f#./}" ;;
      esac
    done )
  chmod 755 "$API_DIR/apimain" 2>/dev/null || true
  if [ -n "$API_DOMAIN" ]; then API_ADDR="https://$API_DOMAIN"; fi
  if [ -f "$API_DIR/conf/config.yaml" ]; then
    sed -i "s|^\(\s*api-server:\s*\).*|\1\"$API_ADDR\"|" "$API_DIR/conf/config.yaml" || true
  fi
  cat > "$UNIT_DIR/rustdesk-api.service" <<EOF
[Unit]
Description=RustDesk API Server (lejianwen v2.7)
After=network.target rustdesk-hbbs.service rustdesk-hbbr.service
Wants=rustdesk-hbbs.service rustdesk-hbbr.service

[Service]
Type=simple
WorkingDirectory=/opt/rustdesk-api
ExecStart=/opt/rustdesk-api/apimain
Environment=TZ=Asia/Shanghai
Environment=RUSTDESK_API_LANG=zh-CN
Restart=always
RestartSec=5
LimitNOFILE=1000000

[Install]
WantedBy=multi-user.target
EOF
  log "rustdesk-api 已铺设（API 地址 $API_ADDR）"
fi

# ------------------------------------------------------------ 生效 --------
if [ -n "$DESTDIR" ]; then
  log "DESTDIR 测试模式：跳过 systemctl / ufw / nginx / certbot，文件已铺设到 $DESTDIR"
else
  if command -v ufw >/dev/null 2>&1 && ufw status | grep -q "Status: active"; then
    ufw allow 21115:21119/tcp >/dev/null && ufw allow 21116/udp >/dev/null
    [ "$WITH_API" = "1" ] && [ -z "$API_DOMAIN" ] && ufw allow 21114/tcp >/dev/null
    log "ufw 已放行 21115:21119/tcp 与 21116/udp"
  fi
  systemctl daemon-reload
  systemctl enable --now rustdesk-hbbs rustdesk-hbbr >/dev/null 2>&1 || \
    { systemctl enable rustdesk-hbbs rustdesk-hbbr; systemctl restart rustdesk-hbbs rustdesk-hbbr; }
  [ "$WITH_API" = "1" ] && { systemctl enable rustdesk-api >/dev/null 2>&1; systemctl restart rustdesk-api; }
  if [ "$WITH_API" = "1" ] && [ -n "$API_DOMAIN" ]; then
    log "配置 Nginx 反代与 HTTPS 证书（$API_DOMAIN）..."
    export DEBIAN_FRONTEND=noninteractive
    command -v nginx >/dev/null 2>&1 || apt-get update -q && apt-get install -y -q nginx
    command -v certbot >/dev/null 2>&1 || apt-get install -y -q certbot python3-certbot-nginx
    cat > "/etc/nginx/sites-available/rustdesk-api" <<EOF
server {
    listen 80;
    server_name $API_DOMAIN;
    location / {
        proxy_pass http://127.0.0.1:21114;
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 86400;
    }
}
EOF
    ln -sf /etc/nginx/sites-available/rustdesk-api /etc/nginx/sites-enabled/rustdesk-api
    nginx -t && systemctl reload nginx
    if [ -n "${CERTBOT_EMAIL:-}" ]; then
      certbot --nginx -d "$API_DOMAIN" --non-interactive --agree-tos -m "$CERTBOT_EMAIL" || \
        log "警告：证书申请失败（域名解析未指向本机？），API 暂走 http://$API_DOMAIN"
    else
      certbot --nginx -d "$API_DOMAIN" --non-interactive --agree-tos --register-unsafely-without-email || \
        log "警告：证书申请失败（域名解析未指向本机？），API 暂走 http://$API_DOMAIN"
    fi
  fi
  sleep 3
  for s in rustdesk-hbbs rustdesk-hbbr; do
    systemctl is-active --quiet "$s" && log "$s 运行中" || die "$s 未能启动，看 /var/log/rustdesk-server/ 日志"
  done
  [ "$WITH_API" = "1" ] && { systemctl is-active --quiet rustdesk-api && log "rustdesk-api 运行中" || log "警告：rustdesk-api 未启动，看 journalctl -u rustdesk-api"; }
  for i in $(seq 1 10); do [ -f /var/lib/rustdesk-server/id_ed25519.pub ] && break; sleep 1; done
fi

# ------------------------------------------------------------ 汇总 --------
KEY="$(cat "$WORK_DIR/id_ed25519.pub" 2>/dev/null || echo '(服务首次启动后生成，见 /var/lib/rustdesk-server/id_ed25519.pub)')"
echo
echo "================ RustDesk 服务端安装完成 ================"
echo "客户端（设置 → 网络 → ID/中继服务器）填："
echo "  ID 服务器：   $RELAY_IP:21116"
echo "  中继服务器：  $RELAY_IP:21117"
[ "$WITH_API" = "1" ] && echo "  API 服务器：  $API_ADDR"
echo "  Key：         $KEY"
echo "=========================================================="
