"""分类、阈值、去重、合并与推送文案。

只报链上公开转账事实：金额、数量、从哪到哪、时间、原文链接。
不做行情判断、不给点位、不评价该不该进场。
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

CST = timezone(timedelta(hours=8))

# 阈值：BTC/ETH/USDT/USDC 及主流稳定币 1000 万美元，其他代币 2000 万美元。
MAJOR_SYMBOLS = {
    "BTC", "ETH", "WBTC", "WETH", "USDT", "USDC", "DAI", "FDUSD", "TUSD", "USDE", "PYUSD",
    "USD1", "BUSD", "FRAX", "LUSD", "GUSD", "SUSDE", "EURC", "EURS", "USDS", "USDD", "USDF",
    "XUSD", "RLUSD", "USDP", "USDG", "FEI", "SUSD",
}
THRESHOLD_MAJOR = 10_000_000
THRESHOLD_OTHER = 20_000_000
URGENT_USD = 50_000_000

# 交易所名与「钱包型」标签：命中即视为交易所地址。
EXCHANGE_NAMES = [
    "Binance", "BNB", "OKX", "OKEx", "Coinbase", "Kraken", "Bitfinex", "Bybit", "Gate.io",
    "Gate", "MEXC", "KuCoin", "HTX", "Huobi", "Bitget", "Deribit", "Gemini", "Crypto.com",
    "Bithumb", "Poloniex", "Upbit", "bitstamp", "Hyperliquid", "Coincheck", "LBank",
    "BingX", "BitMart", "AscendEX", "ProBit", "WazirX", "KickEX", "XT.com", "Stablr",
    "Bullish", "Bitvavo", "CryptoCompare", "Mercado Bitcoin", "Bitcoin.com", "Payoneer",
    "BitPay", "Binance.US", "Coinbase Prime", "Binance Prime", "MEXC Global", "Coinone",
    "Bitrue", "Toobit", "Gate.io Hot Wallet", "Deepcoin", "EasiCoin", "Hotcoin", "WEEX",
    "MGBX", "YUBIT", "Bitbaby", "BitTap", "MEXC Hot Wallet",
]
EXCHANGE_MARKERS = (
    "hot wallet", "cold wallet", "prime custody", "prime", "exchange", "deposit address",
    "withdrawal address", "hotwallet", "coldwallet", "hot-wallet", "cold-wallet",
    "交易所", "充币", "提币", "热钱包", "冷钱包",
)
EXCHANGE_ALIASES = {
    "binance.com": "Binance", "binance": "Binance", "币安": "Binance",
    "okx.com": "OKX", "欧易": "OKX", "okex": "OKX",
    "coinbase.com": "Coinbase", "kraken.com": "Kraken", "bitfinex.com": "Bitfinex",
    "bybit.com": "Bybit", "gate.io": "Gate.io", "mexc.com": "MEXC",
    "kucoin.com": "KuCoin", "htx.com": "HTX", "huobi.com": "Huobi",
    "bitget.com": "Bitget", "deribit.com": "Deribit", "gemini.com": "Gemini",
    "crypto.com": "Crypto.com", "bithumb.com": "Bithumb", "poloniex.com": "Poloniex",
    "upbit.com": "Upbit", "bitstamp.com": "bitstamp", "hyperliquid.xyz": "Hyperliquid",
}

# 机构与做市商：不是交易所，不进方向判定（不会被标成「转入/转出交易所」），
# 但算「认得出来」，显示真实名字，不会当成未知钱包被过滤。
INSTITUTIONS: List[Tuple[str, str]] = [
    ("coinbase institutional", "Coinbase Institutional"),
    ("coinbase prime", "Coinbase Prime"),
    ("cumberland drw", "Cumberland DRW"),
    ("cumberland", "Cumberland DRW"),
    ("hudson river trading", "Hudson River Trading"),
    ("susquehanna", "Susquehanna"),
    ("citadel securities", "Citadel Securities"),
    ("galaxy digital", "Galaxy Digital"),
    ("alameda research", "Alameda Research"),
    ("jump crypto", "Jump Trading"),
    ("jump trading", "Jump Trading"),
    ("wintermute", "Wintermute"),
    ("falconx", "FalconX"),
    ("blackrock", "BlackRock"),
    ("贝莱德", "BlackRock"),
    ("ceffu", "Ceffu"),
    ("alameda", "Alameda Research"),
    ("maverick", "Maverick"),
    ("citadel", "Citadel Securities"),
    ("galaxy", "Galaxy Digital"),
    ("susquehanna", "Susquehanna"),
    ("drw", "DRW"),
    ("hrt", "Hudson River Trading"),
    ("jump", "Jump Trading"),
    ("cme", "CME"),
]

# 协议金库 / 借贷 / 质押：按用户口径一律过滤，既不推也不留档。
PROTOCOL_MARKERS = (
    "aave", "compound", "morpho", "sky", "maker", "lido", "rocket pool", "rocketpool",
    "eigenlayer", "ethena", "pendle", "hypercore", "tether treasury",
    "usdc treasury", "usdt treasury", "circle", "treasury", "stake", "staked",
    "staking", "unstake", "vault", "pool", "质押", "借贷", "金库", "铸币", "销毁",
)
LOAN_STAKE_MARKERS = (
    "aave", "compound", "lido", "stake", "staking", "unstake", "borrow", "lending",
    "hypercore", "质押", "借贷", "赎回",
)
MINT_BURN_MARKERS = (
    "minted at", "minted", "burned", "burnt", "mint", "burn", "铸造", "销毁",
)

# 方向
DIR_IN = "转入交易所"
DIR_OUT = "转出交易所"
DIR_CEX2CEX = "所间转账"
DIR_W2W = "钱包互转"
DIR_UNKNOWN = "方向未知"


@dataclass
class Transfer:
    """一条链上转账事实（已归一化）。"""
    source: str
    chain: str
    symbol: str
    amount: float
    usd: float
    ts: float = 0.0
    url: str = ""
    from_label: str = ""
    to_label: str = ""
    tx_hash: str = ""
    direction_hint: str = ""
    raw_from: str = ""
    raw_to: str = ""
    extra: Dict = field(default_factory=dict)

    def key(self) -> str:
        """跨源去重键：优先交易哈希，其次链接，最后金额+时间+从到哪。"""
        if self.tx_hash:
            return "tx:" + self.tx_hash.lower().removeprefix("0x")
        if self.url:
            from urllib.parse import urlsplit
            path = urlsplit(self.url).path.rstrip("/")
            return "url:" + path.lower()
        stamp = int(self.ts // 300)
        raw = f"{self.from_label}|{self.to_label}|{self.symbol}|{round(self.usd)}|{stamp}"
        return "fp:" + hashlib.md5(raw.encode("utf-8")).hexdigest()[:16]


def is_exchange_label(label: str) -> bool:
    text = (label or "").strip()
    if not text:
        return False
    low = text.lower()
    for name in EXCHANGE_NAMES:
        if name.lower() in low:
            return True
    for marker in EXCHANGE_MARKERS:
        if marker in low:
            return True
    for alias, name in EXCHANGE_ALIASES.items():
        if alias in low:
            return True
    return False


def normalize_exchange_label(label: str) -> str:
    """把 'Binance: Hot Wallet (binance.com)' 这类标签收敛成 'Binance'。"""
    text = (label or "").strip()
    if not text:
        return ""
    low = text.lower()
    for alias, name in EXCHANGE_ALIASES.items():
        if low.startswith(alias):
            return name
    for name in EXCHANGE_NAMES:
        if name.lower() in low:
            return name
    if ":" in text:
        head = text.split(":", 1)[0].strip()
        if head and len(head) <= 28:
            return head
    return text


def institution_name(label: str) -> str:
    """机构/做市商标签归一成真实名字（如 Jump Crypto → Jump Trading）。"""
    text = (label or "").strip().lower()
    if not text:
        return ""
    for pattern, canonical in INSTITUTIONS:
        if re.search(r"(?<![a-z0-9])" + re.escape(pattern) + r"(?![a-z0-9])", text):
            return canonical
    return ""


def is_institution_label(label: str) -> bool:
    return bool(institution_name(label))


def is_known_label(label: str) -> bool:
    """认得出来 = 交易所或机构/做市商；两头都不认得的不推。"""
    return is_exchange_label(label) or is_institution_label(label)


def normalize_label(label: str) -> str:
    """对外显示的标签：先机构（更具体，如 Coinbase Prime），再交易所，否则保留原文。"""
    text = (label or "").strip()
    if not text:
        return ""
    canonical = institution_name(text)
    if canonical:
        return canonical
    if is_exchange_label(text):
        return normalize_exchange_label(text)
    return text


def is_protocol_label(label: str) -> bool:
    low = (label or "").lower()
    return any(marker in low for marker in PROTOCOL_MARKERS)


def is_loan_or_stake(label: str, extra_text: str = "") -> bool:
    low = f"{(label or '')} {extra_text}".lower()
    return any(marker in low for marker in LOAN_STAKE_MARKERS)


def is_mint_burn(text: str, direction_code: str = "") -> bool:
    if (direction_code or "").lower() in ("mint", "minting", "burn", "burning"):
        return True
    low = (text or "").lower()
    return any(marker in low for marker in MINT_BURN_MARKERS)


def _is_unknown(label: str) -> bool:
    text = (label or "").strip().lower()
    return text in ("", "unknown wallet", "unknown", "unknown address", "未知钱包", "未知地址")


def is_unlabeled(transfer: Transfer) -> bool:
    """两头都认不出来的转账：不推。

    认得出来 = 源自带交易所/机构标签、Blockscout 标签、cex-list 或 wallet-attribution
    能反查出名字（含做市商与机构）。只给钱不说是谁，金额再大对用户也没价。
    """
    return not is_known_label(transfer.from_label) and not is_known_label(transfer.to_label)


def classify(transfer: Transfer) -> Tuple[str, bool]:
    """返回 (方向, 是否过滤掉)。过滤 = 铸销/借贷质押这类不推不留档。"""
    from_ex = is_exchange_label(transfer.from_label)
    to_ex = is_exchange_label(transfer.to_label)
    if from_ex and to_ex:
        direction = DIR_CEX2CEX
    elif from_ex:
        direction = DIR_OUT
    elif to_ex:
        direction = DIR_IN
    else:
        direction = DIR_W2W

    for label in (transfer.from_label, transfer.to_label):
        if is_protocol_label(label):
            return direction, True
    blob = " ".join([transfer.from_label, transfer.to_label, transfer.direction_hint,
                     transfer.extra.get("note", "")])
    if is_loan_or_stake("", blob):
        return direction, True
    if is_mint_burn(blob, transfer.extra.get("direction_code", "")):
        return direction, True
    return direction, False


def threshold_for(symbol: str) -> float:
    return THRESHOLD_MAJOR if (symbol or "").upper() in MAJOR_SYMBOLS else THRESHOLD_OTHER


def meets_threshold(transfer: Transfer, direction: str) -> bool:
    """达到金额门槛才算可推。低于门槛的交易所进出只留档。"""
    return transfer.usd >= threshold_for(transfer.symbol)


def format_usd(usd: float) -> str:
    value = float(usd or 0)
    if value >= 100_000_000:
        return "约 " + _trim(f"{value / 100_000_000:.4f}") + " 亿美元"
    if value >= 10_000:
        return "约 " + _trim(f"{value / 10_000:.2f}") + " 万美元"
    return "约 " + f"{value:,.0f}" + " 美元"


def _trim(text: str) -> str:
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def format_amount(amount: float, symbol: str) -> str:
    value = float(amount or 0)
    if abs(value) >= 1000:
        number = f"{value:,.0f}"
    elif abs(value) >= 1:
        number = _trim(f"{value:.4f}")
    else:
        number = _trim(f"{value:.6f}")
    return f"{number} {symbol}".strip()


def format_time(ts: float, now: Optional[float] = None) -> str:
    moment = datetime.fromtimestamp(float(ts or 0), tz=timezone.utc)
    local = moment.astimezone(CST)
    today = datetime.now(CST).date()
    if local.date() == today:
        return "约 " + local.strftime("%H:%M")
    return "约 " + local.strftime("%m-%d %H:%M")


def merge_group_key(transfer: Transfer, direction: str) -> str:
    """同一主体短时间内多笔合并：按主体+代币+方向+30 分钟桶聚合。

    标签为空时用原始地址（哪怕是截断地址）区分主体，避免不同地址被并成一条。
    """
    bucket = int(transfer.ts // 1800)
    left = transfer.from_label or transfer.raw_from or ""
    right = transfer.to_label or transfer.raw_to or ""
    return f"{left}|{right}|{transfer.symbol}|{direction}|{bucket}"


def merge_note(items: List[Transfer]) -> str:
    """合并后的笔数说明：等额写「N 笔各 X」，不等写「N 笔」。"""
    count = len(items)
    if count <= 1:
        return ""
    amounts = [round(item.amount, 8) for item in items]
    symbol = items[0].symbol
    if len(set(amounts)) == 1:
        return f"（{count} 笔各 {format_amount(amounts[0], symbol)}）"
    return f"（{count} 笔）"


def format_message(*, direction: str, items: List[Transfer], usd: float) -> str:
    """一条一行的推送文案：金额｜数量｜从哪 → 到哪｜时间。

    方向由分组标题表达，逐条不再写方向标签；加急标记保留。
    推送里不带任何网址（链接只进 state 分支的归档，方便以后查证）。
    """
    head = items[0]
    quantity = format_amount(sum(item.amount for item in items), head.symbol)
    quantity += merge_note(items)
    source = head.from_label or "未知钱包"
    target = head.to_label or "未知钱包"
    # 方向已由分组标题说明，逐条不再重复写【转入交易所】这类标签；只保留加急标记。
    prefix = "【加急】" if usd >= URGENT_USD else ""
    pieces = [
        format_usd(usd),
        quantity,
        f"{source} → {target}",
        format_time(head.ts),
    ]
    return prefix + "｜".join(pieces)


def dedup_fingerprint(transfer: Transfer) -> str:
    return transfer.key()
