import sys
from pathlib import Path

import torch
import torch.ao.quantization.fx._decomposed as quantized


def choose_qparams_tensor(values, qmin, qmax, eps, dtype):
    assert values.dtype in (torch.float32, torch.float16, torch.bfloat16)
    assert dtype in quantized._DTYPE_TO_QVALUE_BOUNDS
    quantized.validate_qmin_qmax(qmin, qmax)
    low, high = torch.aminmax(values)
    # Integer extraction avoids unsupported symbolic Boolean conversions in the importer.
    valid = (low <= high).to(torch.int64).item()
    torch.ops.aten._assert_scalar.default(valid == 1, 'invalid quantization range')
    low = torch.minimum(low, torch.zeros_like(low))
    high = torch.maximum(high, torch.zeros_like(high))
    scale = torch.maximum((high - low) / float(qmax - qmin),
                          torch.full((1,), eps, device=values.device))
    zero = qmin - torch.round(low / scale).to(torch.int32)
    return scale.to(torch.float64), zero.clamp(qmin, qmax).to(torch.int64)


def quantization_decompositions():
    names = ('choose_qparams_per_token', 'quantize_per_token', 'dequantize_per_token')
    table = {getattr(torch.ops.quantized_decomposed, name).default: getattr(quantized, name)
             for name in names}
    table[torch.ops.quantized_decomposed.choose_qparams.tensor] = choose_qparams_tensor
    return table


def freeze_buffers(program):
    model = program.module()
    # This pinned importer freezes parameters but leaves buffers as runtime inputs.
    # Register constant buffers as non-trainable parameters without changing values.
    for name, value in list(model.named_buffers()):
        parent, _, attribute = name.rpartition('.')
        owner = model.get_submodule(parent)
        delattr(owner, attribute)
        owner.register_parameter(attribute, torch.nn.Parameter(value, requires_grad=False))
    return torch.export.export(model, program.example_inputs[0], strict=False)


if __name__ == '__main__':
    from torch_mlir import fx

    torch.set_num_threads(1)
    with torch.no_grad():
        program = freeze_buffers(torch.export.load(sys.argv[1]))
        table = fx.get_decomposition_table() | quantization_decompositions()
        module = fx.export_and_import(program, output_type='raw', decomposition_table=table)
        module.operation.verify()
        Path(sys.argv[2]).write_text(str(module))
