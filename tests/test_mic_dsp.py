"""In-app mic conditioning (_MicDSP): boost, clip protection, gentle high-pass."""
import numpy as np

from assist_client import _MicDSP


def test_inactive_when_neutral():
    # gain 0 dB + high-pass off => nothing to do; caller skips it entirely.
    assert _MicDSP(0.0, False).active is False
    assert _MicDSP(6.0, False).active is True
    assert _MicDSP(0.0, True).active is True


def test_gain_boosts_amplitude():
    dsp = _MicDSP(6.0, False)                     # +6 dB ≈ ×1.995
    out = dsp.process(np.array([1000, -1000, 500], dtype=np.int16))
    assert out.dtype == np.int16
    assert abs(int(out[0]) - 1995) <= 3
    assert abs(int(out[1]) + 1995) <= 3


def test_gain_hard_clips_instead_of_overflowing():
    # 20000 × ~4 (+12 dB) = ~80000; must clip to the int16 range, NOT wrap negative.
    dsp = _MicDSP(12.0, False)
    out = dsp.process(np.full(64, 20000, dtype=np.int16))
    assert out.max() == 32767            # clipped, not wrapped
    assert out.min() >= -32768
    assert int(out[0]) > 0               # would be negative if int16 overflow wrapped


def test_highpass_removes_dc_offset():
    # A constant (pure-DC) signal must decay toward 0 through the DC-blocker.
    dsp = _MicDSP(0.0, True)
    out = dsp.process(np.full(2000, 5000, dtype=np.int16))
    assert abs(int(out[0])) > 1000       # starts near the input level
    assert abs(int(out[-1])) < 50        # DC removed by the tail


def test_highpass_preserves_state_across_blocks():
    # Feeding the same constant across two blocks keeps decaying (state carried),
    # it doesn't reset to the input level at the block boundary.
    dsp = _MicDSP(0.0, True)
    dsp.process(np.full(2000, 5000, dtype=np.int16))
    out2 = dsp.process(np.full(200, 5000, dtype=np.int16))
    assert abs(int(out2[0])) < 100       # continues from the settled state, no jump


def test_passband_signal_survives_highpass():
    # A mid-frequency tone (well above the ~80 Hz cutoff) must pass ~intact.
    t = np.arange(1600)
    tone = (8000 * np.sin(2 * np.pi * 440 * t / 16000)).astype(np.int16)  # 440 Hz @ 16 kHz
    out = _MicDSP(0.0, True).process(tone)
    assert out.astype(np.float32).std() > 0.7 * tone.astype(np.float32).std()
