"""Capture the declared static context domain without expected token inputs.

These graphs are inputs to generation linking, not evidence that feedback
control or full inference hardware is already implemented.
"""
from pathlib import Path
import torch
from .frontend.capture import CaptureReceipt, capture_model
from .frontend.package_inputs import CompileInputs
from .frontend.package_model import build_fixed_point_model


REQUIRED_CONTEXTS = tuple(range(4, 20))


class GenerationStep(torch.nn.Module):
    """Graph-derived next token and last-row logits; no feedback controller."""
    def __init__(self, model):
        super().__init__()
        self.model = model

    def forward(self, tokens):
        logits = self.model(tokens)[0, -1]
        # PyTorch argmax selects the first index on a tie. Both the logits and
        # reduction remain computations in the exported graph.
        return torch.argmax(logits), logits


def capture_contexts(inputs: CompileInputs, lengths: tuple[int, ...],
                     output_dir: Path) -> tuple[CaptureReceipt, ...]:
    return _capture_contexts(inputs, lengths, output_dir, step=False)


def capture_generation_contexts(inputs: CompileInputs, lengths: tuple[int, ...],
                                output_dir: Path) -> tuple[CaptureReceipt, ...]:
    """Capture all sixteen step functions for subsequent native linking."""
    return _capture_contexts(inputs, lengths, output_dir, step=True)


def _capture_contexts(inputs, lengths, output_dir, *, step):
    if type(lengths) is not tuple or lengths != REQUIRED_CONTEXTS or any(
            type(length) is not int for length in lengths):
        raise ValueError('generation requires ordered unique contexts 4 through 19')
    model = build_fixed_point_model(inputs)
    if step:
        model = GenerationStep(model)
    receipts = []
    for length in lengths:
        # Example values specify tensor shape/dtype only. Capture preserves the
        # token tensor as a runtime graph argument; no generated token is read.
        example = torch.ones((1, length), dtype=torch.int64)
        receipt = capture_model(model, (example,),
                                Path(output_dir) / f'context-{length:02d}')
        if (receipt.user_input_count != 1 or
                receipt.inputs[0]['shape'] != [1, length] or
                receipt.inputs[0]['dtype'] != 'torch.int64'):
            raise ValueError(f'context {length}: runtime token signature changed')
        receipts.append(receipt)
    return tuple(receipts)
