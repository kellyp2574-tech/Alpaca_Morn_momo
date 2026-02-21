"""
Token bucket rate limiter for API calls.

Implements a token bucket algorithm to respect API rate limits
while maximizing throughput.
"""

import time
import random
import threading
from typing import Optional
from dataclasses import dataclass


@dataclass
class TokenBucket:
    """Token bucket for rate limiting."""
    capacity: int  # Maximum tokens
    tokens: int  # Current tokens
    refill_rate: float  # Tokens per second
    last_refill: float  # Last refill timestamp
    
    def __init__(self, capacity: int, refill_rate: float):
        self.capacity = capacity
        self.tokens = capacity
        self.refill_rate = refill_rate
        self.last_refill = time.time()
        self._lock = threading.Lock()
    
    def consume(self, tokens: int = 1) -> bool:
        """Consume tokens if available."""
        with self._lock:
            self._refill()
            
            if self.tokens >= tokens:
                self.tokens -= tokens
                return True
            return False
    
    def _refill(self):
        """Refill tokens based on elapsed time."""
        now = time.time()
        elapsed = now - self.last_refill
        
        # Add tokens based on elapsed time
        new_tokens = elapsed * self.refill_rate
        self.tokens = min(self.capacity, self.tokens + new_tokens)
        self.last_refill = now


class RateLimiter:
    """Rate limiter with exponential backoff for API calls."""
    
    def __init__(self, max_calls_per_minute: int = 20):
        """
        Initialize rate limiter.
        
        Args:
            max_calls_per_minute: Maximum API calls per minute
        """
        self.bucket = TokenBucket(
            capacity=max_calls_per_minute,
            refill_rate=max_calls_per_minute / 60.0
        )
        self.max_calls_per_minute = max_calls_per_minute
    
    def wait_for_token(self, tokens: int = 1) -> bool:
        """
        Wait for tokens to become available.
        
        Args:
            tokens: Number of tokens needed
            
        Returns:
            True if tokens consumed, False if interrupted
        """
        while True:
            if self.bucket.consume(tokens):
                return True
            
            # Calculate wait time
            wait_time = 60.0 / self.max_calls_per_minute
            time.sleep(wait_time)
    
    def execute_with_backoff(self, func, *args, max_retries: int = 5, **kwargs):
        """
        Execute function with exponential backoff on rate limit errors.
        
        Args:
            func: Function to execute
            max_retries: Maximum retry attempts
            *args, **kwargs: Function arguments
            
        Returns:
            Function result
            
        Raises:
            Exception: If all retries exhausted
        """
        for attempt in range(max_retries + 1):
            # Wait for rate limit token
            if not self.wait_for_token():
                raise Exception("Rate limiting interrupted")
            
            try:
                return func(*args, **kwargs)
            except Exception as e:
                # Check if it's a rate limit error (429) or server error (5xx)
                error_msg = str(e).lower()
                is_rate_limit = "429" in error_msg or "rate limit" in error_msg
                is_server_error = any(code in error_msg for code in ["500", "502", "503", "504"])
                
                if not (is_rate_limit or is_server_error):
                    # Not a retryable error, re-raise
                    raise
                
                if attempt == max_retries:
                    # All retries exhausted
                    raise
                
                # Calculate backoff with jitter
                base_delay = min(300, (2 ** attempt))  # Cap at 5 minutes
                jitter = random.uniform(0.1, 0.3) * base_delay
                delay = base_delay + jitter
                
                print(f"Rate limit hit, retrying in {delay:.1f}s (attempt {attempt + 1}/{max_retries + 1})")
                time.sleep(delay)
        
        raise Exception("All retries exhausted")


# Global rate limiter instance
_rate_limiter: Optional[RateLimiter] = None


def get_rate_limiter(calls_per_minute: int = 20) -> RateLimiter:
    """Get or create global rate limiter."""
    global _rate_limiter
    if _rate_limiter is None or _rate_limiter.max_calls_per_minute != calls_per_minute:
        _rate_limiter = RateLimiter(calls_per_minute)
    return _rate_limiter


def rate_limited(max_retries: int = 5):
    """Decorator for rate-limited functions."""
    def decorator(func):
        def wrapper(*args, **kwargs):
            limiter = get_rate_limiter()
            return limiter.execute_with_backoff(func, *args, max_retries=max_retries, **kwargs)
        return wrapper
    return decorator
