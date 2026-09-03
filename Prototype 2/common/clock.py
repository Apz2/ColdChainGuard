"""sim clock with time compression so 12 h fits in a few real minutes."""

from __future__ import annotations

import time


class SimClock:
    """compressing sim time — compression=120 means 120 sim seconds per real second.

    so a 12 h scenario finishes in ~6 real minutes.
    """

    def __init__(self, start_epoch: float, compression: float) -> None:
        self._start_epoch = start_epoch
        self._compression = compression
        self._real_start = time.time()

    def now(self) -> float:
        """returning current sim epoch."""
        elapsed_real_s = time.time() - self._real_start
        return self._start_epoch + elapsed_real_s * self._compression

    def sleep_sim(self, sim_seconds: float) -> None:
        """sleeping for sim_seconds of simulated time."""
        real_seconds = sim_seconds / self._compression
        time.sleep(real_seconds)
