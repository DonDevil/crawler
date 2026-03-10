import asyncio
import aiohttp
from loguru import logger


class AsyncCrawler:

    def __init__(
        self,
        frontier,
        parser=None,
        concurrency=50,
        timeout=15,
        max_retries=3,
    ):

        self.frontier = frontier
        self.parser = parser
        self.concurrency = concurrency
        self.timeout = timeout
        self.max_retries = max_retries

        self.queue = asyncio.Queue()

    async def fetch(self, session, url):

        for attempt in range(self.max_retries):

            try:

                async with session.get(url, timeout=self.timeout) as response:

                    if response.status != 200:
                        logger.warning(f"{url} returned {response.status}")
                        return None

                    html = await response.text()
                    return html

            except Exception as e:

                logger.warning(f"Fetch failed ({attempt+1}) {url} : {e}")

                await asyncio.sleep(1)

        logger.error(f"Giving up on {url}")
        return None

    async def worker(self, session):

        while True:

            url = await self.queue.get()

            try:

                html = await self.fetch(session, url)

                if html and self.parser:

                    new_links = self.parser.extract_links(html, url)

                    logger.info(f"{len(new_links)} links extracted from {url}")

                    for link in new_links:

                        self.frontier.add_url(link)

                self.frontier.mark_visited(url)

                logger.info(f"Crawled: {url}")

            except Exception as e:

                logger.error(f"Worker error for {url}: {e}")

            finally:

                self.queue.task_done()

    async def scheduler(self):

        while True:

            url = self.frontier.get_next_url()

            if url:
                await self.queue.put(url)

            else:
                await asyncio.sleep(1)

    async def run(self):

        connector = aiohttp.TCPConnector(limit=self.concurrency)

        async with aiohttp.ClientSession(connector=connector) as session:

            workers = [
                asyncio.create_task(self.worker(session))
                for _ in range(self.concurrency)
            ]

            scheduler_task = asyncio.create_task(self.scheduler())

            await asyncio.gather(scheduler_task, *workers)