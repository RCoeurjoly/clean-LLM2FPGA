"""Declared integer arithmetic, separated from historical verification.

Rules are preserved from the frozen exact adapter; no oracle or provenance
loader is imported. Materialization rounds ties to even; runtime right shifts
round ties away from zero. GEMV accumulates ascending indices with i64 wrap.
"""
import math
import torch
from TinyStories.serial_gemv_boundary import serial_gemv

Q_VALUE = 16
Q_SCALE = 24


def _require(condition, code, message):
    if not condition:
        raise ValueError(f"{code}: {message}")

def round_shift_signed(values: torch.Tensor, shift: int) -> torch.Tensor:
    """Signed nearest rounding, with ties away from zero, then right shift."""

    _require(isinstance(shift, int) and 0 <= shift < 63, "invalid_shift", str(shift))
    values = values.to(torch.int64)
    if shift == 0:
        return values.clone()
    divisor = 1 << shift
    floor_quotient = torch.div(values, divisor, rounding_mode="floor")
    floor_remainder = torch.remainder(values, divisor)
    negative = values < 0
    negative_quotient = -floor_quotient - (floor_remainder != 0).to(torch.int64)
    negative_remainder = torch.where(
        floor_remainder == 0, torch.zeros_like(floor_remainder), divisor - floor_remainder
    )
    quotient = torch.where(negative, negative_quotient, floor_quotient)
    remainder = torch.where(negative, negative_remainder, floor_remainder)
    rounded = quotient + (remainder >= (divisor >> 1)).to(torch.int64)
    return torch.where(values < 0, -rounded, rounded)


def activation_qdq(values_q16: torch.Tensor, scales_q24: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor]:
    """Execute one symmetric per-channel INT8 Q/DQ boundary explicitly."""

    values_q16 = values_q16.to(torch.int64)
    scales_q24 = scales_q24.to(torch.int64)
    if not torch.compiler.is_compiling():
        _require(bool(((values_q16 >= -(1 << 31)) & (values_q16 < (1 << 31))).all()),
                 "q16_range_violation", "activation input is outside signed Q16.16 int32")
        _require(bool(((scales_q24 > 0) & (scales_q24 < (1 << 24))).all()),
                 "q24_range_violation", "activation scale is outside unsigned Q8.24 u24")
    numerator = torch.bitwise_left_shift(values_q16, Q_SCALE - Q_VALUE)
    magnitude = torch.div(
        torch.abs(numerator) + torch.div(scales_q24, 2, rounding_mode="floor"),
        scales_q24,
        rounding_mode="floor",
    )
    signed = torch.where(numerator < 0, -magnitude, magnitude)
    codes = torch.clamp(signed, -128, 127).to(torch.int64)
    dequantized = round_shift_signed(codes * scales_q24, Q_SCALE - Q_VALUE)
    return codes, dequantized


def _trunc_divide(numerator: torch.Tensor, denominator: torch.Tensor) -> torch.Tensor:
    quotient = torch.div(torch.abs(numerator), torch.abs(denominator), rounding_mode="floor")
    return torch.where((numerator < 0) != (denominator < 0), -quotient, quotient)


def _integer_sqrt(values: torch.Tensor) -> torch.Tensor:
    """Vectorized restoring square root for non-negative signed-64 values."""

    remainder = values.to(torch.int64)
    result = torch.zeros_like(remainder)
    bit = 1 << 62
    for _ in range(32):
        candidate = result + bit
        take = remainder >= candidate
        remainder = torch.where(take, remainder - candidate, remainder)
        result = torch.where(take, torch.bitwise_right_shift(result, 1) + bit,
                             torch.bitwise_right_shift(result, 1))
        bit >>= 2
    return result


def _fixed_layer_norm(values: torch.Tensor, gamma: torch.Tensor, beta: torch.Tensor) -> torch.Tensor:
    original_shape = values.shape
    rows = values.reshape(-1, original_shape[-1]).to(torch.int64)
    width = rows.shape[1]
    row_sum = torch.sum(rows, dim=1, keepdim=True, dtype=torch.int64)
    mean_magnitude = torch.div(torch.abs(row_sum), width, rounding_mode="floor")
    mean = torch.where(row_sum < 0, -mean_magnitude, mean_magnitude)
    deltas = rows - mean
    variance = torch.div(torch.sum(deltas * deltas, dim=1, keepdim=True, dtype=torch.int64),
                         width, rounding_mode="floor") + 42950
    deviation = _integer_sqrt(variance)
    normalized = _trunc_divide(torch.bitwise_left_shift(deltas, Q_VALUE), deviation)
    affine = torch.bitwise_right_shift(normalized * gamma, Q_VALUE) + beta
    return affine.reshape(original_shape)


def _gelu_table() -> torch.Tensor:
    values = []
    for index in range(8192):
        value = -8.0 + index / 512.0
        gelu = 0.5 * value * (
            1.0 + math.tanh(math.sqrt(2.0 / math.pi) * (value + 0.044715 * value ** 3))
        )
        values.append(max(-32768, min(32767, round(gelu * 4096.0))))
    return torch.tensor(values, dtype=torch.int64)


def _exp_table() -> torch.Tensor:
    return torch.tensor(
        [round(math.exp((index - 4096) / 256.0) * (1 << 20)) for index in range(4096)],
        dtype=torch.int64,
    )
