import sys
from pathlib import Path

import torch
from torch.ao.quantization.quantize_pt2e import convert_pt2e, prepare_pt2e
from torch.ao.quantization.quantizer.xnnpack_quantizer import (
    XNNPACKQuantizer, get_symmetric_quantization_config,
)
from transformers import GPTNeoForCausalLM


class Logits(torch.nn.Module):
    def __init__(self, snapshot):
        super().__init__()
        self.model = GPTNeoForCausalLM.from_pretrained(
            snapshot, local_files_only=True, attn_implementation='eager',
        )

    def forward(self, tokens):
        return self.model(tokens, use_cache=False, return_dict=False)[0]


def load_model(snapshot):
    return Logits(snapshot).eval()


def token_windows(path):
    tokens = torch.frombuffer(bytearray(path.read_bytes()), dtype=torch.int32).to(torch.int64)
    return [window.reshape(1, 32) for window in tokens.split(32) if window.numel() == 32]


def quantize(model, windows):
    exported = torch.export.export(model, (windows[0],), strict=False)
    config = get_symmetric_quantization_config(is_per_channel=True, is_dynamic=True)
    quantizer = XNNPACKQuantizer().set_module_type(torch.nn.Linear, config)
    prepared = prepare_pt2e(exported.module(), quantizer)
    for tokens in windows:
        prepared(tokens)
    return convert_pt2e(prepared, use_reference_representation=True)


if __name__ == '__main__':
    torch.set_num_threads(1)
    snapshot, calibration, output = sys.argv[1:]
    with torch.no_grad():
        windows = token_windows(Path(calibration))
        model = quantize(load_model(snapshot), windows[:-1])
        exported = torch.export.export(model, (windows[0],), strict=False)
        torch.export.save(exported, output)
