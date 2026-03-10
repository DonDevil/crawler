
# System Architecture

The crawler is designed using a **modular discovery → crawling → analysis pipeline**.

```text
                    +----------------------+
                    |   Seed URL Sources   |
                    |----------------------|
                    |  piracy sites list   |
                    |  torrent indexes     |
                    |  search engines      |
                    |  dark web indexes    |
                    +----------+-----------+
                               |
                               v
                     +------------------+
                     | Discovery Engine |
                     +--------+---------+
                              |
                              v
                     +------------------+
                     |    URL Frontier  |
                     |------------------|
                     | deduplication    |
                     | prioritization   |
                     | domain politeness|
                     +--------+---------+
                              |
                              v
                     +------------------+
                     |   Scheduler      |
                     +--------+---------+
                              |
                              v
                     +------------------+
                     |  Worker Pool     |
                     | (async crawlers) |
                     +--------+---------+
                              |
                              v
                     +------------------+
                     |  Page Crawlers   |
                     |------------------|
                     | HTTP crawler     |
                     | Tor crawler      |
                     | Playwright       |
                     | Selenium         |
                     +--------+---------+
                              |
                              v
                     +------------------+
                     |   Page Parsers   |
                     |------------------|
                     | link extraction  |
                     | media detection  |
                     | metadata parsing |
                     +--------+---------+
                              |
                              v
                     +------------------+
                     | Data Storage     |
                     |------------------|
                     | URL database     |
                     | domain database  |
                     | crawl state      |
                     +--------+---------+
                              |
                              v
                     +------------------+
                     | Intelligence     |
                     |------------------|
                     | piracy classifier|
                     | domain scoring   |
                     +------------------+
```

---