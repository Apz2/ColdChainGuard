"""edge excursion state machine — NOMINAL / WARNING / EXCURSION / RECOVERED."""

from __future__ import annotations

_STATE_NOMINAL = "NOMINAL"
_STATE_WARNING = "WARNING"
_STATE_EXCURSION = "EXCURSION"
_STATE_RECOVERED = "RECOVERED"


class ExcursionStateMachine:
    """tracking temp breaches with dwell before escalating to EXCURSION.

    NOMINAL   -> WARNING    first reading outside limits
    WARNING   -> EXCURSION  still outside after dwell_s
    WARNING   -> NOMINAL    back inside before dwell (flap suppression)
    EXCURSION -> RECOVERED  inside limits for recovery_s
    RECOVERED -> NOMINAL    next in-range reading
    """

    def __init__(
        self,
        low_c: float,
        high_c: float,
        dwell_s: float = 120.0,
        recovery_s: float = 300.0,
    ) -> None:
        self._low_c = low_c
        self._high_c = high_c
        self._dwell_s = dwell_s
        self._recovery_s = recovery_s
        self._state = _STATE_NOMINAL
        self._breach_started_ts: float | None = None
        self._recovery_started_ts: float | None = None

    @property
    def dwell_s(self) -> float:
        """how long breach needs to last before EXCURSION."""
        return self._dwell_s

    @dwell_s.setter
    def dwell_s(self, value: float) -> None:
        self._dwell_s = value

    @property
    def state(self) -> str:
        """where the state machine is right now."""
        return self._state

    def _is_outside(self, temp_c: float) -> bool:
        return temp_c < self._low_c or temp_c > self._high_c

    def _transition(self, new_state: str) -> str:
        self._state = new_state
        return new_state

    def update(self, temp_c: float, ts: float) -> str | None:
        """feeding a reading — returning new state name if it changed."""
        outside = self._is_outside(temp_c)

        if self._state == _STATE_NOMINAL:
            if outside:
                self._breach_started_ts = ts
                self._recovery_started_ts = None
                return self._transition(_STATE_WARNING)
            return None

        if self._state == _STATE_WARNING:
            if outside:
                if (
                    self._breach_started_ts is not None
                    and (ts - self._breach_started_ts) >= self._dwell_s
                ):
                    self._breach_started_ts = None
                    self._recovery_started_ts = None
                    return self._transition(_STATE_EXCURSION)
                return None

            self._breach_started_ts = None
            self._recovery_started_ts = None
            return self._transition(_STATE_NOMINAL)

        if self._state == _STATE_EXCURSION:
            if outside:
                self._recovery_started_ts = None
                return None

            if self._recovery_started_ts is None:
                self._recovery_started_ts = ts

            if (ts - self._recovery_started_ts) >= self._recovery_s:
                self._recovery_started_ts = None
                return self._transition(_STATE_RECOVERED)
            return None

        if self._state == _STATE_RECOVERED:
            if outside:
                self._breach_started_ts = ts
                self._recovery_started_ts = None
                return self._transition(_STATE_WARNING)

            return self._transition(_STATE_NOMINAL)

        return None
