#!/usr/bin/env bash
# 从 state 分支恢复去重状态与地址表缓存，避免每轮都从零开始。
set -euo pipefail

STATE_BRANCH="${STATE_BRANCH:-state}"
git fetch origin "$STATE_BRANCH"
mkdir -p state
if git cat-file -e "origin/$STATE_BRANCH:state/state.json" 2>/dev/null; then
  git show "origin/$STATE_BRANCH:state/state.json" > state/state.json
  echo "已从 $STATE_BRANCH 分支恢复去重状态。"
else
  echo "$STATE_BRANCH 分支尚无 state.json，本轮按首次运行处理。"
fi

if git cat-file -e "origin/$STATE_BRANCH:state/labels/eth_exchanges.json" 2>/dev/null; then
  mkdir -p state/labels
  git show "origin/$STATE_BRANCH:state/labels/eth_exchanges.json" > state/labels/eth_exchanges.json
  echo "已恢复地址表缓存 state/labels/eth_exchanges.json。"
else
  echo "尚无地址表缓存，本轮只用 cex-list 与 Blockscout 自带标签。"
fi
