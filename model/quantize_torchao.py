"""Export the quantization scheme used by ~/TorchLean/compile_tinystories.py.

Linear layers use TorchAO's version-2 INT8 weights per output channel and
symmetric dynamic INT8 activations per token. Other layers stay floating point.
"""

import sys
from pathlib import Path

import torch
from torch._inductor.decomposition import decompositions
from torchao.quantization import Int8DynamicActivationInt8WeightConfig, quantize_
from torchao.utils import unwrap_tensor_subclass

from model.quantize import load_model, token_windows


def quantize(model):
    quantize_(model, Int8DynamicActivationInt8WeightConfig(version=2))
    return model


def int_mm(left, right):
    # Preserve signed INT8 values and INT32 accumulation in portable ATen.
    return torch.mm(left.to(torch.int32), right.to(torch.int32))


def export_model(model, tokens):
    unwrap_tensor_subclass(model)
    model.requires_grad_(False)
    # PyTorch 2.9's archive loader reconstructs Parameters with requires_grad=True.
    # Store integer weights as buffers so they can be loaded without gradients.
    for name, value in list(model.named_parameters()):
        if not value.is_floating_point() and not value.is_complex():
            parent, _, attribute = name.rpartition('.')
            owner = model.get_submodule(parent)
            delattr(owner, attribute)
            owner.register_buffer(attribute, value.detach())
    program = torch.export.export(model, (tokens,), strict=False)
    # Use TorchAO's own implementations, keeping its scale, rounding and range.
    table = {getattr(torch.ops.torchao, name).default:
             decompositions[getattr(torch.ops.torchao, name).default]
             for name in ('choose_qparams_affine', 'quantize_affine')}
    table[torch.ops.aten._int_mm.default] = int_mm
    return program.run_decompositions(table)


if __name__ == '__main__':
    torch.set_num_threads(1)
    snapshot, calibration, output = sys.argv[1:]
    with torch.no_grad():
        tokens = token_windows(Path(calibration))[0]
        model = quantize(load_model(snapshot))
        torch.export.save(export_model(model, tokens), output)
