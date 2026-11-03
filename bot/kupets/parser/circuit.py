"""Глобальный circuit-breaker при всплеске 403 от Ozon."""

from __future__ import annotations

import time

from utils import get_logger

logger = get_logger("parser.circuit")


class CircuitBreaker:
    def __init__(self, threshold: int = 5, cooldown_sec: float = 60.0) -> None:
        self.threshold = threshold
        self.cooldown_sec = cooldown_sec
        self._fails = 0
        self._opened_at: float | None = None

    @property
    def is_open(self) -> bool:
        if self._opened_at is None:
            return False
        if time.monotonic() - self._opened_at >= self.cooldown_sec:
            self._opened_at = None
            self._fails = 0
            logger.info("circuit_half_open")
            return False
        return True

    def record_success(self) -> None:
        self._fails = 0
        self._opened_at = None

    def record_failure(self, status: int | None = None) -> None:
        if status not in {403, 429, None}:
            return
        self._fails += 1
        if self._fails >= self.threshold:
            self._opened_at = time.monotonic()
            logger.warning(
                "circuit_open",
                fails=self._fails,
                cooldown=self.cooldown_sec,
            )

    def remaining_sec(self) -> int:
        if not self.is_open or self._opened_at is None:
            return 0
        left = self.cooldown_sec - (time.monotonic() - self._opened_at)
        return max(0, int(left))


ozon_circuit = CircuitBreaker(threshold=5, cooldown_sec=90.0)
