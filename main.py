"""巨鲸转账监控入口。默认只干跑，不接 Telegram。

python main.py --self-check                离线自检
python main.py --list-sources              列出数据源
python main.py --fixtures tests/fixtures/sample.json --dry-run
python main.py --dry-run                   真实抓源（无 token 也安全）
python main.py --send                      显式接推送后才真正发 Telegram
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from monitor import http, labels, rules, sources  # noqa: E402
from monitor import run as monitor_run  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="巨鲸转账监控")
    parser.add_argument("--dry-run", action="store_true", help="只打印候选消息，不发送（默认）")
    parser.add_argument("--send", action="store_true", help="显式启用 Telegram 推送")
    parser.add_argument("--self-check", action="store_true", help="离线自检，不联网不写状态")
    parser.add_argument("--list-sources", action="store_true", help="列出全部数据源")
    parser.add_argument("--fixtures", default="", help="用本地 JSON 样例替代真实网络，做离线演示")
    parser.add_argument("--state", default="state/state.json", help="状态文件路径")
    parser.add_argument("--report", default="report.json", help="运行报告路径")
    parser.add_argument("--max-queries", type=int, default=3, help="本轮云搜索查询上限")
    parser.add_argument("--no-cloud", action="store_true", help="关闭云端搜索（只跑发现层与链上直查）")
    parser.add_argument("--fetch-window-hours", type=float, default=monitor_run.FETCH_WINDOW_HOURS,
                        help="抓取窗口小时数，默认 6")
    parser.add_argument("--push-window-hours", type=float, default=monitor_run.PUSH_WINDOW_HOURS,
                        help="推送窗口小时数，默认 5")
    parser.add_argument("--max-push", type=int, default=0, help="每轮最多推送条数，0 表示不设上限")
    parser.add_argument("--ignore-seen", action="store_true",
                        help="人工核验用：忽略去重记录重发（会重复推送，平时不要开）")
    parser.add_argument("--per-round-addresses", type=int, default=16,
                        help="每轮轮询的交易所地址数量")
    parser.add_argument("--refresh-labels", action="store_true",
                        help="下载最新版地址表并写入 state/labels 缓存（每日工作流用）")
    args = parser.parse_args()

    if args.refresh_labels:
        try:
            result = labels.refresh_attribution_cache()
        except RuntimeError as exc:
            print(f"地址表刷新失败：{exc}", file=sys.stderr)
            return 5
        print(f"地址表已刷新：{result['count']} 条交易所地址 -> {result['path']}")
        return 0

    if args.self_check:
        failures = []
        if rules.THRESHOLD_MAJOR != 10_000_000 or rules.THRESHOLD_OTHER != 20_000_000:
            failures.append("阈值不是 1000 万 / 2000 万")
        if rules.URGENT_USD != 50_000_000:
            failures.append("加急阈值不是 5000 万")
        if not rules.is_exchange_label("Binance: Hot Wallet"):
            failures.append("交易所标签识别失败")
        if rules.is_exchange_label("未知钱包"):
            failures.append("未知钱包被误判为交易所")
        if not rules.is_protocol_label("USDC Treasury"):
            failures.append("协议金库识别失败")
        sample = rules.Transfer(source="t", chain="ethereum", symbol="BTC", amount=5320, usd=483_000_000,
                                from_label="BlackRock", to_label="Coinbase", ts=0, url="https://x/1")
        direction, filtered = rules.classify(sample)
        if direction != rules.DIR_IN:
            failures.append("方向判定失败：应为转入交易所")
        if filtered:
            failures.append("普通转账被误过滤")
        mint = rules.Transfer(source="t", chain="ethereum", symbol="USDC", amount=1, usd=1,
                              from_label="USDC Treasury", to_label="未知钱包", ts=0, url="https://x/2")
        if not rules.classify(mint)[1]:
            failures.append("铸销未被过滤")
        loan = rules.Transfer(source="t", chain="ethereum", symbol="USDT", amount=1, usd=1,
                              from_label="未知钱包", to_label="Aave", ts=0, url="https://x/3")
        if not rules.classify(loan)[1]:
            failures.append("借贷质押未被过滤")
        blank = rules.Transfer(source="t", chain="ethereum", symbol="BTC", amount=5000,
                               usd=320_000_000, from_label="未知钱包", to_label="未知钱包",
                               ts=0, url="https://x/4")
        if not rules.is_unlabeled(blank):
            failures.append("两头都是未知钱包的大额样本没被过滤")
        mm = rules.Transfer(source="t", chain="ethereum", symbol="ETH", amount=20000,
                            usd=36_000_000, from_label="未知钱包", to_label="Wintermute",
                            ts=0, url="https://x/6")
        if rules.is_unlabeled(mm):
            failures.append("做市商样本被当成未知钱包过滤")
        if rules.normalize_label("Jump Crypto") != "Jump Trading":
            failures.append("做市商标签归一失败：Jump Crypto")
        if rules.is_exchange_label("Wintermute"):
            failures.append("做市商被误判为交易所（方向会错）")
        mm_message = rules.format_message(direction=rules.DIR_W2W, items=[mm], usd=mm.usd)
        if "Wintermute" not in mm_message:
            failures.append("做市商名字没出现在推送文案里")
        # 分组顺序：转入 → 转出 → 钱包互转 → 所间转账，空组不显示标题
        def _cand(direction, usd, target):
            sample_item = rules.Transfer(source="t", chain="ethereum", symbol="USDT", amount=1,
                                         usd=usd, ts=0, url=f"https://x/{usd}",
                                         from_label="未知钱包", to_label=target)
            return monitor_run.Candidate(direction=direction, items=[sample_item], usd=usd,
                                         url=sample_item.url, keys=[sample_item.key()])
        def _cand_at(direction, usd, target, ts):
            sample_item = rules.Transfer(source="t", chain="ethereum", symbol="USDT", amount=1,
                                         usd=usd, ts=ts, url=f"https://x/{usd}",
                                         from_label="未知钱包", to_label=target)
            return monitor_run.Candidate(direction=direction, items=[sample_item], usd=usd,
                                         url=sample_item.url, keys=[sample_item.key()])
        grouped = monitor_run.build_round_text([
            _cand_at(rules.DIR_IN, 20_000_000, "Binance", 3000),
            _cand_at(rules.DIR_IN, 30_000_000, "COINBASE".title(), 1000),
            _cand_at(rules.DIR_OUT, 25_000_000, "未知钱包", 2000),
            _cand_at(rules.DIR_CEX2CEX, 22_000_000, "Kraken", 1500),
        ])
        grouped_lines = grouped.splitlines()
        if not grouped_lines[0].startswith("巨鲸转账 · 本轮 4 条"):
            failures.append("分组消息总标题条数不对")
        if "共计" in grouped_lines[0]:
            failures.append("分组消息总标题不该再显示合计（合计只在分组标题里）")
        if "共计" not in grouped[grouped.index("转入交易所（2 条）"):]:
            failures.append("分组标题缺金额合计")
        if "转入交易所（2 条）" not in grouped:
            failures.append("分组消息缺「转入交易所（2 条）」小标题")
        if "转出交易所（1 条）" not in grouped or "所间转账（1 条）" not in grouped:
            failures.append("分组消息缺转出/所间小标题")
        if not (grouped.index("转入交易所") < grouped.index("转出交易所") < grouped.index("所间转账")):
            failures.append("分组顺序不对：应为转入 → 转出 → 所间")
        # 组内按时间升序：转入组里 ts=1000 的那条（金额更小）要排在 ts=3000 前面
        group_block = grouped.split("转入交易所（2 条）")[1].split("转出交易所")[0]
        if group_block.index("3000 万美元") > group_block.index("2000 万美元"):
            failures.append("组内没有按时间升序（早的应在最上）")
        items_block = grouped.split("转入交易所（2 条）")[1].split("转出交易所")[0]
        if any(tag in items_block for tag in ("【转入交易所】", "【转出交易所】")):
            failures.append("分组里每条还带着方向标签")
        if "转入交易所（2 条）" not in grouped:
            failures.append("分组标题丢了方向")
        # 三个组 → 两条分割线，且最后一组后面没有线
        if grouped.count(monitor_run.GROUP_DIVIDER) != 2:
            failures.append("分组之间的分割线条数不对（三组应为两条）")
        if grouped.rstrip().endswith(monitor_run.GROUP_DIVIDER):
            failures.append("最后一组后面不该有分割线")
        if monitor_run.build_round_text([_cand_at(rules.DIR_IN, 20_000_000, "Binance", 1)]).count(
                monitor_run.GROUP_DIVIDER) != 0:
            failures.append("只有一组时不该有分割线")
        if monitor_run.build_round_text([]).startswith("巨鲸转账"):
            failures.append("空轮不应生成消息")

        known = rules.Transfer(source="t", chain="ethereum", symbol="BTC", amount=5000,
                               usd=320_000_000, from_label="未知钱包", to_label="Kraken",
                               ts=0, url="https://x/5")
        if rules.is_unlabeled(known):
            failures.append("一头认得出来的转账被误过滤")
        message = rules.format_message(direction=rules.DIR_IN, items=[sample], usd=483_000_000)
        for field in ("亿美元", "BTC", "BlackRock", "Coinbase"):
            if field not in message:
                failures.append(f"推送文案缺字段：{field}")
        if "http" in message or "原文" in message:
            failures.append("推送文案里还有网址/原文链接")
        if not sample.url:
            failures.append("样例条目缺链接，归档会丢链接")
        if not message.startswith("【加急】"):
            failures.append("5000 万以上没有加急标记")
        if any(tag in message for tag in ("【转入交易所】", "【转出交易所】", "【所间转账】")):
            failures.append("逐条还在写方向标签（应由分组标题表达）")
        # 备用域名轮询：主域名不通要能自动换下一个
        class _FakeResult:
            def __init__(self, ok, url, error_type=""):
                self.ok, self.url, self.error_type, self.error = ok, url, error_type, "stub"

        def _fake_fetch(url, **kwargs):
            if "telegram.me" in url:
                return _FakeResult(False, url, "provider_error")
            return _FakeResult(True, url)

        _, used, tried = sources.fetch_telegram_channel("demo", fetcher=_fake_fetch)
        if "telegram.dog" not in used or not tried:
            failures.append("备用域名轮询没生效（主域名挂了没切下一个）")
        # 429 的 Retry-After 两种格式都要能算
        if http.parse_retry_after("30") != 30.0:
            failures.append("Retry-After 秒数解析失败")
        import time as _time
        future = _time.strftime("%a, %d %b %Y %H:%M:%S GMT", _time.gmtime(_time.time() + 45))
        parsed = http.parse_retry_after(future)
        if not (30 <= parsed <= 50):
            failures.append(f"Retry-After HTTP 日期解析失败（得到 {parsed}）")
        if http.parse_retry_after("乱码") != 0.0:
            failures.append("Retry-After 乱码应返回 0")
        if "jujing-zhuanzhang" not in http.PROJECT_UA:
            failures.append("UA 里没有项目标识")
        # 自续跑的两道保险：push 轮不续跑、退出码 >1 不续跑
        if monitor_run.should_chain("push", 0, "true"):
            failures.append("push 干跑轮竟然要续跑（会无限循环）")
        if monitor_run.should_chain("workflow_dispatch", 2, "true"):
            failures.append("推送失败轮竟然要续跑（会疯狂空转）")
        if not monitor_run.should_chain("schedule", 1, "true"):
            failures.append("正式轮成功且开关为 true 时没有续跑")
        for off in ("", "false", "0"):
            if monitor_run.should_chain("schedule", 0, off):
                failures.append(f"总开关 SELF_CHAIN={off!r} 没关掉续跑")
        if not monitor_run.should_chain("schedule", 0, "true"):
            failures.append("总开关 SELF_CHAIN=true 时正式轮没有续跑")

        if failures:
            for item in failures:
                print("自检失败：" + item, file=sys.stderr)
            return 5
        print("自检通过：阈值、方向、铸销借贷与两头未知过滤、机构命名、按方向分组、推送文案均正常")
        return 0

    if args.list_sources:
        print("发现层：crypvu（中文，主力）｜trackwhale（频道，带方向标签）｜whale_alert（频道，超大额）")
        print("         flashai（中文多链）｜coinstrooper（备用）｜ourscrypto（比特币）")
        print("链上直查：Blockscout 公开 API v2（以太坊，免 key）")
        print("标注层：tradezon/cex-list（每轮下载）｜wallet-attribution（每日刷新缓存）｜Blockscout 自带标签")
        print("兜底层：Exa 公共 MCP｜Anysearch")
        print(f"阈值：主流币 {rules.THRESHOLD_MAJOR:,.0f} 美元｜其他 {rules.THRESHOLD_OTHER:,.0f} 美元｜"
              f"加急 {rules.URGENT_USD:,.0f} 美元")
        return 0

    if args.send and args.dry_run:
        print("--send 与 --dry-run 不能同时用", file=sys.stderr)
        return 5
    return monitor_run.run(
        fixtures=args.fixtures, state_path=args.state, report_path=args.report,
        send=args.send, no_cloud=args.no_cloud, max_queries=args.max_queries,
        fetch_window_hours=args.fetch_window_hours, push_window_hours=args.push_window_hours,
        max_push=args.max_push, per_round_addresses=args.per_round_addresses,
        ignore_seen=args.ignore_seen,
    )


if __name__ == "__main__":
    sys.exit(main())
