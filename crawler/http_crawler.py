"""HTTP crawler using httpx AsyncClient for clear-web crawling."""

from __future__ import annotations

import asyncio
from typing import Optional

import httpx
from loguru import logger

from core.claim_heartbeat import ClaimLostError, resolve_heartbeat_interval, run_with_heartbeat
from core.frontier import Frontier, FrontierClaim, FrontierUnavailable
from core.frontier_executor import AsyncFrontier
from core.media_evidence_executor import AsyncMediaEvidence
from core.url_frontier import URLFrontier
from parsers.streaming_manifest_parser import StreamingManifestParser
from storage.url_database import URLDatabase
from utils.request_headers import get_default_headers
from utils.url_utils import URLUtils


class HTTPCrawler:
	"""Queue-driven crawler that fetches pages using httpx."""

	def __init__(
		self,
		frontier: Frontier,
		parser=None,
		concurrency=25,
		timeout=15,
		max_retries=3,
		max_pages: Optional[int] = None,
		user_agent: Optional[str] = None,
		url_database: Optional[URLDatabase] = None,
		media_database=None,
		heartbeat_interval: Optional[float] = None,
	):
		self.frontier = AsyncFrontier(frontier)
		self.parser = parser
		self.concurrency = max(1, min(concurrency, max_pages)) if max_pages else max(1, concurrency)
		self.timeout = timeout
		self.max_retries = max_retries
		self.max_pages = max_pages
		self.heartbeat_interval = resolve_heartbeat_interval(
			heartbeat_interval, getattr(getattr(frontier, "raw", frontier), "lease_ttl", None)
		)
		self.user_agent = user_agent
		self.url_database = url_database
		# See docs/architecture/redis-sqlite-boundary-decision.md: mirroring
		# status into url_database only applies to the local frontier.
		self._sql_mode_mirror = url_database is not None and isinstance(self.frontier.raw, URLFrontier)
		# Non-blocking boundary for Media Evidence writes -- see
		# core/media_evidence_executor.py and
		# docs/architecture/fetch-extractor-audit.md §8/§14.
		self.media_database = AsyncMediaEvidence(media_database) if media_database is not None else None
		self._manifest_parser = StreamingManifestParser()

		self.queue: asyncio.Queue[FrontierClaim] = asyncio.Queue()
		self._stop_event = asyncio.Event()
		self._pages_crawled = 0
		self._pages_failed = 0
		self._active_workers = 0

	async def fetch(self, client: httpx.AsyncClient, url: str) -> tuple[Optional[str], Optional[str]]:
		"""Fetch a URL and return body/error tuple."""

		if URLUtils.is_onion_url(url):
			return None, "onion URL requires Tor crawler"

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
						asset_id = await self.media_database.record_media_link(
							url=final_url,
							source_page=url,
							referrer_url=url,
							discovered_by="http",
							discovery_method="direct-response",
							media_type=URLUtils.classify_media_url(final_url, content_type),
							mime_type=content_type,
							content_length=int(response.headers.get("Content-Length", "0") or 0) or None,
							priority=6,
						)
						if URLUtils.classify_media_url(final_url, content_type) == "stream-manifest":
							parsed = self._manifest_parser.parse_manifest(body_text, manifest_url=final_url)
							await self.media_database.record_manifest_variants(asset_id, parsed.get("variants", []))
						return "", None
					return None, f"Unsupported content type: {content_type}"

				return response.text, None

			except asyncio.CancelledError:
				raise
			except Exception as exc:
				last_error = str(exc)
				logger.warning(f"Fetch failed ({attempt}/{self.max_retries}) {url}: {exc}")
				await asyncio.sleep(1)

		return None, last_error or "unknown fetch error"

	async def worker(self, client: httpx.AsyncClient):
		while not self._stop_event.is_set():
			claim = await self.queue.get()
			self._active_workers += 1
			url = claim.url if claim else None

			try:
				if claim is None:
					continue

				if URLUtils.is_blacklisted(url):
					logger.info(f"Skipping blacklisted URL during crawl: {url}")
					await self.frontier.mark_skipped(claim)
					if self._sql_mode_mirror:
						self.url_database.update_status(url, "skipped")
					continue

				if self._sql_mode_mirror:
					self.url_database.add_url(url, status="pending")

				(html, failure_reason), claim = await run_with_heartbeat(
					self.frontier, claim, self.fetch(client, url), self.heartbeat_interval
				)
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
							await self.media_database.record_media_link(
								url=media["url"],
								source_page=url,
								referrer_url=url,
								discovered_by="http",
								discovery_method=media.get("detection_method", "parser"),
								media_type=media.get("media_type"),
								mime_type=media.get("mime_type"),
								priority=max(0, URLUtils.get_link_priority(url, media["url"]) - 2),
							)
						except Exception as exc:
							logger.debug(f"Skipping media evidence capture for {url}: {exc}")
					for link in links:
						await self.frontier.add_url(link, priority=URLUtils.get_link_priority(url, link))
				elif failure_reason:
					status = "failed"
					self._pages_failed += 1
					logger.warning(f"Failed to crawl {url}: {failure_reason}")

				if status == "failed":
					await self.frontier.mark_failed(claim, failure_reason or "")
				else:
					await self.frontier.mark_visited(claim)
				if self._sql_mode_mirror:
					self.url_database.update_status(url, status)

				self._pages_crawled += 1

				if self.max_pages and self._pages_crawled >= self.max_pages:
					logger.info("Reached max pages limit, stopping crawler")
					self._stop_event.set()

			except asyncio.CancelledError:
				if claim is not None:
					try:
						await self.frontier.mark_failed(claim, "worker cancelled")
					except FrontierUnavailable as e:
						logger.warning(
							f"Frontier unavailable while abandoning cancelled claim for {url}: {e}"
						)
				raise
			except ClaimLostError:
				logger.warning(
					f"Claim lost for {url}: lease was reclaimed before this worker "
					"finished (crashed-worker recovery or another owner); abandoning "
					"without marking completion"
				)
			except FrontierUnavailable as e:
				logger.error(
					f"Frontier unavailable while processing {url}: {e}; abandoning "
					"without marking completion (lease-based recovery will retry once "
					"the frontier is reachable again)"
				)
			except Exception as exc:
				logger.error(f"Worker error for {url}: {exc}")
				if claim is not None:
					try:
						await self.frontier.mark_failed(claim, str(exc))
					except FrontierUnavailable as unavailable:
						logger.error(
							f"Frontier unavailable while recording failure for {url}: "
							f"{unavailable}; abandoning without marking completion"
						)
			finally:
				self._active_workers = max(0, self._active_workers - 1)
				self.queue.task_done()

	async def scheduler(self):
		idle_loops = 0

		while not self._stop_event.is_set():
			try:
				claim = await self.frontier.get_next_url()
			except FrontierUnavailable as e:
				logger.error(f"Frontier unavailable, will retry: {e}")
				claim = None

			if claim:
				idle_loops = 0
				await self.queue.put(claim)
				continue

			try:
				if self.queue.empty() and self._active_workers == 0 and not await self.frontier.has_pending():
					idle_loops += 1
					if idle_loops >= 10:
						logger.info("No more URLs to crawl, stopping crawler")
						self._stop_event.set()
						break
				else:
					idle_loops = 0
			except FrontierUnavailable as e:
				logger.error(f"Frontier unavailable while checking pending state, will retry: {e}")
				idle_loops = 0

			await asyncio.sleep(0.5)

	async def run(self):
		headers = get_default_headers(self.user_agent)

		async with httpx.AsyncClient(
			timeout=self.timeout,
			follow_redirects=True,
			headers=headers,
			limits=httpx.Limits(max_connections=self.concurrency),
		) as client:
			workers = [
				asyncio.create_task(self.worker(client))
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
					"HTTP crawler finished: processed={} failed={} pending_frontier={}",
					self._pages_crawled,
					self._pages_failed,
					await self.frontier.pending_count(),
				)
