'''
core/url_frontier.py
'''


'''
add_url() - adds url to the frontier
get_next_url() - gets the next url to crawl
mark_visited() - marks a url as visited
domain_rate_check()  - will be update in future
'''

import time
import heapq
from collections import defaultdict, deque
from urllib.parse import urlparse


class URLFrontier:

    def __init__(self, rate_limit=1.0):
        self.visited = set()
        self.domain_queues = defaultdict(deque)
        self.domain_next_time = {}
        self.priority_queue = []
        self.rate_limit = rate_limit

    def add_url(self, url, priority=10):

        if url in self.visited:
            return

        domain = urlparse(url).netloc
        self.domain_queues[domain].append(url)

        heapq.heappush(self.priority_queue, (priority, domain))

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