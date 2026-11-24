"""编排：抓取 → 补数量 → 分类过滤 → 跨源去重 → 合并 → 选条 → 干跑/推送 → 状态与归档。

双窗口闸门：抓取窗口（默认 6 小时）大于推送窗口（默认 5 小时），
用来吸收 GitHub 调度延迟，同时保证不推旧消息。
去重状态写在 state 分支，不靠 cache；推送失败不写 seen，下轮重试。
"""
from __future__ import annotations

import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import cloud, labels, rules, sources
from . import state as state_mod
from . import telegram

EXIT_OK, EXIT_PARTIAL, EXIT_ALL_FAILED, EXIT_CONFIG = 0, 1, 2, 5
FETCH_WINDOW_HOURS = 6.0
PUSH_WINDOW_HOURS = 5.0


def should_chain(event_name: str, exit_code: int, self_chain: str = "") -> bool:
    """工作流末尾「自续跑」步骤的判断逻辑镜像，供自检与测试核对。

    总开关 SELF_CHAIN 必须显式设成 'true' 才开（默认关闭）；只有 schedule / workflow_dispatch
    这类正式轮且采集退出码 <=1 才 dispatch 下一轮；push 干跑轮和失败轮绝不续跑。
    """
    if str(self_chain).strip().lower() != "true":
        return False
    if event_name not in ("schedule", "workflow_dispatch"):
        return False
    try:
        return int(exit_code or 0) <= 1
    except (TypeError, ValueError):
        return False


@dataclass
class Candidate:
    direction: str
    items: List[rules.Transfer] = field(default_factory=list)
    usd: float = 0.0
    url: str = ""
    keys: List[str] = field(default_factory=list)

    def message(self) -> str:
        return rules.format_message(direction=self.direction, items=self.items, usd=self.usd)


def _load_fixtures(path: str) -> List[rules.Transfer]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    items = []
    for row in raw.get("transfers", []):
        data = {k: v for k, v in row.items() if k in rules.Transfer.__dataclass_fields__}
        if not float(data.get("ts") or 0):
            data["ts"] = time.time()  # 样例数据不带真实时间戳，按当前时间处理
        items.append(rules.Transfer(**data))
    return items


def _enrich_quantity(item: rules.Transfer, prices: Dict[str, float]) -> bool:
    """有些源只给美元金额不给数量，用行情按现价折算一个约数；没有行情就放弃这条。"""
    if item.amount and item.amount > 0:
        return True
    if item.usd <= 0:
        return False
    price = 0.0
    for alias in _price_aliases(item.symbol):
        if alias in prices:
            price = float(prices[alias])
            break
    if price <= 0:
        return False
    item.amount = item.usd / price
    item.extra["derived_quantity"] = True
    return True


def _price_aliases(symbol: str) -> List[str]:
    from . import chains
    key = (symbol or "").upper()
    return chains.PRICE_ALIASES.get(key, [key])


def _merge_candidates(items: List[rules.Transfer]) -> List[Candidate]:
    """同一主体短时间内多笔合并成一条，注笔数。"""
    groups: Dict[str, Candidate] = {}
    order: List[str] = []
    for item in items:
        key = rules.merge_group_key(item, item.extra.get("direction", rules.DIR_W2W))
        candidate = groups.get(key)
        if candidate is None:
            candidate = Candidate(direction=item.extra.get("direction", rules.DIR_W2W))
            groups[key] = candidate
            order.append(key)
        candidate.items.append(item)
        candidate.usd += item.usd
        candidate.keys.append(item.key())
        if len(item.url) > len(candidate.url):
            candidate.url = item.url
    result = [groups[key] for key in order]
    for candidate in result:
        candidate.items.sort(key=lambda item: item.usd, reverse=True)
    return result


GROUP_TITLES = {
    rules.DIR_IN: "转入交易所",
    rules.DIR_OUT: "转出交易所",
    rules.DIR_W2W: "钱包互转",
    rules.DIR_CEX2CEX: "所间转账",
}
# 分组顺序：先转入、再转出、再钱包互转，所间转账放最后。
GROUP_ORDER = [rules.DIR_IN, rules.DIR_OUT, rules.DIR_W2W, rules.DIR_CEX2CEX]
# 组与组之间用一排横线隔开，避免几组挨着糊在一起。
GROUP_DIVIDER = "━" * 16


def build_round_text(selected: List[Candidate]) -> str:
    """一轮所有候选拼成一条消息，按方向分组；空组不显示标题。

    开头总标题写本轮条数，每组前面一行「方向（N 条）」。
    组内按转账时间升序（最早的在最上），时间相同再按金额降序。
    总长度由调用方按 Telegram 单条上限检查。
    """
    if not selected:
        return ""
    round_total = sum(item.usd for item in selected)
    lines = [f"巨鲸转账 · 本轮 {len(selected)} 条 · 共计{rules.format_usd_sum(round_total)}", ""]
    grouped_ids = set()
    first_group = True
    for direction in GROUP_ORDER:
        group = [(index, item) for index, item in enumerate(selected) if item.direction == direction]
        if not group:
            continue
        grouped_ids.update(index for index, _ in group)
        # 组内按时间升序，最早的在最上；同一时间按金额降序。
        group.sort(key=lambda pair: (min(i.ts for i in pair[1].items), -pair[1].usd))
        if not first_group:
            lines.append(GROUP_DIVIDER)
        first_group = False
        group_total = sum(item.usd for _, item in group)
        lines.append(f"{GROUP_TITLES[direction]}（{len(group)} 条）· 共计{rules.format_usd_sum(group_total)}")
        lines.extend(item.message() for _, item in group)
    leftovers = [(index, item) for index, item in enumerate(selected) if index not in grouped_ids]
    if leftovers:
        leftovers.sort(key=lambda pair: (min(i.ts for i in pair[1].items), -pair[1].usd))
        if not first_group:
            lines.append(GROUP_DIVIDER)
        leftover_total = sum(item.usd for _, item in leftovers)
        lines.append(f"其他转账（{len(leftovers)} 条）· 共计{rules.format_usd_sum(leftover_total)}")
        lines.extend(item.message() for _, item in leftovers)
    return "\n".join(lines)


def run(*, fixtures: str = "", state_path: str = "state/state.json", report_path: str = "",
        send: bool = False, no_cloud: bool = False, max_queries: int = 3,
        fetch_window_hours: float = FETCH_WINDOW_HOURS, push_window_hours: float = PUSH_WINDOW_HOURS,
        max_push: int = 0, per_round_addresses: int = 24, ignore_seen: bool = False) -> int:
    started = datetime.now(timezone.utc).isoformat()
    started_ts = time.time()
    st = state_mod.State(state_path).load()
    gap = st.record_run(started)
    print(f"本轮开始：{started}｜距上轮 {gap if gap >= 0 else '未知'} 分钟｜"
          f"抓取窗口 {fetch_window_hours} 小时，推送窗口 {push_window_hours} 小时")

    reports: List[sources.SourceReport] = []
    prices: Dict[str, float] = {}
    price_source = ""
    book = labels.LabelBook()

    if fixtures:
        raw_items = _load_fixtures(fixtures)
        reports.append(sources.SourceReport(name="fixture", layer="fixture", ok=True,
                                            status="ok", items=raw_items))
        print(f"使用本地样例：{len(raw_items)} 条")
    else:
        prices, price_source = sources.fetch_prices()
        print(f"行情源：{price_source or '不可用'}｜{len(prices)} 个价格")
        try:
            book.load_cex_list()
        except RuntimeError as exc:
            print(f"cex-list 抓取失败：{exc}", file=sys.stderr)
        cached = book.load_cached_attribution()
        print(f"地址表：cex-list {book.stats['cex']} 条｜wallet-attribution 缓存 "
              f"{book.stats['attribution']} 条（{'有' if cached else '无'}）")

        with ThreadPoolExecutor(max_workers=7) as pool:
            futures = {
                "crypvu": pool.submit(sources.fetch_crypvu, fetch_window_hours),
                "trackwhale": pool.submit(sources.fetch_trackwhale, fetch_window_hours),
                "whale_alert": pool.submit(sources.fetch_whale_alert, fetch_window_hours),
                "flashai": pool.submit(sources.fetch_flashai, fetch_window_hours),
                "coinstrooper": pool.submit(sources.fetch_coinstrooper, fetch_window_hours),
                "ourscrypto": pool.submit(sources.fetch_ourscrypto, fetch_window_hours),
                "blockscout": pool.submit(sources.fetch_blockscout, book, prices,
                                          per_round_addresses, fetch_window_hours),
            }
            for name, future in futures.items():
                try:
                    reports.append(future.result())
                except Exception as exc:  # noqa: BLE001 单源异常不拖垮整轮，但要留痕
                    reports.append(sources._fail(name, "discovery", "provider_error",
                                                 f"{type(exc).__name__}: {exc}"))

    for report in reports:
        state_line = f"源 {report.name}：{report.status}｜条目 {len(report.items)}"
        if not report.ok:
            state_line += f"｜{report.error[:120]}"
        print(state_line)

    cloud_report = cloud.CloudReport()
    items: List[rules.Transfer] = []
    for report in reports:
        items.extend(report.items)

    if not fixtures and not no_cloud:
        cloud_report = cloud.run_cloud(st.daily_queries, max_queries=max_queries, enabled=True)
        cloud_items = cloud.hits_to_transfers(cloud_report.hits)
        items.extend(cloud_items)
        print(f"云端搜索：{cloud_report.to_dict()}")
        st.mark_queries(cloud_report.queries_used, datetime.now(timezone.utc).strftime("%Y-%m-%d"))

    # ---- 分类与过滤
    stats = {"raw": len(items), "no_quantity": 0, "filtered": 0, "unlabeled": 0, "no_link": 0,
             "too_old": 0, "below_threshold": 0, "pushable": 0}
    kept: List[rules.Transfer] = []
    archive_only: List[rules.Transfer] = []
    now_ts = time.time()
    for item in items:
        if not item.url or not item.url.startswith(("http://", "https://")):
            stats["no_link"] += 1
            continue
        direction, filtered = rules.classify(item)
        if filtered:
            stats["filtered"] += 1  # 铸销、借贷质押：既不推也不留档
            continue
        if rules.is_unlabeled(item):
            stats["unlabeled"] += 1  # 两头都是未知钱包：不推（金额再大也不推）
            continue
        if not _enrich_quantity(item, prices):
            stats["no_quantity"] += 1
            continue
        if item.ts < now_ts - fetch_window_hours * 3600:
            stats["too_old"] += 1
            continue
        item.extra["direction"] = direction
        if item.usd < rules.threshold_for(item.symbol):
            stats["below_threshold"] += 1
            if direction in (rules.DIR_IN, rules.DIR_OUT, rules.DIR_CEX2CEX):
                archive_only.append(item)  # 交易所进出低于门槛：只留档不推
            continue
        if item.ts < now_ts - push_window_hours * 3600:
            stats["too_old"] += 1
            continue
        stats["pushable"] += 1
        kept.append(item)

    # ---- 跨源去重：同一笔只留一条
    unique: Dict[str, rules.Transfer] = {}
    for item in sorted(kept, key=lambda x: (bool(x.tx_hash), x.usd), reverse=True):
        key = item.key()
        if key not in unique:
            unique[key] = item
        else:
            unique[key].extra.setdefault("also_from", []).append(item.source)
    deduped = list(unique.values())

    # ---- 合并多笔
    candidates = _merge_candidates(deduped)

    # ---- 去重状态过滤
    fresh: List[Candidate] = []
    repeated = 0
    for candidate in candidates:
        if not ignore_seen and any(st.already_seen(key) for key in candidate.keys):
            repeated += 1
            continue
        fresh.append(candidate)
    if ignore_seen:
        print("注意：本轮带 --ignore-seen（人工核验用），已推过的条目也会重新参与选条。", file=sys.stderr)
    fresh.sort(key=lambda c: c.usd, reverse=True)
    selected = fresh[:max_push] if max_push else fresh
    rest = fresh[max_push:] if max_push else []

    print(f"原始 {stats['raw']} 条｜过滤转销借贷 {stats['filtered']}｜两头未知 {stats['unlabeled']}｜"
          f"缺数量 {stats['no_quantity']}｜"
          f"低于门槛 {stats['below_threshold']}｜过旧 {stats['too_old']}｜可推 {stats['pushable']}｜"
          f"跨源去重后 {len(deduped)}｜合并后 {len(candidates)}｜已推过 {repeated}｜本轮选入 {len(selected)}")

    all_failed = bool(reports) and all(not report.ok for report in reports)
    if all_failed:
        print("全部来源同时失败，单独报警。", file=sys.stderr)

    # ---- 推送 / 干跑：一轮只发一条消息，按方向分组
    pushed: List[dict] = []
    failed: List[str] = []
    round_text = build_round_text(selected)
    telegram_messages = 0
    skipped_too_long = False
    if round_text and len(round_text) > telegram.MAX_CHARS:
        # 超过 Telegram 单条上限：本轮不推，条目保持未推送，下轮再试。
        skipped_too_long = True
        print(f"本轮合并后 {len(round_text)} 字，超过 Telegram 单条上限 "
              f"{telegram.MAX_CHARS}，暂停推送（条目保持未推送，下轮重试）。", file=sys.stderr)
    if send and round_text and not skipped_too_long:
        token, chat_id = telegram.credentials()
        if not token or not chat_id:
            print("缺少 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID，拒绝推送。", file=sys.stderr)
            return EXIT_CONFIG
        try:
            telegram.send_message(token, chat_id, round_text)
        except telegram.PushError as exc:
            failed.append(str(exc)[:120])
            print(f"推送失败，保持未推送下轮重试：{exc}", file=sys.stderr)
        else:
            telegram_messages = 1
            for candidate in selected:
                for key in candidate.keys:
                    st.mark_pushed(key, {"usd": candidate.usd, "direction": candidate.direction,
                                         "url": candidate.url, "at": candidate.items[0].ts})
                pushed.append({"message": candidate.message(), "usd": candidate.usd,
                               "direction": candidate.direction, "url": candidate.url,
                               "count": len(candidate.items),
                               "delay_minutes": round((time.time() - candidate.items[0].ts) / 60, 1)})
    elif not send:
        if round_text:
            print("\n--- 干跑：本轮合并为一条消息，不会发送 ---\n" + round_text)
        else:
            print("\n--- 干跑：本轮无内容 ---")
        if round_text and not skipped_too_long:
            for candidate in selected:
                for key in candidate.keys:
                    st.mark_pushed(key, {"usd": candidate.usd, "direction": candidate.direction,
                                         "url": candidate.url, "at": candidate.items[0].ts,
                                         "dry_run": True})
                pushed.append({"message": candidate.message(), "usd": candidate.usd,
                               "direction": candidate.direction, "url": candidate.url,
                               "count": len(candidate.items),
                               "delay_minutes": round((time.time() - candidate.items[0].ts) / 60, 1)})

    # 留档：推过的 + 低于门槛的交易所进出（铸销与借贷质押已在过滤阶段排除）
    archive_records = []
    for item in archive_only:
        if st.already_archived(item.key()):
            continue
        record = {"ts": datetime.fromtimestamp(item.ts, tz=timezone.utc).isoformat(),
                  "source": item.source, "symbol": item.symbol, "amount": item.amount,
                  "usd": item.usd, "from": item.from_label, "to": item.to_label,
                  "direction": item.extra.get("direction", ""), "url": item.url,
                  "pushed": False}
        archive_records.append(record)
        st.mark_archived(item.key(), record)
    pushed_urls = {record["url"] for record in pushed}
    for candidate in selected:
        for item in candidate.items:
            archive_records.append({
                "ts": datetime.fromtimestamp(item.ts, tz=timezone.utc).isoformat(),
                "source": item.source, "symbol": item.symbol, "amount": item.amount,
                "usd": item.usd, "from": item.from_label, "to": item.to_label,
                "direction": item.extra.get("direction", ""), "url": item.url,
                "pushed": item.url in pushed_urls,
            })
    archive_path = st.archive(archive_records)

    st.prune()
    st.save()

    report = {
        "started_at": started,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "gap_minutes": gap,
        "mode": "send" if send else "dry-run",
        "fetch_window_hours": fetch_window_hours,
        "push_window_hours": push_window_hours,
        "sources": [item.to_dict() for item in reports],
        "source_failures": st.source_failures,
        "all_sources_failed": all_failed,
        "price_source": price_source,
        "labels": book.to_dict(),
        "cloud": cloud_report.to_dict(),
        "stats": stats,
        "candidates": [{"message": item["message"], "usd": item["usd"],
                        "direction": item["direction"], "count": item["count"],
                        "url": item["url"], "delay_minutes": item.get("delay_minutes")}
                       for item in pushed],
        "telegram_messages": telegram_messages,
        "round_message": round_text[:6000],
        "skipped_too_long": skipped_too_long,
        "archive": archive_path,
        "archive_only": len(archive_only),
        "push_delay_minutes": {
            "min": min((item["delay_minutes"] for item in pushed), default=None),
            "max": max((item["delay_minutes"] for item in pushed), default=None),
        },
        "push_failed": failed,
        "rest": [{"usd": item.usd, "direction": item.direction, "url": item.url} for item in rest],
    }
    if report_path:
        target = Path(report_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"报告已写入 {report_path}｜归档 {archive_path}")
    if failed:
        return EXIT_ALL_FAILED
    if all_failed:
        return EXIT_ALL_FAILED
    if any(not item.ok for item in reports):
        return EXIT_PARTIAL
    return EXIT_OK
