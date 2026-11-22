"""发现层：把各个公开源的原始数据抓成统一的 Transfer 记录。

三段式架构：
1. 发现层（这里）：各站自带交易所标签，先用它给的。
2. 标注层（labels.py）：Blockscout 标签 + wallet-attribution / cex-list 地址表反查未知地址。
3. 兜底层（cloud.py）：Exa 公共 MCP、Anysearch。

只取「转账」类事实：铸销、借贷质押由 rules 过滤，行情分析/新闻类消息在解析阶段就丢掉。
"""
from __future__ import annotations

import html as html_lib
import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from . import chains, http, rules

CRYPVU_URL = "https://crypvu.com/zh/whales.html"
TRACKWHALE_CHANNEL = "trackwhale"
WHALE_ALERT_CHANNEL = "whale_alert_io"
# 电报公开预览页的备用域名：一个被墙或挂了就按顺序换下一个，全不通才算这个源失败。
TELEGRAM_MIRRORS = (
    "https://telegram.me/s/{channel}",
    "https://telegram.dog/s/{channel}",
    "https://t.me/s/{channel}",
)
FLASHAI_URL = "https://flashai.vip/whales"
COINSTROOPER_URL = "https://coinstrooper.com/whales"
OURSCRYPTO_URL = "https://ourscrypto.com/zh-CN/bitcoin/whale-transfers"

# crypvu 页面 20MB 起步，只读前缀：数据按时间倒序，前缀就是最新的。
CRYPVU_MAX_BYTES = 3 * 1024 * 1024

KNOWN_TREASURY = ("treasury", "usdc treasury", "tether treasury")


@dataclass
class SourceReport:
    name: str
    layer: str
    ok: bool
    status: str
    items: List[rules.Transfer] = field(default_factory=list)
    error: str = ""
    detail: Dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"name": self.name, "layer": self.layer, "ok": self.ok, "status": self.status,
                "count": len(self.items), "error": self.error[:200], "detail": self.detail}


def _fail(name: str, layer: str, error_type: str, error: str, detail: Optional[dict] = None) -> SourceReport:
    return SourceReport(name=name, layer=layer, ok=False, status=error_type,
                        error=error[:300], detail=detail or {})


def _usd_from_text(text: str) -> float:
    """把 '$4,341.21万USD' / '$41M' / '1.2亿美元' 之类统一成美元数值。"""
    raw = (text or "").replace(",", "").strip()
    if not raw:
        return 0.0
    match = re.search(r"([\d.]+)\s*(亿|万|[KMB])?", raw, re.I)
    if not match:
        return 0.0
    try:
        value = float(match.group(1))
    except ValueError:
        return 0.0
    unit = (match.group(2) or "").lower()
    if unit == "亿":
        value *= 100_000_000
    elif unit == "万":
        value *= 10_000
    elif unit == "k":
        value *= 1_000
    elif unit == "m":
        value *= 1_000_000
    elif unit == "b":
        value *= 1_000_000_000
    return value


def _number_from_text(text: str) -> float:
    raw = (text or "").replace(",", "").strip()
    match = re.search(r"([\d.]+)", raw)
    if not match:
        return 0.0
    try:
        return float(match.group(1))
    except ValueError:
        return 0.0


def _symbol_from_text(text: str) -> str:
    match = re.search(r"#([A-Za-z0-9]{2,10})", text or "")
    if match:
        return match.group(1).upper()
    match = re.search(r"\$([A-Za-z0-9]{2,10})", text or "")
    if match:
        return match.group(1).upper()
    return ""


def _unknown_to_known(label: str) -> str:
    """把 'unknown wallet' / '未知钱包' 归一成空，后面统一显示「未知钱包」。"""
    low = (label or "").strip().lower()
    if not low:
        return ""
    if low in ("unknown wallet", "unknown", "unknown address", "未知钱包", "未知地址"):
        return ""
    return label.strip()


def _tx_hash_from_link(url: str) -> str:
    """从交易链接里抽交易哈希，统一去掉 0x 前缀并转小写，方便跨源对齐。"""
    tail = (url or "").rstrip("/").split("/")[-1]
    if re.fullmatch(r"0x[0-9a-fA-F]{64}|[0-9a-fA-F]{64}", tail or ""):
        return tail.lower().removeprefix("0x")
    return ""


def _clean_label(label: str) -> str:
    """统一标签：去空白、去 # 号；交易所与机构/做市商收敛成标准写法。"""
    text = _unknown_to_known((label or "").strip().lstrip("#").strip())
    if not text:
        return ""
    return rules.normalize_label(text)


# ---------------------------------------------------------------- crypvu

def fetch_crypvu(window_hours: float = 6.0, now: Optional[float] = None) -> SourceReport:
    """crypvu 中文站：页面里内嵌 <script id='wdata'> 的 JSON 数组，按时间倒序。"""
    name = "crypvu"
    result = http.fetch(CRYPVU_URL, timeout=45, retries=1, max_bytes=CRYPVU_MAX_BYTES,
                        allow_truncated=True, check_login_wall=True)
    if not result.ok:
        return _fail(name, "discovery", result.error_type, result.error)
    body = result.text()
    marker = "id='wdata'>"
    index = body.find(marker)
    if index < 0:
        index = body.find('id="wdata">')
        marker = 'id="wdata">'
    if index < 0:
        return _fail(name, "discovery", "low_quality", "页面里找不到 wdata 数据块", {"bytes": len(body)})
    payload = body[index + len(marker):]
    decoder = json.JSONDecoder()
    pos = 1 if payload.startswith("[") else 0
    entries = []
    cutoff = (now or time.time()) - window_hours * 3600
    while pos < len(payload):
        try:
            obj, end = decoder.raw_decode(payload, pos)
        except ValueError:
            break
        entries.append(obj)
        pos = end
        while pos < len(payload) and payload[pos] in " ,\n\r\t":
            pos += 1
        if pos < len(payload) and payload[pos] == "]":
            break
        if len(entries) > 40000:
            break
    if not entries:
        return _fail(name, "discovery", "low_quality", "wdata 解析不到条目", {"bytes": len(body)})
    items: List[rules.Transfer] = []
    skipped_old = 0
    for entry in entries:
        try:
            ts = float(entry.get("t") or 0)
        except (TypeError, ValueError):
            continue
        if ts < cutoff:
            skipped_old += 1
            continue
        symbol = str(entry.get("s") or "").upper()
        amount = float(entry.get("a") or 0)
        usd = float(entry.get("u") or 0)
        direction_code = str(entry.get("d") or "")
        from_label = _clean_label(str(entry.get("f") or "")) if entry.get("fl") else ""
        to_label = _clean_label(str(entry.get("o") or "")) if entry.get("tl") else ""
        link = str(entry.get("e") or "")
        chain = str(entry.get("c") or "").lower()
        tx_hash = _tx_hash_from_link(link)
        if not symbol or amount <= 0 or not link:
            continue
        items.append(rules.Transfer(
            source=name, chain=chain, symbol=symbol, amount=amount, usd=usd,
            from_label=from_label, to_label=to_label, ts=ts, url=link, tx_hash=tx_hash,
            direction_hint=direction_code, raw_from=str(entry.get("f") or ""), raw_to=str(entry.get("o") or ""),
            extra={"direction_code": direction_code, "note": direction_code},
        ))
    return SourceReport(name=name, layer="discovery", ok=True, status="ok", items=items,
                        detail={"entries": len(entries), "in_window": len(items),
                                "skipped_old": skipped_old, "truncated": result.truncated,
                                "bytes": len(body)})


# ------------------------------------------------------- Telegram 频道

def fetch_telegram_channel(channel: str, *, window_hours: float = 6.0,
                           fetcher=None) -> Tuple[http.FetchResult, str, List[str]]:
    """按备用域名顺序抓电报公开频道页，返回 (结果, 成功的地址, 失败记录)。

    fetcher 可注入（测试/自检用假实现），默认走 http.fetch。
    """
    do_fetch = fetcher or http.fetch
    tried: List[str] = []
    result = None
    for template in TELEGRAM_MIRRORS:
        url = template.format(channel=channel)
        result = do_fetch(url, timeout=25, retries=1, max_bytes=4_000_000)
        if result.ok:
            return result, url, tried
        tried.append(f"{url.split('/')[2]}:{result.error_type}")
    return result, "", tried


def _telegram_messages(page: str) -> List[dict]:
    """把 t.me 公开预览页拆成 [{text, time, links}]。"""
    blocks = re.split(r'<div class="tgme_widget_message_wrap', page)
    messages = []
    for block in blocks:
        text_match = re.search(r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>\s*(?:<div class="tgme_widget_message_footer|</div>\s*<div class="tgme_widget_message_footer)', block, re.S)
        if not text_match:
            continue
        raw = text_match.group(1)
        text = html_lib.unescape(re.sub(r"<br\s*/?>", "\n", raw))
        text = html_lib.unescape(re.sub(r"<[^>]+>", "", text)).strip()
        time_match = re.search(r'<time datetime="([^"]+)"', block)
        stamp = time_match.group(1) if time_match else ""
        links = [html_lib.unescape(link) for link in re.findall(r'href="(https?://[^"]+)"', block)]
        messages.append({"text": text, "time": stamp, "links": links})
    return messages


def _iso_to_ts(value: str) -> float:
    if not value:
        return 0.0
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def fetch_trackwhale(window_hours: float = 6.0, now: Optional[float] = None,
                     fetcher=None) -> SourceReport:
    """TrackWhale 公开频道：自带方向标签（EXCHANGE INFLOW/OUTFLOW 等）。"""
    name = "trackwhale"
    result, used_url, tried = fetch_telegram_channel(TRACKWHALE_CHANNEL, fetcher=fetcher)
    if not result or not result.ok:
        return _fail(name, "discovery", result.error_type if result else "provider_error",
                     (result.error if result else "全部备用域名都失败"),
                     {"mirrors_failed": tried})
    messages = _telegram_messages(result.text())
    if not messages:
        return _fail(name, "discovery", "low_quality", "公开预览页没有解析出消息")
    items: List[rules.Transfer] = []
    for message in messages:
        text = message["text"]
        ts = _iso_to_ts(message["time"])
        if not ts:
            continue
        if ts < (now or time.time()) - window_hours * 3600:
            continue
        if not re.search(r"EXCHANGE\s+(INFLOW|OUTFLOW)|ACCUMULATION|DISTRIBUTION", text, re.I):
            continue
        usd = _usd_from_text(text)
        symbol = _symbol_from_text(text)
        if not usd or not symbol:
            continue
        link = ""
        tx_hash = ""
        for candidate in message["links"]:
            if "trackwhale.io/transaction/" in candidate:
                link = candidate
                tx_hash = _tx_hash_from_link(candidate)
                break
        if not link:
            continue
        parts = re.split(r"➔|➜|→", text)
        from_label = ""
        to_label = ""
        if len(parts) >= 2:
            from_label = _clean_label(parts[0].split("\n")[-1].strip())
            to_label = _clean_label(parts[1].split("\n")[0].strip())
        direction_hint = ""
        if re.search(r"EXCHANGE\s+INFLOW", text, re.I):
            direction_hint = rules.DIR_IN
        elif re.search(r"EXCHANGE\s+OUTFLOW", text, re.I):
            direction_hint = rules.DIR_OUT
        items.append(rules.Transfer(
            source=name, chain="", symbol=symbol, amount=0.0, usd=usd,
            from_label=from_label, to_label=to_label, ts=ts, url=link, tx_hash=tx_hash,
            direction_hint=direction_hint, extra={"note": text[:200]},
        ))
    return SourceReport(name=name, layer="discovery", ok=True, status="ok", items=items,
                        detail={"messages": len(messages), "in_window": len(items),
                                "mirror": used_url, "mirrors_failed": tried})


def fetch_whale_alert(window_hours: float = 6.0, now: Optional[float] = None,
                      fetcher=None) -> SourceReport:
    """Whale Alert 公开频道：'1,533 $BTC (99,486,387 USD) transferred from #Kraken to unknown wallet'。"""
    name = "whale_alert"
    result, used_url, tried = fetch_telegram_channel(WHALE_ALERT_CHANNEL, fetcher=fetcher)
    if not result or not result.ok:
        return _fail(name, "discovery", result.error_type if result else "provider_error",
                     (result.error if result else "全部备用域名都失败"),
                     {"mirrors_failed": tried})
    messages = _telegram_messages(result.text())
    if not messages:
        return _fail(name, "discovery", "low_quality", "公开预览页没有解析出消息")
    pattern = re.compile(
        r"(?P<qty>[\d,]+(?:\.\d+)?)\s*\$(?P<sym>[A-Za-z0-9]{2,10})\s*"
        r"\((?P<usd>[\d,]+(?:\.\d+)?)\s*USD\)\s*"
        r"(?P<verb>transferred|moved|sent)\s+from\s+(?P<from>.+?)\s+to\s+(?P<to>.+?)\s*$",
        re.I | re.M)
    items: List[rules.Transfer] = []
    for message in messages:
        text = message["text"]
        ts = _iso_to_ts(message["time"])
        if not ts or ts < (now or time.time()) - window_hours * 3600:
            continue
        match = pattern.search(text)
        if not match:
            continue
        link = ""
        tx_hash = ""
        chain = ""
        for candidate in message["links"]:
            if "whale-alert.io/transaction/" in candidate:
                link = candidate
                parts = candidate.rstrip("/").split("/")
                chain = parts[-2] if len(parts) >= 2 else ""
                tx_hash = _tx_hash_from_link(candidate)
                break
        if not link:
            continue
        from_label = _clean_label(match.group("from"))
        to_label = _clean_label(match.group("to"))
        items.append(rules.Transfer(
            source=name, chain=chain, symbol=match.group("sym").upper(),
            amount=float(match.group("qty").replace(",", "")),
            usd=float(match.group("usd").replace(",", "")),
            from_label=from_label, to_label=to_label, ts=ts, url=link, tx_hash=tx_hash,
            extra={"note": text[:200]},
        ))
    return SourceReport(name=name, layer="discovery", ok=True, status="ok", items=items,
                        detail={"messages": len(messages), "in_window": len(items),
                                "mirror": used_url, "mirrors_failed": tried})


# ------------------------------------------------------------ 中文表格站

def _map_header(header: List[str]) -> Dict[str, int]:
    mapping = {}
    for index, cell in enumerate(header):
        low = cell.strip().lower()
        if "转出" in low and "转入" in low:
            mapping.setdefault("pair", index)
        elif any(word in low for word in ("时间", "time", "日期")):
            mapping.setdefault("time", index)
        elif any(word in low for word in ("链", "chain", "网络", "network")):
            mapping.setdefault("chain", index)
        elif "转账金额" in low or any(word in low for word in ("数量", "amount", "qty")):
            mapping.setdefault("amount", index)
        elif any(word in low for word in ("美元", "usd", "$", "价值", "转移金额", "金额")):
            mapping.setdefault("usd", index)
        elif any(word in low for word in ("币", "coin", "token", "代币", "资产", "asset")):
            mapping.setdefault("symbol", index)
        elif any(word in low for word in ("类型", "type", "方向", "动作", "分类")):
            mapping.setdefault("type", index)
        elif low.startswith("from") or "转出" in low or "来源" in low:
            mapping.setdefault("from", index)
        elif low.startswith("to") or "转入" in low or "去向" in low:
            mapping.setdefault("to", index)
        elif "哈希" in low or "hash" in low or "详情" in low or "explorer" in low or low == "tx":
            mapping.setdefault("link", index)
    return mapping


def _rows_from_tables(html: str) -> List[List[dict]]:
    rows = []
    for table in http.parse_html_tables(html):
        rows.extend(table)
    return rows


def fetch_table_source(name: str, url: str, window_hours: float = 6.0,
                       now: Optional[float] = None, tz_offset_hours: int = 0) -> SourceReport:
    """通用表格源：按表头关键字映射列，认不出的列就不取。

    tz_offset_hours：站点时间的时区偏移（中文站一般是北京时间 +8）。
    """
    result = http.fetch(url, timeout=35, retries=1, max_bytes=12_000_000, allow_truncated=True,
                        check_login_wall=True)
    if not result.ok:
        return _fail(name, "discovery", result.error_type, result.error)
    rows = _rows_from_tables(result.text())
    if not rows:
        return _fail(name, "discovery", "low_quality", "页面里没有解析到表格行",
                     {"bytes": len(result.data)})
    header = rows[0]
    mapping = _map_header([cell["text"] for cell in header])
    if "usd" not in mapping and "amount" not in mapping:
        return _fail(name, "discovery", "low_quality", "表头认不出金额列",
                     {"header": [cell["text"][:20] for cell in header]})
    items: List[rules.Transfer] = []
    now_ts = now or time.time()
    for row in rows[1:]:
        def cell(key: str) -> str:
            index = mapping.get(key)
            if index is None or index >= len(row):
                return ""
            return row[index]["text"]

        def cell_obj(key: str) -> dict:
            index = mapping.get(key)
            if index is None or index >= len(row):
                return {}
            return row[index]

        def link(key: str) -> str:
            index = mapping.get(key)
            if index is None or index >= len(row):
                return ""
            return row[index].get("href", "")

        raw_amount = cell("amount")
        raw_usd = cell("usd")
        if "$" in raw_amount or "USD" in raw_amount.upper():
            # 这一列写的是美元金额（coinstrooper 的 Amount 列就是 $ 金额），数量缺失时后面按行情折算。
            if not raw_usd:
                raw_usd = raw_amount
            raw_amount = ""
        symbol = ""
        symbol_cell = cell("symbol")
        if symbol_cell:
            first = symbol_cell.split("·")[0].strip().split()[0] if symbol_cell.strip() else ""
            if re.fullmatch(r"[A-Za-z]{2,10}", first or ""):
                symbol = first.upper()
        if not symbol:
            symbol = _symbol_from_text(raw_amount) or _symbol_from_text(symbol_cell)
        if not symbol:
            match = re.search(r"([A-Za-z]{2,10})\s*$", (raw_amount or symbol_cell).strip())
            symbol = match.group(1).upper() if match else ""
        amount = _number_from_text(raw_amount) if raw_amount else 0.0
        usd = _usd_from_text(raw_usd) if raw_usd else 0.0
        if not symbol or (usd <= 0 and amount <= 0):
            continue
        href = link("link")
        if not href:
            # 有些表格最后一列才是详情链接，表头还是空的。
            for cell_data in reversed(row):
                if cell_data.get("href"):
                    href = cell_data["href"]
                    break
        if href and href.startswith("/"):
            href = url.rsplit("/", 1)[0] + href
        ts = now_ts
        time_text = cell("time")
        if not time_text and symbol_cell:
            time_text = symbol_cell
        if time_text:
            parsed = _parse_table_time(time_text, now_ts, tz_offset_hours)
            ts = parsed or now_ts
        if ts < now_ts - window_hours * 3600:
            continue
        if not href:
            continue
        from_label = _clean_label(cell("from"))
        to_label = _clean_label(cell("to"))
        if "pair" in mapping:
            pair_cell = cell_obj("pair")
            spans = pair_cell.get("spans") or []
            if len(spans) >= 2:
                from_label = from_label or _clean_label(spans[0])
                to_label = to_label or _clean_label(spans[1])
            else:
                left, right = _split_pair(cell("pair"))
                from_label = from_label or left
                to_label = to_label or right
        direction_hint = _direction_from_type(cell("type"))
        items.append(rules.Transfer(
            source=name, chain=cell("chain").strip().lower(), symbol=symbol, amount=amount,
            usd=usd, from_label=from_label, to_label=to_label, ts=ts, url=href,
            direction_hint=direction_hint, extra={"note": cell("type")},
        ))
    return SourceReport(name=name, layer="discovery", ok=True, status="ok", items=items,
                        detail={"rows": len(rows), "in_window": len(items),
                                "header": [cell["text"][:16] for cell in header]})


def _split_pair(text: str) -> Tuple[str, str]:
    """表格里的「转出方 → 转入方」拼在一格时拆成两半。"""
    raw = (text or "").strip()
    if not raw:
        return "", ""
    parts = re.split(r"\s{1,}(?=[0-9a-fA-F]{0,4}[.…])|(?<=[…\w])\s(?=[A-Za-z0-9])", raw)
    if len(parts) >= 2:
        return _unknown_to_known(parts[0]), _unknown_to_known(parts[1])
    return _unknown_to_known(raw), ""


def _parse_table_time(text: str, now_ts: float, offset_hours: int = 0) -> float:
    raw = (text or "").strip()
    # 先看有没有写明日期的格式（2026-11-17 14:50），有就按它算，别当成今天。
    match = re.search(r"(\d{4}-\d{2}-\d{2})[ T]?(\d{1,2}:\d{2})?", raw)
    if match:
        try:
            base = datetime.strptime(match.group(1), "%Y-%m-%d")
            if match.group(2):
                hour, minute = (int(part) for part in match.group(2).split(":"))
                base = base.replace(hour=hour, minute=minute)
            return base.replace(tzinfo=timezone.utc).timestamp() - offset_hours * 3600
        except ValueError:
            return 0.0
    # 只有 HH:MM 的，按站点当地时间推当天（早于现在就没问题，晚于现在多半是昨天）。
    match = re.search(r"(\d{1,2}):(\d{2})", raw)
    if match:
        wall = datetime.fromtimestamp(now_ts + offset_hours * 3600, tz=timezone.utc)
        hour, minute = int(match.group(1)), int(match.group(2))
        candidate = wall.replace(hour=hour, minute=minute, second=0, microsecond=0)
        stamp = candidate.timestamp() - offset_hours * 3600
        if stamp > now_ts + 3600:
            stamp -= 86400
        return stamp
    return 0.0


def _direction_from_type(text: str) -> str:
    low = (text or "").strip()
    if not low:
        return ""
    if "进所" in low or "转入交易所" in low or "inflow" in low.lower() or "转入" in low:
        return rules.DIR_IN
    if "出所" in low or "转出交易所" in low or "outflow" in low.lower() or "转出" in low:
        return rules.DIR_OUT
    if "跨所" in low or "所间" in low or "调仓" in low:
        return rules.DIR_CEX2CEX
    return ""


def fetch_flashai(window_hours: float = 6.0, now: Optional[float] = None) -> SourceReport:
    return fetch_table_source("flashai", FLASHAI_URL, window_hours, now, tz_offset_hours=8)


def fetch_coinstrooper(window_hours: float = 6.0, now: Optional[float] = None) -> SourceReport:
    return fetch_table_source("coinstrooper", COINSTROOPER_URL, window_hours, now, tz_offset_hours=0)


def fetch_ourscrypto(window_hours: float = 6.0, now: Optional[float] = None) -> SourceReport:
    return fetch_table_source("ourscrypto", OURSCRYPTO_URL, window_hours, now, tz_offset_hours=8)


# ------------------------------------------------------------- 链上直查

def kraken_symbol(raw: str) -> str:
    """把 Kraken 的交易对名字归一成常见币种名（XXBTZUSD → BTC，USDTZUSD → USDT）。"""
    text = (raw or "").upper().split("/")[0]
    for quote in ("ZUSDT", "USDT", "ZUSD", "USD"):
        if text.endswith(quote):
            text = text[: -len(quote)]
            break
    text = text.replace("XBT", "BTC")
    if len(text) > 3 and text[0] in ("X", "Z"):
        text = text[1:]
    if text in ("DG", "XDG"):
        return "DOGE"
    return text


def fetch_prices() -> Tuple[Dict[str, float], str]:
    """行情：优先 Binance 公开行情，失败换 Kraken。只用来给缺数量的源补数量与估值。"""
    for source_name, url in chains.PRICE_APIS:
        result = http.fetch_json(url, timeout=15, retries=1, max_bytes=1_000_000)
        if not result.ok:
            continue
        payload = result.json()
        prices: Dict[str, float] = {}
        if source_name == "binance":
            for row in payload or []:
                symbol = str(row.get("symbol", "")).upper()
                try:
                    prices[symbol] = float(row.get("price"))
                except (TypeError, ValueError):
                    continue
        else:
            result_block = (payload or {}).get("result") or {}
            for key, value in result_block.items():
                symbol = kraken_symbol(key)
                try:
                    prices[symbol] = float(value["c"][0])
                except (KeyError, IndexError, TypeError, ValueError):
                    continue
        if prices:
            return prices, source_name
    return {}, ""


def _price_for(symbol: str, prices: Dict[str, float]) -> float:
    key = (symbol or "").upper()
    for alias in chains.PRICE_ALIASES.get(key, [key]):
        if alias in prices:
            return float(prices[alias])
    return 0.0


def _blockscout_value(item: dict) -> Tuple[float, float]:
    """返回 (数量, 每枚美元价)。稳定币用接口自带的 exchange_rate。"""
    total = item.get("total") or {}
    raw = total.get("value")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = 0.0
    try:
        decimals = int(total.get("decimals") or 18)
    except (TypeError, ValueError):
        decimals = 18
    amount = value / (10 ** decimals)
    token = item.get("token") or {}
    try:
        rate = float(token.get("exchange_rate"))
    except (TypeError, ValueError):
        rate = 0.0
    return amount, rate


def fetch_blockscout(book, prices: Dict[str, float], per_round: int = 24,
                     window_hours: float = 6.0, now: Optional[float] = None) -> SourceReport:
    """Blockscout 直查链上：按代币扫全网转账流，再轮询交易所地址看进出。"""
    name = "blockscout"
    now_ts = now or time.time()
    cutoff = now_ts - window_hours * 3600
    items: List[rules.Transfer] = []
    errors: List[str] = []
    scanned = 0

    for symbol, address in chains.ETH_TOKENS.items():
        url = chains.token_feed_url("ethereum", address)
        result = http.fetch_json(url, timeout=25, retries=1, max_bytes=4_000_000)
        if not result.ok:
            errors.append(f"{symbol}:{result.error_type}")
            continue
        scanned += 1
        for entry in (result.json() or {}).get("items", []):
            ts = _iso_to_ts(entry.get("timestamp", ""))
            if not ts or ts < cutoff:
                continue
            amount, rate = _blockscout_value(entry)
            if amount <= 0:
                continue
            price = rate or _price_for(symbol, prices)
            usd = amount * price
            from_entity = entry.get("from") or {}
            to_entity = entry.get("to") or {}
            from_label = _clean_label(str(from_entity.get("name") or "")) or book.lookup(str(from_entity.get("hash") or ""))
            to_label = _clean_label(str(to_entity.get("name") or "")) or book.lookup(str(to_entity.get("hash") or ""))
            tx_hash = str(entry.get("transaction_hash") or "")
            items.append(rules.Transfer(
                source=name, chain="ethereum", symbol=symbol, amount=amount, usd=usd,
                from_label=from_label, to_label=to_label, ts=ts,
                url=chains.explorer_tx_url("ethereum", tx_hash), tx_hash=tx_hash,
                raw_from=str(from_entity.get("hash") or ""), raw_to=str(to_entity.get("hash") or ""),
                extra={"note": "blockscout token feed"},
            ))

    # 轮询交易所地址：cex-list 里的以太坊地址，按轮次轮转，覆盖所有所的进出。
    addresses = sorted(book.cex.keys())
    if addresses:
        start = (int(now_ts // 300) * per_round) % len(addresses)
        for offset in range(min(per_round, len(addresses))):
            address = addresses[(start + offset) % len(addresses)]
            url = chains.address_transfers_url("ethereum", address)
            result = http.fetch_json(url, timeout=25, retries=1, max_bytes=6_000_000)
            if not result.ok:
                errors.append(f"addr:{result.error_type}")
                continue
            scanned += 1
            for entry in (result.json() or {}).get("items", []):
                if entry.get("type") not in (None, "token_transfer"):
                    continue
                ts = _iso_to_ts(entry.get("timestamp", ""))
                if not ts or ts < cutoff:
                    continue
                amount, rate = _blockscout_value(entry)
                if amount <= 0:
                    continue
                token = entry.get("token") or {}
                symbol = str(token.get("symbol") or "").upper()
                if not symbol:
                    continue
                price = rate or _price_for(symbol, prices)
                from_entity = entry.get("from") or {}
                to_entity = entry.get("to") or {}
                from_hash = str(from_entity.get("hash") or "")
                to_hash = str(to_entity.get("hash") or "")
                if from_hash.lower() == address:
                    counterparty, inbound = to_entity, False
                else:
                    counterparty, inbound = from_entity, True
                counter_hash = str(counterparty.get("hash") or "")
                counter_label = book.lookup(counter_hash)
                if not counter_label:
                    counter_label = _clean_label(str(counterparty.get("name") or ""))
                exchange_label = book.cex.get(address, "")
                tx_hash = str(entry.get("transaction_hash") or "")
                items.append(rules.Transfer(
                    source=name, chain="ethereum", symbol=symbol, amount=amount,
                    usd=amount * price,
                    from_label=exchange_label if not inbound else counter_label,
                    to_label=counter_label if inbound else exchange_label,
                    ts=ts, url=chains.explorer_tx_url("ethereum", tx_hash), tx_hash=tx_hash,
                    raw_from=from_hash, raw_to=to_hash,
                    direction_hint=rules.DIR_OUT if inbound else rules.DIR_IN,
                    extra={"note": "blockscout address scan", "exchange_address": address},
                ))
    status = "ok" if scanned else "provider_error"
    if scanned and errors:
        status = "ok" if len(errors) < scanned else "provider_error"
    return SourceReport(name=name, layer="chain", ok=bool(scanned), status=status, items=items,
                        detail={"scanned": scanned, "errors": errors[:6], "addresses": len(book.cex)})
