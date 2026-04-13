# Claude (Ollama) Agent Instructions - Anti-Piracy Crawler

## 🧠 Purpose
This repository implements a **modular anti‑piracy web crawler** focusing on **discovery → crawling → parsing**. The goal of the Claude agent is to help maintain, extend, and troubleshoot this codebase by: 

- Understanding the current implemented portions (crawler + URL frontier + HTML link extraction)
- Identifying missing/placeholder modules (many files are stubs)
- Producing precise modifications (diffs, file paths, and runnable commands)

> ⚠️ Note: This repo contains a Python virtual environment (`env/`) and datasets (`datasets/`). **Do not modify or commit changes to `env/` or `datasets/`.**

---

## 🧩 Repository Overview (Key Components)

### ✅ Implemented (Working) Code
- **`main.py`** – entry point that runs the crawler.
- **`core/url_frontier.py`** – priority-based URL frontier with rate-limiting.
- **`crawler/async_crawler.py`** – async crawler that fetches pages, extracts links, and adds them back to the frontier.
- **`parsers/html_link_extractor.py`** – extracts `<a href>` links and normalizes URLs using `utils/url_utils.py`.
- **`utils/url_utils.py`** – URL normalization/validation and basic crawler-trap detection.

### 🧪 Tests (Minimal / Not Automated)
- `tests/crawler_test.py` – runs the crawler against a single seed URL.
- `tests/parser_test.py` – manual link extraction demo.
- `tests/discovery_test.py` – empty placeholder.

### 🧱 Stub / Placeholder Modules (Empty Files)
Many modules exist as scaffolding but are not implemented. For example:
- `core/scheduler.py`, `core/worker_pool.py`, `core/rate_limiter.py`
- `crawler/http_crawler.py`, `crawler/tor_crawler.py`, `crawler/playwright_crawler.py`, `crawler/selenium_crawler.py`
- `discovery/*`, `search_engines/*`, `storage/*`, `intelligence/*`, `tor/*`, `utils/*` (some are empty)

---

## 🧪 How to Run the Project (Local Development)

1. **Activate the virtualenv**

```bash
source env/bin/activate
```

2. **Install dependencies (if not already installed)**

```bash
pip install -r requirements.txt
```

3. **Run the crawler (quick smoke test)**

```bash
python main.py
```

> Note: The current crawler has no stopping condition; it will continue fetching until manually stopped.

---

## 🧠 Claude-Agent / Ollama Usage (Recommended)

### 1) Base command (interactive, for code reviews / fixes)

```bash
ollama run claude --prompt "@CLAUDE.md"
```

### 2) If you want to provide a task prompt alongside the repository rules

```bash
ollama run claude --prompt "@CLAUDE.md\n\n### Task:\n<your task here>"
```

### 3) Useful flags (optional)

- `--temperature 0` → deterministic recommendations
- `--max-tokens 2048` → allow longer responses

---

## 🧩 Agent Rules (Must-Follow)

1. **Do not touch**:
   - `env/` (virtual environment)
   - `datasets/` (large dataset files)
   - `__pycache__/` or `.pyc` files
   - `.git/` or any Git metadata

2. **When suggesting code changes**:
   - Always reference the exact file path(s) (e.g., `crawler/async_crawler.py`).
   - Provide a patch-style diff or a clear code block. 
   - Make sure changes are runnable in this repo (Python 3.12, uses `requirements.txt`).
   - Prefer small incremental changes with clear behavior.

3. **Testing & Validation**:
   - If you add or modify logic, also propose a minimal test in `tests/` (pytest-style) or a runnable script (e.g., `python -m pytest tests/crawler_test.py`).

4. **What to prioritize**:
   - Make the existing crawler stable (stop condition, error handling, rate limiting).
   - Improve URL filtering (avoid traps and non-HTML assets).
   - Add clear `README` / `docs/` instructions for any new behavior.

5. **If asked to implement new features** (e.g., “add Tor crawling”):
   - Use existing module scaffolds (`crawler/tor_crawler.py`, `tor/tor_manager.py`) and implement minimal working code.
   - Keep external dependencies minimal and document additions to `requirements.txt`.

---

## 🗒️ Quick “State of the Code” Notes

- **Most modules are placeholders**; actual crawler behavior lives in `core/url_frontier.py` + `crawler/async_crawler.py`.
- **No persistent storage is currently used**; there is no database integration.
- **No structured configuration** (e.g., `config.yaml` is empty).

If you need additional guidance (e.g., “make the crawler obey robots.txt”, “add Redis-backed frontier”), include that request explicitly.

