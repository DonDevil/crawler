"""Tor-aware crawler using httpx and SOCKS proxy routing."""

from __future__ import annotations

import asyncio
from typing import Optional

import httpx
from loguru import logger

from parsers.streaming_manifest_parser import StreamingManifestParser
from storage.url_database import URLDatabase
from tor.proxy_config import get_default_tor_proxy
from utils.request_headers import get_default_headers
from utils.url_utils import URLUtils


class TorCrawler:
	"""Queue-driven crawler that routes onion traffic through Tor."""

	def __init__(
		self,
		frontier,
		parser=None,
		concurrency=25,
		timeout=20,
		max_retries=3,
		max_pages: Optional[int] = None,
		user_agent: Optional[str] = None,
		url_database: Optional[URLDatabase] = None,
		media_database=None,
		use_tor_for_clearweb: bool = False,
	):
		self.frontier = frontier
		self.parser = parser
		self.concurrency = max(1, min(concurrency, max_pages)) if max_pages else max(1, concurrency)
		self.timeout = timeout
		self.max_retries = max_retries
		self.max_pages = max_pages
		self.user_agent = user_agent
		self.url_database = url_database
		self.media_database = media_database
		self._manifest_parser = StreamingManifestParser()
		self.use_tor_for_clearweb = use_tor_for_clearweb

		self.queue = asyncio.Queue()
		self._stop_event = asyncio.Event()
		self._pages_crawled = 0
		self._pages_failed = 0
		self._active_workers = 0

	async def fetch(
		self,
		url: str,
		tor_client: httpx.AsyncClient,
		direct_client: httpx.AsyncClient,
	) -> tuple[Optional[str], Optional[str]]:
		"""Fetch URL and route via Tor when needed."""

		use_tor = URLUtils.is_onion_url(url) or self.use_tor_for_clearweb
		client = tor_client if use_tor else direct_client

		last_error: Optional[str] = None
		for attempt in range(1, self.max_retries + 1):
			try:
				response = await client.get(url)
				if response.status_code != 200:
					last_error = f"HTTP {response.status_code}"
					logger.warning(f"Fetch failed for {url}: {last_error}")

					if response.status_code >= 500 and attempt < self.max_retries:
						await asyncio.sleep(1)
						continue

					return None, last_error

				final_url = str(response.url)
				if URLUtils.is_suspicious_redirect(url, final_url):
					return None, f"Suspicious redirect to {final_url}"

				content_type = response.headers.get("Content-Type", "")
				if content_type and "html" not in content_type.lower() and "xml" not in content_type.lower():
					if self.media_database and (
						URLUtils.looks_like_media_content_type(content_type)
						or URLUtils.is_media_file(final_url)
					):
						body_text = response.text
						asset_id = self.media_database.record_media_link(
							url=final_url,
							source_page=url,
							referrer_url=url,
							discovered_by="tor",
							discovery_method="direct-response",
							media_type=URLUtils.classify_media_url(final_url, content_type),
							mime_type=content_type,
							content_length=int(response.headers.get("Content-Length", "0") or 0) or None,
							priority=5,
						)
						if URLUtils.classify_media_url(final_url, content_type) == "stream-manifest":
							parsed = self._manifest_parser.parse_manifest(body_text, manifest_url=final_url)
							self.media_database.record_manifest_variants(asset_id, parsed.get("variants", []))
						return "", None
					return None, f"Unsupported content type: {content_type}"

				return response.text, None

			except asyncio.CancelledError:
				raise
			except Exception as exc:
				last_error = str(exc)
				logger.warning(f"Tor fetch failed ({attempt}/{self.max_retries}) {url}: {exc}")
				await asyncio.sleep(1)

		return None, last_error or "unknown fetch error"

	async def worker(self, tor_client: httpx.AsyncClient, direct_client: httpx.AsyncClient):
		while not self._stop_event.is_set():
			url = await self.queue.get()
			self._active_workers += 1

			try:
				if not url:
					continue

				if URLUtils.is_blacklisted(url):
					logger.info(f"Skipping blacklisted URL during crawl: {url}")
					self.frontier.mark_visited(url)
					if self.url_database:
						self.url_database.update_status(url, "skipped")
					continue

				if self.url_database:
					self.url_database.add_url(url, status="pending")

				html, failure_reason = await self.fetch(url, tor_client, direct_client)
				status = "visited"

				if html and self.parser:
					parsed_content = (
						self.parser.extract_content(html, url)
						if hasattr(self.parser, "extract_content")
						else {"links": self.parser.extract_links(html, url), "media_links": []}
					)
					links = parsed_content.get("links", set())
					media_links = parsed_content.get("media_links", [])
					for media in media_links:
						if not self.media_database:
							continue
						try:
							self.media_database.record_media_link(
								url=media["url"],
								source_page=url,
								referrer_url=url,
								discovered_by="tor",
								discovery_method=media.get("detection_method", "parser"),
								media_type=media.get("media_type"),
								mime_type=media.get("mime_type"),
								priority=max(0, URLUtils.get_link_priority(url, media["url"]) - 2),
							)
						except Exception as exc:
							logger.debug(f"Skipping media evidence capture for {url}: {exc}")
					for link in links:
						self.frontier.add_url(link, priority=URLUtils.get_link_priority(url, link))
				elif failure_reason:
					status = "failed"
					self._pages_failed += 1
					logger.warning(f"Failed to crawl {url}: {failure_reason}")

				self.frontier.mark_visited(url)
				if self.url_database:
					self.url_database.update_status(url, status)

				self._pages_crawled += 1

				if self.max_pages and self._pages_crawled >= self.max_pages:
					logger.info("Reached max pages limit, stopping crawler")
					self._stop_event.set()

			except asyncio.CancelledError:
				raise
			except Exception as exc:
				logger.error(f"Worker error for {url}: {exc}")
			finally:
				self._active_workers = max(0, self._active_workers - 1)
				self.queue.task_done()

	async def scheduler(self):
		idle_loops = 0
		while not self._stop_event.is_set():
			url = self.frontier.get_next_url()
			if url:
				idle_loops = 0
				await self.queue.put(url)
				continue

			if self.queue.empty() and self._active_workers == 0 and not self.frontier.has_pending():
				idle_loops += 1
				if idle_loops >= 10:
					logger.info("No more URLs to crawl, stopping crawler")
					self._stop_event.set()
					break
			else:
				idle_loops = 0

			await asyncio.sleep(0.5)

	async def run(self):
		headers = get_default_headers(self.user_agent)
		proxy = get_default_tor_proxy()

		async with httpx.AsyncClient(
			timeout=self.timeout,
			follow_redirects=True,
			headers=headers,
			proxy=proxy,
			limits=httpx.Limits(max_connections=self.concurrency),
		) as tor_client, httpx.AsyncClient(
			timeout=self.timeout,
			follow_redirects=True,
			headers=headers,
			limits=httpx.Limits(max_connections=self.concurrency),
		) as direct_client:
			workers = [
				asyncio.create_task(self.worker(tor_client, direct_client))
				for _ in range(self.concurrency)
			]
			scheduler_task = asyncio.create_task(self.scheduler())

			try:
				await self._stop_event.wait()
			finally:
				scheduler_task.cancel()
				for task in workers:
					task.cancel()

				await asyncio.gather(scheduler_task, *workers, return_exceptions=True)

				logger.info(
					"Tor crawler finished: processed={} failed={} pending_frontier={}",
					self._pages_crawled,
					self._pages_failed,
					self.frontier.pending_count(),
				)
