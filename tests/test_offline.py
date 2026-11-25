"""离线自检：不联网、不需要任何 token，也不触碰真实 state。"""
from __future__ import annotations

import json
import os
os.environ.setdefault("TELEGRAM_RETRY_DELAYS", "0")  # 单测不等待重试
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from monitor import http, rules, sources  # noqa: E402
from monitor import cloud  # noqa: E402
from monitor import labels, state as state_mod  # noqa: E402
from monitor import run as monitor_run  # noqa: E402


class TestRules(unittest.TestCase):
    def test_thresholds(self):
        self.assertEqual(rules.threshold_for("BTC"), 10_000_000)
        self.assertEqual(rules.threshold_for("USDT"), 10_000_000)
        self.assertEqual(rules.threshold_for("PEPE"), 20_000_000)

    def test_exchange_label(self):
        self.assertTrue(rules.is_exchange_label("Binance: Hot Wallet"))
        self.assertTrue(rules.is_exchange_label("Coinbase Prime Custody"))
        self.assertFalse(rules.is_exchange_label("未知钱包"))
        self.assertFalse(rules.is_exchange_label("0x3f5CE5FBFe3E9af3971dD833D26bA9b5C936f0bE"))

    def test_direction_three_kinds(self):
        base = dict(source="t", chain="ethereum", symbol="BTC", amount=1, usd=1, ts=0, url="https://x/1")
        inflow = rules.Transfer(from_label="未知钱包", to_label="Kraken", **base)
        outflow = rules.Transfer(from_label="Kraken", to_label="未知钱包", **base)
        between = rules.Transfer(from_label="Binance", to_label="Kraken", **base)
        self.assertEqual(rules.classify(inflow)[0], rules.DIR_IN)
        self.assertEqual(rules.classify(outflow)[0], rules.DIR_OUT)
        self.assertEqual(rules.classify(between)[0], rules.DIR_CEX2CEX)

    def test_mint_and_loan_filtered(self):
        base = dict(source="t", chain="ethereum", symbol="USDC", amount=1, usd=1, ts=0, url="https://x/1")
        self.assertTrue(rules.classify(rules.Transfer(from_label="USDC Treasury", to_label="未知钱包", **base))[1])
        self.assertTrue(rules.classify(rules.Transfer(from_label="未知钱包", to_label="Aave", **base))[1])
        self.assertTrue(rules.classify(rules.Transfer(from_label="未知钱包", to_label="Tether Treasury", **base))[1])

    def test_market_maker_and_institution_labels(self):
        self.assertEqual(rules.normalize_label("Wintermute"), "Wintermute")
        self.assertEqual(rules.normalize_label("Jump Crypto"), "Jump Trading")
        self.assertEqual(rules.normalize_label("Ceffu"), "Ceffu")
        self.assertEqual(rules.normalize_label("贝莱德"), "BlackRock")
        self.assertEqual(rules.normalize_label("Coinbase Prime"), "Coinbase Prime")
        self.assertEqual(rules.normalize_label("Cumberland DRW"), "Cumberland DRW")
        self.assertTrue(rules.is_known_label("Wintermute"))
        self.assertTrue(rules.is_known_label("FalconX"))
        # 做市商不是交易所，不能因此把方向标成转入/转出交易所
        self.assertFalse(rules.is_exchange_label("Wintermute"))
        mm = rules.Transfer(source="t", chain="ethereum", symbol="ETH", amount=20000,
                            usd=36_000_000, from_label="未知钱包", to_label="Wintermute",
                            ts=0, url="https://x/7")
        self.assertFalse(rules.is_unlabeled(mm))
        self.assertEqual(rules.classify(mm)[0], rules.DIR_W2W)

    def test_unlabeled_transfer_not_pushed(self):
        blank = rules.Transfer(source="t", chain="bitcoin", symbol="BTC", amount=5000,
                               usd=320_000_000, from_label="未知钱包", to_label="未知钱包",
                               ts=0, url="https://x/9")
        self.assertTrue(rules.is_unlabeled(blank))
        one_side = rules.Transfer(source="t", chain="bitcoin", symbol="BTC", amount=5000,
                                  usd=320_000_000, from_label="未知钱包", to_label="Kraken",
                                  ts=0, url="https://x/9")
        self.assertFalse(rules.is_unlabeled(one_side))
        agency = rules.Transfer(source="t", chain="bitcoin", symbol="BTC", amount=5000,
                                usd=320_000_000, from_label="BlackRock", to_label="未知钱包",
                                ts=0, url="https://x/9")
        self.assertFalse(rules.is_unlabeled(agency))

    def test_message_format(self):
        item = rules.Transfer(source="t", chain="bitcoin", symbol="BTC", amount=5320, usd=483_000_000,
                              from_label="BlackRock", to_label="Coinbase", ts=1763316360,
                              url="https://whale-alert.io/transaction/bitcoin/abc")
        text = rules.format_message(direction=rules.DIR_IN, items=[item], usd=483_000_000)
        self.assertIn("亿美元", text)
        self.assertIn("5,320 BTC", text)
        self.assertIn("BlackRock → Coinbase", text)
        self.assertTrue(text.startswith("【加急】"))
        self.assertEqual(text.count("\n"), 0)

    def test_merge_note(self):
        items = [rules.Transfer(source="t", chain="c", symbol="BTC", amount=693, usd=1, ts=0, url="https://x/1")
                 for _ in range(3)]
        self.assertIn("3 笔各 693 BTC", rules.merge_note(items))
        items[2].amount = 700
        self.assertIn("（3 笔）", rules.merge_note(items))

    def test_amount_format(self):
        self.assertIn("亿美元", rules.format_usd(483_000_000))
        self.assertIn("万美元", rules.format_usd(14_378_000))
        self.assertIn("美元", rules.format_usd(1234))


class TestHttp(unittest.TestCase):
    def test_ssrf_rejects_private_host(self):
        with self.assertRaises(http.FetchFailure):
            http.validate_url("http://127.0.0.1:8080/admin")

    def test_retry_after_seconds_and_date(self):
        import time as time_mod
        self.assertEqual(http.parse_retry_after("30"), 30.0)
        self.assertEqual(http.parse_retry_after("0"), 0.0)
        self.assertEqual(http.parse_retry_after(""), 0.0)
        self.assertEqual(http.parse_retry_after("garbage"), 0.0)
        now = time_mod.time()
        future = time_mod.strftime("%a, %d %b %Y %H:%M:%S GMT", time_mod.gmtime(now + 60))
        self.assertTrue(55 <= http.parse_retry_after(future, now=now) <= 61)
        past = time_mod.strftime("%a, %d %b %Y %H:%M:%S GMT", time_mod.gmtime(now - 60))
        self.assertEqual(http.parse_retry_after(past, now=now), 0.0)

    def test_self_chain_guards(self):
        self.assertTrue(monitor_run.should_chain("schedule", 0, "true"))
        self.assertTrue(monitor_run.should_chain("workflow_dispatch", 1, "true"))
        self.assertFalse(monitor_run.should_chain("push", 0, "true"))
        self.assertFalse(monitor_run.should_chain("schedule", 2, "true"))
        self.assertFalse(monitor_run.should_chain("workflow_dispatch", 5, "true"))
        for off in ("", "false", "0"):
            self.assertFalse(monitor_run.should_chain("schedule", 0, off))

    def test_workflow_has_self_chain_settings(self):
        workflow = (ROOT / ".github/workflows/monitor.yml").read_text(encoding="utf-8")
        self.assertIn("actions: write", workflow)
        self.assertIn("vars.SELF_CHAIN == 'true'", workflow)
        self.assertIn("github.event_name == 'schedule' || github.event_name == 'workflow_dispatch'", workflow)
        self.assertIn("steps.collect.outputs.exit_code <= 1", workflow)
        self.assertIn("actions/workflows/monitor.yml/dispatches", workflow)
        self.assertIn("cancel-in-progress: ${{ github.event_name == 'push' }}", workflow)
        self.assertIn('echo "exit_code=$code" >> "$GITHUB_OUTPUT"', workflow)

    def test_project_user_agent(self):
        self.assertIn("jujing-zhuanzhang", http.PROJECT_UA)
        self.assertEqual(http.HEADERS["User-Agent"], http.PROJECT_UA)

    def test_charset_detection(self):
        result = http.FetchResult("https://x", "<meta charset='gb18030'>".encode("gb18030"),
                                   200, content_type="text/html")
        self.assertEqual(result.charset(), "gb18030")
        self.assertIn("巨鲸", result.text() + "巨鲸")


class TestSources(unittest.TestCase):
    def test_usd_parsing(self):
        self.assertEqual(sources._usd_from_text("$4,341.21万USD"), 43_412_100)
        self.assertEqual(sources._usd_from_text("$41M"), 41_000_000)
        self.assertEqual(sources._usd_from_text("1.2亿美元"), 120_000_000)

    def test_telegram_mirror_fallback(self):
        class _R:
            def __init__(self, ok, url, error_type=""):
                self.ok, self.url, self.error_type, self.error = ok, url, error_type, "stub"

        calls = []

        def fake_fetch(url, **kwargs):
            calls.append(url)
            return _R(False, url, "provider_error") if "telegram.me" in url else _R(True, url)

        result, used, tried = sources.fetch_telegram_channel("demo", fetcher=fake_fetch)
        self.assertTrue(result.ok)
        self.assertIn("telegram.dog", used)
        self.assertEqual(len(tried), 1)
        self.assertEqual(calls[0].split("/")[2], "telegram.me")

    def test_telegram_mirror_all_down(self):
        class _R:
            def __init__(self, url):
                self.ok, self.url, self.error_type, self.error = False, url, "blocked", "stub"

        tried = []
        result, used, failures = sources.fetch_telegram_channel(
            "demo", fetcher=lambda url, **kw: (tried.append(url), _R(url))[1])
        self.assertFalse(result.ok)
        self.assertEqual(used, "")
        self.assertEqual(len(failures), 3)

    def test_kraken_pair_names(self):
        self.assertEqual(sources.kraken_symbol("XXBTZUSD"), "BTC")
        self.assertEqual(sources.kraken_symbol("XETHZUSD"), "ETH")
        self.assertEqual(sources.kraken_symbol("USDTZUSD"), "USDT")
        self.assertEqual(sources.kraken_symbol("XDGUSD"), "DOGE")

    def test_table_header_mapping(self):
        mapping = sources._map_header(["资产 / 时间", "转移金额", "数量", "转出方 → 转入方", "动作", "网络", "详情"])
        self.assertIn("usd", mapping)
        self.assertIn("amount", mapping)
        self.assertIn("chain", mapping)

    def test_telegram_parser(self):
        page = ('<div class="tgme_widget_message_wrap js-widget_message_wrap">'
                '<div class="tgme_widget_message_text js-message_text" dir="auto">'
                '<b>EXCHANGE INFLOW</b><br/><b>$41M </b><a href="?q=%23BTC"><b>#BTC</b></a><br/>'
                '<i>Unknown</i> ➔ <i>Coinbase</i><br/>'
                '<a href="https://trackwhale.io/transaction/' + "a" * 64 + '">View Transaction</a></div>'
                '<div class="tgme_widget_message_footer compact js-message_footer">'
                '<span class="tgme_widget_message_info short js-message_info">'
                '<time datetime="2026-11-16T17:35:52+00:00" class="time">17:35</time></span></div></div>')
        messages = sources._telegram_messages(page)
        self.assertEqual(len(messages), 1)
        self.assertIn("#BTC", messages[0]["text"])
        self.assertEqual(messages[0]["time"], "2026-11-16T17:35:52+00:00")
        self.assertTrue(any("trackwhale.io/transaction/" in link for link in messages[0]["links"]))

    def test_tx_hash_normalized_across_sources(self):
        digest = "a" * 64
        one = rules.Transfer(source="a", chain="ethereum", symbol="BTC", amount=1, usd=1,
                             ts=0, url="https://a/tx/0x" + digest, tx_hash="0x" + digest)
        two = rules.Transfer(source="b", chain="ethereum", symbol="BTC", amount=1, usd=1,
                             ts=0, url="https://b/transaction/" + digest, tx_hash=digest)
        self.assertEqual(one.key(), two.key())

    def test_table_time_offset(self):
        import time as time_mod
        from datetime import datetime, timedelta, timezone as tz
        now = time_mod.time()
        expected = datetime(2026, 11, 17, 14, 50, tzinfo=tz.utc).timestamp()
        stamp = sources._parse_table_time("2026-11-17 14:50", now, 0)
        self.assertAlmostEqual(stamp, expected, delta=2)
        shifted = sources._parse_table_time("22:50", now, 8)
        local = datetime.fromtimestamp(shifted, tz=tz.utc).astimezone(tz(timedelta(hours=8)))
        self.assertEqual((local.hour, local.minute), (22, 50))

    def test_generic_table_source(self):
        html = ("<table><tr><th>Time (UTC)</th><th>Chain</th><th>Type</th><th>Asset</th>"
                "<th>Amount</th><th>From</th><th>To</th><th>Tx</th></tr>"
                "<tr><td>2026-11-17 14:50</td><td>ETH</td><td>Exchange Inflow</td><td>USDC</td>"
                "<td>$9.0M</td><td>0x8ca6…c5e8</td><td>Circle USDC</td>"
                "<td><a href=\"https://etherscan.io/tx/0xabc\">View</a></td></tr></table>")
        rows = sources._rows_from_tables(html)
        mapping = sources._map_header([cell["text"] for cell in rows[0]])
        self.assertEqual(mapping["amount"], 4)
        self.assertEqual(mapping["link"], 7)


class TestState(unittest.TestCase):
    def test_seen_and_prune(self):
        with tempfile.TemporaryDirectory() as tmp:
            st = state_mod.State(os.path.join(tmp, "state.json"))
            self.assertFalse(st.already_seen("tx:abc"))
            st.mark_pushed("tx:abc", {"usd": 1})
            self.assertTrue(st.already_seen("tx:abc"))

    def test_corrupt_state_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text("{broken", encoding="utf-8")
            with self.assertRaises(RuntimeError):
                state_mod.State(str(path)).load()

    def test_archive_by_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["STATE_ARCHIVE_DIR"] = os.path.join(tmp, "archive")
            st = state_mod.State(os.path.join(tmp, "state.json"))
            path = st.archive([{"usd": 1}], day="2026-11-17", base_dir=os.path.join(tmp, "archive"))
            self.assertTrue(Path(path).exists())


class TestLabels(unittest.TestCase):
    def test_attribution_subset(self):
        csv_text = ("address,network,entity,label,category,source,source_url,confidence,last_updated\n"
                    "0xabc,ethereum,Binance,binance-deposit,exchange,x,x,high,2026-10-01\n"
                    "0xdef,ethereum,Uniswap,Uniswap Bridge,bridge,x,x,high,2026-10-01\n")
        subset = labels.build_attribution_subset(csv_text)
        self.assertIn("0xabc", subset)
        self.assertNotIn("0xdef", subset)

    def test_blockscout_tags(self):
        entity = {"metadata": {"tags": [{"name": "Binance: Hot Wallet", "tagType": "name"},
                                        {"name": "HOT WALLET", "tagType": "generic"}]}}
        self.assertEqual(labels.blockscout_tags(entity), "Binance")
        self.assertEqual(labels.blockscout_tags({}), "")


class TestTelegramBatching(unittest.TestCase):
    def _candidate(self, direction=rules.DIR_IN, usd=20_000_000, url="https://x/a",
                   symbol="BTC", to_label="Binance"):
        item = rules.Transfer(source="t", chain="c", symbol=symbol, amount=1, usd=usd,
                              ts=0, url=url, from_label="未知钱包", to_label=to_label)
        return monitor_run.Candidate(direction=direction, items=[item], usd=usd,
                                     url=url, keys=[item.key()])

    def test_grouped_by_direction_order_and_titles(self):
        items = [self._candidate(rules.DIR_IN, 60_000_000, "https://x/1"),
                 self._candidate(rules.DIR_IN, 30_000_000, "https://x/2"),
                 self._candidate(rules.DIR_OUT, 40_000_000, "https://x/3", to_label="未知钱包"),
                 self._candidate(rules.DIR_CEX2CEX, 25_000_000, "https://x/4", to_label="Kraken")]
        text = monitor_run.build_round_text(items)
        body = [line for line in text.splitlines() if line.strip()]
        self.assertTrue(body[0].startswith("巨鲸转账 · 本轮 4 条"))
        self.assertNotIn("共计", body[0])
        self.assertTrue(body[1].startswith("转入交易所（2 条）"))
        self.assertIn("共计约 9000 万美元", body[1])
        self.assertEqual(text.count(monitor_run.GROUP_DIVIDER), 2)
        # 转入组排在转出组前面，所间转账放最后
        self.assertLess(text.index("转入交易所（2 条）"), text.index("转出交易所（1 条）"))
        self.assertLess(text.index("转出交易所（1 条）"), text.index("所间转账（1 条）"))

    def test_group_sorted_by_time_ascending(self):
        def cand(usd, ts, target="Binance"):
            item = rules.Transfer(source="t", chain="c", symbol="BTC", amount=1, usd=usd,
                                  ts=ts, url=f"https://x/{ts}", from_label="未知钱包",
                                  to_label=target)
            return monitor_run.Candidate(direction=rules.DIR_OUT, items=[item], usd=usd,
                                         url=item.url, keys=[item.key()])
        text = monitor_run.build_round_text([cand(30_000_000, 4_000, "未知钱包"),
                                             cand(10_000_000, 1_000, "未知钱包"),
                                             cand(20_000_000, 3_000, "未知钱包")])
        body = [line for line in text.splitlines() if line.strip()]
        self.assertTrue(body[1].startswith("转出交易所（3 条）"))
        self.assertIn("共计约 6000 万美元", body[1])
        self.assertEqual(text.count(monitor_run.GROUP_DIVIDER), 0)
        # 时间升序：ts=1000 → 3000 → 4000，与金额大小无关
        self.assertIn("1000 万美元", body[2])
        self.assertIn("2000 万美元", body[3])
        self.assertIn("3000 万美元", body[4])

    def test_same_time_sorted_by_usd_desc(self):
        def cand(usd, ts):
            item = rules.Transfer(source="t", chain="c", symbol="BTC", amount=1, usd=usd,
                                  ts=ts, url=f"https://x/{usd}", from_label="未知钱包",
                                  to_label="Kraken")
            return monitor_run.Candidate(direction=rules.DIR_CEX2CEX, items=[item], usd=usd,
                                         url=item.url, keys=[item.key()])
        text = monitor_run.build_round_text([cand(10_000_000, 500), cand(40_000_000, 500)])
        body = [line for line in text.splitlines() if line.strip()]
        self.assertIn("4000 万美元", body[2])
        self.assertIn("1000 万美元", body[3])

    def test_divider_only_between_groups(self):
        inp = self._candidate(rules.DIR_IN, 20_000_000, "https://x/1")
        out = self._candidate(rules.DIR_OUT, 10_000_000, "https://x/2", to_label="未知钱包")
        one = monitor_run.build_round_text([inp])
        self.assertEqual(one.count(monitor_run.GROUP_DIVIDER), 0)
        two = monitor_run.build_round_text([inp, out])
        self.assertEqual(two.count(monitor_run.GROUP_DIVIDER), 1)
        # 分割线在两组之间：第一组条目之后、第二组标题之前
        lines = two.splitlines()
        divider_at = lines.index(monitor_run.GROUP_DIVIDER)
        self.assertTrue(lines[divider_at + 1].startswith("转出交易所（1 条）"))
        self.assertFalse(lines[divider_at - 1].startswith("巨鲸转账 · 本轮 2 条"))
        self.assertFalse(two.rstrip().endswith(monitor_run.GROUP_DIVIDER))

    def test_empty_group_hidden(self):
        text = monitor_run.build_round_text([self._candidate(rules.DIR_OUT, 10_000_000,
                                                              "https://x/9", to_label="未知钱包")])
        self.assertNotIn("转入交易所", text)
        self.assertNotIn("所间转账", text)
        self.assertIn("转出交易所（1 条）", text)
        self.assertEqual(monitor_run.build_round_text([]), "")

    def test_payload_flags(self):
        from unittest import mock
        from monitor import telegram

        captured = []

        class FakeResponse:
            def read(self):
                return b'{"ok":true}'

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        def fake_urlopen(request, timeout=None):
            captured.append(json.loads(request.data.decode("utf-8")))
            return FakeResponse()

        with mock.patch.object(telegram.urllib.request, "urlopen", fake_urlopen):
            telegram.send_message("fake-token", "fake-chat", "测试消息")
        self.assertTrue(captured[0]["disable_web_page_preview"])

    def test_overlong_message_refused(self):
        from monitor import telegram
        with self.assertRaises(telegram.PushError):
            telegram.send_message("fake-token", "fake-chat", "长" * (telegram.MAX_CHARS + 1))


class TestCloud(unittest.TestCase):
    def test_anysearch_endpoint_is_com(self):
        self.assertEqual(cloud.ANYSEARCH_DEFAULT_BASE_URL, "https://api.anysearch.com")

    def test_anysearch_json_parsed(self):
        payload = json.dumps({"code": 0, "data": {"results": [{
            "title": "Whale moved 12,000,000 USDT to exchange",
            "url": "https://example.com/tx/1",
            "snippet": "12,000,000 USDT from cold wallet to Binance",
        }]}})
        hits = cloud._hits_from_text(payload, "whale", "anysearch")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].url, "https://example.com/tx/1")


class TestDryRun(unittest.TestCase):
    def test_fixture_dry_run_without_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ.pop("TELEGRAM_BOT_TOKEN", None)
            os.environ["STATE_ARCHIVE_DIR"] = os.path.join(tmp, "archive")
            code = monitor_run.run(
                fixtures=str(ROOT / "tests/fixtures/sample.json"),
                state_path=os.path.join(tmp, "state.json"),
                report_path=os.path.join(tmp, "report.json"),
                send=False, no_cloud=True,
            )
            self.assertEqual(code, 0)
            payload = json.loads((Path(tmp) / "report.json").read_text(encoding="utf-8"))
            self.assertTrue(payload["candidates"])
            self.assertEqual(payload["mode"], "dry-run")
            messages = [item["message"] for item in payload["candidates"]]
            # 方向只在分组标题里出现，逐条不再背方向标签
            self.assertFalse(any("转入交易所" in message or "转出交易所" in message
                                 or "所间转账" in message for message in messages))
            self.assertFalse(any("USDC Treasury" in message for message in messages))
            # 两头都是未知钱包的大额样本不推
            self.assertFalse(any("未知钱包 → 未知钱包" in message for message in messages))
            self.assertTrue(any("Wintermute" in message for message in messages))
            # 推送消息里不能出现任何网址；归档记录里链接仍在
            self.assertFalse(any("http" in message for message in messages))
            self.assertFalse(any("原文" in message for message in messages))
            self.assertTrue(all(item.get("url", "").startswith("https://")
                                for item in payload["candidates"]))
            # Whale Alert 照常推送，同时进归档
            self.assertTrue(any("77,700,000 USDC" in message for message in messages))
            archive = list((Path(tmp) / "archive").glob("*.jsonl"))
            self.assertTrue(archive, "应有归档文件")
            archived = [json.loads(line) for line in archive[0].read_text(encoding="utf-8").splitlines() if line.strip()]
            self.assertTrue(any(row.get("source") == "whale_alert" for row in archived),
                            "Whale Alert 条目也要留档")
            self.assertGreaterEqual(payload["stats"]["unlabeled"], 1)


if __name__ == "__main__":
    unittest.main()
