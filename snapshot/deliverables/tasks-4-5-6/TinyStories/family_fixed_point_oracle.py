"""Shape-derived TinyStories oracle using the accepted 1M integer arithmetic.

This is a software reference for compiler/RTL equivalence, not accelerator RTL.
The fixed-point operations are reused from the authenticated 1M adapter; only
model dimensions, layer scheduling, package decoding and trace naming vary.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np
import torch

from TinyStories import model_adapter_exact_package as exact
from scripts.pipeline.tinystories_family_spec import FamilyModelSpec


class FamilyOracleError(ValueError):
    """A package or inference step cannot satisfy the fixed-point contract."""


def _slice(image: bytes, offset: int, length: int, name: str) -> bytes:
    if offset < 0 or length < 1 or offset + length > len(image):
        raise FamilyOracleError(f"{name}: image extent outside package")
    return image[offset:offset + length]


def _q24(values: np.ndarray, name: str) -> torch.Tensor:
    tensor = torch.from_numpy(values.astype(np.float64, copy=True))
    rounded = torch.round(tensor * (1 << exact.Q_SCALE))
    if not bool(torch.isfinite(rounded).all()) or not bool(
        ((rounded > 0) & (rounded < (1 << 24))).all()
    ):
        raise FamilyOracleError(f"{name}: non-finite or out-of-range Q8.24 scale")
    return rounded.to(torch.int64)


def _decode_package(spec: FamilyModelSpec, package: Path) -> tuple[
    dict[str, torch.Tensor], dict[str, torch.Tensor], dict[str, torch.Tensor]
]:
    manifest = json.loads((package / "manifest.json").read_text())
    tensors = manifest.get("tensors")
    if not isinstance(tensors, dict) or set(tensors) != set(spec.source_shapes):
        raise FamilyOracleError("package tensor set differs from validated family spec")
    activations = manifest.get("activation_scales")
    if not isinstance(activations, dict) or set(activations) != set(spec.qdq_names):
        missing = sorted(set(spec.qdq_names) - set(activations or {}))
        extra = sorted(set(activations or {}) - set(spec.qdq_names))
        raise FamilyOracleError(f"activation scales: missing={missing[:3]} extra={extra[:3]}")
    weights = (package / "weights.bin").read_bytes()
    scale_image = (package / "scales.bin").read_bytes()
    codes: dict[str, torch.Tensor] = {}
    scales: dict[str, torch.Tensor] = {}
    parameters: dict[str, torch.Tensor] = {}
    for name, descriptor in tensors.items():
        shape = spec.source_shapes[name]
        if descriptor.get("logical_shape") != list(shape):
            raise FamilyOracleError(f"{name}: package shape differs from validated spec")
        data = _slice(weights, int(descriptor["offset"]), int(descriptor["nbytes"]), name)
        count = int(np.prod(shape))
        if len(shape) == 2:
            if descriptor.get("format") != "symmetric_int8_per_output" or descriptor.get("bits") != 8:
                raise FamilyOracleError(f"{name}: expected per-output INT8 codes")
            if len(data) != count:
                raise FamilyOracleError(f"{name}: INT8 byte count differs from shape")
            codes[name] = torch.from_numpy(np.frombuffer(data, dtype=np.int8).copy()).reshape(shape).to(torch.int64)
            scale_data = _slice(scale_image, int(descriptor["scale_offset"]),
                                int(descriptor["scale_nbytes"]), name)
            if len(scale_data) != shape[0] * 4:
                raise FamilyOracleError(f"{name}: per-output scale count differs from shape")
            scales[name] = _q24(np.frombuffer(scale_data, dtype="<f4"), name)
        else:
            if descriptor.get("format") != "float32" or len(data) != count * 4:
                raise FamilyOracleError(f"{name}: expected float32 parameter payload")
            values = torch.from_numpy(np.frombuffer(data, dtype="<f4").copy()).reshape(shape).to(torch.float64)
            rounded = torch.round(values * (1 << exact.Q_VALUE))
            if not bool(torch.isfinite(rounded).all()) or not bool(
                ((rounded >= -(1 << 31)) & (rounded < (1 << 31))).all()
            ):
                raise FamilyOracleError(f"{name}: non-finite or out-of-range Q16.16 parameter")
            parameters[name] = rounded.to(torch.int64)
    for name in spec.qdq_names:
        try:
            values = np.asarray(activations[name], dtype=np.float64)
        except (TypeError, ValueError) as error:
            raise FamilyOracleError(f"{name}: invalid activation scale") from error
        scales[f"activation::{name}"] = _q24(values, name)
    return codes, scales, parameters


class FamilyFixedPointModel(exact._ExactFixedPointModel):
    def __init__(self, spec: FamilyModelSpec, codes: Mapping[str, torch.Tensor],
                 scales: Mapping[str, torch.Tensor], parameters: Mapping[str, torch.Tensor]) -> None:
        super().__init__(codes, scales, parameters, lambda _: None)
        self.spec = spec

    def _attention(self, query: torch.Tensor, key: torch.Tensor, value: torch.Tensor) -> torch.Tensor:
        length = query.shape[1]
        heads: list[torch.Tensor] = []
        for head in range(self.spec.heads):
            columns = slice(head * self.spec.head_dim, (head + 1) * self.spec.head_dim)
            q_head = query[:, :, columns]
            k_head = key[:, :, columns]
            v_head = value[:, :, columns]
            positions: list[torch.Tensor] = []
            for position in range(length):
                products = q_head[:, position:position + 1, :] * k_head[:, :position + 1, :]
                scores = torch.bitwise_right_shift(torch.sum(products, dim=-1, dtype=torch.int64), 24)
                maxima = torch.amax(scores, dim=-1, keepdim=True)
                delta = torch.clamp(scores - maxima, -4096, 0)
                table_index = torch.minimum(4096 + delta, torch.full_like(delta, 4095))
                probabilities = torch.where(delta == 0, torch.full_like(delta, 1 << 20),
                                            self._exp_lut[table_index])
                denominator = torch.sum(probabilities, dim=-1, keepdim=True, dtype=torch.int64)
                numerator = torch.sum(probabilities.unsqueeze(-1) * v_head[:, :position + 1, :],
                                      dim=1, dtype=torch.int64)
                correction = torch.where(numerator < 0, -torch.div(denominator, 2, rounding_mode="floor"),
                                         torch.div(denominator, 2, rounding_mode="floor"))
                positions.append(exact._trunc_divide(numerator + correction, denominator))
            heads.append(torch.stack(positions, dim=1))
        return torch.cat(heads, dim=-1)

    def _execute(self, input_ids: torch.Tensor) -> tuple[torch.Tensor, dict[str, torch.Tensor]]:
        length = input_ids.shape[1]
        positions = torch.arange(length, dtype=torch.int64, device=input_ids.device).unsqueeze(0)
        x = self._embedding("token_embedding.weight", input_ids)
        x = x + self._embedding("position_embedding.weight", positions)
        checkpoints: dict[str, torch.Tensor] = {}
        for layer in range(self.spec.layers):
            block = f"blocks.{layer}"
            source = f"transformer.h.{layer}"
            block_input = x
            normalized = exact._fixed_layer_norm(
                x, self._buffer("parameter", f"{block}.ln1.weight"),
                self._buffer("parameter", f"{block}.ln1.bias"),
            )
            query, _, _ = self._gemv(normalized, f"{block}.attn.q.weight", None,
                                      f"{source}.attn.attention.q_proj")
            key, _, _ = self._gemv(normalized, f"{block}.attn.k.weight", None,
                                    f"{source}.attn.attention.k_proj")
            value, _, _ = self._gemv(normalized, f"{block}.attn.v.weight", None,
                                      f"{source}.attn.attention.v_proj")
            context = self._attention(query, key, value)
            attention, _, _ = self._gemv(context, f"{block}.attn.out.weight",
                                          f"{block}.attn.out.bias", f"{source}.attn.attention.out_proj")
            residual_attention = x + attention
            normalized_2 = exact._fixed_layer_norm(
                residual_attention, self._buffer("parameter", f"{block}.ln2.weight"),
                self._buffer("parameter", f"{block}.ln2.bias"),
            )
            hidden, _, _ = self._gemv(normalized_2, f"{block}.mlp.fc.weight",
                                       f"{block}.mlp.fc.bias", f"{source}.mlp.c_fc")
            activated = self._fixed_gelu(hidden)
            projected, _, _ = self._gemv(activated, f"{block}.mlp.proj.weight",
                                          f"{block}.mlp.proj.bias", f"{source}.mlp.c_proj")
            x = residual_attention + projected
            if layer == 0:
                last = length - 1
                checkpoints = {
                    "block.0.input": block_input[0, last],
                    "block.0.ln_1.output": normalized[0, last],
                    "block.0.attention.q": query[0, last].reshape(self.spec.heads, self.spec.head_dim),
                    "block.0.attention.k": key[0, last].reshape(self.spec.heads, self.spec.head_dim),
                    "block.0.attention.v": value[0, last].reshape(self.spec.heads, self.spec.head_dim),
                    "block.0.attention.output": attention[0, last],
                    "block.0.residual.attention": residual_attention[0, last],
                    "block.0.ln_2.output": normalized_2[0, last],
                    "block.0.mlp.fc_in": hidden[0, last],
                    "block.0.mlp.activation": activated[0, last],
                    "block.0.mlp.fc_out": projected[0, last],
                    "block.0.output": x[0, last],
                }
        normalized = exact._fixed_layer_norm(
            x, self._buffer("parameter", "final_ln.weight"), self._buffer("parameter", "final_ln.bias")
        )
        input_scale = self._activation_scale("lm_head.input")
        input_codes, _ = exact.activation_qdq(normalized.reshape(-1, self.spec.hidden), input_scale)
        accumulator = exact.serial_gemv(input_codes * input_scale,
                                        self._buffer("code", "token_embedding.weight"))
        logits = exact.round_shift_signed(
            accumulator * self._buffer("scale", "token_embedding.weight"),
            2 * exact.Q_SCALE - exact.Q_VALUE,
        ).reshape(1, length, self.spec.vocab)
        return logits, checkpoints


def load_family_oracle(spec: FamilyModelSpec, package: Path) -> FamilyFixedPointModel:
    codes, scales, parameters = _decode_package(spec, Path(package))
    return FamilyFixedPointModel(spec, codes, scales, parameters)


def capture_first_token(model: FamilyFixedPointModel, prompt_ids: Sequence[int]) -> dict[str, Any]:
    model.spec.validate_prompt(prompt_ids)
    tokens = torch.tensor([list(prompt_ids)], dtype=torch.int64)
    if len(prompt_ids) > model.spec.context:
        raise FamilyOracleError("prompt length exceeds model context")
    with torch.no_grad():
        logits, checkpoints = model._execute(tokens)
    return {
        "prompt_ids": list(prompt_ids),
        "token_id": int(torch.argmax(logits[0, -1]).item()),
        "checkpoints": {name: value.detach().cpu().tolist() for name, value in checkpoints.items()},
    }


def generate_tokens(model: FamilyFixedPointModel, prompt_ids: Sequence[int], *, steps: int) -> dict[str, Any]:
    """Generate with model-produced token feedback; expose the first semantic trace."""
    if steps < 1 or len(prompt_ids) + steps - 1 > model.spec.context:
        raise FamilyOracleError("generation steps exceed model context")
    first = capture_first_token(model, prompt_ids)
    context = list(prompt_ids)
    generated = [first["token_id"]]
    lengths = [len(context)]
    with torch.no_grad():
        for _ in range(1, steps):
            context.append(generated[-1])
            tokens = torch.tensor([context], dtype=torch.int64)
            model._validate_input(tokens)
            logits, _ = model._execute(tokens)
            generated.append(int(torch.argmax(logits[0, -1]).item()))
            lengths.append(len(context))
    return {"prompt_ids": list(prompt_ids), "tokens": generated,
            "context_lengths": lengths, "first_token": first}


def compare_logits(observed: Sequence[int], expected: Sequence[int]) -> None:
    if len(observed) != len(expected):
        raise FamilyOracleError(f"logit count differs: {len(observed)} vs {len(expected)}")
    for index, (actual, wanted) in enumerate(zip(observed, expected)):
        if actual != wanted:
            raise FamilyOracleError(f"logit {index}: observed {actual}, expected {wanted}")
