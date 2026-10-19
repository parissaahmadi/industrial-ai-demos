"""Radix-2 FFT in pure Python (power-of-two lengths), plus a naive DFT used only to check it in tests."""

from __future__ import annotations

import cmath
import math
from functools import lru_cache


def is_pow2(n: int) -> bool:
    return n >= 1 and n & (n - 1) == 0


@lru_cache(maxsize=8)
def _twiddles(n: int) -> tuple[complex, ...]:
    return tuple(cmath.exp(-2j * math.pi * k / n) for k in range(n // 2))


@lru_cache(maxsize=8)
def _bitrev(n: int) -> tuple[int, ...]:
    bits = n.bit_length() - 1
    return tuple(int(format(i, f"0{bits}b")[::-1], 2) if bits else 0 for i in range(n))


def fft(x: list[float] | list[complex]) -> list[complex]:
    n = len(x)
    if not is_pow2(n):
        raise ValueError(f"length must be a power of two, got {n}")
    a = [complex(x[i]) for i in _bitrev(n)]
    tw = _twiddles(n)
    size = 2
    while size <= n:
        half, step = size // 2, n // size
        for start in range(0, n, size):
            for k in range(half):
                u, v = a[start + k], a[start + k + half] * tw[k * step]
                a[start + k], a[start + k + half] = u + v, u - v
        size *= 2
    return a


def dft(x: list[float]) -> list[complex]:
    n = len(x)
    return [sum(x[t] * cmath.exp(-2j * math.pi * k * t / n) for t in range(n)) for k in range(n)]


def hann(n: int) -> list[float]:
    return [0.5 - 0.5 * math.cos(2 * math.pi * i / n) for i in range(n)]  # periodic Hann
