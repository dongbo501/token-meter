"""Local-only real CLI write-I/O benchmark; never contacts a model provider.

Mount this directory as /tests in an image and run:
  python /tests/benchmark_io.py [--memory]
Use --tmpfs /tmp:size=256m,mode=1777 with the optimized image.
"""

import argparse
import json
import os
import resource
import sys
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, "/app")
from test_meter import Gateway

from ledger import Ledger
from worker import complete


def writes():
    result = {}
    for line in Path("/sys/fs/cgroup/io.stat").read_text().splitlines():
        device, *stats = line.split()
        result[device] = {
            key: int(value) for key, value in (part.split("=") for part in stats)
        }
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--memory", action="store_true")
    parser.add_argument("--requests", type=int, default=30)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", 0), Gateway)
    server.calls = []
    server.reply = (200, {"usage": {"input_tokens": 100, "output_tokens": 20}})
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    book = Ledger(
        "/data/benchmark.sqlite3", **({"storage_mode": "memory"} if args.memory else {})
    )
    book.save(
        {
            "base_url": f"http://127.0.0.1:{server.server_port}",
            "api_key": "local-fixture",
            "model": "mock",
            "api_style": "codex",
            "mode": "tokens",
            "token_target": 10000000,
            "token_limit": 11000000,
            "input_price": 1,
            "output_price": 2,
            "cached_price": 0.25,
        }
    )
    book.switch(True)
    os.sync()
    before = writes()
    started = time.monotonic()
    for _ in range(args.requests):
        job = book.reserve()
        assert job, book.snapshot()
        complete(book, job)
    assert len(server.calls) == args.requests
    state = book.snapshot()
    assert state["totals"]["count"] == args.requests
    assert state["totals"]["estimated_count"] == 0
    if args.memory:
        book.flush(force=True)  # Include a recovery snapshot in the measurement.
    else:
        book.maintenance(force=True)
    book.db.close()
    os.sync()
    after = writes()
    delta = {
        device: {
            key: value - before.get(device, {}).get(key, 0)
            for key, value in stats.items()
        }
        for device, stats in after.items()
    }
    print(
        json.dumps(
            {
                "mode": "memory+tmpfs" if args.memory else "original-disk",
                "requests": args.requests,
                "elapsed_seconds": round(time.monotonic() - started, 2),
                "device_io_delta": delta,
                "child_write_bytes": resource.getrusage(
                    resource.RUSAGE_CHILDREN
                ).ru_oublock
                * 512,
                "snapshot_bytes": Path("/data/benchmark.sqlite3").stat().st_size,
            },
            indent=2,
        ),
        flush=True,
    )
    server.shutdown()
    server.server_close()


if __name__ == "__main__":
    main()
