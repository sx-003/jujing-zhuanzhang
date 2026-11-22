"""兜底层：Exa 公共 MCP + Anysearch 搜索补漏。

Exa 公共 MCP 免 key（https://mcp.exa.ai/mcp），Anysearch 用环境变量里的密钥轮换。
兜底只补「发现层没抓到的大额转账」，命中后同样要走阈值、去重与推送格式。
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from . import http, rules

EXA_MCP = "https://mcp.exa.ai/mcp"
ANYSEARCH_DEFAULT_BASE_URL = "https://api.anysearch.com"
JSONRPC_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
    "User-Agent": http.BROWSER_UA,
    "Origin": "https://mcp.exa.ai",
}

DISCOVERY_QUERIES = [
    "巨鲸转账 交易所 转入 大额",
    "大额 USDT 转入 交易所 链上",
    "whale transfer exchange inflow 10 million",
    "large crypto transfer to exchange wallet",
    "bitcoin whale exchange inflow",
]


@dataclass
class SearchHit:
    query: str
    engine: str
    url: str
    title: str
    snippet: str = ""
    published_at: str = ""


@dataclass
class CloudReport:
    hits: List[SearchHit] = field(default_factory=list)
    queries_used: List[str] = field(default_factory=list)
    engines: Dict[str, int] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)
    skipped: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"queries": self.queries_used, "engine_counts": self.engines, "hits": len(self.hits),
                "errors": self.errors[:8], "skipped": self.skipped[:8]}


def _parse_sse_or_json(payload: str):
    if payload.lstrip().startswith("{"):
        return json.loads(payload)
    messages = []
    for line in payload.splitlines():
        if line.startswith("data:"):
            raw = line[5:].strip()
            if raw and raw != "[DONE]":
                try:
                    messages.append(json.loads(raw))
                except ValueError:
                    continue
    return messages[-1] if messages else {}


def mcp_call(endpoint: str, tool_name: str, arguments: dict, *, timeout: float = 25.0,
             origin: str = "https://mcp.exa.ai") -> str:
    headers = dict(JSONRPC_HEADERS)
    headers["Origin"] = origin
    init_payload = {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                   "clientInfo": {"name": "juejing-zhuanzhang", "version": "1.0.0"}},
    }
    init = http.fetch(endpoint, method="POST", body=json.dumps(init_payload).encode(),
                      headers=headers, timeout=timeout, retries=1, max_bytes=1_000_000)
    if not init.ok:
        raise RuntimeError(f"MCP 初始化失败: {init.error_type} {init.error[:120]}")
    call_payload = {
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": tool_name, "arguments": arguments},
    }
    result = http.fetch(endpoint, method="POST", body=json.dumps(call_payload).encode(),
                        headers=headers, timeout=timeout, retries=1, max_bytes=2_000_000)
    if not result.ok:
        raise RuntimeError(f"MCP {tool_name} 失败: {result.error_type} {result.error[:120]}")
    try:
        data = _parse_sse_or_json(result.text())
    except ValueError as exc:
        raise RuntimeError(f"MCP {tool_name} 返回非 JSON") from exc
    if isinstance(data, list):
        data = next((item for item in data if isinstance(item, dict) and item.get("id") == 2),
                    data[-1] if data and isinstance(data[-1], dict) else {})
    if not isinstance(data, dict) or "error" in data:
        raise RuntimeError(f"MCP {tool_name} 报错: {str(data)[:200]}")
    payload = (data.get("result") or {}).get("content") or []
    texts = [str(block.get("text") or "") for block in payload if isinstance(block, dict)]
    return "\n".join(text for text in texts if text)


def _hits_from_text(text: str, query: str, engine: str) -> List[SearchHit]:
    hits: List[SearchHit] = []
    chunks = re.split(r"\n(?=Title:)", text)
    for chunk in chunks:
        url_match = re.search(r"^URL:\s*(https?://\S+)", chunk, re.M)
        if not url_match:
            continue
        title_match = re.search(r"^Title:\s*(.+)$", chunk, re.M)
        highlight_match = re.search(r"^Highlights:\s*(.*)$", chunk, re.M | re.S)
        published_match = re.search(r"^Published:\s*(.+)$", chunk, re.M)
        snippet = highlight_match.group(1).strip()[:1000] if highlight_match else ""
        hits.append(SearchHit(
            query, engine, url_match.group(1).rstrip(".,;)"),
            (title_match.group(1).strip() if title_match else "")[:300],
            snippet,
            (published_match.group(1).strip()[:40] if published_match else ""),
        ))
    if hits:
        return hits[:8]
    # Anysearch 走 /v1/search 返回 JSON（{"code":0,"data":{"results":[...]}}）。
    seen = set()
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        parsed = None
    if parsed is not None:
        def walk(value):
            if isinstance(value, dict):
                url = str(value.get("url") or value.get("link") or "")
                if url.startswith("http") and url not in seen:
                    seen.add(url)
                    hits.append(SearchHit(
                        query, engine, url,
                        str(value.get("title") or "")[:300],
                        str(value.get("snippet") or value.get("text")
                            or value.get("content") or "")[:800],
                        str(value.get("published") or value.get("date") or "")[:40],
                    ))
                for child in value.values():
                    walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)
        walk(parsed)
    return hits[:8]


def exa_search(query: str) -> List[SearchHit]:
    objective = ("找出最近几小时内链上大额加密货币转账的事实：金额、数量、转出方、转入方、"
                 "时间与交易链接；优先交易所流入流出与巨鲸转账原文，排除行情预测与营销内容。")
    text = mcp_call(EXA_MCP, "web_search_exa",
                    {"query": query, "objective": objective, "numResults": 5})
    return _hits_from_text(text, query, "exa_public")


def _anysearch_keys() -> List[str]:
    raw = os.environ.get("ANYSEARCH_KEYS", "")
    return [key.strip() for key in raw.replace(";", ",").split(",") if key.strip()]


def anysearch_search(query: str, key: str) -> List[SearchHit]:
    url = f"{os.environ.get('ANYSEARCH_BASE_URL', ANYSEARCH_DEFAULT_BASE_URL).rstrip('/')}/v1/search"
    payload = json.dumps({"query": query, "max_results": 5}).encode()
    headers = {"Content-Type": "application/json", "User-Agent": http.BROWSER_UA,
               "Authorization": f"Bearer {key}", "X-API-Key": key}
    result = http.fetch(url, method="POST", body=payload, headers=headers, timeout=25, retries=1)
    if not result.ok:
        raise RuntimeError(f"Anysearch {result.error_type}: {result.error[:120]}")
    return _hits_from_text(result.text(), query, "anysearch")


def build_query_plan(daily_used: Dict[str, str], max_queries: int) -> List[str]:
    plan = []
    for query in DISCOVERY_QUERIES:
        if query not in daily_used and len(plan) < max_queries:
            plan.append(query)
    return plan


def run_cloud(daily_used: Dict[str, str], *, max_queries: int = 3, enabled: bool = True) -> CloudReport:
    report = CloudReport()
    if not enabled:
        report.skipped.append("已用 --no-cloud 关闭云端搜索")
        return report
    keys = _anysearch_keys()
    for query in build_query_plan(daily_used, max_queries):
        hits = []
        try:
            hits = exa_search(query)
        except Exception as exc:  # noqa: BLE001 云搜索失败留给 Anysearch 兜底
            message = f"{query}: {type(exc).__name__} {str(exc)[:120]}"
            if keys:
                try:
                    hits = anysearch_search(query, keys[0])
                    report.errors.append(message + " → Anysearch 兜底成功")
                except Exception as exc2:  # noqa: BLE001
                    report.errors.append(message + f" → Anysearch 也失败: {str(exc2)[:120]}")
                    hits = []
            else:
                report.errors.append(message + " → 未配置 ANYSEARCH_KEYS，无法兜底")
                hits = []
        if not hits:
            report.skipped.append(f"{query}: 无结果")
            continue
        report.hits.extend(hits)
        report.queries_used.append(query)
        report.engines[hits[0].engine] = report.engines.get(hits[0].engine, 0) + 1
    return report


_AMOUNT_RE = re.compile(r"\$\s?([\d,]+(?:\.\d+)?)\s*([KMB])?\b", re.I)
_WAN_RE = re.compile(r"([\d,]+(?:\.\d+)?)\s*(亿|万)")
_SYMBOL_RE = re.compile(r"#([A-Za-z0-9]{2,10})")


def hits_to_transfers(hits: List[SearchHit]) -> List[rules.Transfer]:
    """从搜索命中里解析出转账事实；解析不出金额与方向的直接丢掉。"""
    items: List[rules.Transfer] = []
    for hit in hits:
        text = f"{hit.title} {hit.snippet}"
        usd = 0.0
        match = _AMOUNT_RE.search(text)
        if match:
            value = float(match.group(1).replace(",", ""))
            unit = (match.group(2) or "").upper()
            usd = value * {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000}.get(unit, 1)
        if not usd:
            match = _WAN_RE.search(text)
            if match:
                value = float(match.group(1).replace(",", ""))
                usd = value * (100_000_000 if match.group(2) == "亿" else 10_000)
        if not usd:
            continue
        symbol_match = _SYMBOL_RE.search(text)
        symbol = symbol_match.group(1).upper() if symbol_match else ""
        if not symbol:
            continue
        parts = re.split(r"➔|➜|→|->", text)
        if len(parts) < 2:
            continue
        from_label = rules.normalize_exchange_label(parts[0].strip()[-40:]) if rules.is_exchange_label(parts[0]) else ""
        to_label = rules.normalize_exchange_label(parts[1].strip()[:40]) if rules.is_exchange_label(parts[1]) else ""
        if not from_label and not to_label:
            continue
        if not hit.url.startswith(("http://", "https://")):
            continue
        ts = datetime.now(timezone.utc).timestamp()
        items.append(rules.Transfer(
            source="cloud", chain="", symbol=symbol, amount=0.0, usd=usd,
            from_label=from_label, to_label=to_label, ts=ts, url=hit.url,
            extra={"note": "搜索补漏，交易所标签可能不准", "query": hit.query},
        ))
    return items
