import time
from collections import deque


class RateLimiter:
    def __init__(self, max_requests=10, time_window=60):
        """
        Simple rate limiter.

        Args:
            max_requests: Maximum number of requests allowed (default: 10)
            time_window: Time window in seconds (default: 60 seconds)
        """
        self.max_requests = max_requests
        self.time_window = time_window
        self.requests = deque()

    def _cleanup_old_requests(self):
        """Remove requests older than the time window."""
        current_time = time.time()
        while self.requests and self.requests[0] <= current_time - self.time_window:
            self.requests.popleft()

    def wait_if_needed(self):
        """Wait if necessary to respect rate limits."""
        self._cleanup_old_requests()

        if len(self.requests) >= self.max_requests:
            # Calculate how long to wait
            oldest_request = self.requests[0]
            wait_time = self.time_window - (time.time() - oldest_request) + 0.1

            if wait_time > 0:
                print(f"Rate limit reached. Waiting {wait_time:.1f} seconds...")
                time.sleep(wait_time)
                self._cleanup_old_requests()

        # Record this request
        self.requests.append(time.time())

    def can_proceed(self):
        """Check if a request can proceed without waiting."""
        self._cleanup_old_requests()
        return len(self.requests) < self.max_requests

    def get_status(self):
        """Get current rate limiter status."""
        self._cleanup_old_requests()
        current_requests = len(self.requests)

        return {
            "max_requests": self.max_requests,
            "time_window": self.time_window,
            "current_requests": current_requests,
            "requests_remaining": self.max_requests - current_requests,
        }

    def reset(self):
        """Reset the rate limiter."""
        self.requests.clear()


def rate_limited(max_requests=10, time_window=60):
    """
    Decorator to apply rate limiting to any function.

    Example:
        @rate_limited(max_requests=5, time_window=30)
        def api_call():
            return requests.get('https://api.example.com')
    """
    limiter = RateLimiter(max_requests, time_window)

    def decorator(func):
        def wrapper(*args, **kwargs):
            limiter.wait_if_needed()
            return func(*args, **kwargs)

        wrapper.rate_limiter = limiter
        return wrapper

    return decorator


# Example usage
if __name__ == "__main__":

    # Example 1: Direct usage
    print("=== Example 1: Direct Usage ===")
    limiter = RateLimiter(max_requests=3, time_window=10)  # 3 requests per 10 seconds

    for i in range(5):
        print(f"Making request {i+1}")
        print(f"Status: {limiter.get_status()}")

        limiter.wait_if_needed()
        print(f"Request {i+1} completed at {time.strftime('%H:%M:%S')}")
        print("-" * 30)

    print("\n" + "=" * 50)

    # Example 2: Using decorator
    print("=== Example 2: Decorator Usage ===")

    @rate_limited(max_requests=2, time_window=5)  # 2 requests per 5 seconds
    def scrape_page(url):
        print(f"Scraping: {url} at {time.strftime('%H:%M:%S')}")
        # Simulate request
        time.sleep(0.1)
        return f"Content from {url}"

    urls = ["page1.com", "page2.com", "page3.com", "page4.com"]

    for url in urls:
        status = scrape_page.rate_limiter.get_status()
        print(f"Remaining requests: {status['requests_remaining']}")

        result = scrape_page(url)
        print(f"Result: {result}")
        print("-" * 30)

    print("\n" + "=" * 50)

    # Example 3: Web scraping with rate limiting
    print("=== Example 3: Web Scraping ===")

    # Default: 10 requests per minute
    scraper_limiter = RateLimiter()

    def scrape_with_limit(url):
        scraper_limiter.wait_if_needed()

        try:
            print(f"Scraping: {url}")
            # In real scenario: response = requests.get(url)
            time.sleep(0.1)  # Simulate network delay
            return f"Success: {url}"
        except Exception as e:
            return f"Error: {e}"

    # Scrape multiple pages
    pages = [f"https://example.com/page{i}" for i in range(1, 8)]

    for page in pages:
        status = scraper_limiter.get_status()
        print(
            f"Status: {status['current_requests']}/{status['max_requests']} requests used"
        )

        result = scrape_with_limit(page)
        print(f"Result: {result}")
        print("-" * 20)
