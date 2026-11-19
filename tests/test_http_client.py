import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from codex_runner import read_response, stream_usage
from http_client import read_chunks
from worker import post


class TimedResponse:
    """Advance a virtual clock for each socket read, respecting its timeout."""

    def __init__(self, clock, chunks):
        self.clock, self.chunks, self.waits = clock, list(chunks), []
        self.fp = SimpleNamespace(raw=SimpleNamespace(_sock=self))

    def settimeout(self, seconds):
        self.waits.append(seconds)

    def isclosed(self):
        return not self.chunks

    def read1(self, _size):
        delay, data = self.chunks.pop(0)
        if delay > self.waits[-1]:
            self.clock[0] += self.waits[-1]
            raise TimeoutError("simulated socket timeout")
        self.clock[0] += delay
        return data

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass


class RequestDeadlineTest(unittest.TestCase):
    def setUp(self):
        self.clock = [0]
        clock = patch("http_client.time.monotonic", side_effect=lambda: self.clock[0])
        clock.start()
        self.addCleanup(clock.stop)

    def test_278_second_stream_completes_with_600_second_timeout(self):
        start = b'data: {"type":"response.created"}\n\n'
        final = (
            b'data: {"type":"response.completed","response":'
            b'{"usage":{"input_tokens":90,"output_tokens":16}}}\n\n'
        )
        response = TimedResponse(self.clock, [(2, start), (276, final), (1000, b"")])
        raw = read_response(response, "text/event-stream", 600)
        self.assertEqual(stream_usage(raw, "text/event-stream"), (90, None, 16))
        self.assertEqual(self.clock[0], 278)
        self.assertEqual(response.waits, [600, 598])
        self.assertEqual(len(response.chunks), 1)  # Terminal SSE event ends the read.

    def test_continuous_output_does_not_restart_total_timeout(self):
        chunk = b'data: {"type":"response.created"}\n\n'
        response = TimedResponse(self.clock, [(50, chunk)] * 4)
        with self.assertRaises(TimeoutError):
            read_response(response, "text/event-stream", 120)
        self.assertEqual(self.clock[0], 120)
        self.assertEqual(response.waits, [120, 70, 20])

    def test_json_body_has_same_total_deadline(self):
        response = TimedResponse(self.clock, [(50, b" ")] * 3 + [(0, b"{}")])
        with self.assertRaises(TimeoutError):
            read_response(response, "application/json", 120)
        self.assertEqual(self.clock[0], 120)

    def test_direct_request_long_response_and_header_wait_share_deadline(self):
        def connect(*_args, **kwargs):
            self.assertEqual(kwargs["timeout"], 600)
            self.clock[0] += 2
            return TimedResponse(self.clock, [(276, b'{"ok":true}')])

        with patch("worker.build_opener", return_value=Mock(open=connect)):
            self.assertEqual(post("http://fixture.invalid", {}, timeout=600), {"ok": True})
        self.assertEqual(self.clock[0], 278)

    def test_waiting_for_headers_consumes_response_budget(self):
        def connect(*_args, **kwargs):
            self.assertEqual(kwargs["timeout"], 60)
            self.clock[0] += 40
            return TimedResponse(self.clock, [(25, b"{}")])

        with patch("worker.build_opener", return_value=Mock(open=connect)):
            with self.assertRaises(TimeoutError):
                post("http://fixture.invalid", {}, timeout=60)
        self.assertEqual(self.clock[0], 60)

    def test_expired_deadline_never_reads_response(self):
        response = TimedResponse(self.clock, [(0, b"{}")])
        with self.assertRaises(TimeoutError):
            list(read_chunks(response, 0))
        self.assertEqual(response.waits, [])

    def test_body_size_limit_still_applies_across_reads(self):
        response = TimedResponse(self.clock, [(1, b"abc"), (1, b"def")])
        with self.assertRaises(ValueError):
            list(read_chunks(response, 600, limit=5))


if __name__ == "__main__":
    unittest.main()
