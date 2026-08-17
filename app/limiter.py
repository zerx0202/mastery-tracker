import asyncio
import time
from collections import deque

# Marginesy pod limitami Riota (100:120, 20:1) - lepiej wolniej niz 429.
LONG_WINDOW, LONG_MAX = 120.0, 90
SHORT_WINDOW, SHORT_MAX = 1.0, 15


class RateLimiter:
    def __init__(self):
        self.long = deque()
        self.short = deque()
        self.lock = asyncio.Lock()

    async def acquire(self):
        while True:
            async with self.lock:
                now = time.monotonic()
                while self.long and now - self.long[0] > LONG_WINDOW:
                    self.long.popleft()
                while self.short and now - self.short[0] > SHORT_WINDOW:
                    self.short.popleft()
                if len(self.long) < LONG_MAX and len(self.short) < SHORT_MAX:
                    self.long.append(now)
                    self.short.append(now)
                    return
                wait = 0.25
                if len(self.long) >= LONG_MAX:
                    wait = max(wait, LONG_WINDOW - (now - self.long[0]) + 0.1)
            await asyncio.sleep(min(wait, 5.0))
