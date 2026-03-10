import asyncio
from core.url_frontier import URLFrontier
from crawler.async_crawler import AsyncCrawler

frontier = URLFrontier()

frontier.add_url("https://saec.ac.in")

crawler = AsyncCrawler(frontier, concurrency=100)

asyncio.run(crawler.run())