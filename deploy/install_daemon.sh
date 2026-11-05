#!/usr/bin/env bash
# 一键安装 agent-wechat 守护进程（Linux x64 / arm64），给 astrbot_plugin_wechat_agent 用。
#
#   bash install_daemon.sh                         # 装到 ~/agent-wechat，开机自启
#   GH_PROXY=https://gh-proxy.com/ bash install_daemon.sh   # 国内服务器连不上 GitHub 时加镜像前缀
#
# 可选环境变量：
#   WX_VERSION  守护进程版本，默认 1.5.1（插件测试过的版本）
#   WX_HOME     安装目录，默认 ~/agent-wechat（数据、token、配置都在这里）
#   WX_LISTEN   监听地址，默认 127.0.0.1:6174（守护进程只允许听本机）
#   GH_PROXY    GitHub 下载加速前缀，例如 https://gh-proxy.com/
#
# 做的事：下载 wx 并核对 sha256 → 生成 token 和配置 → 用 systemd 托管（没有 systemd 就用 wx daemon）。
# 不做的事：不登录微信。登录交给插件：AstrBot 里启用适配器后，二维码会打在 AstrBot 日志里。
set -euo pipefail

WX_VERSION="${WX_VERSION:-1.5.1}"
WX_VERSION="${WX_VERSION#v}"
WX_HOME="${WX_HOME:-$HOME/agent-wechat}"
WX_LISTEN="${WX_LISTEN:-127.0.0.1:6174}"
GH_PROXY="${GH_PROXY:-}"
BIN_DIR="$HOME/.local/bin"
WX="$BIN_DIR/wx"

say() { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m错误：\033[0m%s\n' "$*" >&2; exit 1; }

[ "$(uname -s)" = Linux ] || die "这个脚本只支持 Linux。macOS / Windows 请到 https://github.com/thisnick/agent-wechat/releases 手动下载。"
case "$(uname -m)" in
  x86_64|amd64) ARCH=x64 ;;
  aarch64|arm64) ARCH=arm64 ;;
  *) die "不支持的 CPU 架构：$(uname -m)" ;;
esac
command -v curl >/dev/null || die "需要 curl：sudo apt install -y curl"

ASSET="wx-v${WX_VERSION}-linux-${ARCH}-gnu"
BASE="${GH_PROXY}https://github.com/thisnick/agent-wechat/releases/download/v${WX_VERSION}"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

say "下载 agent-wechat v${WX_VERSION}（${ASSET}）"
curl -fSL --retry 3 -o "$TMP/wx" "$BASE/$ASSET" || die "下载失败。国内服务器试试：GH_PROXY=https://gh-proxy.com/ bash $0"
curl -fsSL --retry 3 -o "$TMP/sums.json" "$BASE/wx-v${WX_VERSION}-checksums.json" || die "校验文件下载失败"

# checksums.json 是多行 JSON：找到这个文件名那一项，取它后面第一个 sha256
WANT="$(awk -v n="\"$ASSET\"" 'index($0, n) {f=1} f && /"sha256"/ {gsub(/[",]/, "", $2); print $2; exit}' "$TMP/sums.json")"
GOT="$(sha256sum "$TMP/wx" | cut -d' ' -f1)"
[ -n "$WANT" ] && [ "$WANT" = "$GOT" ] || die "sha256 对不上（期望 ${WANT:-无}，实际 $GOT），文件可能被篡改或下载不完整，已停止。"
say "sha256 校验通过"

mkdir -p "$BIN_DIR"
# 重复运行（升级）：换程序之前先停掉旧的守护进程，换完旧进程就认不出来、停不掉了
if [ -x "$WX" ]; then
  if systemctl is-active --quiet agent-wechat 2>/dev/null; then
    sudo systemctl stop agent-wechat
  elif [ -f "$WX_HOME/server.toml" ]; then
    "$WX" daemon stop --server-config "$WX_HOME/server.toml" >/dev/null 2>&1 || true
  fi
fi
install -m 755 "$TMP/wx" "$WX"
say "已安装到 $WX（$("$WX" --version 2>/dev/null || echo "版本未知")）"

umask 077
mkdir -p "$WX_HOME/data" "$WX_HOME/secrets"
TOKEN_FILE="$WX_HOME/secrets/api-token"
if [ ! -s "$TOKEN_FILE" ]; then
  head -c 32 /dev/urandom | base64 | tr -d '\n=' > "$TOKEN_FILE"
  say "已生成 token：$TOKEN_FILE"
else
  say "沿用已有 token：$TOKEN_FILE"
fi
CONFIG="$WX_HOME/server.toml"
cat > "$CONFIG" <<EOF
schemaVersion = 1
listen = "$WX_LISTEN"
dataDir = "$WX_HOME/data"
tokenFile = "$TOKEN_FILE"
EOF

if command -v systemctl >/dev/null && [ -d /run/systemd/system ] && sudo -n true 2>/dev/null; then
  sudo tee /etc/systemd/system/agent-wechat.service >/dev/null <<EOF
[Unit]
Description=agent-wechat daemon (for AstrBot)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$(id -un)
ExecStart=$WX serve --server-config $CONFIG
Restart=always
RestartSec=5
UMask=0077
NoNewPrivileges=yes

[Install]
WantedBy=multi-user.target
EOF
  sudo systemctl daemon-reload
  sudo systemctl enable --now agent-wechat
  MANAGE="sudo systemctl status agent-wechat（重启：sudo systemctl restart agent-wechat）"
else
  "$WX" daemon start --server-config "$CONFIG" >/dev/null
  MANAGE="$WX daemon status --server-config $CONFIG（没有 systemd，机器重启后要重新运行：$WX daemon start --server-config $CONFIG）"
fi

for _ in $(seq 1 15); do
  curl -fs -o /dev/null -H "Authorization: Bearer $(cat "$TOKEN_FILE")" "http://$WX_LISTEN/v1/accounts" && break
  sleep 1
done
curl -fs -o /dev/null -H "Authorization: Bearer $(cat "$TOKEN_FILE")" "http://$WX_LISTEN/v1/accounts" \
  || die "守护进程没起来，看看日志：journalctl -u agent-wechat -n 50（或 $WX daemon logs）"

say "守护进程已在 $WX_LISTEN 运行"
cat <<EOF

────────────── 接下来在 AstrBot 里 ──────────────
1. 插件市场搜「微信个人号适配器」安装（或从链接安装 https://github.com/heimixieb/astrbot_plugin_wechat_agent）
2. 机器人 → 新增 → 选「微信 (agent-wechat)」，填：
     守护进程地址  http://$WX_LISTEN
     Token 文件    AstrBot 直接装在这台机器上：$TOKEN_FILE
                   AstrBot 在 Docker 里：把它挂进容器，并给容器开 host 网络：
                     network_mode: host
                     volumes:
                       - $TOKEN_FILE:/run/secrets/wechat-api-token:ro
                   然后 Token 文件填 /run/secrets/wechat-api-token
3. 保存启用，打开 AstrBot「控制台」日志，用手机微信扫出现的二维码，并在手机上点「登录」。

管理守护进程：$MANAGE
EOF
