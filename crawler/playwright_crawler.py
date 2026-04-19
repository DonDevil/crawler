"""Browser-rendering crawler using Playwright."""

from __future__ import annotations

import asyncio
from typing import Optional

from loguru import logger

from storage.url_database import URLDatabase
from utils.url_utils import URLUtils

try:
	from playwright.async_api import Error as PlaywrightError
	from playwright.async_api import async_playwright
except Exception:  # pragma: no cover
	async_playwright = None
	PlaywrightError = Exception


class PlaywrightCrawler:
	"""Queue-driven crawler for JS-heavy pages using Playwright."""

	def __init__(
		self,
		frontier,
		parser=None,
		concurrency=3,
		timeout=30,
		max_retries=2,
		max_pages: Optional[int] = None,
		user_agent: Optional[str] = None,
		url_database: Optional[URLDatabase] = None,
		media_database=None,
	):
		self.frontier = frontier
		self.parser = parser
		self.concurrency = max(1, min(concurrency, 8, max_pages)) if max_pages else max(1, min(concurrency, 8))
		self.timeout = timeout
		self.max_retries = max_retries
		self.max_pages = max_pages
		self.user_agent = user_agent
		self.url_database = url_database
		self.media_database = media_database

		self.queue = asyncio.Queue()
		self._stop_event = asyncio.Event()
		self._pages_crawled = 0
		self._pages_failed = 0
		self._active_workers = 0

		self._playwright = None
		self._browser = None

	async def _start_browser(self) -> None:
		if async_playwright is None:
			raise RuntimeError("playwright is not installed")

		self._playwright = await async_playwright().start()
		self._browser = await self._playwright.chromium.launch(
			headless=True,
			args=[
				"--no-sandbox",
				"--disable-setuid-sandbox",
				"--disable-dev-shm-usage",
				"--disable-gpu",
				"--disable-notifications",
			],
		)

	async def _stop_browser(self) -> None:
		if self._browser is not None:
			await self._browser.close()
			self._browser = None
		if self._playwright is not None:
			await self._playwright.stop()
			self._playwright = None

	async def _route_request(self, route) -> None:
		request = route.request
		if request.resource_type == "media":
			if self.media_database:
				try:
					self.media_database.record_media_link(
						url=request.url,
						source_page=getattr(request.frame, "url", "") or request.url,
						referrer_url=getattr(request.frame, "url", "") or request.url,
						discovered_by="playwright",
						discovery_method="network-request",
						media_type=URLUtils.classify_media_url(request.url),
						priority=6,
					)
				except Exception as exc:
					logger.debug(f"Skipping media network capture for {request.url}: {exc}")
			await route.abort()
			return
		if request.resource_type in {"image", "font", "beacon"}:
			await route.abort()
			return
		if URLUtils.is_probable_ad_domain(request.url) or URLUtils.is_blacklisted(request.url):
			await route.abort()
			return
		await route.continue_()

	async def fetch(self, url: str) -> tuple[Optional[str], Optional[str]]:
		"""Fetch and render a page using Playwright."""

		if self._browser is None:
			return None, "Playwright browser is not initialized"

		last_error: Optional[str] = None

		for attempt in range(1, self.max_retries + 1):
			context = None
			page = None
			try:
				context = await self._browser.new_context(user_agent=self.user_agent)
				await context.route("**/*", self._route_request)
				page = await context.new_page()
				page.on("popup", lambda popup: asyncio.create_task(popup.close()))
				response = await page.goto(url, wait_until="domcontentloaded", timeout=self.timeout * 1000)
				await page.wait_for_load_state("networkidle", timeout=self.timeout * 1000)
				html = await page.content()
				final_url = page.url or url

				if URLUtils.is_suspicious_redirect(url, final_url):
					return None, f"Suspicious redirect to {final_url}"

				if response is not None and response.status >= 400:
					return None, f"HTTP {response.status}"

				return html, None

			except asyncio.CancelledError:
				raise
			except PlaywrightError as exc:
				last_error = str(exc)
				logger.warning(f"Playwright fetch failed ({attempt}/{self.max_retries}) {url}: {exc}")
				await asyncio.sleep(1)
			except Exception as exc:
				last_error = str(exc)
				logger.warning(f"Playwright fetch failed ({attempt}/{self.max_retries}) {url}: {exc}")
				await asyncio.sleep(1)
			finally:
				if page is not None:
					await page.close()
				if context is not None:
					await context.close()

		return None, last_error or "unknown fetch error"

	async def worker(self):
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

				html, failure_reason = await self.fetch(url)
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
								discovered_by="playwright",
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
		await self._start_browser()
		workers = [
			asyncio.create_task(self.worker())
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
			await self._stop_browser()

			logger.info(
				"Playwright crawler finished: processed={} failed={} pending_frontier={}",
				self._pages_crawled,
				self._pages_failed,
				self.frontier.pending_count(),
			)
