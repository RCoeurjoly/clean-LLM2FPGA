"""Exportable, exact serial-GEMV boundary for the TinyStories fixed-point model."""

from __future__ import annotations

import torch


def _require_i64_matrix(values: torch.Tensor, label: str) -> None:
    if values.dtype != torch.int64:
        raise ValueError(f"{label} must be a signed int64 tensor")
    if values.ndim != 2:
        raise ValueError(f"{label} must be rank-2")


def _validate_inputs(scaled_input_q24: torch.Tensor, weight_codes: torch.Tensor) -> None:
    _require_i64_matrix(scaled_input_q24, "scaled_input_q24")
    _require_i64_matrix(weight_codes, "weight_codes")
    if scaled_input_q24.shape[1] != weight_codes.shape[1]:
        raise ValueError("serial GEMV input widths differ")


def ascending_i64_wrapping_mac(
    scaled_input_q24: torch.Tensor, weight_codes: torch.Tensor
) -> torch.Tensor:
    """Ascending-index signed-64 serial MAC; torch int64 supplies two's-complement wrap."""

    _validate_inputs(scaled_input_q24, weight_codes)
    accumulator = torch.zeros(
        (scaled_input_q24.shape[0], weight_codes.shape[0]),
        dtype=torch.int64,
        device=scaled_input_q24.device,
    )
    for input_index in range(weight_codes.shape[1]):
        term = scaled_input_q24[:, input_index:input_index + 1] * weight_codes[:, input_index]
        accumulator = accumulator + term
    return accumulator


@torch.library.custom_op("llm2fpga::serial_gemv", mutates_args=())
def serial_gemv(scaled_input_q24: torch.Tensor, weight_codes: torch.Tensor) -> torch.Tensor:
    """Expose the exact serial MAC as one compiler-owned custom operation."""

    return ascending_i64_wrapping_mac(scaled_input_q24, weight_codes)


@torch.library.register_fake("llm2fpga::serial_gemv")
def _serial_gemv_fake(scaled_input_q24: torch.Tensor, weight_codes: torch.Tensor) -> torch.Tensor:
    _validate_inputs(scaled_input_q24, weight_codes)
    return torch.empty(
        (scaled_input_q24.shape[0], weight_codes.shape[0]),
        dtype=torch.int64,
        device=scaled_input_q24.device,
    )
