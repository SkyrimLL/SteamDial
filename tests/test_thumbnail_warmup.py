import threading
import time
import unittest
from pathlib import Path

from thumbnail_warmup import ThumbnailWarmupService


class FakeResolver:
    """Stand-in for ThumbnailService.resolve(), recording every AppID it was asked about."""

    def __init__(self, available=(), gate=None):
        self._available = set(available)
        self._gate = gate
        self._lock = threading.Lock()
        self.seen = []

    def resolve(self, appid: int):
        with self._lock:
            self.seen.append(appid)
        if self._gate is not None:
            self._gate.wait(5)
        return Path(f"{appid}.jpg") if appid in self._available else None


class ExplodingResolver(FakeResolver):
    def resolve(self, appid: int):
        super().resolve(appid)
        if appid == 2:
            raise RuntimeError("provider blew up")
        return None


class ThumbnailWarmupServiceTests(unittest.TestCase):
    def wait_for_idle(self, service, timeout=5.0):
        deadline = time.monotonic() + timeout
        while service.is_running() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertFalse(service.is_running(), "warm-up did not finish in time")

    def test_every_appid_is_resolved_once_regardless_of_duplicates(self):
        resolver = FakeResolver(available=[1, 2])
        service = ThumbnailWarmupService(resolver, max_workers=2)
        self.assertTrue(service.start([1, 2, 3, 2, 1]))
        self.wait_for_idle(service)
        self.assertEqual(sorted(resolver.seen), [1, 2, 3])

    def test_one_failing_appid_does_not_abandon_the_rest(self):
        resolver = ExplodingResolver()
        service = ThumbnailWarmupService(resolver, max_workers=1)
        with self.assertLogs("thumbnail_warmup", level="ERROR"):
            self.assertTrue(service.start([1, 2, 3]))
            self.wait_for_idle(service)
        self.assertEqual(sorted(resolver.seen), [1, 2, 3])

    def test_an_empty_batch_finishes_without_starting_a_thread(self):
        service = ThumbnailWarmupService(FakeResolver())
        self.assertTrue(service.start([]))
        self.assertFalse(service.is_running())

    def test_a_second_run_is_refused_while_one_is_in_flight(self):
        gate = threading.Event()
        resolver = FakeResolver(gate=gate)
        service = ThumbnailWarmupService(resolver, max_workers=1)
        self.addCleanup(gate.set)
        self.assertTrue(service.start([1]))
        self.assertTrue(service.is_running())
        self.assertFalse(service.start([2]))
        gate.set()
        self.wait_for_idle(service)
        self.assertEqual(resolver.seen, [1])


if __name__ == "__main__":
    unittest.main()
