"""巨鲸转账监控：公开源 HTTP 抓取（零第三方依赖）。

设计底线：
- 只允许公网 HTTP(S)，禁止私网/回环/元数据地址；跟随跳转时同样校验。
- 响应体硬上限，避免异常源撑爆 Actions 内存；crypvu 这类超大页面走前缀读取。
- 403/地区限制记 blocked，登录墙记 login_wall，404 记 missing，解析不出内容记 low_quality，
  其余记 provider_error。「源坏了」和「本来取不到」分开，不能把失败伪装成没转账。
- 页面编码不固定（UTF-8 / GB18030），按响应头或 <meta charset> 解码，避免中文变乱码。
- 不登录、不提交任何账户凭据。
"""
from __future__ import annotations

import ipaddress
import json
import re
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Dict, Optional
from urllib.parse import urlsplit

MAX_BYTES = 8 * 1024 * 1024
DEFAULT_TIMEOUT = 20.0
# 固定的项目标识 UA：对端能认出我们是谁，不容易被当成匿名爬虫无差别封。
# 少数非要浏览器 UA 才放行的站点，可以按请求单独传 headers 覆盖。
PROJECT_UA = (
    "Mozilla/5.0 (compatible; jujing-zhuanzhang/1.0; "
    "+https://github.com/sx-003/jujing-zhuanzhang)"
)
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
HEADERS: Dict[str, str] = {
    "User-Agent": PROJECT_UA,
    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "sec-ch-ua": '"Chromium";v="131", "Not_A Brand";v="24"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}
LOGIN_WALL_RE = re.compile(
    r"just a moment|access denied|verify you are human|请登录|登录后查看|安全验证|"
    r"enable javascript and cookies to continue",
    re.IGNORECASE,
)
CHARSET_RE = re.compile(rb"""<meta[^>]+charset=["']?([\w-]+)""", re.I)

# 本机与部分代理会把公网域名解析到 198.18.0.0/15（fake-IP 段），它不是真实内网地址。
FAKE_IP_RANGE = ipaddress.ip_network("198.18.0.0/15")


MAX_RETRY_AFTER = 30.0


def parse_retry_after(value: str, *, now: Optional[float] = None) -> float:
    """把 429 的 Retry-After 解析成秒数：支持「30」这种秒数，也支持 HTTP 日期格式。

    解析不出来（空、乱码、已过期）就返回 0，由调用方决定怎么处理。
    """
    raw = (value or "").strip()
    if not raw:
        return 0.0
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    from email.utils import parsedate_to_datetime
    try:
        moment = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return 0.0
    if moment is None:
        return 0.0
    if moment.tzinfo is None:
        from datetime import timezone as _tz
        moment = moment.replace(tzinfo=_tz.utc)
    current = now if now is not None else time.time()
    return max(0.0, moment.timestamp() - current)


class FetchFailure(Exception):
    def __init__(self, message: str, error_type: str = "provider_error", status: int = 0):
        super().__init__(message)
        self.error_type = error_type
        self.status = status


@dataclass
class FetchResult:
    url: str
    data: bytes = b""
    status: int = 0
    error_type: str = ""
    error: str = ""
    duration_ms: int = 0
    content_type: str = ""
    truncated: bool = False

    @property
    def ok(self) -> bool:
        return not self.error_type

    def charset(self) -> str:
        raw = (self.content_type or "").lower()
        match = re.search(r"charset=([\w-]+)", raw)
        if match:
            return match.group(1)
        match = CHARSET_RE.search(self.data[:4096])
        if match:
            try:
                return match.group(1).decode("ascii", "ignore")
            except Exception:  # noqa: BLE001
                pass
        return "utf-8"

    def text(self, encoding: str = "") -> str:
        enc = encoding or self.charset()
        try:
            return self.data.decode(enc, "replace")
        except LookupError:
            return self.data.decode("utf-8", "replace")

    def json(self):
        return json.loads(self.text())


def validate_url(url: str) -> None:
    parsed = urlsplit(url)
    if parsed.scheme.lower() not in ("http", "https"):
        raise FetchFailure(f"协议不允许: {parsed.scheme}", "config_error")
    if parsed.username or parsed.password:
        raise FetchFailure("URL 不允许带用户名或密码", "config_error")
    host = (parsed.hostname or "").rstrip(".").lower()
    if not host or host in ("localhost", "metadata.google.internal"):
        raise FetchFailure(f"主机名不允许: {host}", "config_error")
    if host.endswith((".local", ".localhost", ".internal")):
        raise FetchFailure(f"主机名不允许: {host}", "config_error")
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None and not literal.is_global:
        raise FetchFailure(f"非公网 IP 被拒绝: {literal}", "config_error")
    port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    try:
        answers = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise FetchFailure(f"DNS 解析失败: {host}: {exc}", "dns_error") from exc
    ips = {str(a[4][0]).split("%", 1)[0] for a in answers if a[4]}
    if not ips:
        raise FetchFailure(f"DNS 无可用地址: {host}", "dns_error")
    parsed_ips = []
    for raw in ips:
        try:
            parsed_ips.append(ipaddress.ip_address(raw))
        except ValueError as exc:
            raise FetchFailure(f"DNS 返回非法地址: {raw}", "dns_error") from exc
    if not all(addr.is_global or addr in FAKE_IP_RANGE for addr in parsed_ips):
        raise FetchFailure(f"DNS 含非公网地址，已拒绝: {host} -> {sorted(ips)[:3]}", "dns_error")


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _error_type(exc: Exception, status: int = 0) -> str:
    if status in (401, 403, 407):
        return "blocked"
    if status in (404, 410):
        return "missing"
    if isinstance(exc, urllib.error.HTTPError):
        if status == 429 or 500 <= status <= 599:
            return "provider_error"
        return "blocked" if status in (400, 401, 403) else "provider_error"
    return "provider_error"


def fetch(
    url: str,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    retries: int = 1,
    headers: Optional[Dict[str, str]] = None,
    body: Optional[bytes] = None,
    method: str = "GET",
    max_bytes: int = MAX_BYTES,
    check_login_wall: bool = False,
    allow_truncated: bool = False,
) -> FetchResult:
    started = time.time()
    validate_url(url)
    request_headers = dict(HEADERS)
    request_headers.update(headers or {})
    opener = urllib.request.build_opener(_SafeRedirect(), urllib.request.ProxyHandler({}))
    last: Optional[Exception] = None
    last_status = 0
    retried_429 = False
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, data=body, headers=request_headers, method=method)
            with opener.open(req, timeout=timeout) as resp:
                last_status = int(getattr(resp, "status", 0))
                content_type = resp.headers.get("Content-Type", "")
                declared = resp.headers.get("Content-Length", "")
                truncated = False
                try:
                    if declared and int(declared) > max_bytes:
                        if not allow_truncated:
                            raise FetchFailure(f"响应超过上限 {max_bytes} 字节", "too_large", last_status)
                        truncated = True
                except ValueError:
                    pass
                data = resp.read(max_bytes + 1)
                if len(data) > max_bytes:
                    if not allow_truncated:
                        raise FetchFailure(f"响应超过上限 {max_bytes} 字节", "too_large", last_status)
                    data = data[:max_bytes]
                    truncated = True
                if check_login_wall and LOGIN_WALL_RE.search(data[:5000].decode("utf-8", "ignore")):
                    raise FetchFailure("疑似登录墙/验证页", "login_wall", last_status)
                if not data:
                    raise FetchFailure("响应体为空", "missing", last_status)
                return FetchResult(url, data, last_status, duration_ms=int((time.time() - started) * 1000),
                                   content_type=content_type, truncated=truncated)
        except FetchFailure as exc:
            last = exc
            if exc.error_type in ("blocked", "missing", "login_wall", "too_large", "config_error", "dns_error"):
                break
        except urllib.error.HTTPError as exc:
            last = exc
            last_status = int(exc.code)
            if last_status == 429 and not retried_429:
                # 被限流：按 Retry-After 等一会儿再试一次，最多一次，不无限循环。
                retried_429 = True
                wait = parse_retry_after(exc.headers.get("Retry-After", "")) if exc.headers else 0.0
                wait = min(max(wait, 1.0), MAX_RETRY_AFTER)
                time.sleep(wait)
                continue
            if last_status in (400, 401, 403, 404, 405, 407, 410, 429):
                break
        except Exception as exc:  # noqa: BLE001 网络异常种类多，统一按可重试处理
            last = exc
        if attempt < retries:
            time.sleep(1.0 * (attempt + 1))
    error = str(last) if last else "未知抓取失败"
    kind = last.error_type if isinstance(last, FetchFailure) else _error_type(last, last_status)  # type: ignore[union-attr]
    return FetchResult(
        url, status=last_status, error_type=kind or "provider_error", error=error[:500],
        duration_ms=int((time.time() - started) * 1000),
    )


def fetch_json(url: str, **kwargs) -> FetchResult:
    result = fetch(url, **kwargs)
    if not result.ok:
        return result
    try:
        result.json()
    except (ValueError, UnicodeDecodeError) as exc:
        return FetchResult(url, result.data, result.status, "low_quality", f"JSON 解析失败: {exc}",
                           content_type=result.content_type)
    return result


def strip_tags(raw: str) -> str:
    text = re.sub(r"<[^>]+>", " ", raw or "")
    for a, b in (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
                 ("&quot;", '"'), ("&#39;", "'")):
        text = text.replace(a, b)
    return re.sub(r"\s+", " ", text).strip()


def parse_html_tables(html: str) -> list:
    """把 HTML 里的表格拆成 [[单元格文本, ...], ...]，不带表头识别。"""
    tables = []
    for table in re.findall(r"<table[^>]*>(.*?)</table>", html or "", re.S | re.I):
        rows = []
        for row in re.findall(r"<tr[^>]*>(.*?)</tr>", table, re.S | re.I):
            cells = []
            for cell in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.S | re.I):
                link = re.search(r'href="([^"]+)"', cell)
                spans = [strip_tags(part) for part in re.findall(r"<span[^>]*>(.*?)</span>", cell, re.S | re.I)]
                cells.append({"text": strip_tags(cell), "href": link.group(1) if link else "",
                              "spans": [span for span in spans if span]})
            if cells:
                rows.append(cells)
        if rows:
            tables.append(rows)
    return tables
