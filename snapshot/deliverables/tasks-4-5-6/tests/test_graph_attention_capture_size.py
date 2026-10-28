"""Attention graph size must not grow by replicating every causal position."""
import unittest
import torch
from compiler.frontend.package_model import FixedPointModel
from compiler.frontend.fixed_point_ops import _exp_table


class Attention(torch.nn.Module):
    def __init__(self):
        super().__init__()
        self.register_buffer('_exp_lut', _exp_table())

    def forward(self, query, key, value):
        return FixedPointModel._attention(self, query, key, value)


class AttentionCaptureSizeTests(unittest.TestCase):
    def test_attention_keeps_one_tensor_graph_for_all_required_context_lengths(self):
        counts = []
        for context in (4, 19):
            args = tuple(torch.zeros((1, context, 64), dtype=torch.int64) for _ in range(3))
            graph = torch.export.export(Attention(), args).graph_module.graph
            counts.append(sum(node.op == 'call_function' for node in graph.nodes))
        self.assertLess(max(counts), 200, counts)
        self.assertLessEqual(abs(counts[1] - counts[0]), 8, counts)


def scalar_attention(query, key, value):
    """Scalar i64 arithmetic; no tensor graph or compiler helper constructs answers."""
    import math
    def wrap(number):
        return (number + (1 << 63)) % (1 << 64) - (1 << 63)
    q, k, v = query.tolist()[0], key.tolist()[0], value.tolist()[0]
    result = [[0] * 64 for _ in q]
    table = [round(math.exp((index - 4096) / 256) * (1 << 20)) for index in range(4096)]
    for position in range(len(q)):
        for head in range(16):
            scores = [wrap(sum(wrap(q[position][head*4+lane] * k[p][head*4+lane])
                               for lane in range(4))) >> 24 for p in range(position+1)]
            maximum = max(scores)
            delta = [max(-4096, min(0, wrap(score - maximum))) for score in scores]
            probabilities = [(1 << 20) if d == 0 else table[min(4096+d, 4095)] for d in delta]
            denominator = sum(probabilities)
            for lane in range(4):
                numerator = wrap(sum(wrap(probability * v[p][head*4+lane])
                                     for p, probability in enumerate(probabilities)))
                rounded = wrap(numerator + (-(denominator//2) if numerator < 0 else denominator//2))
                # The production helper uses signed-i64 abs before floor division.
                quotient = wrap(abs(rounded)) // denominator
                result[position][head*4+lane] = -quotient if rounded < 0 else quotient
    return torch.tensor([result], dtype=torch.int64)


class AttentionIntegerTests(unittest.TestCase):
    def test_all_required_contexts_match_independent_scalar_integer_reference(self):
        generator = torch.Generator().manual_seed(713)
        for context in range(4, 20):
            args = tuple(torch.randint(-(1 << 20), 1 << 20, (1, context, 64),
                                      dtype=torch.int64, generator=generator) for _ in range(3))
            with self.subTest(context=context):
                self.assertTrue(torch.equal(Attention()(*args), scalar_attention(*args)))

    def test_causal_ties_signed_rounding_and_wrapping_products(self):
        for query_value, key_value in [(0, 0), ((1 << 63)-1, -7), (-(1 << 63), 3)]:
            q = torch.full((1, 4, 64), query_value, dtype=torch.int64)
            k = torch.full_like(q, key_value)
            v = torch.arange(256, dtype=torch.int64).reshape(1, 4, 64) - 128
            self.assertTrue(torch.equal(Attention()(q, k, v), scalar_attention(q, k, v)))
