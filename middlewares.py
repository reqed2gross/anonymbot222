import time
from collections import defaultdict, deque

# In-memory антиспам. Сбрасывается при рестарте процесса (приемлемо).
_user_times: dict[int, deque] = defaultdict(deque)


async def check_rate_limit(user_id: int, limit: int, window: int) -> bool:
    """True — можно, False — превышен лимит."""
    now = time.time()
    q = _user_times[user_id]
    while q and now - q[0] > window:
        q.popleft()
    if len(q) >= limit:
        return False
    q.append(now)
    return True