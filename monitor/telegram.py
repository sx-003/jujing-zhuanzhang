"""Telegram 推送：只有 Bot API 返回 ok 才算成功。

一轮只发一条消息（链接预览关闭）；超过 Telegram 单条长度上限的分块静音发送。
失败保持未推送状态，下轮重试。token 和 chat id 只读环境变量，不写文件、不进日志。
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

API = "https://api.telegram.org/bot{token}/sendMessage"
MAX_CHARS = 4000


class PushError(Exception):
    pass


def credentials() -> tuple:
    return os.environ.get("TELEGRAM_BOT_TOKEN", ""), os.environ.get("TELEGRAM_CHAT_ID", "")


def send_message(token: str, chat_id: str, text: str, *, timeout: float = 20.0,
                 disable_notification: bool = False) -> bool:
    if not token or not chat_id:
        raise PushError("缺少 TELEGRAM_BOT_TOKEN 或 TELEGRAM_CHAT_ID")
    text = (text or "").strip()
    if not text:
        raise PushError("消息为空")
    if len(text) > MAX_CHARS:
        # 不截断：截断会丢条目，超长交给调用方暂停推送。
        raise PushError(f"消息超长（{len(text)} > {MAX_CHARS}）")
    # disable_web_page_preview：链接只显示纯文本，不让 Telegram 展开卡片。
    # disable_notification：保留参数，本轮统一整条发送，默认不静音。
    payload = json.dumps({
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
        "disable_notification": disable_notification,
    }, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(API.format(token=token), data=payload,
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:160]
        raise PushError(f"Telegram HTTP {exc.code}: {detail}") from exc
    except Exception as exc:  # noqa: BLE001
        raise PushError(f"Telegram 请求失败: {type(exc).__name__}") from exc
    if not body.get("ok"):
        raise PushError(f"Telegram 返回失败: {str(body.get('description') or '')[:160]}")
    return True
