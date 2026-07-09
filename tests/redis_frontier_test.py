"""Test for multi-worker coordination via Redis frontier.

This test verifies that the Redis frontier correctly handles coordination
between multiple concurrent workers, preventing race conditions and
duplicates across workers.

Run this test ONLY if Redis is available locally on port 6379.
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import List

import pytest
import redis

from core.redis_frontier import RedisURLFrontier
from storage.url_database import URLDatabase


@pytest.fixture
def redis_frontier() -> RedisURLFrontier:
    """Create a Redis frontier and clear it for testing."""
    try:
        frontier = RedisURLFrontier(
            redis_host="localhost",
            redis_port=6379,
            redis_db=1,  # Use DB 1 for testing to avoid production data
            namespace="test_crawler",
        )
        frontier.clear()  # Clean slate for test
        yield frontier
        frontier.clear()  # Clean up after test
        frontier.close()
    except redis.ConnectionError:
        pytest.skip("Redis not available on localhost:6379")


@pytest.fixture
def url_database() -> URLDatabase:
    """Create a test URL database in memory (SQLite)."""
    import tempfile
    import os
    
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    
    db = URLDatabase(path=path)
    yield db
    db.close()
    
    # Cleanup temp file
    try:
        os.remove(path)
    except OSError:
        pass


class TestMultiWorkerCoordination:
    """Test multi-worker coordination through Redis frontier."""

    def test_add_url_deduplication(self, redis_frontier: RedisURLFrontier):
        """Verify that adding same URL twice doesn't duplicate."""
        url = "https://piracy.example.com/movie1"
        
        result1 = redis_frontier.add_url(url, priority=10)
        result2 = redis_frontier.add_url(url, priority=10)
        
        assert result1 is True, "First add should succeed"
        assert result2 is False, "Duplicate add should fail"
        
        count = redis_frontier.pending_count()
        assert count == 1, f"Should have exactly 1 URL, got {count}"

    def test_concurrent_worker_adds(self, redis_frontier: RedisURLFrontier):
        """Simulate multiple workers adding URLs concurrently."""
        urls = [
            f"https://piracy.example.com/movie{i}" 
            for i in range(100)
        ]
        
        def add_urls_worker(start_idx: int, batch_size: int):
            """Worker thread adds its batch of URLs."""
            frontier = RedisURLFrontier(
                redis_host="localhost",
                redis_port=6379,
                redis_db=1,
                namespace="test_crawler",
            )
            added = 0
            for i in range(start_idx, start_idx + batch_size):
                if frontier.add_url(urls[i], priority=10):
                    added += 1
            frontier.close()
            return added
        
        # Simulate 4 workers, 25 URLs each
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = [
                executor.submit(add_urls_worker, i * 25, 25)
                for i in range(4)
            ]
            results = [f.result() for f in futures]
        
        total_added = sum(results)
        assert total_added == 100, f"Should add 100 URLs, got {total_added}"
        
        pending = redis_frontier.pending_count()
        assert pending == 100, f"Should have 100 pending, got {pending}"

    def test_get_next_url_no_duplicates(self, redis_frontier: RedisURLFrontier):
        """Verify that get_next_url doesn't return duplicates to different workers."""
        # Add test URLs
        urls = [
            f"https://piracy.example.com/page{i}"
            for i in range(10)
        ]
        
        for url in urls:
            redis_frontier.add_url(url, priority=10)
        
        # Simulate 3 workers each getting next URL
        fetched_urls = []
        
        def worker_fetch():
            """Worker fetches next URL."""
            frontier = RedisURLFrontier(
                redis_host="localhost",
                redis_port=6379,
                redis_db=1,
                namespace="test_crawler",
            )
            urls = []
            for _ in range(3):  # Each worker fetches 3 URLs
                url = frontier.get_next_url()
                if url:
                    urls.append(url)
            frontier.close()
            return urls
        
        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = [executor.submit(worker_fetch) for _ in range(3)]
            results = [f.result() for f in futures]
        
        fetched_urls = [url for worker_urls in results for url in worker_urls]
        
        # Check no duplicates across all workers
        assert len(fetched_urls) == len(set(fetched_urls)), \
            f"Found duplicate URLs across workers: {fetched_urls}"
        
        assert len(fetched_urls) == 9, \
            f"Should fetch 9 URLs (3 workers × 3 each), got {len(fetched_urls)}"

    def test_mark_visited_consistency(self, redis_frontier: RedisURLFrontier):
        """Verify mark_visited is consistent across workers."""
        url = "https://piracy.example.com/film"
        
        # Add URL
        redis_frontier.add_url(url, priority=10)
        
        # Worker 1 fetches it
        fetched = redis_frontier.get_next_url()
        assert fetched == url, "Should fetch the added URL"
        
        # Worker 2 tries to fetch while Worker 1 still holds it
        # (Actually marks visited by Worker 1)
        frontier2 = RedisURLFrontier(
            redis_host="localhost",
            redis_port=6379,
            redis_db=1,
            namespace="test_crawler",
        )
        fetched2 = frontier2.get_next_url()
        frontier2.close()
        
        # Worker 1 marks it visited
        redis_frontier.mark_visited(url)
        
        # Now it shouldn't be available to anyone
        frontier3 = RedisURLFrontier(
            redis_host="localhost",
            redis_port=6379,
            redis_db=1,
            namespace="test_crawler",
        )
        fetched3 = frontier3.get_next_url()
        frontier3.close()
        
        assert fetched3 is None, "URL should not be available after marking visited"
        
        counts = redis_frontier.get_status_counts()
        assert counts.get("visited", 0) == 1, "Should have 1 visited URL"

    def test_rate_limit_per_domain(self, redis_frontier: RedisURLFrontier):
        """Verify that rate limiting is enforced per domain."""
        import time
        
        # Add 3 URLs from same domain
        domain = "https://piracy.example.com"
        urls = [f"{domain}/page{i}" for i in range(3)]
        
        for url in urls:
            redis_frontier.add_url(url, priority=10)
        
        # First fetch should work
        url1 = redis_frontier.get_next_url()
        assert url1 == urls[0], "Should get first URL"
        
        # Second fetch immediately should fail (rate limited)
        url2 = redis_frontier.get_next_url()
        assert url2 is None or url2.split('/')[2] != domain.split('/')[2], \
            "Should not get another URL from same domain immediately"
        
        # After rate limit delay
        time.sleep(1.1)  # Wait for rate limit to expire
        
        url3 = redis_frontier.get_next_url()
        assert url3 == urls[1], "Should get next URL from same domain after rate limit"

    def test_clear_frontier(self, redis_frontier: RedisURLFrontier):
        """Verify clear() wipes all state."""
        # Add URLs and mark some visited
        for i in range(5):
            redis_frontier.add_url(f"https://example.com/page{i}", priority=10)
        
        url = redis_frontier.get_next_url()
        redis_frontier.mark_visited(url)
        
        # Verify state exists
        counts = redis_frontier.get_status_counts()
        assert counts["queued"] > 0, "Should have queued URLs"
        
        # Clear
        redis_frontier.clear()
        
        # Verify cleared
        counts = redis_frontier.get_status_counts()
        assert counts.get("queued", 0) == 0, "Should have no queued after clear"
        assert counts.get("visited", 0) == 0, "Should have no visited after clear"

    def test_namespace_isolation(self):
        """Verify that different namespaces don't interfere."""
        frontier1 = RedisURLFrontier(
            redis_host="localhost",
            redis_port=6379,
            redis_db=1,
            namespace="crawler_a",
        )
        frontier2 = RedisURLFrontier(
            redis_host="localhost",
            redis_port=6379,
            redis_db=1,
            namespace="crawler_b",
        )
        
        try:
            # Clear both
            frontier1.clear()
            frontier2.clear()
            
            # Add to frontier1
            frontier1.add_url("https://example.com/a", priority=10)
            
            # Verify frontier2 doesn't see it
            url = frontier2.get_next_url()
            assert url is None, "Different namespace should not see other's URLs"
            
            # Add to frontier2
            frontier2.add_url("https://example.com/b", priority=10)
            
            # Verify frontier1 doesn't see it
            url = frontier1.get_next_url()
            assert url == "https://example.com/a", "Should only see own namespace"
            
        finally:
            frontier1.clear()
            frontier2.clear()
            frontier1.close()
            frontier2.close()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
