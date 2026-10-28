from __future__ import annotations

"""Independent adapter for the authenticated TinyStories-1M model package.

The adapter reconstructs the package's weights into the public Transformers
GPT-Neo state-dict ABI.  It does not import or copy kev-gpt implementation
code.  The package does not authenticate an activation rounding/clamping
policy, so its 97 calibrated Q/DQ boundaries are preserved in the receipt but
are deliberately not inserted as executable operations.
"""

import hashlib
import importlib.util
import json
import math
import os
import re
import sys
from pathlib import Path
from typing import Any, Mapping

import torch
from transformers import AutoConfig, AutoModelForCausalLM


EXPORT_STRICT = False
CONFIG_SHA256 = "ff74c30d5ebb5ab1da0f2ea479adf7197c504b42b5522a858c334ab91ed4958c"
CONTRACT_RELATIVE = Path("artifacts/reference/tinystories-1m-kev-gpt-contract.json")
VERIFIER_RELATIVE = Path("scripts/comparison/verify_tinystories_1m_reference_input.py")
VERIFIER_SHA256 = "4066f5535fbf64bd732cc65c78a07a3e1534037a3696a35fa3f4f73b4cdc586c"
CHECKPOINT_SHAPES: dict[str, tuple[int, ...]] = {
    "block.input": (64,),
    "block.ln_1.output": (64,),
    "block.attention.q": (16, 4),
    "block.attention.k": (16, 4),
    "block.attention.v": (16, 4),
    "block.attention.output": (64,),
    "block.residual.attention": (64,),
    "block.ln_2.output": (64,),
    "block.mlp.fc_in": (256,),
    "block.mlp.activation": (256,),
    "block.mlp.fc_out": (64,),
    "block.output": (64,),
}


class PackageAdapterError(ValueError):
    """The authenticated package could not be mapped without approximation."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


class PackageBundle:
    def __init__(
        self,
        *,
        contract: dict[str, Any],
        manifest: dict[str, Any],
        model: torch.nn.Module,
        state_dict: dict[str, torch.Tensor],
        receipt: dict[str, Any],
    ) -> None:
        self.contract = contract
        self.manifest = manifest
        self.model = model
        self.state_dict = state_dict
        self.receipt = receipt


def _require(condition: bool, code: str, message: str) -> None:
    if not condition:
        raise PackageAdapterError(code, message)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def canonical_sha256(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def receipt_sha256(receipt: Mapping[str, Any]) -> str:
    return canonical_sha256({key: value for key, value in receipt.items() if key != "receipt_sha256"})


def trace_sha256(trace: Mapping[str, Any]) -> str:
    return canonical_sha256({key: value for key, value in trace.items() if key != "trace_sha256"})


def _load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise PackageAdapterError("invalid_json", f"{label}: {error}") from error
    _require(isinstance(value, dict), "invalid_json", f"{label} must be an object")
    return value


def _load_verifier() -> Any:
    configured = os.environ.get("TINYSTORIES_REFERENCE_VERIFIER")
    verifier_path = Path(configured) if configured else _repo_root() / VERIFIER_RELATIVE
    _require(verifier_path.is_file(), "verifier_unavailable", f"missing frozen-input verifier: {verifier_path}")
    _require(
        _sha256(verifier_path) == VERIFIER_SHA256,
        "verifier_identity_mismatch",
        f"verifier SHA-256 is not the canonical frozen verifier: {verifier_path}",
    )
    spec = importlib.util.spec_from_file_location("tinystories_1m_reference_input_verifier", verifier_path)
    _require(spec is not None and spec.loader is not None, "verifier_unavailable", str(verifier_path))
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


def validate_verifier_receipt(
    receipt: object,
    contract: Mapping[str, Any],
    contract_path: Path | str,
    package_path: Path | str,
) -> None:
    """Require the canonical verifier to prove the exact compiler input."""

    contract_path = Path(contract_path)
    package_path = Path(package_path)
    package_contract = contract.get("package")
    model_contract = contract.get("model")
    _require(
        isinstance(receipt, dict)
        and isinstance(package_contract, dict)
        and isinstance(model_contract, dict),
        "verifier_identity_mismatch",
        "verifier receipt or frozen identity is malformed",
    )
    assert isinstance(receipt, dict)
    assert isinstance(package_contract, dict)
    assert isinstance(model_contract, dict)
    receipt_package = receipt.get("package")
    receipt_model = receipt.get("model")
    _require(
        receipt.get("schema") == "tinystories-1m-reference-compiler-input-v1"
        and receipt.get("status") == "identity_verified_adapter_required"
        and receipt.get("frozen_contract_sha256") == _sha256(contract_path)
        and isinstance(receipt.get("frozen_contract_path"), str)
        and Path(receipt["frozen_contract_path"]).resolve() == contract_path.resolve()
        and isinstance(receipt_package, dict)
        and isinstance(receipt_model, dict),
        "verifier_identity_mismatch",
        "canonical verifier did not return a verified contract-bound receipt",
    )
    assert isinstance(receipt_package, dict)
    assert isinstance(receipt_model, dict)
    expected_package = {
        "path": str(package_path),
        "manifest_sha256": package_contract.get("manifest_sha256"),
        "weights_sha256": package_contract.get("sha256"),
        "scales_sha256": package_contract.get("files", {}).get("scales.bin"),
        "calibration_ids_sha256": package_contract.get("files", {}).get("calibration_ids.bin"),
        "receipt_sha256": package_contract.get("files", {}).get("receipt.json"),
        "weight_bytes": contract.get("memory_image", {}).get("bytes"),
    }
    _require(
        all(receipt_package.get(key) == value for key, value in expected_package.items())
        and isinstance(receipt_package.get("path"), str)
        and Path(receipt_package["path"]).resolve() == package_path.resolve(),
        "verifier_identity_mismatch",
        "canonical verifier package identity differs from the frozen contract",
    )
    expected_model = {
        "source_model_id": model_contract.get("source_model_id"),
        "source_revision": model_contract.get("source_revision"),
        "model_type": "gpt_neo",
        "n_layer": model_contract.get("n_layer"),
        "hidden_size": model_contract.get("hidden_size"),
        "n_head": model_contract.get("n_head"),
        "head_dim": model_contract.get("head_dim"),
        "vocab_size": model_contract.get("vocab_size"),
        "max_context": model_contract.get("max_context"),
        "tie_word_embeddings": model_contract.get("tie_word_embeddings"),
        "activation_function": model_contract.get("activation_function"),
    }
    _require(
        receipt_model == expected_model,
        "verifier_identity_mismatch",
        "canonical verifier model identity differs from the frozen contract",
    )


def _expected_source_names(n_layer: int) -> set[str]:
    names = {
        "token_embedding.weight",
        "position_embedding.weight",
        "final_ln.weight",
        "final_ln.bias",
    }
    suffixes = {
        "ln1.weight",
        "ln1.bias",
        "ln2.weight",
        "ln2.bias",
        "attn.k.weight",
        "attn.q.weight",
        "attn.v.weight",
        "attn.out.weight",
        "attn.out.bias",
        "mlp.fc.weight",
        "mlp.fc.bias",
        "mlp.proj.weight",
        "mlp.proj.bias",
    }
    names.update(f"blocks.{layer}.{suffix}" for layer in range(n_layer) for suffix in suffixes)
    return names


_BLOCK_TARGET_SUFFIXES = {
    "ln1.weight": "ln_1.weight",
    "ln1.bias": "ln_1.bias",
    "ln2.weight": "ln_2.weight",
    "ln2.bias": "ln_2.bias",
    "attn.k.weight": "attn.attention.k_proj.weight",
    "attn.q.weight": "attn.attention.q_proj.weight",
    "attn.v.weight": "attn.attention.v_proj.weight",
    "attn.out.weight": "attn.attention.out_proj.weight",
    "attn.out.bias": "attn.attention.out_proj.bias",
    "mlp.fc.weight": "mlp.c_fc.weight",
    "mlp.fc.bias": "mlp.c_fc.bias",
    "mlp.proj.weight": "mlp.c_proj.weight",
    "mlp.proj.bias": "mlp.c_proj.bias",
}


def package_name_to_gpt_neo_key(name: str) -> str:
    direct = {
        "token_embedding.weight": "transformer.wte.weight",
        "position_embedding.weight": "transformer.wpe.weight",
        "final_ln.weight": "transformer.ln_f.weight",
        "final_ln.bias": "transformer.ln_f.bias",
    }
    if name in direct:
        return direct[name]
    match = re.fullmatch(r"blocks\.(\d+)\.(.+)", name)
    _require(match is not None, "source_tensor_set_mismatch", f"unknown package tensor {name!r}")
    suffix = _BLOCK_TARGET_SUFFIXES.get(match.group(2))
    _require(suffix is not None, "source_tensor_set_mismatch", f"unknown package tensor {name!r}")
    return f"transformer.h.{match.group(1)}.{suffix}"


def _shape(entry: Mapping[str, Any], name: str) -> tuple[int, ...]:
    value = entry.get("logical_shape")
    _require(
        isinstance(value, list)
        and bool(value)
        and all(isinstance(dim, int) and not isinstance(dim, bool) and dim > 0 for dim in value),
        "tensor_shape_mismatch",
        f"{name}: malformed logical_shape",
    )
    return tuple(value)


def _integer(entry: Mapping[str, Any], key: str, name: str) -> int:
    value = entry.get(key)
    _require(isinstance(value, int) and not isinstance(value, bool) and value >= 0,
             "malformed_tensor_entry", f"{name}.{key} is not a non-negative integer")
    return value


def _slice(image: bytes, offset: int, length: int, name: str, label: str) -> bytes:
    end = offset + length
    _require(end <= len(image), "package_extent_mismatch", f"{name} exceeds {label}")
    return image[offset:end]


def reconstruct_state_dict(
    manifest: Mapping[str, Any],
    weight_image: bytes,
    scale_image: bytes,
    target_shapes: Mapping[str, tuple[int, ...]],
) -> tuple[dict[str, torch.Tensor], dict[str, dict[str, Any]]]:
    """Reconstruct every package tensor and bind it to an exact GPT-Neo key."""

    model = manifest.get("model")
    tensors = manifest.get("tensors")
    _require(isinstance(model, dict) and isinstance(tensors, dict), "invalid_package", "model/tensors missing")
    n_layer = model.get("n_layer")
    _require(isinstance(n_layer, int) and not isinstance(n_layer, bool), "identity_mismatch", "n_layer missing")
    expected_sources = _expected_source_names(n_layer)
    _require(set(tensors) == expected_sources, "source_tensor_set_mismatch", "package tensor set is not exact")

    expected_targets = {package_name_to_gpt_neo_key(name) for name in expected_sources}
    expected_targets.add("lm_head.weight")
    _require(set(target_shapes) == expected_targets, "target_tensor_set_mismatch", "GPT-Neo state tensor set is not exact")
    _require(sys.byteorder == "little", "unsupported_host_endianness", "float32 image requires a little-endian host")

    state: dict[str, torch.Tensor] = {}
    mapping: dict[str, dict[str, Any]] = {}
    for source_name in sorted(tensors):
        entry = tensors[source_name]
        _require(isinstance(entry, dict), "malformed_tensor_entry", source_name)
        target_name = package_name_to_gpt_neo_key(source_name)
        shape = _shape(entry, source_name)
        _require(target_shapes.get(target_name) == shape, "tensor_shape_mismatch",
                 f"{source_name}: {shape} != {target_shapes.get(target_name)}")
        offset = _integer(entry, "offset", source_name)
        nbytes = _integer(entry, "nbytes", source_name)
        scale_offset = _integer(entry, "scale_offset", source_name)
        scale_nbytes = _integer(entry, "scale_nbytes", source_name)
        raw = _slice(weight_image, offset, nbytes, source_name, "weights.bin")
        _require(hashlib.sha256(raw).hexdigest() == entry.get("sha256"), "tensor_hash_mismatch", source_name)
        element_count = math.prod(shape)
        tensor_format = entry.get("format")
        if tensor_format == "float32":
            _require(nbytes == element_count * 4 and scale_nbytes == 0,
                     "tensor_shape_mismatch", f"{source_name}: float32 extent does not match shape")
            tensor = torch.frombuffer(bytearray(raw), dtype=torch.float32).clone().reshape(shape)
        elif tensor_format == "symmetric_int8_per_output":
            _require(nbytes == element_count, "tensor_shape_mismatch",
                     f"{source_name}: INT8 extent does not match shape")
            _require(entry.get("bits") == 8 and entry.get("signed") is True,
                     "malformed_tensor_entry", f"{source_name}: not signed INT8")
            _require(scale_nbytes == shape[0] * 4, "malformed_scale_image",
                     f"{source_name}: expected {shape[0]} little-endian float32 scales")
            scale_raw = _slice(scale_image, scale_offset, scale_nbytes, source_name, "scales.bin")
            _require(hashlib.sha256(scale_raw).hexdigest() == entry.get("scale_sha256"),
                     "scale_hash_mismatch", source_name)
            quantized = torch.frombuffer(bytearray(raw), dtype=torch.int8).clone().reshape(shape).to(torch.float32)
            scales = torch.frombuffer(bytearray(scale_raw), dtype=torch.float32).clone()
            _require(bool(torch.isfinite(scales).all()) and bool((scales > 0).all()),
                     "non_finite_tensor", f"{source_name}: scales must be finite and positive")
            broadcast = scales.reshape((shape[0],) + (1,) * (len(shape) - 1))
            tensor = quantized * broadcast
        else:
            raise PackageAdapterError("unsupported_tensor_format", f"{source_name}: {tensor_format!r}")
        _require(bool(torch.isfinite(tensor).all()), "non_finite_tensor", source_name)
        state[target_name] = tensor
        mapping[source_name] = {
            "target": target_name,
            "format": tensor_format,
            "shape": list(shape),
            "weight_sha256": entry.get("sha256"),
            "scale_sha256": entry.get("scale_sha256"),
        }

    state["lm_head.weight"] = state["transformer.wte.weight"]
    return state, mapping


def _expected_activation_names(n_layer: int) -> set[str]:
    names = {"lm_head.input"}
    suffixes = {
        "attn.attention.k_proj.input",
        "attn.attention.k_proj.output",
        "attn.attention.q_proj.input",
        "attn.attention.q_proj.output",
        "attn.attention.v_proj.input",
        "attn.attention.v_proj.output",
        "attn.attention.out_proj.input",
        "attn.attention.out_proj.output",
        "mlp.c_fc.input",
        "mlp.c_fc.output",
        "mlp.c_proj.input",
        "mlp.c_proj.output",
    }
    names.update(f"transformer.h.{layer}.{suffix}" for layer in range(n_layer) for suffix in suffixes)
    return names


def _activation_boundaries(manifest: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    model = manifest.get("model")
    values = manifest.get("activation_scales")
    _require(isinstance(model, dict) and isinstance(values, dict), "invalid_package", "activation metadata missing")
    expected = _expected_activation_names(model.get("n_layer"))
    _require(set(values) == expected, "activation_boundary_set_mismatch", "97 Q/DQ boundaries are not exact")
    result: dict[str, dict[str, Any]] = {}
    for name in sorted(values):
        scales = values[name]
        _require(
            isinstance(scales, list)
            and len(scales) in (64, 256)
            and all(
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and math.isfinite(value)
                and value > 0
                for value in scales
            ),
            "malformed_activation_scales",
            name,
        )
        normalized = [float(value) for value in scales]
        result[name] = {"width": len(normalized), "scales": normalized, "sha256": canonical_sha256(normalized)}
    return result


def _validate_config(model_path: Path, manifest: Mapping[str, Any]) -> Any:
    config_path = model_path / "config.json"
    _require(config_path.is_file(), "identity_mismatch", f"missing config.json under {model_path}")
    _require(_sha256(config_path) == CONFIG_SHA256, "identity_mismatch", "GPT-Neo config hash is not frozen")
    config = AutoConfig.from_pretrained(model_path, local_files_only=True)
    model = manifest["model"]
    expected = {
        "model_type": model["model_type"],
        "hidden_size": model["hidden_size"],
        "num_layers": model["n_layer"],
        "num_heads": model["n_head"],
        "vocab_size": model["vocab_size"],
        "window_size": model["local_window"],
        "activation_function": model["activation_function"],
    }
    for key, value in expected.items():
        _require(getattr(config, key, None) == value, "identity_mismatch", f"config.{key} differs from package")
    _require(config.max_position_embeddings >= model["max_context"], "identity_mismatch", "position capacity too small")
    _require(config.attention_layers == ["global", "local"] * 4,
             "identity_mismatch", "GPT-Neo attention-layer schedule is not the frozen schedule")
    config.max_position_embeddings = model["max_context"]
    config.use_cache = False
    return config


def load_authenticated_package(
    contract_path: Path | str,
    package_path: Path | str,
    model_path: Path | str,
) -> PackageBundle:
    """Verify first, then reconstruct and strictly load the GPT-Neo model."""

    contract_path = Path(contract_path)
    package_path = Path(package_path)
    model_path = Path(model_path)
    verifier = _load_verifier()
    # This call is intentionally the first package read performed here.
    verifier_receipt = verifier.verify_input(contract_path, package_path)

    contract = _load_json(contract_path, "contract")
    validate_verifier_receipt(verifier_receipt, contract, contract_path, package_path)
    manifest = _load_json(package_path / "manifest.json", "package manifest")
    config = _validate_config(model_path, manifest)
    model = AutoModelForCausalLM.from_config(config).eval()
    target_shapes = {name: tuple(tensor.shape) for name, tensor in model.state_dict().items()}
    state, mapping = reconstruct_state_dict(
        manifest,
        (package_path / "weights.bin").read_bytes(),
        (package_path / "scales.bin").read_bytes(),
        target_shapes,
    )
    incompatible = model.load_state_dict(state, strict=True)
    _require(not incompatible.missing_keys and not incompatible.unexpected_keys,
             "target_tensor_set_mismatch", str(incompatible))
    model.tie_weights()
    boundaries = _activation_boundaries(manifest)
    receipt: dict[str, Any] = {
        "schema": "tinystories-1m-package-gpt-neo-adapter-v1",
        "status": "dequantized_weight_export_ready",
        "identity": {
            "contract_sha256": _sha256(contract_path),
            "model": contract["model"],
            "tokenizer": contract["tokenizer"],
            "package": contract["package"],
            "config_sha256": CONFIG_SHA256,
        },
        "verified_input": verifier_receipt,
        "tensor_mapping": mapping,
        "tied_parameters": {"lm_head.weight": "transformer.wte.weight"},
        "quantization": {
            "weight_format": "symmetric_int8_per_output",
            "int8_weight_tensor_count": sum(
                entry["format"] == "symmetric_int8_per_output" for entry in mapping.values()
            ),
            "scale_format": "little-endian float32",
        },
        "activation_qdq_boundaries": boundaries,
        "activation_qdq_execution": {
            "status": "metadata_only",
            "reason_code": "activation_rounding_semantics_unavailable",
        },
        "example_input": {
            "prompt_tokens": contract["reference"]["prompt_tokens"],
            "shape": [1, len(contract["reference"]["prompt_tokens"])],
            "dtype": "torch.int64",
        },
    }
    receipt["receipt_sha256"] = receipt_sha256(receipt)
    return PackageBundle(
        contract=contract,
        manifest=manifest,
        model=model,
        state_dict=state,
        receipt=receipt,
    )


class _LogitsOnly(torch.nn.Module):
    def __init__(self, model: torch.nn.Module) -> None:
        super().__init__()
        self.model = model

    def forward(self, input_ids: torch.Tensor) -> torch.Tensor:
        return self.model(input_ids=input_ids, use_cache=False, return_dict=False)[0]


class _BlockZeroTokenStepTrace(torch.nn.Module):
    """Expose the fixed 12-checkpoint block-0 token-step interface."""

    def __init__(self, model: torch.nn.Module) -> None:
        super().__init__()
        self.transformer = model.transformer
        self.num_heads = int(model.config.num_heads)
        self.head_dim = int(model.config.hidden_size // model.config.num_heads)

    def forward(self, input_ids: torch.Tensor) -> tuple[torch.Tensor, ...]:
        batch, sequence = input_ids.shape
        positions = torch.arange(sequence, device=input_ids.device).unsqueeze(0).expand(batch, sequence)
        hidden = self.transformer.drop(self.transformer.wte(input_ids) + self.transformer.wpe(positions))
        block = self.transformer.h[0]
        block_input = hidden[:, -1, :]
        ln_1 = block.ln_1(hidden)
        attention = block.attn.attention
        q = attention.q_proj(ln_1).view(batch, sequence, self.num_heads, self.head_dim).transpose(1, 2)
        k = attention.k_proj(ln_1).view(batch, sequence, self.num_heads, self.head_dim).transpose(1, 2)
        v = attention.v_proj(ln_1).view(batch, sequence, self.num_heads, self.head_dim).transpose(1, 2)
        # GPT-Neo's public attention implementation deliberately does not
        # apply the common 1/sqrt(head_dim) scaling factor.
        scores = torch.matmul(q.to(torch.float32), k.to(torch.float32).transpose(-1, -2))
        causal = torch.ones((sequence, sequence), dtype=torch.bool, device=input_ids.device).tril()
        scores = torch.where(causal, scores, torch.full_like(scores, torch.finfo(scores.dtype).min))
        probabilities = torch.softmax(scores, dim=-1)
        merged = torch.matmul(probabilities, v).transpose(1, 2).contiguous().view(batch, sequence, -1)
        attention_output_all = attention.out_proj(merged)
        residual_attention_all = hidden + attention_output_all
        ln_2_all = block.ln_2(residual_attention_all)
        fc_in_all = block.mlp.c_fc(ln_2_all)
        activation_all = block.mlp.act(fc_in_all)
        fc_out_all = block.mlp.c_proj(activation_all)
        block_output_all = residual_attention_all + fc_out_all
        return (
            block_input[0],
            ln_1[0, -1],
            q[0, :, -1, :],
            k[0, :, -1, :],
            v[0, :, -1, :],
            attention_output_all[0, -1],
            residual_attention_all[0, -1],
            ln_2_all[0, -1],
            fc_in_all[0, -1],
            activation_all[0, -1],
            fc_out_all[0, -1],
            block_output_all[0, -1],
        )


def _tensor_payload(tensor: torch.Tensor) -> list[Any]:
    _require(bool(torch.isfinite(tensor).all()), "non_finite_trace", "trace contains non-finite values")
    return tensor.detach().cpu().tolist()


def build_numeric_trace_gate(bundle: PackageBundle) -> dict[str, Any]:
    prompt_ids = bundle.contract["reference"]["prompt_tokens"]
    prompt = torch.tensor([prompt_ids], dtype=torch.long)
    trace_module = _BlockZeroTokenStepTrace(bundle.model).eval()
    with torch.no_grad():
        reconstructed = tuple(trace_module(prompt))
    exported = torch.export.export(trace_module, (prompt,), strict=False)
    with torch.no_grad():
        replayed = tuple(exported.module()(prompt))
    _require(len(reconstructed) == len(CHECKPOINT_SHAPES) == len(replayed),
             "trace_schema_mismatch", "checkpoint count differs")
    checkpoints: dict[str, dict[str, Any]] = {}
    first_mismatch: str | None = None
    for (name, expected_shape), eager_tensor, exported_tensor in zip(
        CHECKPOINT_SHAPES.items(), reconstructed, replayed, strict=True
    ):
        _require(tuple(eager_tensor.shape) == expected_shape and tuple(exported_tensor.shape) == expected_shape,
                 "trace_schema_mismatch", f"{name}: shape differs")
        eager_value = _tensor_payload(eager_tensor)
        exported_value = _tensor_payload(exported_tensor)
        eager_sha = canonical_sha256(eager_value)
        exported_sha = canonical_sha256(exported_value)
        if eager_sha != exported_sha and first_mismatch is None:
            first_mismatch = name
        checkpoints[name] = {
            "shape": list(expected_shape),
            "package_reconstruction": eager_value,
            "package_reconstruction_sha256": eager_sha,
            "exported_program": exported_value,
            "exported_program_sha256": exported_sha,
        }
    trace: dict[str, Any] = {
        "schema": "tinystories-1m-package-export-trace-v1",
        "status": "matched" if first_mismatch is None else "mismatch",
        "identity": {
            "contract_sha256": bundle.receipt["identity"]["contract_sha256"],
            "package_manifest_sha256": bundle.receipt["identity"]["package"]["manifest_sha256"],
            "package_weights_sha256": bundle.receipt["identity"]["package"]["sha256"],
            "prompt_tokens_sha256": canonical_sha256(prompt_ids),
            "block_index": 0,
            "token_index": len(prompt_ids) - 1,
            "execution": "dequantized_package_weights",
        },
        "checkpoints": checkpoints,
        "first_mismatch": first_mismatch,
    }
    trace["trace_sha256"] = trace_sha256(trace)
    return trace


def export_bundle_program(bundle: PackageBundle) -> torch.export.ExportedProgram:
    """Export a previously authenticated bundle without reading inputs again."""

    prompt = torch.tensor([bundle.contract["reference"]["prompt_tokens"]], dtype=torch.long)
    return torch.export.export(_LogitsOnly(bundle.model).eval(), (prompt,), strict=EXPORT_STRICT)


def _default_contract() -> Path:
    configured = os.environ.get("TINYSTORIES_REFERENCE_CONTRACT")
    return Path(configured) if configured else _repo_root() / CONTRACT_RELATIVE


def _default_package(contract_path: Path) -> Path:
    configured = os.environ.get("TINYSTORIES_REFERENCE_PACKAGE")
    if configured:
        return Path(configured)
    return Path(_load_json(contract_path, "contract")["package"]["origin"])


def build_model(model_path: str | None) -> torch.nn.Module:
    if model_path is None:
        raise RuntimeError("TinyStories reference-package adapter requires --model-path")
    contract = _default_contract()
    return load_authenticated_package(contract, _default_package(contract), Path(model_path)).model


def example_inputs() -> tuple[torch.Tensor, ...]:
    contract = _load_json(_default_contract(), "contract")
    return (torch.tensor([contract["reference"]["prompt_tokens"]], dtype=torch.long),)


def export_program(model_path: str | None) -> torch.export.ExportedProgram:
    """Return the compiler-facing, logits-only GPT-Neo ExportedProgram."""

    if model_path is None:
        raise RuntimeError("TinyStories reference-package adapter requires --model-path")
    contract = _default_contract()
    bundle = load_authenticated_package(contract, _default_package(contract), Path(model_path))
    return export_bundle_program(bundle)


def export_program_with_package(
    model_path: str | Path,
    package_path: str | Path,
    contract_path: str | Path,
) -> torch.export.ExportedProgram:
    """Export only after authenticating the explicit compiler package inputs.

    This is the package-aware frontend entry point used by the generic
    materializer.  Keeping all three paths explicit prevents the compiler
    pipeline from silently falling back to the ordinary FP32 HuggingFace
    adapter when a package build is requested.
    """

    if not model_path or not package_path or not contract_path:
        raise PackageAdapterError(
            "package_frontend_inputs_missing",
            "model, package, and frozen contract paths are required",
        )
    bundle = load_authenticated_package(
        Path(contract_path), Path(package_path), Path(model_path)
    )
    return export_bundle_program(bundle)
