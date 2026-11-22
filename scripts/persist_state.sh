#!/usr/bin/env bash
# 把本轮去重状态、按天归档与地址表缓存提交到独立的 state 分支。
# 不用 actions/cache：缓存被逐出会导致每轮重发。
set -euo pipefail

STATE_BRANCH="${STATE_BRANCH:-state}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

mkdir -p "$TMP"
[ -f state/state.json ] && cp state/state.json "$TMP/state.json"
[ -f report.json ] && cp report.json "$TMP/last-report.json"
if [ -d state/archive ]; then
  mkdir -p "$TMP/archive"
  cp -r state/archive/. "$TMP/archive/" 2>/dev/null || true
fi
if [ -d state/labels ]; then
  mkdir -p "$TMP/labels"
  cp -r state/labels/. "$TMP/labels/" 2>/dev/null || true
fi

if [ ! -f "$TMP/state.json" ] && [ ! -d "$TMP/labels" ] && [ ! -d "$TMP/archive" ]; then
  echo "本轮没有任何状态产物，跳过状态写入。"
  exit 0
fi

git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
git fetch origin "$STATE_BRANCH"
git checkout -B "$STATE_BRANCH" "origin/$STATE_BRANCH"
mkdir -p state state/archive state/labels
[ -f "$TMP/state.json" ] && cp "$TMP/state.json" state/state.json
[ -f "$TMP/last-report.json" ] && cp "$TMP/last-report.json" state/last-report.json
if [ -d "$TMP/archive" ]; then cp -r "$TMP/archive/." state/archive/ 2>/dev/null || true; fi
if [ -d "$TMP/labels" ]; then cp -r "$TMP/labels/." state/labels/ 2>/dev/null || true; fi
git add -f state
if git diff --cached --quiet; then
  echo "状态无变化，无需提交。"
  exit 0
fi
git commit -m "chore: 更新去重状态与运行报告 $STAMP"
git push origin "$STATE_BRANCH"
echo "状态已提交到 $STATE_BRANCH 分支。"
