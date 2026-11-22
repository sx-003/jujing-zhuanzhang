"""标注层：地址 → 交易所/协议名。

地址表全部来自公开可下载、自动更新的开源数据集，我们自己一份名单都不维护：
- tradezon/cex-list：22 家交易所的以太坊地址（22KB，每轮下载成本可忽略）。
- prettydeath/wallet-attribution：48 条网络、63 万地址，每行带来源与置信度；
  本仓只取「交易所类别且能对上交易所名」的子集，由每日一次的工作流刷新后存到 state 分支。
另外 Blockscout 返回里自带的 tags 也当作一级标注。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Dict, Optional

from . import http, rules

CEX_LIST_URL = "https://raw.githubusercontent.com/tradezon/cex-list/main/data/ethereum-mainnet.json"
WALLET_ATTRIBUTION_URL = "https://raw.githubusercontent.com/prettydeath/wallet-attribution/main/data/ethereum.csv"
LABEL_CACHE = "state/labels/eth_exchanges.json"


class LabelBook:
    """地址标注簿：cex-list 实时下载 + wallet-attribution 子集缓存 + Blockscout 标签。"""

    def __init__(self) -> None:
        self.cex: Dict[str, str] = {}
        self.attribution: Dict[str, str] = {}
        self.stats = {"cex": 0, "attribution": 0, "hit": 0, "miss": 0}

    def load_cex_list(self) -> None:
        result = http.fetch_json(CEX_LIST_URL, timeout=25, retries=1, max_bytes=2_000_000)
        if not result.ok:
            raise RuntimeError(f"cex-list 抓取失败: {result.error_type} {result.error[:100]}")
        payload = result.json()
        if not isinstance(payload, dict):
            raise RuntimeError("cex-list 结构异常")
        for address, name in payload.items():
            if not isinstance(address, str) or not address.startswith("0x"):
                continue
            label = rules.normalize_exchange_label(str(name))
            if label:
                self.cex[address.lower()] = label
        self.stats["cex"] = len(self.cex)

    def load_cached_attribution(self, path: str = LABEL_CACHE) -> bool:
        target = Path(path)
        if not target.exists():
            return False
        try:
            payload = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        table = payload.get("exchanges") or {}
        self.attribution = {str(k).lower(): str(v) for k, v in table.items()}
        self.stats["attribution"] = len(self.attribution)
        return True

    def lookup(self, address: str) -> str:
        key = (address or "").strip().lower()
        if not key:
            return ""
        label = self.cex.get(key) or self.attribution.get(key)
        if label:
            self.stats["hit"] += 1
            return label
        self.stats["miss"] += 1
        return ""

    def to_dict(self) -> dict:
        return dict(self.stats)


def build_attribution_subset(csv_text: str) -> Dict[str, str]:
    """从 wallet-attribution 的 CSV 里挑出交易所与机构/做市商行，做成地址 → 名称的紧凑表。"""
    import csv
    import io

    subset: Dict[str, str] = {}
    reader = csv.DictReader(io.StringIO(csv_text))
    for row in reader:
        category = (row.get("category") or "").strip()
        if category not in ("exchange", "entity"):
            continue
        address = (row.get("address") or "").strip().lower()
        if not address.startswith("0x"):
            continue
        entity = (row.get("entity") or "").strip()
        label = (row.get("label") or "").strip()
        candidate = entity or label
        if not candidate:
            continue
        if not rules.is_known_label(candidate):
            continue
        subset[address] = rules.normalize_label(candidate)
    return subset


def refresh_attribution_cache(csv_path: str = LABEL_CACHE, csv_url: str = WALLET_ATTRIBUTION_URL) -> dict:
    """下载最新版地址表并写入缓存文件（每日一次的工作流调用）。"""
    result = http.fetch(csv_url, timeout=120, retries=1, max_bytes=64 * 1024 * 1024)
    if not result.ok:
        raise RuntimeError(f"wallet-attribution 抓取失败: {result.error_type} {result.error[:120]}")
    text = result.text()
    subset = build_attribution_subset(text)
    payload = {
        "updated_at": result.duration_ms and "",
        "source": csv_url,
        "count": len(subset),
        "exchanges": subset,
    }
    from datetime import datetime, timezone
    payload["updated_at"] = datetime.now(timezone.utc).isoformat()
    target = Path(csv_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return {"count": len(subset), "path": str(target)}


def blockscout_tags(entity: dict) -> str:
    """从 Blockscout 返回的 address metadata 里取交易所/机构标签。"""
    if not isinstance(entity, dict):
        return ""
    meta = entity.get("metadata") or {}
    tags = meta.get("tags") or []
    for tag in tags:
        if not isinstance(tag, dict):
            continue
        name = str(tag.get("name") or "").strip()
        if not name:
            continue
        if tag.get("tagType") == "generic" and name.lower() in ("hot wallet", "cold wallet", "exchange"):
            continue
        if rules.is_known_label(name):
            return rules.normalize_label(name)
    return ""


def find_label(book: LabelBook, address: str, blockscout_entity: Optional[dict] = None) -> str:
    """先看 Blockscout 自带标签，再查地址表。"""
    tag = blockscout_tags(blockscout_entity or {})
    if tag:
        return tag
    return book.lookup(address)
