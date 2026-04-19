"""Browser-rendering crawler using Selenium WebDriver."""

from __future__ import annotations

import asyncio
import shutil
from typing import Optional

from loguru import logger

from storage.url_database import URLDatabase
from utils.url_utils import URLUtils

try:
	from selenium import webdriver
	from selenium.common.exceptions import WebDriverException
	from selenium.webdriver.chrome.options import Options
except Exception:  # pragma: no cover
	webdriver = None
	Options = None
	WebDriverException = Exception


class SeleniumCrawler:
	"""Queue-driven crawler for JS-heavy pages using Selenium."""

	def __init__(
		self,
		frontier,
		parser=None,
		concurrency=2,
		timeout=30,
		max_retries=2,
		max_pages: Optional[int] = None,
		user_agent: Optional[str] = None,
		url_database: Optional[URLDatabase] = None,
		media_database=None,
	):
		self.frontier = frontier
		self.parser = parser
		self.concurrency = max(1, min(concurrency, 4, max_pages)) if max_pages else max(1, min(concurrency, 4))
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

	def _make_driver(self):
		if webdriver is None or Options is None:
			raise RuntimeError("selenium is not installed")

		options = Options()
		chrome_binary = (
			shutil.which("google-chrome")
			or shutil.which("chromium")
			or shutil.which("chromium-browser")
		)
		if chrome_binary:
			options.binary_location = chrome_binary

		prefs = {
			"profile.default_content_setting_values.notifications": 2,
			"profile.default_content_settings.popups": 0,
			"profile.managed_default_content_settings.images": 2,
		}
		options.add_experimental_option("prefs", prefs)

		for arg in (
			"--headless=new",
			"--disable-gpu",
			"--no-sandbox",
			"--disable-setuid-sandbox",
			"--disable-dev-shm-usage",
			"--disable-software-rasterizer",
			"--disable-extensions",
			"--disable-notifications",
			"--no-first-run",
			"--no-default-browser-check",
			"--remote-debugging-port=9222",
			"--remote-debugging-address=127.0.0.1",
			"--window-size=1280,720",
		):
			options.add_argument(arg)
		if self.user_agent:
			options.add_argument(f"--user-agent={self.user_agent}")

		driver = webdriver.Chrome(options=options)
		driver.set_page_load_timeout(self.timeout)
		return driver

	def _fetch_sync(self, url: str) -> tuple[Optional[str], Optional[str]]:
		driver = None
		try:
			driver = self._make_driver()
			driver.get(url)
			final_url = driver.current_url or url
			if URLUtils.is_suspicious_redirect(url, final_url):
				return None, f"Suspicious redirect to {final_url}"
			html = driver.page_source
			return html, None
		except WebDriverException as exc:
			return None, f"WebDriver error: {exc}"
		except Exception as exc:
			return None, str(exc)
		finally:
			if driver is not None:
				try:
					driver.quit()
				except Exception:
					pass

	async def fetch(self, url: str) -> tuple[Optional[str], Optional[str]]:
		last_error: Optional[str] = None

		for attempt in range(1, self.max_retries + 1):
			html, error = await asyncio.to_thread(self._fetch_sync, url)
			if html:
				return html, None

			last_error = error or "unknown fetch error"
			logger.warning(f"Selenium fetch failed ({attempt}/{self.max_retries}) {url}: {last_error}")
			await asyncio.sleep(1)

		return None, last_error

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
								discovered_by="selenium",
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

			logger.info(
				"Selenium crawler finished: processed={} failed={} pending_frontier={}",
				self._pages_crawled,
				self._pages_failed,
				self.frontier.pending_count(),
			)
