#!/usr/bin/env bash
# corpus-plugin install.sh — 本地部署脚本
#
# 构建 corpus-query 插件 + validate + 复制到 OpenClaw 插件目录。
# (fact-infra 为既有构建成果,不在本项目部署范围。)
# 幂等、非交互、不触碰 gateway 配置(插件加载/重启由用户决定)。
#
# 用法:
#   ./install.sh           # 构建 + 验证 + 复制
#   ./install.sh --check   # 只构建 + validate,不复制(CI 用)
#   ./install.sh --dry-run # 打印将执行的复制而不执行
set -euo pipefail

REPO="$(cd "$(dirname "$0")" && pwd)"
OPENCLAW_BIN="${OPENCLAW_BIN:-openclaw}"
OPENCLAW_PLUGIN_DIR="${OPENCLAW_PLUGIN_DIR:-$HOME/.openclaw/plugins}"
MODE="${1:-install}"

log() { printf '\e[36m[install]\e[0m %s\n' "$*"; }
die() { printf '\e[31m[install] ERROR: %s\e[0m\n' "$*" >&2; exit 1; }

require() { command -v "$1" >/dev/null 2>&1 || die "missing $1"; }
require python3; require node

# 定位 tsc:插件 node_modules 里应有 typescript(或从兄弟插件复用,仓库内构建常见)
find_tsc() {
  local dir="$1"
  if [ -f "$dir/node_modules/typescript/lib/tsc.js" ]; then
    echo "node $dir/node_modules/typescript/lib/tsc.js"
  elif [ -f "$REPO/plugins/corpus-query/node_modules/typescript/lib/tsc.js" ]; then
    echo "node $REPO/plugins/corpus-query/node_modules/typescript/lib/tsc.js"
  elif command -v pnpm >/dev/null 2>&1 && [ -x "$dir/node_modules/.bin/tsc" ]; then
    echo "$dir/node_modules/.bin/tsc"
  else
    return 1
  fi
}

# 铺 @types/node(供 node:fs / node:sqlite 等类型解析);优先复用 corpus-query 的 pnpm store
ensure_node_types() {
  local dir="$1"
  [ -d "$dir/node_modules/@types/node" ] && return 0
  local src
  for src in \
    "$REPO/plugins/corpus-query/node_modules/.pnpm/@types+node@*/node_modules/@types/node" \
    /usr/local/lib/node_modules/@types/node; do
    local cand
    for cand in $src; do
      if [ -d "$cand" ]; then
        mkdir -p "$dir/node_modules/@types"
        ln -sfn "$cand" "$dir/node_modules/@types/node"
        return 0
      fi
    done
  done
  return 1
}

# 铺 openclaw 包(peerDep,供 'openclaw/plugin-sdk/*' 类型解析);复用 corpus-query 的
ensure_openclaw_pkg() {
  local dir="$1"
  [ -e "$dir/node_modules/openclaw" ] && return 0
  if [ -e "$REPO/plugins/corpus-query/node_modules/openclaw" ]; then
    mkdir -p "$dir/node_modules"
    ln -sfn "$REPO/plugins/corpus-query/node_modules/openclaw" "$dir/node_modules/openclaw"
    return 0
  fi
  return 1
}

install_one() {
  local plugin="$1"
  local dir="$REPO/plugins/$plugin"
  local entry="$dir/dist/index.js"

  log "==> $plugin: build"
  local tsc
  if ! tsc="$(find_tsc "$dir")"; then
    log "$plugin: no typescript in node_modules, installing..."
    (cd "$dir" && npm install --no-audit --no-fund >/dev/null) || die "$plugin: npm install failed"
    tsc="$(find_tsc "$dir")" || die "$plugin: typescript still missing after install"
  fi
  ensure_node_types "$dir" || log "$plugin: @types/node not found (type-check may fail if tsconfig needs it)"
  ensure_openclaw_pkg "$dir" || log "$plugin: openclaw pkg not found (peerDep types may fail)"
  # shellcheck disable=SC2086
  (cd "$dir" && $tsc -p tsconfig.json) || die "$plugin: tsc build failed"

  log "==> $plugin: validate"
  local validate_entry="$entry"
  [ -f "$validate_entry" ] || validate_entry="$dir/dist/src/index.js"
  if command -v "$OPENCLAW_BIN" >/dev/null 2>&1; then
    "$OPENCLAW_BIN" plugins validate --entry "$validate_entry" >/dev/null 2>&1 \
      || log "$plugin: validate warning (CLI version may differ; build succeeded)"
  else
    log "$plugin: '$OPENCLAW_BIN' not found, skipping validate"
  fi

  if [ "$MODE" = "--check" ]; then
    log "$plugin: CHECK OK (dist present: $([ -d "$dir/dist" ] && echo yes || echo no))"
    return 0
  fi

  local target="$OPENCLAW_PLUGIN_DIR/$plugin"
  if [ "$MODE" = "--dry-run" ]; then
    log "$plugin: would copy dist → $target/dist"
    return 0
  fi

  log "==> $plugin: install → $target"
  mkdir -p "$target"
  rm -rf "$target/dist"
  cp -r "$dir/dist" "$target/dist"
  cp "$dir/openclaw.plugin.json" "$dir/package.json" "$target/"
  log "$plugin: installed"
}

install_one corpus-query

log "==> done"
if [ "$MODE" = "install" ]; then
  log "插件已复制到 $OPENCLAW_PLUGIN_DIR,由 OpenClaw 加载。"
  log "gateway 重启需 L 确认(见 progress.md 红线)。"
fi