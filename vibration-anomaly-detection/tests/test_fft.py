import cmath
import math
import random

import pytest

from vibration_anomaly.fft import dft, fft, hann, is_pow2


@pytest.mark.parametrize("n", [1, 2, 4, 8, 16, 64, 256])
def test_fft_matches_the_naive_dft(n):
    rng = random.Random(n)
    x = [rng.uniform(-1, 1) for _ in range(n)]
    for a, b in zip(fft(x), dft(x), strict=True):
        assert abs(a - b) < 1e-9


def test_impulse_gives_a_flat_spectrum_and_dc_gives_one_bin():
    assert all(abs(v - 1) < 1e-12 for v in fft([1.0] + [0.0] * 7))
    s = fft([2.0] * 8)
    assert abs(s[0] - 16) < 1e-12 and all(abs(v) < 1e-12 for v in s[1:])


def test_a_pure_tone_lands_in_its_bin_and_the_mirror_bin():
    n, k = 64, 5
    s = fft([math.sin(2 * math.pi * k * i / n) for i in range(n)])
    peaks = sorted(range(n), key=lambda i: -abs(s[i]))[:2]
    assert sorted(peaks) == [k, n - k] and abs(abs(s[k]) - n / 2) < 1e-9


def test_parseval():
    rng = random.Random(3)
    x = [rng.gauss(0, 1) for _ in range(128)]
    assert abs(sum(v * v for v in x) - sum(abs(c) ** 2 for c in fft(x)) / 128) < 1e-9


def test_complex_input_and_input_is_not_modified():
    x = [1 + 2j, 3 - 1j, 0, 2j]
    copy = list(x)
    out = fft(x)
    assert x == copy
    assert abs(out[0] - sum(x)) < 1e-12
    assert abs(out[1] - sum(v * cmath.exp(-2j * math.pi * i / 4) for i, v in enumerate(x))) < 1e-12


@pytest.mark.parametrize("n", [0, 3, 6, 12, 1000])
def test_length_must_be_a_power_of_two(n):
    with pytest.raises(ValueError):
        fft([0.0] * n)


def test_is_pow2():
    assert [n for n in range(0, 20) if is_pow2(n)] == [1, 2, 4, 8, 16]
    assert not is_pow2(-4)


def test_hann_shape():
    w = hann(8)
    assert w[0] == 0 and abs(w[4] - 1) < 1e-12 and abs(w[2] - w[6]) < 1e-12  # periodic, peak at n/2
    assert abs(sum(w) - 4) < 1e-12  # mean 0.5
