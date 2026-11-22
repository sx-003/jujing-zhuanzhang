"""链与代币配置：Blockscout 公开实例（免 key）、主网代币合约、行情源。

Blockscout v2 实测可直读的链：以太坊、Optimism、Celo、zkSync、Scroll。
Base / Arbitrum / Gnosis / Polygon 的公开实例 403/404，先记着不用。
"""
from __future__ import annotations

from typing import Dict, List

CHAINS: Dict[str, dict] = {
    "ethereum": {
        "name": "以太坊",
        "blockscout": "https://eth.blockscout.com",
        "explorer": "https://etherscan.io",
    },
    "optimism": {
        "name": "Optimism",
        "blockscout": "https://optimism.blockscout.com",
        "explorer": "https://optimistic.etherscan.io",
    },
    "celo": {
        "name": "Celo",
        "blockscout": "https://celo.blockscout.com",
        "explorer": "https://celoscan.io",
    },
    "zksync": {
        "name": "zkSync",
        "blockscout": "https://zksync.blockscout.com",
        "explorer": "https://explorer.zksync.io",
    },
    "scroll": {
        "name": "Scroll",
        "blockscout": "https://scroll.blockscout.com",
        "explorer": "https://scrollscan.com",
    },
}

# 以太坊主网重点代币合约地址：Blockscout 按代币给全网转账流，用来扫大额。
ETH_TOKENS: Dict[str, str] = {
    "USDT": "0xdac17f958d2ee523a2206206994597c13d831ec7",
    "USDC": "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",
    "WBTC": "0x2260fac5e5542a773aa44fbcfedf7c193bc2c599",
    "WETH": "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",
}

# 行情源：优先 Binance 公开行情，失败换 Kraken，再失败换 DefiLlama（按合约地址）。
PRICE_APIS = [
    ("binance", "https://api.binance.com/api/v3/ticker/price?symbols=%5B%22BTCUSDT%22%2C%22ETHUSDT%22%2C"
                "%22USDCUSDT%22%2C%22USDTUSDT%22%2C%22DAIUSDT%22%2C%22FDUSDUSDT%22%2C%22TUSDUSDT%22%2C"
                "%22USDEUSDT%22%2C%22PYUSDUSDT%22%5D"),
    ("kraken", "https://api.kraken.com/0/public/Ticker?pair=XBTUSD,ETHUSD,USDTUSD,USDCUSD,DAIUSD,USDEUSD"),
]
LLAMA_PRICE = "https://coins.llama.fi/prices/current/ethereum:{address}"

# 价格换算用的符号别名（行情站返回的名字不完全一致）。
PRICE_ALIASES = {
    "BTC": ["BTC", "XBT", "BTCUSDT", "XBTUSDT"],
    "ETH": ["ETH", "ETHUSDT", "ETHUSD"],
    "USDT": ["USDT", "USDTUSDT", "USDTUSD"],
    "USDC": ["USDC", "USDCUSDT", "USDCUSD"],
    "DAI": ["DAI", "DAIUSDT", "DAIUSD"],
    "FDUSD": ["FDUSD", "FDUSDUSDT"],
    "TUSD": ["TUSD", "TUSDUSDT"],
    "USDE": ["USDE", "USDEUSDT"],
    "PYUSD": ["PYUSD", "PYUSDUSDT"],
    "WETH": ["WETH", "ETH"],
    "WBTC": ["WBTC", "BTC"],
}


def blockscout_base(chain: str) -> str:
    return CHAINS.get(chain, {}).get("blockscout", "")


def explorer_tx_url(chain: str, tx_hash: str) -> str:
    base = CHAINS.get(chain, {}).get("explorer", "")
    if not base or not tx_hash:
        return ""
    return f"{base.rstrip('/')}/tx/{tx_hash}"


def token_feed_url(chain: str, token_address: str) -> str:
    base = blockscout_base(chain)
    if not base:
        return ""
    return f"{base.rstrip('/')}/api/v2/tokens/{token_address}/transfers"


def address_transfers_url(chain: str, address: str) -> str:
    base = blockscout_base(chain)
    if not base:
        return ""
    return f"{base.rstrip('/')}/api/v2/addresses/{address}/token-transfers"


def chain_list() -> List[str]:
    return list(CHAINS.keys())
