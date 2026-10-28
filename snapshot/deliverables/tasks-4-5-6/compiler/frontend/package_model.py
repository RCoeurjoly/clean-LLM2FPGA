"""PyTorch computation over authenticated package data, without golden answers."""
import math
import sys
import torch
from .package_inputs import CompileInputs
from .fixed_point_ops import (
    Q_VALUE, Q_SCALE, serial_gemv, round_shift_signed, activation_qdq,
    _fixed_layer_norm, _trunc_divide, _gelu_table, _exp_table,
)


def _slice(image, offset, size, label):
    if not isinstance(offset, int) or not isinstance(size, int) or offset < 0 or size <= 0:
        raise ValueError(f'invalid tensor extent: {label}')
    if offset + size > len(image):
        raise ValueError(f'tensor extent outside image: {label}')
    return bytearray(image[offset:offset + size])


def _quantize(values, fraction, low, high, label):
    rounded = torch.round(values.to(torch.float64) * (1 << fraction))
    if not bool((torch.isfinite(rounded) & (rounded >= low) & (rounded < high)).all()):
        raise ValueError(f'fixed-point materialization outside range: {label}')
    return rounded.to(torch.int64)


def materialize_tensors(inputs):
    if sys.byteorder != 'little':
        raise ValueError('package materialization requires little-endian host')
    codes, scales, parameters = {}, {}, {}
    for name, descriptor in inputs.manifest['tensors'].items():
        shape = tuple(descriptor['logical_shape'])
        if not shape or any(not isinstance(n, int) or n <= 0 for n in shape):
            raise ValueError(f'invalid tensor shape: {name}')
        raw = _slice(inputs.weights, descriptor['offset'], descriptor['nbytes'], name)
        if descriptor['format'] == 'symmetric_int8_per_output':
            if len(raw) != math.prod(shape):
                raise ValueError(f'tensor byte count differs: {name}')
            codes[name] = torch.frombuffer(raw, dtype=torch.int8).clone().reshape(shape).to(torch.int64)
            scale_raw = _slice(inputs.scales, descriptor['scale_offset'],
                               descriptor['scale_nbytes'], name)
            if len(scale_raw) != shape[0] * 4:
                raise ValueError(f'per-output scale count differs: {name}')
            scales[name] = _quantize(torch.frombuffer(scale_raw, dtype=torch.float32),
                                    Q_SCALE, 1, 1 << 24, name)
        elif descriptor['format'] == 'float32':
            if len(raw) != math.prod(shape) * 4:
                raise ValueError(f'tensor byte count differs: {name}')
            parameters[name] = _quantize(torch.frombuffer(raw, dtype=torch.float32).reshape(shape),
                                        Q_VALUE, -(1 << 31), 1 << 31, name)
        else:
            raise ValueError(f'unsupported tensor format: {name}')
    for name, values in inputs.manifest['activation_scales'].items():
        scales[f'activation::{name}'] = _quantize(torch.tensor(values, dtype=torch.float64),
                                                Q_SCALE, 1, 1 << 24, name)
    return codes, scales, parameters


class FixedPointModel(torch.nn.Module):
    def __init__(self, inputs):
        super().__init__()
        self.geometry = dict(inputs.geometry)
        self._buffer_names = {}
        for prefix, values in zip(('code', 'scale', 'parameter'), materialize_tensors(inputs)):
            for index, (name, tensor) in enumerate(sorted(values.items())):
                attribute = f'_{prefix}_{index}'
                self.register_buffer(attribute, tensor)
                self._buffer_names[f'{prefix}::{name}'] = attribute
        self.register_buffer('_gelu_lut', _gelu_table())
        self.register_buffer('_exp_lut', _exp_table())

    def _buffer(self, prefix, name):
        return getattr(self, self._buffer_names[f'{prefix}::{name}'])

    def _activation_scale(self, name):
        return self._buffer('scale', f'activation::{name}')

    def _embedding(self, name, rows):
        return round_shift_signed(self._buffer('code', name)[rows] *
                                  self._buffer('scale', name)[rows].unsqueeze(-1), 8)

    def _gemv(self, values, weight, bias, module):
        flattened = values.reshape(-1, values.shape[-1])
        input_scale = self._activation_scale(f'{module}.input')
        input_codes, _ = activation_qdq(flattened, input_scale)
        accumulator = serial_gemv(input_codes * input_scale, self._buffer('code', weight))
        real = round_shift_signed(accumulator * self._buffer('scale', weight), 32)
        if bias is not None:
            real = real + self._buffer('parameter', bias)
        _, result = activation_qdq(real, self._activation_scale(f'{module}.output'))
        return result.reshape(values.shape[:-1] + (result.shape[-1],))

    def _fixed_gelu(self, values):
        biased = torch.clamp(round_shift_signed(values, 4), -32768, 32767) + 32768
        index = torch.bitwise_right_shift(biased, 3)
        fraction = torch.bitwise_and(biased, 7)
        upper = torch.minimum(index + 1, torch.full_like(index, 8191))
        interpolated = self._gelu_lut[index] + torch.bitwise_right_shift(
            (self._gelu_lut[upper] - self._gelu_lut[index]) * fraction, 3)
        return torch.bitwise_left_shift(interpolated, 4)

    def _attention(self, query, key, value):
        # Axes are batch, query position, key position, head, head lane.
        # Tensor reductions become structured loops; Python does not replicate
        # the graph for each head or causal position.
        batch, context, _ = query.shape
        q = query.reshape(batch, context, 16, 4).unsqueeze(2)
        k = key.reshape(batch, context, 16, 4).unsqueeze(1)
        v = value.reshape(batch, context, 16, 4).unsqueeze(1)
        scores = torch.bitwise_right_shift(torch.sum(q * k, dim=-1, dtype=torch.int64), 24)
        positions = torch.arange(context, dtype=torch.int64, device=query.device)
        causal = (positions.unsqueeze(0) - positions.unsqueeze(1) < 1).reshape(1, context, context, 1)
        maximum = torch.amax(torch.where(causal, scores, torch.full_like(scores, -(1 << 63))),
                             dim=2, keepdim=True)
        # Invalid keys are excluded before normalization and contribute zero
        # probability to both reductions. Every query has at least itself.
        delta = torch.clamp(torch.where(causal, scores, maximum) - maximum, -4096, 0)
        index = torch.minimum(4096 + delta, torch.full_like(delta, 4095))
        probabilities = torch.where(delta == 0, torch.full_like(delta, 1 << 20),
                                    self._exp_lut[index])
        probabilities = torch.where(causal, probabilities, torch.zeros_like(probabilities))
        denominator = torch.sum(probabilities, dim=2, dtype=torch.int64).unsqueeze(-1)
        numerator = torch.sum(probabilities.unsqueeze(-1) * v, dim=2, dtype=torch.int64)
        half = torch.div(denominator, 2, rounding_mode='floor')
        result = _trunc_divide(numerator + torch.where(numerator < 0, -half, half), denominator)
        return result.reshape(batch, context, 64)

    def _block(self, x, layer):
        block, source = f'blocks.{layer}', f'transformer.h.{layer}'
        normalized = _fixed_layer_norm(x, self._buffer('parameter', f'{block}.ln1.weight'),
                                      self._buffer('parameter', f'{block}.ln1.bias'))
        q, k, v = (self._gemv(normalized, f'{block}.attn.{key}.weight', None,
                              f'{source}.attn.attention.{key}_proj') for key in ('q', 'k', 'v'))
        residual = x + self._gemv(self._attention(q, k, v), f'{block}.attn.out.weight',
                                 f'{block}.attn.out.bias', f'{source}.attn.attention.out_proj')
        normalized = _fixed_layer_norm(residual, self._buffer('parameter', f'{block}.ln2.weight'),
                                      self._buffer('parameter', f'{block}.ln2.bias'))
        hidden = self._gemv(normalized, f'{block}.mlp.fc.weight', f'{block}.mlp.fc.bias',
                            f'{source}.mlp.c_fc')
        return residual + self._gemv(self._fixed_gelu(hidden), f'{block}.mlp.proj.weight',
                                    f'{block}.mlp.proj.bias', f'{source}.mlp.c_proj')

    def forward(self, input_ids):
        if not torch.compiler.is_compiling():
            if input_ids.dtype != torch.int64 or input_ids.ndim != 2 or input_ids.shape[0] != 1:
                raise ValueError('expected int64 token IDs with shape [1, context]')
            if not 1 <= input_ids.shape[1] <= 32:
                raise ValueError('context capacity exceeded')
            if not bool(((input_ids >= 0) & (input_ids < 50257)).all()):
                raise ValueError('token ID outside vocabulary')
        positions = torch.arange(input_ids.shape[1], dtype=torch.int64,
                                 device=input_ids.device).unsqueeze(0)
        x = self._embedding('token_embedding.weight', input_ids)
        x = x + self._embedding('position_embedding.weight', positions)
        for layer in range(8):
            x = self._block(x, layer)
        normalized = _fixed_layer_norm(x, self._buffer('parameter', 'final_ln.weight'),
                                      self._buffer('parameter', 'final_ln.bias'))
        scale = self._activation_scale('lm_head.input')
        codes, _ = activation_qdq(normalized.reshape(-1, 64), scale)
        accumulator = serial_gemv(codes * scale, self._buffer('code', 'token_embedding.weight'))
        return round_shift_signed(accumulator * self._buffer('scale', 'token_embedding.weight'),
                                  32).reshape(1, input_ids.shape[1], 50257)


def build_fixed_point_model(inputs: CompileInputs):
    expected = {'model_type': 'gpt_neo', 'hidden_size': 64, 'n_head': 16,
                'head_dim': 4, 'n_layer': 8, 'vocab_size': 50257, 'max_context': 32,
                'activation_function': 'gelu_new', 'tie_word_embeddings': True}
    if any(inputs.geometry.get(key) != value for key, value in expected.items()):
        raise ValueError('unsupported fixed-point geometry; no fallback generator')
    if inputs.arithmetic != {'profile': 'tinystories-1m-integer-v1',
                             'value_fraction_bits': 16, 'scale_fraction_bits': 24}:
        raise ValueError('unsupported arithmetic contract')
    return FixedPointModel(inputs).eval()
