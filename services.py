# -*- coding: utf-8 -*-
"""
services.py

把"网络获取 / 域名解析 / 测速"等纯逻辑从 GUI 中抽离出来，便于维护与测试。

说明：
- 本文件不依赖 tkinter/ttkbootstrap，避免与 UI 层耦合。
- Windows 专用能力（ICMP ping / flushdns 等）在需要时做平台判断。
- 支持异步和同步两种调用方式。
- 支持 IPv4 和 IPv6 双栈。
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import ipaddress
import json
import os
import re
import socket
import ssl
import statistics
import subprocess
import sys
import time
from typing import Callable, Iterable, List, Optional, Tuple, Dict, Any, Union, Set

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from config import (
    APP_NAME,
    REMOTE_FETCH_TIMEOUT,
    REMOTE_HOSTS_URLS,
    SPEED_TEST_CONFIG,
    HTTP_CLIENT_CONFIG,
    DNS_RESOLVER_CONFIG,
)
from utils import get_logger


# ---------------------------------------------------------------------
# Remote Hosts
# ---------------------------------------------------------------------
class RemoteHostsClient:
    """获取远程 hosts 并解析出 GitHub 相关域名的 (ip, domain) 列表。"""

    CACHE_TTL_SECONDS = 600

    def __init__(
        self,
        *,
        urls: Optional[List[str]] = None,
        timeout: Tuple[int, int] = REMOTE_FETCH_TIMEOUT,
        app_name: str = APP_NAME,
        session: Optional[requests.Session] = None,
        cache_dir: Optional[str] = None,
    ) -> None:
        self.urls = urls or list(REMOTE_HOSTS_URLS)
        self.timeout = timeout
        self.session = session or self._build_http_session(app_name)
        self._cache_dir = cache_dir
        if cache_dir:
            os.makedirs(cache_dir, exist_ok=True)

    def _get_cache_path(self, url: str) -> Optional[str]:
        if not self._cache_dir:
            return None
        import hashlib
        url_hash = hashlib.md5(url.encode()).hexdigest()
        return os.path.join(self._cache_dir, f"hosts_cache_{url_hash}.json")

    def _read_cache(self, url: str) -> Optional[Tuple[List[Tuple[str, str]], str, float]]:
        cache_path = self._get_cache_path(url)
        if not cache_path or not os.path.exists(cache_path):
            return None
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if time.time() - data.get("timestamp", 0) < self.CACHE_TTL_SECONDS:
                return data.get("records"), data.get("used_url", url), data.get("timestamp", 0)
        except Exception:
            pass
        return None

    def _write_cache(self, url: str, records: List[Tuple[str, str]], used_url: str) -> None:
        cache_path = self._get_cache_path(url)
        if not cache_path:
            return
        try:
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump({
                    "records": records,
                    "used_url": used_url,
                    "timestamp": time.time(),
                }, f, ensure_ascii=False)
        except Exception:
            pass

    @staticmethod
    def _build_retry() -> Retry:
        retry_config = HTTP_CLIENT_CONFIG.get("retry", {})
        kwargs = dict(
            total=retry_config.get("total", 3),
            connect=retry_config.get("connect", 3),
            read=retry_config.get("read", 3),
            backoff_factor=retry_config.get("backoff_factor", 0.5),
            status_forcelist=tuple(retry_config.get("status_forcelist", [429, 500, 502, 503, 504])),
            raise_on_status=False,
        )
        try:
            return Retry(allowed_methods=frozenset(["GET"]), **kwargs)
        except TypeError:
            return Retry(method_whitelist=frozenset(["GET"]), **kwargs)

    @classmethod
    def _build_http_session(cls, app_name: str) -> requests.Session:
        s = requests.Session()
        try:
            s.headers.update({"User-Agent": f"{app_name}/1.0"})
        except Exception:
            pass

        retries = cls._build_retry()
        pool_config = HTTP_CLIENT_CONFIG.get("pool", {})
        adapter = HTTPAdapter(
            max_retries=retries,
            pool_connections=pool_config.get("connections", 20),
            pool_maxsize=pool_config.get("maxsize", 20)
        )
        s.mount("http://", adapter)
        s.mount("https://", adapter)
        return s

    @staticmethod
    def parse_github_hosts_text(
        txt: str,
        *,
        ipv4_only: bool = False,
        ipv6_only: bool = False,
    ) -> List[Tuple[str, str]]:
        """解析 hosts 文本，提取 github 相关域名的 (ip, domain) 列表。

        Args:
            txt: hosts 文本内容
            ipv4_only: 仅返回 IPv4 地址
            ipv6_only: 仅返回 IPv6 地址
            默认返回 IPv4 和 IPv6

        规则与原版一致：
        - 按行解析，支持一行多个域名
        - 严格校验 IP 地址（支持 IPv4 和 IPv6），避免误解析 HTML/杂内容
        - 仅保留 host 中包含 "github" 的记录
        """
        out: List[Tuple[str, str]] = []
        seen: Set[Tuple[str, str]] = set()

        for raw in (txt or "").splitlines():
            line = (raw or "").strip()
            if not line or line.startswith("#"):
                continue

            # 去掉行内注释
            if "#" in line:
                line = line.split("#", 1)[0].strip()
            if not line:
                continue

            parts = line.split()
            if len(parts) < 2:
                continue

            ip_str = parts[0].strip()
            try:
                ip_obj = ipaddress.ip_address(ip_str)

                # 根据 IP 版本过滤
                if ipv4_only and ip_obj.version != 4:
                    continue
                if ipv6_only and ip_obj.version != 6:
                    continue

            except Exception:
                # 不是有效的 IP 地址，跳过
                continue

            for host in parts[1:]:
                host = host.strip()
                if not host:
                    continue

                # hostname 基本校验
                if not re.fullmatch(r"[A-Za-z0-9.-]+", host):
                    continue
                if "." not in host:
                    continue

                if "github" not in host.lower():
                    continue

                key = (ip_str, host.lower())
                if key in seen:
                    continue
                seen.add(key)
                out.append((ip_str, host))

        return out

    def fetch_github_hosts(
        self,
        *,
        url_override: Optional[str] = None,
        ipv4_only: bool = False,
        ipv6_only: bool = False,
    ) -> Tuple[List[Tuple[str, str]], str]:
        """获取并解析远程 hosts（同步版本），带缓存。

        返回：(records, used_url)
        - records: [(ip, domain), ...]
        - used_url: 最终成功的 URL
        """
        urls = [url_override] if url_override else list(self.urls)

        for url in urls:
            cached = self._read_cache(url)
            if cached:
                records, used_url, _ = cached
                if ipv4_only or ipv6_only:
                    records = [(ip, dom) for ip, dom in records
                               if not ipv4_only or ipaddress.ip_address(ip).version == 4]
                    records = [(ip, dom) for ip, dom in records
                               if not ipv6_only or ipaddress.ip_address(ip).version == 6]
                if records:
                    return records, used_url

        last_err: Optional[Exception] = None

        for url in urls:
            try:
                r = self.session.get(url, timeout=self.timeout)
                r.raise_for_status()
                txt = r.text or ""

                ctype = (r.headers.get("content-type") or "").lower()
                head = txt[:500].lower()
                if "text/html" in ctype and ("<html" in head or "<!doctype" in head):
                    continue

                parsed = self.parse_github_hosts_text(txt, ipv4_only=ipv4_only, ipv6_only=ipv6_only)
                if parsed:
                    self._write_cache(url, parsed, url)
                    return parsed, url
            except (requests.RequestException, socket.timeout, OSError) as e:
                last_err = e
                continue
            except Exception as e:
                last_err = e
                continue

        raise RuntimeError(f"所有远程 hosts 源均获取失败：{last_err}" if last_err else "所有远程 hosts 源均获取失败")

    def probe_hosts_url(self, url: str, *, timeout_seconds: float = 10.0) -> bool:
        """快速探测某个 hosts 源是否可用（不抛异常，返回 bool）。

        用途：UI 里的“测试连通性/测试全部”。
        规则：
        - HTTP 200 且内容不是明显的 HTML
        - 内容可解析出至少 1 条 github 相关记录（更贴近真实可用性）
        """
        if not url:
            return False
        try:
            connect_timeout = self.timeout[0] if isinstance(self.timeout, tuple) else 5
            t = (min(float(connect_timeout), float(timeout_seconds)), float(timeout_seconds))
            r = self.session.get(url, timeout=t)
            if r.status_code != 200:
                return False
            txt = r.text or ""
            ctype = (r.headers.get("content-type") or "").lower()
            head = txt[:500].lower()
            if "text/html" in ctype and ("<html" in head or "<!doctype" in head):
                return False
            parsed = self.parse_github_hosts_text(txt, ipv4_only=False, ipv6_only=False)
            return bool(parsed)
        except Exception:
            return False

    def fetch_multiple_urls(
        self,
        urls: List[str],
        *,
        ipv4_only: bool = False,
        ipv6_only: bool = False,
        progress_callback: Optional[Callable[[int, str], None]] = None,
    ) -> Tuple[List[Tuple[str, str]], List[str]]:
        """从多个 URL 同步获取 hosts 并去重（不使用 asyncio）。

        Returns: (records, used_urls)
        """
        if not urls:
            return [], []

        all_records: Dict[Tuple[str, str], Tuple[str, str]] = {}
        used_urls: List[str] = []

        for idx, url in enumerate(urls):
            if progress_callback:
                try:
                    progress_callback(idx + 1, url)
                except Exception:
                    pass
            try:
                r = self.session.get(url, timeout=self.timeout)
                if r.status_code != 200:
                    continue
                txt = r.text or ""
                ctype = (r.headers.get("content-type") or "").lower()
                head = txt[:500].lower()
                if "text/html" in ctype and ("<html" in head or "<!doctype" in head):
                    continue
                parsed = self.parse_github_hosts_text(txt, ipv4_only=ipv4_only, ipv6_only=ipv6_only)
                if not parsed:
                    continue
                for ip, dom in parsed:
                    all_records[(ip, dom)] = (ip, dom)
                used_urls.append(url)
            except Exception:
                continue

        return list(all_records.values()), used_urls

    async def fetch_github_hosts_async(
        self,
        *,
        url_override: Optional[str] = None,
        ipv4_only: bool = False,
        ipv6_only: bool = False,
        concurrent: bool = True,
    ) -> Tuple[List[Tuple[str, str]], str]:
        """异步获取远程 hosts，带缓存。

        Args:
            url_override: 指定单个 URL（单源模式）
            ipv4_only: 仅返回 IPv4 地址
            ipv6_only: 仅返回 IPv6 地址
            concurrent: 是否使用并发获取（自动模式）

        返回：(records, used_url)
        """
        if url_override:
            cached = self._read_cache(url_override)
            if cached:
                records, used_url, _ = cached
                if ipv4_only or ipv6_only:
                    records = [(ip, dom) for ip, dom in records
                               if not ipv4_only or ipaddress.ip_address(ip).version == 4]
                    records = [(ip, dom) for ip, dom in records
                               if not ipv6_only or ipaddress.ip_address(ip).version == 6]
                if records:
                    return records, used_url
            return await self._fetch_single_url_async(url_override, ipv4_only, ipv6_only)

        urls = list(self.urls)
        for url in urls:
            cached = self._read_cache(url)
            if cached:
                records, used_url, _ = cached
                if ipv4_only or ipv6_only:
                    records = [(ip, dom) for ip, dom in records
                               if not ipv4_only or ipaddress.ip_address(ip).version == 4]
                    records = [(ip, dom) for ip, dom in records
                               if not ipv6_only or ipaddress.ip_address(ip).version == 6]
                if records:
                    return records, used_url

        if concurrent:
            return await self._fetch_concurrent_async(ipv4_only, ipv6_only)
        else:
            return await self._fetch_sequential_async(ipv4_only, ipv6_only)

    async def _fetch_single_url_async(
        self,
        url: str,
        ipv4_only: bool,
        ipv6_only: bool,
    ) -> Tuple[List[Tuple[str, str]], str]:
        """异步获取单个 URL 的 hosts 内容。"""
        try:
            parsed = self.parse_github_hosts_text(
                await self._fetch_url_content_async(url),
                ipv4_only=ipv4_only,
                ipv6_only=ipv6_only
            )
            if parsed:
                self._write_cache(url, parsed, url)
                return parsed, url
            raise RuntimeError(f"URL {url} 返回的 hosts 内容为空或无效")
        except Exception as e:
            raise RuntimeError(f"从 {url} 获取 hosts 失败：{e}")

    async def _fetch_sequential_async(
        self,
        ipv4_only: bool,
        ipv6_only: bool,
    ) -> Tuple[List[Tuple[str, str]], str]:
        """顺序获取多个 URL 的 hosts 内容。"""
        urls = list(self.urls)
        last_err: Optional[Exception] = None

        for url in urls:
            try:
                parsed = self.parse_github_hosts_text(
                    await self._fetch_url_content_async(url),
                    ipv4_only=ipv4_only,
                    ipv6_only=ipv6_only
                )
                if parsed:
                    return parsed, url
            except Exception as e:
                last_err = e
                continue

        raise RuntimeError(f"所有远程 hosts 源均获取失败：{last_err}" if last_err else "所有远程 hosts 源均获取失败")

    async def _fetch_concurrent_async(
        self,
        ipv4_only: bool,
        ipv6_only: bool,
    ) -> Tuple[List[Tuple[str, str]], str]:
        """并发获取多个 URL，优先返回第一个成功的结果。"""
        urls = list(self.urls)
        if not urls:
            raise RuntimeError("没有可用的 hosts 源")

        timeout = self.timeout[1] if isinstance(self.timeout, tuple) else 10.0
        tasks = []
        task_map = {}

        for url in urls:
            task = asyncio.create_task(
                self._fetch_url_content_async(url),
                name=f"fetch_{url}"
            )
            tasks.append(task)
            task_map[task] = url

        try:
            done, pending = await asyncio.wait(
                tasks,
                return_when=asyncio.FIRST_COMPLETED,
                timeout=timeout
            )

            for task in done:
                url = task_map[task]
                try:
                    content = task.result()
                    parsed = self.parse_github_hosts_text(content, ipv4_only=ipv4_only, ipv6_only=ipv6_only)
                    if parsed:
                        self._write_cache(url, parsed, url)
                        for p in pending:
                            p.cancel()
                        return parsed, url
                except Exception:
                    continue

            for p in pending:
                p.cancel()

            raise RuntimeError("所有远程 hosts 源均获取失败")

        except asyncio.TimeoutError:
            for task in tasks:
                task.cancel()
            raise RuntimeError(f"获取 hosts 超时（{timeout}秒）")

    async def _fetch_url_content_async(self, url: str, max_retries: int = 3) -> str:
        """异步获取单个 URL 的内容，支持重试机制。"""
        import urllib.parse

        for attempt in range(max_retries):
            try:
                parsed_url = urllib.parse.urlparse(url)
                host = parsed_url.hostname
                port = 443 if parsed_url.scheme == "https" else 80
                path = parsed_url.path or "/"

                ssl_context = ssl.create_default_context()

                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(host, port, ssl=ssl_context if parsed_url.scheme == "https" else None),
                    timeout=self.timeout[1] if isinstance(self.timeout, tuple) else 10.0
                )

                request = f"GET {path} HTTP/1.1\r\nHost: {host}\r\nUser-Agent: {APP_NAME}/1.0\r\nConnection: close\r\n\r\n"
                writer.write(request.encode())
                await writer.drain()

                response_data = await reader.read()
                writer.close()
                await writer.wait_closed()

                response_text = response_data.decode('utf-8', errors='ignore')
                lines = response_text.split('\r\n')
                headers_end = False
                content = []
                is_html = False

                for line in lines:
                    if not headers_end:
                        if line.lower().startswith('content-type:'):
                            if 'text/html' in line.lower():
                                is_html = True
                        if line == '':
                            headers_end = True
                    else:
                        content.append(line)

                txt = '\n'.join(content)

                if is_html and ('<html' in txt[:500].lower() or '<!doctype' in txt[:500].lower()):
                    raise RuntimeError(f"URL {url} 返回的是 HTML 内容而非 hosts 文件")

                return txt

            except (asyncio.TimeoutError, socket.timeout, OSError, ssl.SSLError) as e:
                if attempt < max_retries - 1:
                    await asyncio.sleep(0.5 * (attempt + 1))
                    continue
                raise RuntimeError(f"从 {url} 获取内容失败（重试 {max_retries} 次后）：{e}")
            except Exception as e:
                if attempt < max_retries - 1:
                    await asyncio.sleep(0.5 * (attempt + 1))
                    continue
                raise RuntimeError(f"从 {url} 获取内容失败（重试 {max_retries} 次后）：{e}")

    async def fetch_multiple_urls_async(
        self,
        urls: List[str],
        ipv4_only: bool = False,
        ipv6_only: bool = False,
        progress_callback: Optional[Callable[[int, str], None]] = None,
    ) -> Tuple[List[Tuple[str, str]], List[str]]:
        """从多个URL获取hosts数据并去重。

        Args:
            urls: URL列表
            ipv4_only: 仅返回IPv4
            ipv6_only: 仅返回IPv6
            progress_callback: 进度回调函数，参数为 (current_index, url)

        Returns: (records, used_urls) - 去重后的记录和成功获取的URL列表
        """
        if not urls:
            return [], []

        all_records: Dict[Tuple[str, str], Tuple[str, str]] = {}
        used_urls = []

        for idx, url in enumerate(urls):
            # 调用进度回调
            if progress_callback:
                try:
                    progress_callback(idx + 1, url)
                except Exception:
                    pass
            
            try:
                content = await self._fetch_url_content_async(url)
                parsed = self.parse_github_hosts_text(content, ipv4_only=ipv4_only, ipv6_only=ipv6_only)
                if parsed:
                    for record in parsed:
                        key = (record[0], record[1])
                        all_records[key] = record
                    used_urls.append(url)
            except Exception:
                # 静默忽略失败，让其他源继续
                continue

        return list(all_records.values()), used_urls


# ---------------------------------------------------------------------
# DNS Resolver
# ---------------------------------------------------------------------
class DNSCache:
    """DNS 内存缓存，支持 TTL 自动过期。"""

    def __init__(self, ttl_seconds: int = 300) -> None:
        self._cache: Dict[str, Tuple[List[str], float]] = {}
        self._ttl = ttl_seconds

    def get(self, domain: str) -> Optional[List[str]]:
        domain_lower = domain.lower()
        if domain_lower in self._cache:
            ips, timestamp = self._cache[domain_lower]
            if time.time() - timestamp < self._ttl:
                return ips
            del self._cache[domain_lower]
        return None

    def set(self, domain: str, ips: List[str]) -> None:
        self._cache[domain.lower()] = (ips, time.time())

    def clear(self) -> None:
        self._cache.clear()

    def clean_expired(self) -> int:
        now = time.time()
        expired = [
            d for d, (_, ts) in self._cache.items()
            if now - ts >= self._ttl
        ]
        for d in expired:
            del self._cache[d]
        return len(expired)


class DomainResolver:
    """并发 DNS 解析：输入域名列表，输出 (ip, domain) 列表。"""

    def __init__(self, *, max_workers: Optional[int] = None, dns_cache_ttl: int = 300) -> None:
        if max_workers is None:
            max_workers = DNS_RESOLVER_CONFIG.get("max_workers", 20)
        self.max_workers = max(1, int(max_workers))
        self._cache = DNSCache(ttl_seconds=dns_cache_ttl)

    def resolve(
        self,
        domains: Iterable[str],
        *,
        ipv4_only: bool = False,
        ipv6_only: bool = False,
    ) -> List[Tuple[str, str]]:
        """同步 DNS 解析（IPv4/IPv6），带缓存。"""
        ds = [str(d).strip() for d in domains if str(d).strip()]
        if not ds:
            return []

        cached_domains = []
        domains_to_resolve = []
        for d in ds:
            cached_ips = self._cache.get(d)
            if cached_ips is not None:
                for ip in cached_ips:
                    if ipv4_only and ipaddress.ip_address(ip).version != 4:
                        continue
                    if ipv6_only and ipaddress.ip_address(ip).version != 6:
                        continue
                    cached_domains.append((ip, d))
            else:
                domains_to_resolve.append(d)

        if domains_to_resolve:
            with concurrent.futures.ThreadPoolExecutor(self.max_workers) as ex:
                fmap = {ex.submit(self._resolve_single_domain, d, ipv4_only, ipv6_only): d for d in domains_to_resolve}
                for f in concurrent.futures.as_completed(fmap):
                    dom = fmap.get(f, "")
                    try:
                        ips = f.result()
                        if ips:
                            self._cache.set(dom, ips)
                        for ip in ips:
                            cached_domains.append((ip, dom))
                    except Exception:
                        pass

        return cached_domains

    @staticmethod
    def _resolve_single_domain(domain: str, ipv4_only: bool, ipv6_only: bool) -> List[str]:
        """解析单个域名，返回 IP 列表。"""
        ips = []
        try:
            # getaddrinfo 返回 [(family, type, proto, canonname, sockaddr), ...]
            results = socket.getaddrinfo(domain, None)

            for result in results:
                sockaddr = result[4]

                if sockaddr:
                    ip = sockaddr[0]
                    ip_obj = ipaddress.ip_address(ip)

                    # 根据配置过滤
                    if ipv4_only and ip_obj.version != 4:
                        continue
                    if ipv6_only and ip_obj.version != 6:
                        continue

                    if ip not in ips:
                        ips.append(ip)
        except Exception:
            pass

        return ips

    async def resolve_async(
        self,
        domains: Iterable[str],
        *,
        ipv4_only: bool = False,
        ipv6_only: bool = False,
    ) -> List[Tuple[str, str]]:
        """异步 DNS 解析（IPv4/IPv6），带缓存。"""
        ds = [str(d).strip() for d in domains if str(d).strip()]
        if not ds:
            return []

        cached_results = []
        domains_to_resolve = []
        for d in ds:
            cached_ips = self._cache.get(d)
            if cached_ips is not None:
                for ip in cached_ips:
                    if ipv4_only and ipaddress.ip_address(ip).version != 4:
                        continue
                    if ipv6_only and ipaddress.ip_address(ip).version != 6:
                        continue
                    cached_results.append((ip, d))
            else:
                domains_to_resolve.append(d)

        if domains_to_resolve:
            tasks = [self._resolve_single_domain_async(d, ipv4_only, ipv6_only) for d in domains_to_resolve]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            for dom, ips_result in zip(domains_to_resolve, results):
                if isinstance(ips_result, Exception):
                    continue
                if isinstance(ips_result, list) and ips_result:
                    self._cache.set(dom, ips_result)
                    for ip in ips_result:
                        cached_results.append((ip, dom))

        return cached_results

    @staticmethod
    async def _resolve_single_domain_async(
        domain: str,
        ipv4_only: bool,
        ipv6_only: bool,
    ) -> List[str]:
        """异步解析单个域名，返回 IP 列表。"""
        loop = asyncio.get_event_loop()

        try:
            # 使用 run_in_executor 将同步的 getaddrinfo 放到线程池执行
            results = await loop.run_in_executor(
                None,
                lambda: socket.getaddrinfo(domain, None)
            )

            ips = []
            for result in results:
                sockaddr = result[4]

                if sockaddr:
                    ip = sockaddr[0]
                    ip_obj = ipaddress.ip_address(ip)

                    # 根据配置过滤
                    if ipv4_only and ip_obj.version != 4:
                        continue
                    if ipv6_only and ip_obj.version != 6:
                        continue

                    if ip not in ips:
                        ips.append(ip)

            return ips

        except Exception:
            return []


# ---------------------------------------------------------------------
# Speed Test
# ---------------------------------------------------------------------
class SpeedTester:
    """TCP 延迟测速（多次取中位数）+ 可选 ICMP ping 回退。

    支持：
    - IPv4 和 IPv6
    - 同步和异步测速
    - 批量并发测速

    stop_event/stop_flag 用于外部中断（GUI 点"暂停测速"）。
    """

    def __init__(
        self,
        *,
        icmp_fallback: bool = True,
        stop_event: Optional["threading.Event"] = None,
        stop_flag: Optional[Callable[[], bool]] = None,
    ) -> None:
        self.icmp_fallback = bool(icmp_fallback)
        self.stop_event = stop_event
        self.stop_flag = stop_flag

    def _should_stop(self) -> bool:
        if self.stop_event is not None and self.stop_event.is_set():
            return True
        if self.stop_flag is not None and self.stop_flag():
            return True
        return False

    @staticmethod
    def _get_ip_family(ip: str) -> int:
        """获取 IP 地址的地址族（AF_INET 或 AF_INET6）。"""
        try:
            ip_obj = ipaddress.ip_address(ip)
            return socket.AF_INET6 if ip_obj.version == 6 else socket.AF_INET
        except Exception:
            return socket.AF_INET  # 默认使用 IPv4

    @staticmethod
    def _tcp_connect_rtt_ms(
        ip: str,
        *,
        port: int = 443,
        timeout: float = 2.0,
    ) -> Tuple[Optional[float], Optional[str]]:
        """阻塞式 TCP connect 测 RTT（毫秒），支持 IPv4/IPv6。

        成功返回 (rtt_ms, None)，失败返回 (None, err_str)。
        """
        family = SpeedTester._get_ip_family(ip)
        s = socket.socket(family, socket.SOCK_STREAM)
        try:
            s.settimeout(timeout)
            t0 = time.perf_counter_ns()
            addr = (ip, port, 0, 0) if family == socket.AF_INET6 else (ip, port)
            err = s.connect_ex(addr)
            t1 = time.perf_counter_ns()
            if err != 0:
                return None, f"connect_ex_err:{err}"
            so_err = s.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
            if so_err != 0:
                return None, f"so_error:{so_err}"
            return (t1 - t0) / 1_000_000.0, None
        except socket.timeout:
            return None, "timeout"
        except Exception as e:
            return None, f"err:{e}"
        finally:
            try:
                s.close()
            except Exception:
                pass



    def _normalize_sni_host(self, host: Optional[str]) -> str:
        """规范化 SNI 主机名（提取纯域名）。

        - 去除 scheme / path / query / fragment / 端口
        - 返回小写
        """
        h = (host or "").strip()
        if not h:
            return ""
        # 去 scheme
        h = re.sub(r"^https?://", "", h, flags=re.IGNORECASE)
        # 去 path / query / fragment
        h = h.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
        # 去端口（若用户输入 domain:port）
        # 注意：纯 IPv6 地址包含冒号，不应被当作 domain:port
        if ":" in h and not re.match(r"^[0-9a-fA-F:]+$", h):
            h = h.split(":", 1)[0]
        return h.strip().lower()

    def tls_sni_verify(
        self,
        ip: str,
        host: str,
        *,
        port: int = 443,
        timeout: float = 3.0,
        verify_hostname: bool = True,
    ) -> Tuple[bool, Optional[str]]:
        """对 (ip:port) 执行一次 TLS 握手，并使用 host 作为 SNI/主机名校验。

        用途：避免“TCP 可连但并不是目标域名服务”的假可用 IP（例如证书/主机名不匹配）。
        返回 (ok, err_str)。
        """
        h = self._normalize_sni_host(host)
        if not h:
            return True, None

        try:
            ctx = ssl.create_default_context()
            ctx.check_hostname = bool(verify_hostname)
            ctx.verify_mode = ssl.CERT_REQUIRED if verify_hostname else ssl.CERT_NONE

            family = self._get_ip_family(ip)
            with socket.socket(family, socket.SOCK_STREAM) as sock:
                sock.settimeout(timeout)
                addr = (ip, port, 0, 0) if family == socket.AF_INET6 else (ip, port)
                sock.connect(addr)
                with ctx.wrap_socket(sock, server_hostname=h) as ssock:
                    ssock.settimeout(timeout)
                    ssock.do_handshake()
            return True, None
        except ssl.SSLCertVerificationError as e:
            return False, f"cert_verify:{e}"
        except ssl.SSLError as e:
            return False, f"ssl_error:{e}"
        except socket.timeout:
            return False, "timeout"
        except Exception as e:
            return False, f"err:{e}"


    def tls_sni_verify_any(
        self,
        ip: str,
        hosts: Iterable[str],
        *,
        port: int = 443,
        timeout: float = 3.0,
        verify_hostname: bool = True,
        limit: int = 3,
    ) -> Tuple[bool, Optional[str], Optional[str]]:
        """对同一 IP 依次用多个 host 做 TLS/SNI 验证，任一通过即视为通过。

        返回 (ok, used_host, err_str)：
        - ok=True：used_host 为通过验证的域名
        - ok=False：used_host 为最后一次尝试的域名（若有），err_str 为最后错误
        """
        hs: List[str] = []
        for h in hosts:
            nh = self._normalize_sni_host(h)
            if nh and nh not in hs:
                hs.append(nh)
        if not hs:
            return True, None, None

        lim = max(1, int(limit))
        last_err: Optional[str] = None
        last_host: Optional[str] = None
        for h in hs[:lim]:
            if self._should_stop():
                return False, h, "stopped"
            ok, err = self.tls_sni_verify(ip, h, port=port, timeout=timeout, verify_hostname=verify_hostname)
            last_host = h
            if ok:
                return True, h, None
            last_err = err
        return False, last_host, last_err

    async def tls_sni_verify_async(
        self,
        ip: str,
        host: str,
        *,
        port: int = 443,
        timeout: float = 3.0,
        verify_hostname: bool = True,
    ) -> Tuple[bool, Optional[str]]:
        """异步 TLS/SNI 验证（与 tls_sni_verify 语义一致）。"""
        h = self._normalize_sni_host(host)
        if not h:
            return True, None
        try:
            ctx = ssl.create_default_context()
            ctx.check_hostname = bool(verify_hostname)
            ctx.verify_mode = ssl.CERT_REQUIRED if verify_hostname else ssl.CERT_NONE

            async def _do():
                reader, writer = await asyncio.open_connection(
                    host=ip,
                    port=port,
                    ssl=ctx,
                    server_hostname=h,
                )
                try:
                    writer.close()
                    try:
                        await writer.wait_closed()
                    except Exception:
                        pass
                except Exception:
                    pass
                return True, None

            return await asyncio.wait_for(_do(), timeout=timeout)
        except asyncio.TimeoutError:
            return False, "timeout"
        except ssl.SSLCertVerificationError as e:
            return False, f"cert_verify:{e}"
        except ssl.SSLError as e:
            return False, f"ssl_error:{e}"
        except Exception as e:
            return False, f"err:{e}"

    async def _tcp_connect_rtt_ms_async(
        self,
        ip: str,
        *,
        port: int = 443,
        timeout: float = 2.0,
    ) -> Tuple[Optional[float], Optional[str]]:
        """异步 TCP connect 测 RTT（毫秒），支持 IPv4/IPv6。

        成功返回 (rtt_ms, None)，失败返回 (None, err_str)。
        """
        family = self._get_ip_family(ip)
        try:
            t0 = time.perf_counter_ns()
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(ip, port, family=family),
                timeout=timeout,
            )
            t1 = time.perf_counter_ns()
            try:
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:
                    pass
            except Exception:
                pass

            # 近似 RTT：以连接建立耗时作为 connect RTT
            rtt_ms = (t1 - t0) / 1_000_000.0
            return max(0.1, rtt_ms), None
        except asyncio.TimeoutError:
            return None, "timeout"
        except Exception as e:
            return None, f"err:{e}"

    def tcp_median_rtt_ms(
        self,
        ip: str,
        *,
        port: int = 443,
        attempts: int = 5,
        timeout: float = 2.0,
    ) -> Tuple[Optional[float], bool, Optional[str]]:
        """TCP 多次取中位数（更稳），支持 IPv4/IPv6。返回 (median_ms, ok_bool, last_err)。"""
        lat: List[float] = []
        last_err: Optional[str] = None
        for _ in range(max(1, int(attempts))):
            if self._should_stop():
                break
            rtt, err = self._tcp_connect_rtt_ms(ip, port=port, timeout=timeout)
            last_err = err
            if rtt is not None:
                lat.append(rtt)
            time.sleep(0.01)  # 轻微退避，降低瞬时风暴

        if lat:
            return statistics.median(lat), True, None
        return None, False, last_err

    async def tcp_median_rtt_ms_async(
        self,
        ip: str,
        *,
        port: int = 443,
        attempts: int = 5,
        timeout: float = 2.0,
    ) -> Tuple[Optional[float], bool, Optional[str]]:
        """异步 TCP 多次取中位数，支持 IPv4/IPv6。返回 (median_ms, ok_bool, last_err)。"""
        lat: List[float] = []
        last_err: Optional[str] = None
        for _ in range(max(1, int(attempts))):
            if self._should_stop():
                break
            rtt, err = await self._tcp_connect_rtt_ms_async(ip, port=port, timeout=timeout)
            last_err = err
            if rtt is not None:
                lat.append(rtt)
            await asyncio.sleep(0.01)

        if lat:
            return statistics.median(lat), True, None
        return None, False, last_err

    @staticmethod
    def icmp_ping_once(ip: str, *, timeout_ms: int = 1200) -> Optional[int]:
        """ICMP ping 一次，返回延迟 ms（支持 IPv4/IPv6，Windows 优先）。

        注意：
        - ICMP 可能被禁用，因此仅作为补充参考。
        - 非 Windows 平台可能返回 None。
        """
        if sys.platform != "win32":
            return None

        try:
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        except Exception:
            startupinfo = None

        try:
            p = subprocess.run(
                ["ping", "-n", "1", "-w", str(int(timeout_ms)), ip],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="ignore",
                startupinfo=startupinfo,
            )
            out = (p.stdout or "") + "\n" + (p.stderr or "")
            m = re.search(r"(?:time|时间)[=<]\s*(\d+)\s*ms", out, re.IGNORECASE)
            if m:
                v = int(m.group(1))
                return max(1, v)
            if re.search(r"(?:time|时间)<\s*1\s*ms", out, re.IGNORECASE):
                return 1
        except Exception:
            pass
        return None

    def tcp_advanced_metrics(
        self,
        ip: str,
        *,
        port: int = 443,
        attempts: int = 5,
        timeout: float = 2.0,
    ) -> Dict[str, Any]:
        """返回详细测速指标，包含延迟波动分析，支持 IPv4/IPv6。"""
        latencies = []
        success_count = 0
        last_err: Optional[str] = None

        for _ in range(attempts):
            if self._should_stop():
                break
            rtt, err = self._tcp_connect_rtt_ms(ip, port=port, timeout=timeout)
            last_err = err
            if rtt is not None:
                latencies.append(rtt)
                success_count += 1
            time.sleep(0.02)

        if not latencies:
            return {
                "median": None,
                "mean": None,
                "min": None,
                "max": None,
                "jitter": None,
                "packet_loss": 100.0,
                "stability_score": 0.0,
                "success_rate": 0.0,
                "sample_count": 0,
                "ok": False,
                "err": last_err,
            }

        median_val = statistics.median(latencies)
        mean_val = statistics.mean(latencies)
        jitter = statistics.stdev(latencies) if len(latencies) > 1 else 0.0
        packet_loss = ((attempts - success_count) / attempts) * 100

        stability_score = self._calculate_stability_score(median_val, jitter, packet_loss)

        return {
            "median": median_val,
            "mean": mean_val,
            "min": min(latencies),
            "max": max(latencies),
            "jitter": jitter,
            "packet_loss": packet_loss,
            "stability_score": stability_score,
            "success_rate": (success_count / attempts) * 100,
            "sample_count": len(latencies),
            "ok": True,
            "err": None,
        }

    @staticmethod
    def _calculate_stability_score(median: float, jitter: float, loss: float) -> float:
        """综合评分算法：0-100分"""
        latency_score = max(0, 50 - (median / 10))
        jitter_score = max(0, 30 - (jitter / 3.33))
        loss_score = max(0, 20 - (loss / 5))
        return min(100, latency_score + jitter_score + loss_score)


    def test_one_ip(
        self,
        ip: str,
        *,
        port: int = 443,
        attempts: int = 5,
        timeout: float = 2.0,
        icmp_timeout_ms: int = 2000,
        sni_host: Optional[str] = None,
        sni_hosts: Optional[Iterable[str]] = None,
        tls_verify: Optional[bool] = None,
    ) -> Tuple[str, int, str]:
        """对单个 IP 测速并返回 (ip, ms, status)，支持 IPv4/IPv6。

        新增：TLS/SNI 验证（可选，默认跟随配置开启）
        - 仅在 TCP 可用后做一次 TLS 握手验证（SNI=域名），避免“TCP 可连但并不是目标域名服务”的假可用 IP。
        """
        if self._should_stop():
            return ip, 9999, "已停止"

        # 读取 TLS 配置：EnhancedSpeedTester 有 self.config；否则用全局 SPEED_TEST_CONFIG
        cfg = getattr(self, "config", None)
        base_cfg = cfg if isinstance(cfg, dict) else (SPEED_TEST_CONFIG if isinstance(SPEED_TEST_CONFIG, dict) else {})
        tls_cfg = base_cfg.get("tls", {}) if isinstance(base_cfg, dict) else {}
        tls_enabled = bool(tls_cfg.get("enabled", True)) if tls_verify is None else bool(tls_verify)
        tls_timeout = float(tls_cfg.get("timeout", timeout)) if isinstance(tls_cfg, dict) else timeout
        verify_hostname = bool(tls_cfg.get("verify_hostname", True)) if isinstance(tls_cfg, dict) else True
        tls_strict = bool(tls_cfg.get("strict", False)) if isinstance(tls_cfg, dict) else False
        try_hosts_limit = int(tls_cfg.get("try_hosts_limit", 3)) if isinstance(tls_cfg, dict) else 3

        med, ok, err = self.tcp_median_rtt_ms(ip, port=port, attempts=attempts, timeout=timeout)
        if ok and med is not None:
            ms = max(1, int(med))

            # TLS/SNI 验证：可传入单个 sni_host 或多个 sni_hosts（将依次尝试）
            candidates: List[str] = []
            if sni_hosts:
                try:
                    candidates = list(sni_hosts)
                except Exception:
                    candidates = []
            if (not candidates) and sni_host:
                candidates = [sni_host]

            if tls_enabled and candidates:
                tls_ok, used_host, tls_err = self.tls_sni_verify_any(
                    ip,
                    candidates,
                    port=port,
                    timeout=tls_timeout,
                    verify_hostname=verify_hostname,
                    limit=try_hosts_limit,
                )
                if tls_ok:
                    return ip, ms, "可用(TLS)"
                short = (tls_err or "").split(":", 1)[0] if tls_err else "fail"
                if tls_strict:
                    return ip, 9999, f"失败(SNI:{short})"
                return ip, ms, f"可用(TCP,TLS失败:{short})"

            return ip, ms, "可用"

        if (not ok) and self.icmp_fallback and (not self._should_stop()):
            icmp_ms = self.icmp_ping_once(ip, timeout_ms=icmp_timeout_ms)
            if icmp_ms is not None:
                return ip, icmp_ms, "可用(ICMP)"

        return ip, 9999, "失败"

    def test_one_ip_advanced(
        self,
        ip: str,
        *,
        port: int = 443,
        attempts: int = 5,
        timeout: float = 2.0,
        icmp_timeout_ms: int = 2000,
        measure_jitter: bool = True,
        sni_host: Optional[str] = None,
        sni_hosts: Optional[Iterable[str]] = None,
        tls_verify: Optional[bool] = None,
    ) -> Tuple[str, int, str, Dict[str, Any]]:
        """增强版测速，返回 (ip, ms, status, metrics)，支持 IPv4/IPv6。

        新增：TLS/SNI 验证（可选，默认跟随配置开启）
        """
        if self._should_stop():
            return ip, 9999, "已停止", {}

        # TLS 配置（EnhancedSpeedTester -> self.config；否则全局 SPEED_TEST_CONFIG）
        cfg = getattr(self, "config", None)
        base_cfg = cfg if isinstance(cfg, dict) else (SPEED_TEST_CONFIG if isinstance(SPEED_TEST_CONFIG, dict) else {})
        tls_cfg = base_cfg.get("tls", {}) if isinstance(base_cfg, dict) else {}
        tls_enabled = bool(tls_cfg.get("enabled", True)) if tls_verify is None else bool(tls_verify)
        tls_timeout = float(tls_cfg.get("timeout", timeout)) if isinstance(tls_cfg, dict) else timeout
        verify_hostname = bool(tls_cfg.get("verify_hostname", True)) if isinstance(tls_cfg, dict) else True
        tls_strict = bool(tls_cfg.get("strict", False)) if isinstance(tls_cfg, dict) else False
        try_hosts_limit = int(tls_cfg.get("try_hosts_limit", 3)) if isinstance(tls_cfg, dict) else 3

        if measure_jitter:
            metrics = self.tcp_advanced_metrics(ip, port=port, attempts=attempts, timeout=timeout)
        else:
            med, ok, err = self.tcp_median_rtt_ms(ip, port=port, attempts=attempts, timeout=timeout)
            metrics = {"median": med, "ok": ok, "err": err}

        # TCP 成功
        if isinstance(metrics, dict) and ("ok" not in metrics):
            metrics["ok"] = (metrics.get("median") is not None)

        if metrics.get("ok") and (metrics.get("median") is not None):
            ms = max(1, int(metrics.get("median")))

            # TLS/SNI 验证：可传入单个 sni_host 或多个 sni_hosts（将依次尝试）
            candidates: List[str] = []
            if sni_hosts:
                try:
                    candidates = list(sni_hosts)
                except Exception:
                    candidates = []
            if (not candidates) and sni_host:
                candidates = [sni_host]

            if tls_enabled and candidates:
                tls_ok, used_host, tls_err = self.tls_sni_verify_any(
                    ip,
                    candidates,
                    port=port,
                    timeout=tls_timeout,
                    verify_hostname=verify_hostname,
                    limit=try_hosts_limit,
                )
                metrics["tls_ok"] = bool(tls_ok)
                metrics["tls_used_host"] = used_host
                if not tls_ok:
                    metrics["tls_error"] = tls_err
                    short = (tls_err or "").split(":", 1)[0] if tls_err else "fail"
                    if tls_strict:
                        return ip, 9999, f"失败(SNI:{short})", metrics
                    return ip, ms, f"可用(TCP,TLS失败:{short})", metrics
                return ip, ms, "可用(TLS)", metrics

            return ip, ms, "可用", metrics

        # TCP 失败 -> ICMP 回退
        if self.icmp_fallback and (not self._should_stop()):
            icmp_ms = self.icmp_ping_once(ip, timeout_ms=icmp_timeout_ms)
            if icmp_ms is not None:
                return ip, icmp_ms, "可用(ICMP)", {"median": icmp_ms, "method": "ICMP"}

        return ip, 9999, "失败", metrics if isinstance(metrics, dict) else {}

    async def test_one_ip_advanced_async(
        self,
        ip: str,
        *,
        port: int = 443,
        attempts: int = 5,
        timeout: float = 2.0,
        icmp_timeout_ms: int = 2000,
        measure_jitter: bool = True,
        sni_host: Optional[str] = None,
        sni_hosts: Optional[Iterable[str]] = None,
        tls_verify: Optional[bool] = None,
    ) -> Tuple[str, int, str, Dict[str, Any]]:
        """异步增强版测速，支持 IPv4/IPv6（含 TLS/SNI 验证）。"""
        if self._should_stop():
            return ip, 9999, "已停止", {}

        # TLS 配置（EnhancedSpeedTester -> self.config；否则全局 SPEED_TEST_CONFIG）
        cfg = getattr(self, "config", None)
        base_cfg = cfg if isinstance(cfg, dict) else (SPEED_TEST_CONFIG if isinstance(SPEED_TEST_CONFIG, dict) else {})
        tls_cfg = base_cfg.get("tls", {}) if isinstance(base_cfg, dict) else {}
        tls_enabled = bool(tls_cfg.get("enabled", True)) if tls_verify is None else bool(tls_verify)
        tls_timeout = float(tls_cfg.get("timeout", timeout)) if isinstance(tls_cfg, dict) else timeout
        verify_hostname = bool(tls_cfg.get("verify_hostname", True)) if isinstance(tls_cfg, dict) else True
        tls_strict = bool(tls_cfg.get("strict", False)) if isinstance(tls_cfg, dict) else False
        try_hosts_limit = int(tls_cfg.get("try_hosts_limit", 3)) if isinstance(tls_cfg, dict) else 3

        if measure_jitter:
            # 在线程池中执行同步的高级测速（避免阻塞事件循环）
            loop = asyncio.get_event_loop()
            metrics = await loop.run_in_executor(
                None,
                lambda: self.tcp_advanced_metrics(ip, port=port, attempts=attempts, timeout=timeout),
            )
        else:
            med, ok, err = await self._tcp_connect_rtt_ms_async(ip, port=port, timeout=timeout)
            metrics = {"median": med, "ok": ok, "err": err}

        if isinstance(metrics, dict) and ("ok" not in metrics):
            metrics["ok"] = (metrics.get("median") is not None)

        if metrics.get("median") is not None and metrics.get("ok"):
            ms = max(1, int(metrics.get("median")))

            # TLS/SNI 验证：可传入单个 sni_host 或多个 sni_hosts（将依次尝试）
            candidates: List[str] = []
            if sni_hosts:
                try:
                    candidates = list(sni_hosts)
                except Exception:
                    candidates = []
            if (not candidates) and sni_host:
                candidates = [sni_host]

            if tls_enabled and candidates:
                # 异步路径：逐个尝试，任一通过即视为通过
                used_host: Optional[str] = None
                tls_ok: bool = False
                tls_err: Optional[str] = None
                for h in candidates[:max(1, int(try_hosts_limit))]:
                    tls_ok, tls_err = await self.tls_sni_verify_async(
                        ip, h, port=port, timeout=tls_timeout, verify_hostname=verify_hostname
                    )
                    used_host = h
                    if tls_ok:
                        tls_err = None
                        break

                metrics["tls_ok"] = bool(tls_ok)
                metrics["tls_used_host"] = used_host
                if not tls_ok:
                    metrics["tls_error"] = tls_err
                    short = (tls_err or "").split(":", 1)[0] if tls_err else "fail"
                    if tls_strict:
                        return ip, 9999, f"失败(SNI:{short})", metrics
                    return ip, ms, f"可用(TCP,TLS失败:{short})", metrics
                return ip, ms, "可用(TLS)", metrics

            return ip, ms, "可用", metrics

        if self.icmp_fallback and (not self._should_stop()):
            # ICMP ping 仍为同步（Windows ping 命令），放线程池
            loop = asyncio.get_event_loop()
            icmp_ms = await loop.run_in_executor(None, lambda: self.icmp_ping_once(ip, timeout_ms=icmp_timeout_ms))
            if icmp_ms is not None:
                return ip, icmp_ms, "可用(ICMP)", {"median": icmp_ms, "method": "ICMP"}

        return ip, 9999, "失败", metrics if isinstance(metrics, dict) else {}
class EnhancedSpeedTester(SpeedTester):
    """增强版测速器，支持智能重试和配置化，同时支持同步和异步。"""

    def __init__(
        self,
        *,
        config: Optional[Dict[str, Any]] = None,
        stop_event: Optional["threading.Event"] = None,
        stop_flag: Optional[Callable[[], bool]] = None,
    ) -> None:
        self.config = config or SPEED_TEST_CONFIG.copy()

        icmp_enabled = self.config.get("icmp", {}).get("enabled", True)
        super().__init__(
            icmp_fallback=icmp_enabled,
            stop_event=stop_event,
            stop_flag=stop_flag,
        )

    def test_with_retry(
        self,
        ip: str,
        **kwargs,
    ) -> Tuple[str, int, str, Dict[str, Any]]:
        """带智能重试的测速（同步版本）。"""
        retry_config = self.config.get("retry", {})
        max_retries = retry_config.get("max_retries", 2) if retry_config.get("enabled", True) else 0
        backoff_factor = retry_config.get("backoff_factor", 1.5)

        tcp_config = self.config.get("tcp", {})
        advanced_config = self.config.get("advanced", {})

        port = kwargs.get("port", tcp_config.get("port", 443))
        attempts = kwargs.get("attempts", tcp_config.get("attempts", 5))
        timeout = kwargs.get("timeout", tcp_config.get("timeout", 2.0))
        measure_jitter = advanced_config.get("measure_jitter", True)

        metadata = {
            "attempts": 0,
            "retry_count": 0,
            "errors": [],
        }

        for retry in range(max_retries + 1):
            metadata["attempts"] += 1

            ip_result, ms, status, metrics = self.test_one_ip_advanced(
                ip,
                port=port,
                attempts=attempts,
                timeout=timeout,
                measure_jitter=measure_jitter,
                sni_host=kwargs.get('sni_host'),
                sni_hosts=kwargs.get('sni_hosts'),
                tls_verify=kwargs.get('tls_verify'),
            )

            if status.startswith("可用") or status == "已停止":
                metadata.update(metrics)
                return ip_result, ms, status, metadata

            metadata["errors"].append(f"第{retry+1}次: {status}")
            metadata["retry_count"] += 1

            if retry < max_retries:
                wait_time = backoff_factor ** retry * 0.5
                time.sleep(wait_time)

        return ip, 9999, f"失败(重试{max_retries + 1}次)", metadata

    async def test_with_retry_async(
        self,
        ip: str,
        **kwargs,
    ) -> Tuple[str, int, str, Dict[str, Any]]:
        """带智能重试的测速（异步版本）。"""
        retry_config = self.config.get("retry", {})
        max_retries = retry_config.get("max_retries", 2) if retry_config.get("enabled", True) else 0
        backoff_factor = retry_config.get("backoff_factor", 1.5)

        tcp_config = self.config.get("tcp", {})
        advanced_config = self.config.get("advanced", {})

        port = kwargs.get("port", tcp_config.get("port", 443))
        attempts = kwargs.get("attempts", tcp_config.get("attempts", 5))
        timeout = kwargs.get("timeout", tcp_config.get("timeout", 2.0))
        measure_jitter = advanced_config.get("measure_jitter", True)

        metadata = {
            "attempts": 0,
            "retry_count": 0,
            "errors": [],
        }

        for retry in range(max_retries + 1):
            metadata["attempts"] += 1

            ip_result, ms, status, metrics = await self.test_one_ip_advanced_async(
                ip,
                port=port,
                attempts=attempts,
                timeout=timeout,
                measure_jitter=measure_jitter,
                sni_host=kwargs.get('sni_host'),
                sni_hosts=kwargs.get('sni_hosts'),
                tls_verify=kwargs.get('tls_verify'),
            )

            if status.startswith("可用") or status == "已停止":
                metadata.update(metrics)
                return ip_result, ms, status, metadata

            metadata["errors"].append(f"第{retry+1}次: {status}")
            metadata["retry_count"] += 1

            if retry < max_retries:
                wait_time = backoff_factor ** retry * 0.5
                await asyncio.sleep(wait_time)

        return ip, 9999, f"失败(重试{max_retries + 1}次)", metadata

    async def test_batch_with_retry_async(
        self,
        ips: List[str],
        *,
        concurrent_limit: int = 50,
        **kwargs,
    ) -> List[Tuple[str, int, str, Dict[str, Any]]]:
        """批量异步测速，支持重试和 IPv4/IPv6。

        返回 [(ip, ms, status, metadata), ...]
        """
        semaphore = asyncio.Semaphore(concurrent_limit)

        async def test_with_semaphore(ip: str) -> Tuple[str, int, str, Dict[str, Any]]:
            async with semaphore:
                return await self.test_with_retry_async(ip, **kwargs)

        tasks = [test_with_semaphore(ip) for ip in ips]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        output: List[Tuple[str, int, str, Dict[str, Any]]] = []
        for ip, result in zip(ips, results):
            if isinstance(result, Exception):
                output.append((ip, 9999, "失败", {"errors": [str(result)]}))
            elif isinstance(result, tuple) and len(result) == 4:
                output.append(result)
            else:
                output.append((ip, 9999, "失败", {"errors": ["未知错误"]}))

        return output


class SpeedTestConfigManager:
    """测速配置管理器"""
    
    def __init__(self, app_name: str = APP_NAME):
        self.app_name = app_name
        self.config_dir = os.path.join(
            os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
            app_name,
        )
        self.config_file = os.path.join(self.config_dir, "speedtest_config.json")
    
    def load_config(self) -> Dict[str, Any]:
        """从文件加载配置，如果不存在则返回默认配置"""
        logger = get_logger()
        if os.path.exists(self.config_file):
            try:
                with open(self.config_file, "r", encoding="utf-8") as f:
                    user_config = json.load(f)
                
                config = SPEED_TEST_CONFIG.copy()
                for key, value in user_config.items():
                    if key in config and isinstance(value, dict):
                        config[key].update(value)
                    else:
                        config[key] = value
                
                logger.info(f"成功加载测速配置: {self.config_file}")
                return config
            except (json.JSONDecodeError, OSError, ValueError) as e:
                logger.warning(f"加载配置文件失败: {e}，使用默认配置")
            except Exception as e:
                logger.exception(f"加载配置文件时发生未知错误: {e}，使用默认配置")
        else:
            logger.debug(f"配置文件不存在: {self.config_file}，使用默认配置")
        
        return SPEED_TEST_CONFIG.copy()
    
    def save_config(self, config: Dict[str, Any]) -> bool:
        """保存配置到文件"""
        logger = get_logger()
        try:
            os.makedirs(self.config_dir, exist_ok=True)
            
            with open(self.config_file, "w", encoding="utf-8") as f:
                json.dump(config, f, indent=2, ensure_ascii=False)
            
            logger.info(f"成功保存测速配置: {self.config_file}")
            return True
        except Exception as e:
            logger.error(f"保存配置文件失败: {e}", exc_info=True)
            return False
    
    def reset_to_default(self) -> Dict[str, Any]:
        """重置为默认配置"""
        config = SPEED_TEST_CONFIG.copy()
        self.save_config(config)
        return config
    
    def get_config_path(self) -> str:
        """获取配置文件路径"""
        return self.config_file
