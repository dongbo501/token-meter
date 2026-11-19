"""Bound response reads by the request deadline, including trickled bodies."""

import time


def remaining(deadline):
    seconds = deadline - time.monotonic()
    if seconds <= 0:
        raise TimeoutError("请求超过设置的时限")
    return seconds


def read_chunks(response, deadline, limit=4 * 1024 * 1024):
    size = 0
    remaining(deadline)
    while not response.isclosed():
        # HTTPResponse has no public socket-timeout setter. Use read1 so each
        # network read gets the remaining budget instead of restarting it.
        response.fp.raw._sock.settimeout(remaining(deadline))
        chunk = response.read1(min(65536, limit + 1 - size))
        remaining(deadline)
        if not chunk:
            break
        size += len(chunk)
        if size > limit:
            raise ValueError("接口响应超过大小限制")
        yield chunk
