"""Browser-rendering crawler using Playwright."""

from __future__ import annotations

import asyncio
from typing import Optional

from loguru import logger

from storage.url_database import URLDatabase

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
	):
		self.frontier = frontier
		self.parser = parser
		self.concurrency = max(1, min(concurrency, 8))
		self.timeout = timeout
		self.max_retries = max_retries
		self.max_pages = max_pages
		self.user_agent = user_agent
		self.url_database = url_database

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
			],
		)

	async def _stop_browser(self) -> None:
		if self._browser is not None:
			await self._browser.close()
			self._browser = None
		if self._playwright is not None:
			await self._playwright.stop()
			self._playwright = None

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
				page = await context.new_page()
				response = await page.goto(url, wait_until="domcontentloaded", timeout=self.timeout * 1000)
				await page.wait_for_load_state("networkidle", timeout=self.timeout * 1000)
				html = await page.content()

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

				if self.url_database:
					self.url_database.add_url(url, status="pending")

				html, failure_reason = await self.fetch(url)
				status = "visited"

				if html and self.parser:
					links = self.parser.extract_links(html, url)
					for link in links:
						self.frontier.add_url(link)
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
