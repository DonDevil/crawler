'''
core/url_frontier.py
'''
#--------------------------------------------------------
'''
add_url() - adds url to the frontier
get_next_url() - gets the next url to crawl
mark_visited() - marks a url as visited
domain_rate_check()  - will be update in future
'''
#--------------------------------------------------------  
import time
import heapq
from collections import defaultdict, deque
from typing import Optional
from urllib.parse import urlparse

from loguru import logger

from utils.url_utils import URLUtils


class URLFrontier:

    def __init__(self, rate_limit: float = 1.0):
        self.visited: set[str] = set()
        self._queued: set[str] = set()
        self.domain_queues: dict[str, deque[str]] = defaultdict(deque)
        self.domain_next_time: dict[str, float] = {}
        self.priority_queue: list[tuple[int, str]] = []
        self.rate_limit = rate_limit

    def add_url(self, url: str, priority: int = 10) -> None:
        """Add a URL to the frontier if it has not been seen before."""

        cleaned = URLUtils.clean_url(url)
        if not cleaned:
            return

        if cleaned in self.visited or cleaned in self._queued:
            return

        domain = urlparse(cleaned).netloc

        self.domain_queues[domain].append(cleaned)
        self._queued.add(cleaned)
        heapq.heappush(self.priority_queue, (priority, domain))

        logger.debug(f"Added to frontier: {cleaned}")

    def get_next_url(self) -> Optional[str]:

        while self.priority_queue:
            priority, domain = heapq.heappop(self.priority_queue)
            next_time = self.domain_next_time.get(domain, 0)

            if time.time() < next_time:
                heapq.heappush(self.priority_queue, (priority, domain))
                continue

            if self.domain_queues[domain]:
                url = self.domain_queues[domain].popleft()
                self.domain_next_time[domain] = time.time() + self.rate_limit
                return url

        return None

    def mark_visited(self, url: str) -> None:
        """Mark a URL as visited so it is not crawled again."""
        cleaned = URLUtils.clean_url(url)
        if not cleaned:
            return

        self.visited.add(cleaned)
        self._queued.discard(cleaned)

    def has_pending(self) -> bool:
        """Return True if there are pending URLs in the frontier."""
        return bool(self.priority_queue)

    def get_next_url(self):

        while self.priority_queue:

            priority, domain = heapq.heappop(self.priority_queue)

            next_time = self.domain_next_time.get(domain, 0)

            if time.time() < next_time:
                heapq.heappush(self.priority_queue, (priority, domain))
                continue

            if self.domain_queues[domain]:

                url = self.domain_queues[domain].popleft()

                self.domain_next_time[domain] = time.time() + self.rate_limit

                return url

        return None

    def mark_visited(self, url):
        self.visited.add(url)


#Usage : 
'''

frontier = URLFrontier() # initialize frontier

frontier.add_url("https://example.com") #add url to frontier queue
frontier.add_url("https://piratebay.site") #add 2nd url to frontier queue

url = frontier.get_next_url() # fetch the next best url to crawl
print(url)
url = frontier.get_next_url() # fetch the next best url to crawl
print(url)
'''


'''
Later Updates :

The basic frontier will later evolve to include:

Redis frontier (for distributed crawling)
    millions of URLs
    multiple crawler machines

Bloom filters
    Fast duplicate detection.

Priority scoring
    torrent pages → high priority
    media files → very high
    random blogs → low

Host-based scheduling
    Prevent crawling same domain too fast.
'''