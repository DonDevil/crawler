# Anti Piracy Web Crawler

## Overview

The **Anti Piracy Web Crawler** is a modular large-scale discovery engine designed to identify potential piracy sources across the **surface web and dark web**.

It continuously discovers websites, crawls pages, extracts links, and builds a database of domains that may host or distribute pirated content.

This crawler is part of a broader **anti-piracy research system** that will later integrate:

* Image fingerprinting
* Video fingerprinting
* Audio fingerprinting
* Automated piracy detection

At its current stage, the project focuses on **large-scale web discovery and crawling infrastructure**.

---

## Project Structure

```text
crawler/

├── main.py
├── config.yaml
├── requirements.txt

├── core/
│   ├── crawler_manager.py
│   ├── scheduler.py
│   ├── url_frontier.py
│   ├── worker_pool.py
│   └── rate_limiter.py

├── discovery/
│   ├── search_engine_discovery.py
│   ├── piracy_site_seeds.py
│   ├── darkweb_discovery.py
│   ├── torrent_site_discovery.py
│   └── domain_expander.py

├── search_engines/
│   ├── duckduckgo_search.py
│   ├── bing_search.py
│   ├── brave_search.py
│   ├── yandex_search.py
│   ├── ahmia_search.py
│   ├── torch_search.py
│   └── custom_query_generator.py

├── crawler/
│   ├── http_crawler.py
│   ├── tor_crawler.py
│   ├── playwright_crawler.py
│   ├── selenium_crawler.py
│   └── async_crawler.py

├── parsers/
│   ├── html_link_extractor.py
│   ├── page_metadata_parser.py
│   ├── media_link_detector.py
│   └── javascript_link_extractor.py

├── tor/
│   ├── tor_manager.py
│   ├── proxy_config.py
│   └── onion_router.py

├── storage/
│   ├── url_database.py
│   ├── crawl_state_db.py
│   ├── domain_database.py
│   └── result_exporter.py

├── intelligence/
│   ├── piracy_domain_classifier.py
│   ├── domain_reputation.py
│   └── duplicate_url_filter.py

├── utils/
│   ├── logger.py
│   ├── url_utils.py
│   ├── request_headers.py
│   └── retry_handler.py

├── seeds/
│   ├── piracy_sites.txt
│   ├── torrent_sites.txt
│   ├── streaming_sites.txt
│   ├── file_hosts.txt
│   └── darkweb_seeds.txt

├── datasets/
│   ├── known_pirate_domains.txt
│   └── domain_blacklist.txt

├── tests/
│   ├── crawler_test.py
│   ├── parser_test.py
│   └── discovery_test.py

└── docs/
    ├── architecture.md
    ├── crawler_flow.md
    └── modules.md
```

---

## System Requirements

### Operating System

Recommended:

* **Ubuntu 24.04 LTS**

### Python

```
Python 3.12+
```

### Required Services

Install required services:

```bash
sudo apt install tor redis-server postgresql chromium-browser
```

These services support:

* **Tor** → dark web crawling
* **Redis** → crawler queue / scheduling
* **PostgreSQL** → persistent crawler data

---

## Installation

Clone the repository:

```bash
git clone https://github.com/DonDevil/crawler.git
cd crawler
```

Create a Python environment:

```bash
python3 -m venv env
```

Activate environment:

```bash
source env/bin/activate
```

Install dependencies:

```bash
pip install -r requirements.txt
```

Install browser engines for Playwright:

```bash
playwright install
```

---

## Running the Crawler

Run the main crawler:

```bash
python main.py
```

The system will:

1. Load seed URLs
2. Discover domains from search engines
3. Add URLs to the frontier
4. Crawl pages asynchronously
5. Extract links for further discovery

Search discovery is configurable through `config.yaml`. The crawler now supports DuckDuckGo, Bing, Brave, Yandex, Ahmia, and Torch search adapters, but some engines may still return no results at runtime when they require captcha verification, JavaScript-only flows, or a reachable Tor proxy.

---

## Core Components

### URL Frontier

Controls:

* URL deduplication
* crawl scheduling
* domain politeness rules
* crawl priority

### Discovery Engine

Discovers domains from:

* search engines
* torrent indexes
* piracy site seeds
* dark web search engines

### Crawlers

Multiple crawling strategies:

* HTTP crawler
* Async crawler
* Tor crawler
* Playwright crawler
* Selenium crawler

### Parsers

Extract information from pages:

* internal links
* external links
* media URLs
* metadata

---

## Future Development

Planned enhancements include:

* Image piracy detection
* Video fingerprint matching
* Audio fingerprinting
* Distributed crawler nodes
* AI-based piracy classification
* Automated evidence generation

---

## Legal Notice

This project is intended for **research and anti-piracy investigation purposes only**.

Users must ensure all crawling activities comply with:

* applicable laws
* website terms of service
* ethical research guidelines

---

## License

MIT License
