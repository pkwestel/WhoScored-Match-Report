import time
import random

# Fixed delays
time.sleep(2)  # 2 second delay

# Random delays (more human-like)
time.sleep(random.uniform(1, 3))  # Random delay between 1-3 seconds


# Exponential backoff for retries
def exponential_backoff(attempt):
    return min(300, (2**attempt) + random.uniform(0, 1))
