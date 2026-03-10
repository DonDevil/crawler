'''test run for separate module development'''

import asyncio

from core.url_frontier import URLFrontier
from crawler.async_crawler import AsyncCrawler
from parsers.html_link_extractor import HTMLLinkExtractor


async def main():

    frontier = URLFrontier()

    parser = HTMLLinkExtractor()

    seed_url = "https://saec.ac.in"

    frontier.add_url(seed_url)

    crawler = AsyncCrawler(
        frontier=frontier,
        parser=parser,
        concurrency=20
    )

    await crawler.run()


if __name__ == "__main__":

    asyncio.run(main())