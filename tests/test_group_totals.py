import unittest

from monitor import rules, run


def _transfer(usd: float, ts: float, symbol: str = "USDT") -> rules.Transfer:
    return rules.Transfer(
        source="fixture", chain="eth", symbol=symbol,
        amount=usd, usd=usd, ts=ts,
        from_label="未知钱包", to_label="Coinbase",
    )


class TestGroupTotals(unittest.TestCase):
    """每个分组标题后面要带该组金额合计（2026-11-24 用户要求）。"""

    def _build(self):
        first = run.Candidate(direction=rules.DIR_IN,
                              items=[_transfer(150_000_000, 100)], usd=150_000_000)
        second = run.Candidate(direction=rules.DIR_IN,
                               items=[_transfer(23_500_000, 200)], usd=23_500_000)
        third = run.Candidate(direction=rules.DIR_OUT,
                              items=[_transfer(296_000_000, 300)], usd=296_000_000)
        return run.build_round_text([first, second, third])

    def test_group_header_has_total(self):
        text = self._build()
        self.assertIn("转入交易所（2 条）· 共计约 1.74 亿美元", text)
        self.assertIn("转出交易所（1 条）· 共计约 2.96 亿美元", text)

    def test_round_title_has_no_total(self):
        # 用户 2026-11-25 要求：顶部不显示合计，只在分组标题里显示。
        first = self._build().splitlines()[0]
        self.assertIn("本轮 3 条", first)
        self.assertNotIn("共计", first)

    def test_empty_round_returns_empty(self):
        self.assertEqual(run.build_round_text([]), "")


if __name__ == "__main__":
    unittest.main()
