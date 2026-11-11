import argparse
import inspect
import importlib.util
import json
import operator
from pathlib import Path
import torch
import torch.nn as nn
import torch.nn.functional as F

FORMAT = "torchlean.ir.v2"

def _dtype_of(value):
    dtype = getattr(value, 'dtype', None)
    name = str(dtype).removeprefix('torch.')
    if name in ('float16', 'bfloat16', 'float32', 'float64', 'bool'): return name
    if name.startswith(('int', 'uint')) and name.lstrip('uint').isdigit():
        return {'kind': 'integer', 'signed': not name.startswith('uint'),
                'width': int(name.lstrip('uint'))}
    raise NotImplementedError(f'unsupported PyTorch element type: {dtype}')

def _node_dtype(node):
    return _dtype_of(node.meta.get('val', node.meta.get('tensor_meta')))

def _shape_of(value):
    if hasattr(value, "shape"):
        return [int(x) for x in tuple(value.shape)]
    if isinstance(value, (tuple, list)) and value and hasattr(value[0], "shape"):
        return [int(x) for x in tuple(value[0].shape)]
    return []

def _node_shape(node):
    return _shape_of(node.meta.get("val", node.meta.get("tensor_meta")))

def _shape_from_arg(arg):
    return _node_shape(arg) if hasattr(arg, "meta") else []

def _tuple_shapes_of_node(node):
    val = node.meta.get("val", None)
    if isinstance(val, (tuple, list)) and not hasattr(val, "shape"):
        return [_shape_of(x) for x in val]
    tm = node.meta.get("tensor_meta", None)
    if hasattr(tm, "shape"):
        return []
    if isinstance(tm, (tuple, list)):
        return [_shape_of(x) for x in tm]
    return []

def _is_getitem(node):
    return node.op == "call_function" and node.target is operator.getitem

def _is_getattr(node):
    return node.op == "call_function" and node.target is getattr

def _tuple_kind(node, model=None):
    if node.op == "call_module" and model is not None:
        mod = model.get_submodule(str(node.target))
        if isinstance(mod, nn.MultiheadAttention):
            call = inspect.signature(mod.forward).bind(*node.args, **node.kwargs)
            call.apply_defaults()
            arguments = call.arguments
            if mod.add_zero_attn or mod.bias_k is not None or mod.bias_v is not None:
                raise NotImplementedError("nn.MultiheadAttention with added bias/zero sequence entries is outside the current TorchLean IR import subset")
            if mod.kdim != mod.embed_dim or mod.vdim != mod.embed_dim:
                raise NotImplementedError("nn.MultiheadAttention with kdim/vdim different from embed_dim is outside the current TorchLean IR import subset")
            if arguments.get("attn_mask", None) is not None or arguments.get("key_padding_mask", None) is not None:
                raise NotImplementedError("masked nn.MultiheadAttention needs an explicit TorchLean hard-mask lowering")
            if bool(arguments.get("is_causal", False)):
                raise NotImplementedError("causal nn.MultiheadAttention needs an explicit TorchLean hard-mask lowering")
            return {
                "kind": "multihead_attention",
                "embed_dim": int(mod.embed_dim),
                "num_heads": int(mod.num_heads),
                "batch_first": bool(mod.batch_first),
                "dropout_zero": bool(float(mod.dropout) == 0.0),
                "bias": bool(mod.in_proj_bias is not None),
                **_multihead_attention_payload(mod),
            }
    raise NotImplementedError(f"unsupported tuple-valued PyTorch op: {node.target}")

def _load_model(module_path: str, ctor_name: str):
    spec = importlib.util.spec_from_file_location("torchlean_user_model", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load Python module from {module_path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    ctor = getattr(mod, ctor_name)
    model = ctor()
    model.eval()
    return model

def _target_name(target):
    return str(target).replace("torch.ops.", "")

def _is_op(node, aten=(), functions=(), methods=()):
    # ATen overloads have distinct contracts: exp.out writes to an output tensor.
    # FX keeps Python callables or method names, so match those identities explicitly.
    if node.op == "call_function":
        if isinstance(node.target, torch._ops.OpOverload):
            return str(node.target) in aten
        return any(node.target is function for function in functions)
    return node.op == "call_method" and node.target in methods

def _nonnegative(value, model=None):
    # Structural evidence also excludes negative zero, not just negative numbers.
    if not isinstance(value, torch.fx.Node): return False
    if _is_op(value, ('aten.exp.default', 'aten.abs.default'),
                  (torch.exp, torch.abs, torch.absolute), ('exp', 'abs', 'absolute')):
        return True
    if value.op == 'call_module' and model is not None:
        return isinstance(model.get_submodule(str(value.target)), (nn.Softplus, nn.Sigmoid))
    if _is_op(value, ('aten.clamp_min.default',), (torch.clamp_min,), ('clamp_min',)):
        lower = value.args[1] if len(value.args) > 1 else value.kwargs.get('min')
        return isinstance(lower, (int, float)) and lower > 0
    return False

def _node_refs(obj, node_to_id, strict=False):
    refs = []
    def visit(x):
        if isinstance(x, torch.fx.Node):
            ref = node_to_id.get(x, None)
            if ref is not None:
                refs.append(ref)
            elif strict:
                raise NotImplementedError('graph result has no tensor lowering')
        elif isinstance(x, (tuple, list)):
            for y in x: visit(y)
        elif isinstance(x, dict):
            for y in x.values(): visit(y)
        elif strict:
            raise NotImplementedError('graph return leaves must be tensors')
    visit(obj)
    return refs

def _spatial_tuple(x, rank, default):
    if x is None: return [int(default)] * rank
    if isinstance(x, int): return [int(x)] * rank
    if isinstance(x, (tuple, list)) and len(x) == rank: return [int(v) for v in x]
    raise NotImplementedError(f"expected an integer or length-{rank} spatial tuple, got {x!r}")

def _top_k_mask(node):
    if not _is_op(node, ('aten.scatter.value',)): return None
    base, dim, index, value = (list(node.args) + [None] * 4)[:4]
    if value is None: value = node.kwargs.get('value')
    if not isinstance(value, (int, float)) or float(value) != 1.0: return None
    if not _is_getitem(index) or index.args[1] != 1: return None
    topk = index.args[0]
    if not _is_op(topk, ('aten.topk.default',)): return None
    source, k = topk.args[0], topk.args[1]
    rank = len(_shape_from_arg(source))
    topk_dim = topk.args[2] if len(topk.args) > 2 else topk.kwargs.get('dim', -1)
    largest = topk.args[3] if len(topk.args) > 3 else topk.kwargs.get('largest', True)
    if not largest or _normalize_axis(topk_dim, rank) != rank - 1 or _normalize_axis(dim, rank) != rank - 1: return None
    if not _zeros_like(base) or _shape_from_arg(base) != _shape_from_arg(source): return None
    if _node_dtype(base) != _node_dtype(source) or _node_dtype(node) != _node_dtype(source): return None
    return source, int(k)

def _routing_helpers(graph):
    masks = [node for node in graph.nodes if _top_k_mask(node) is not None]
    helpers = set()
    for mask in masks:
        base, index = mask.args[0], mask.args[2]
        topk = index.args[0]
        helpers |= {base, index, topk} | {user for user in topk.users if _is_getitem(user) and not user.users}
    allowed = set(masks) | helpers
    for helper in helpers:
        if any(user not in allowed for user in helper.users):
            raise NotImplementedError('torch.topk values, or zeros shared with other operations, are outside the TorchLean routing idiom')
    return helpers

def _tensor_values(value):
    return value.detach().cpu().tolist()

def _tensor_bits(value):
    name = str(value.dtype).removeprefix('torch.')
    if value.dtype is torch.bool:
        return [int(item) for item in value.detach().cpu().reshape(-1).tolist()]
    width = {'float16': 16, 'bfloat16': 16, 'float32': 32, 'float64': 64}.get(name)
    if width is None and name.startswith(('int', 'uint')):
        width = value.element_size() * 8
    if width is None: raise NotImplementedError(f'no binary word encoding for {name}')
    integer = {8: torch.int8, 16: torch.int16, 32: torch.int32, 64: torch.int64}[width]
    words = value.detach().cpu().contiguous().reshape(-1).view(integer).tolist()
    return [int(word) % (1 << width) for word in words]

def _binary_payload(name, value):
    return {name + '_bits': _tensor_bits(value), name + '_dtype': _dtype_of(value)}

def _constant(value):
    result = {"kind": "const", 'shape': list(value.shape), 'value_kind': 'tensor',
              'dtype': _dtype_of(value), 'bits': _tensor_bits(value)}
    # Flat row-major values, as the payload loader reads them for every constant rank.
    if torch.isfinite(value).all(): result['values'] = value.detach().cpu().reshape(-1).tolist()
    return result

_TENSOR_VALUES = {}

def _resolve_tensor(value):
    if isinstance(value, torch.Tensor): return value
    if isinstance(value, torch.fx.Node) and value.name in _TENSOR_VALUES: return _TENSOR_VALUES[value.name]
    raise NotImplementedError(f"could not resolve tensor argument {value!r} into the exported payload")

def _linear_payload(weight, bias):
    weight = _resolve_tensor(weight)
    if weight.ndim != 2:
        raise NotImplementedError(f"linear weight must have rank 2, got shape {tuple(weight.shape)}")
    out_dim, in_dim = (int(value) for value in weight.shape)
    bias = _resolve_tensor(bias) if bias is not None else weight.new_zeros(out_dim)
    if tuple(bias.shape) != (out_dim,):
        raise NotImplementedError(f"linear bias must have shape {(out_dim,)}, got {tuple(bias.shape)}")
    return {"out_dim": out_dim, "in_dim": in_dim, "weight": _tensor_values(weight), "bias": _tensor_values(bias), **_binary_payload('weight', weight), **_binary_payload('bias', bias)}

def _multihead_attention_payload(mod):
    if mod.in_proj_weight is None:
        raise NotImplementedError("nn.MultiheadAttention with separate q/k/v projection weights is outside the current TorchLean IR import subset")
    embed_dim = int(mod.embed_dim)
    if tuple(mod.in_proj_weight.shape) != (3 * embed_dim, embed_dim):
        raise NotImplementedError(f"unexpected nn.MultiheadAttention in_proj_weight shape {tuple(mod.in_proj_weight.shape)}")
    q_weight, k_weight, v_weight = mod.in_proj_weight.chunk(3, dim=0)
    if mod.in_proj_bias is None:
        q_bias = k_bias = v_bias = mod.in_proj_weight.new_zeros(embed_dim)
    else:
        if tuple(mod.in_proj_bias.shape) != (3 * embed_dim,):
            raise NotImplementedError(f"unexpected nn.MultiheadAttention in_proj_bias shape {tuple(mod.in_proj_bias.shape)}")
        q_bias, k_bias, v_bias = mod.in_proj_bias.chunk(3, dim=0)
    if tuple(mod.out_proj.weight.shape) != (embed_dim, embed_dim):
        raise NotImplementedError(f"unexpected nn.MultiheadAttention out_proj weight shape {tuple(mod.out_proj.weight.shape)}")
    out_bias = mod.out_proj.bias if mod.out_proj.bias is not None else mod.out_proj.weight.new_zeros(embed_dim)
    return {
        "q_weight": _tensor_values(q_weight), "q_bias": _tensor_values(q_bias),
        "k_weight": _tensor_values(k_weight), "k_bias": _tensor_values(k_bias),
        "v_weight": _tensor_values(v_weight), "v_bias": _tensor_values(v_bias),
        "out_weight": _tensor_values(mod.out_proj.weight), "out_bias": _tensor_values(out_bias),
        "scale": float((embed_dim // int(mod.num_heads)) ** -0.5),
    }

def _convolution_payload(weight, bias, groups):
    weight = _resolve_tensor(weight)
    groups = int(groups)
    if weight.ndim < 3:
        raise NotImplementedError(f"convolution weight must have rank at least 3, got shape {tuple(weight.shape)}")
    if groups <= 0:
        raise NotImplementedError(f"convolution groups must be positive, got {groups}")
    out_channels = int(weight.shape[0])
    in_channels_per_group = int(weight.shape[1])
    if out_channels % groups != 0:
        raise NotImplementedError(f"convolution out_channels={out_channels} is not divisible by groups={groups}")
    in_channels = in_channels_per_group * groups
    if groups == 1:
        dense_weight = weight
    else:
        out_channels_per_group = out_channels // groups
        dense_weight = weight.new_zeros((out_channels, in_channels, *weight.shape[2:]))
        for group in range(groups):
            out_start = group * out_channels_per_group
            out_end = out_start + out_channels_per_group
            in_start = group * in_channels_per_group
            in_end = in_start + in_channels_per_group
            dense_weight[out_start:out_end, in_start:in_end] = weight[out_start:out_end]
    bias = _resolve_tensor(bias) if bias is not None else weight.new_zeros(out_channels)
    if tuple(bias.shape) != (out_channels,):
        raise NotImplementedError(f"convolution bias must have shape {(out_channels,)}, got {tuple(bias.shape)}")
    return {"in_channels": in_channels, "out_channels": out_channels, "weight": _tensor_values(dense_weight), "bias": _tensor_values(bias)}

def _flatten_kind(input_shape, output_shape):
    if len(output_shape) == 1:
        return {"kind": "flatten", "value_shape": input_shape}
    return {"kind": "reshape", "in_shape": input_shape, "out_shape": output_shape}

def _normalize_axis(axis, rank):
    axis = int(axis)
    axis = axis + rank if axis < 0 else axis
    if axis < 0 or axis >= rank:
        raise NotImplementedError(f"axis {axis} is outside rank {rank}")
    return axis

def _reduction_axes(axis, rank, op_name):
    raw = range(rank) if axis is None else axis if isinstance(axis, (tuple, list)) else (axis,)
    axes = [_normalize_axis(value, rank) for value in raw]
    if len(set(axes)) != len(axes):
        raise NotImplementedError(f"{op_name} has duplicate reduction axes {axes}")
    return axes

_POINTWISE_KINDS = {'neg': "neg", 'rsqrt': "rsqrt", 'erf': "erf", 'gelu': "gelu", 'div': "div", 'pow': "pow", 'clamp_min': "clamp_min"}
_GELU_FIELD = "approximate"
_DIV_FIELD = "rounding_mode"
_REDUCED_FLOATS = ('bfloat16', 'float16')
_ARITHMETIC_KINDS = {'add': "add", 'sub': "sub", 'mul': "mul_elem"}

def _torch_dtype(dtype):
    if isinstance(dtype, str):
        return getattr(torch, dtype)
    return getattr(torch, ('int' if dtype['signed'] else 'uint') + str(dtype['width']))

def _argument(node, position, *names):
    if len(node.args) > position:
        return node.args[position]
    for name in names:
        if name in node.kwargs:
            return node.kwargs[name]
    return None

def _pointwise_plan(node, model=None):
    # The pointwise operator a call computes, with its attributes and operands, or None.
    if node.op == 'call_module':
        mod = model.get_submodule(str(node.target)) if model is not None else None
        if isinstance(mod, nn.GELU):
            return {'op': 'gelu', 'approximate': mod.approximate, 'operands': [node.args[0]]}
        return None
    first = _argument(node, 0, 'input', 'self')
    second = _argument(node, 1, 'other')
    if _is_op(node, ('aten.neg.default',), (torch.neg, torch.negative, operator.neg),
              ('neg', 'negative')):
        return {'op': 'neg', 'operands': [first]}
    if _is_op(node, ('aten.rsqrt.default',), (torch.rsqrt,), ('rsqrt',)):
        return {'op': 'rsqrt', 'operands': [first]}
    if _is_op(node, ('aten.erf.default',), (torch.erf, torch.special.erf), ('erf',)):
        return {'op': 'erf', 'operands': [first]}
    if _is_op(node, ('aten.gelu.default',), (F.gelu,), ()):
        return {'op': 'gelu', 'approximate': node.kwargs.get('approximate', 'none'),
                'operands': [first]}
    if _is_op(node, ('aten.div.Tensor', 'aten.div.Scalar', 'aten.div.Tensor_mode',
                     'aten.div.Scalar_mode'),
              (torch.div, torch.divide, torch.true_divide, operator.truediv),
              ('div', 'divide', 'true_divide')):
        if node.target is operator.truediv and not isinstance(first, torch.fx.Node):
            # Python's reflected division: Tensor.__rtruediv__ is reciprocal(x) * scalar.
            return {'op': 'rdiv', 'operands': [second, first]}
        return {'op': 'div', 'rounding_mode': node.kwargs.get('rounding_mode'),
                'operands': [first, second]}
    if _is_op(node, ('aten.floor_divide.default', 'aten.floor_divide.Scalar'),
              (torch.floor_divide, operator.floordiv),
              ('floor_divide',)):
        return {'op': 'div', 'rounding_mode': 'floor', 'operands': [first, second]}
    if _is_op(node, ('aten.pow.Tensor_Tensor', 'aten.pow.Tensor_Scalar', 'aten.pow.Scalar'),
              (torch.pow, operator.pow), ('pow',)):
        return {'op': 'pow', 'operands': [first, _argument(node, 1, 'exponent')]}
    if _is_op(node, ('aten.clamp_min.default',), (torch.clamp_min,), ('clamp_min',)):
        bound = _argument(node, 1, 'min')
        if not isinstance(bound, (bool, int, float)):
            # clamp_min.Tensor is IEEE maximum, unlike the scalar bound's kernel.
            raise NotImplementedError('clamp_min requires a scalar bound')
        return {'op': 'clamp_min', 'operands': [first, bound]}
    return None

def _pointwise_kind(node, model=None):
    plan = _pointwise_plan(node, model)
    if plan is None:
        return None
    operands = plan['operands']
    if not any(isinstance(value, torch.fx.Node) for value in operands):
        raise NotImplementedError('a pointwise operation needs a tensor operand')
    for value in operands:
        if not isinstance(value, (torch.fx.Node, bool, int, float)):
            raise NotImplementedError(f'unsupported pointwise operand {value!r}')
    if plan['op'] == 'gelu' and plan['approximate'] not in ('none', 'tanh'):
        raise NotImplementedError(f"gelu has no approximate={plan['approximate']!r} mode")
    if plan['op'] == 'div' and plan['rounding_mode'] not in (None, 'trunc', 'floor'):
        raise NotImplementedError(f"div has no rounding_mode={plan['rounding_mode']!r}")
    dtype = _node_dtype(node)
    if plan['op'] == 'pow' and not isinstance(operands[1], torch.fx.Node):
        exponent = operands[1]
        if isinstance(dtype, dict) and exponent < 0:
            raise NotImplementedError('PyTorch rejects negative integer powers of integers')
        if (dtype in ('float32', 'float64', 'bfloat16') and exponent == 0.5
                and not _nonnegative(operands[0], model)):
            raise NotImplementedError('pow(x, 0.5) is PyTorch sqrt; TorchLean sqrt clamps '
                                      'negative inputs, so the base must be nonnegative')
    return {'kind': plan['op'], '_plan': plan}

def _append_node(nodes, entry):
    entry['id'] = len(nodes)
    nodes.append(entry)
    return entry['id']

def _cast_to(nodes, parent, dtype):
    if nodes[parent]['dtype'] == dtype:
        return parent
    return _append_node(nodes, {'kind': "custom", 'parents': [parent],
                                'shape': nodes[parent]['shape'], 'dtype': dtype,
                                'primitive': 'cast', 'rounding': 'nearestEven'})

def _convert(nodes, parent, dtype):
    # Tensor conversion as c10::convert performs it: binary16 and bfloat16 values are made from
    # binary32, so an integer, Boolean or binary64 source becomes two IR casts and rounds twice.
    if dtype in _REDUCED_FLOATS and nodes[parent]['dtype'] not in (dtype, 'float32'):
        parent = _cast_to(nodes, parent, 'float32')
    return _cast_to(nodes, parent, dtype)

def _broadcast_to(nodes, parent, shape):
    source = nodes[parent]['shape']
    if source == shape:
        return parent
    return _append_node(nodes, {'kind': "broadcast_to", 'parents': [parent], 'shape': shape,
                                'dtype': nodes[parent]['dtype'], 'from_shape': source,
                                'to_shape': shape})

def _scalar_tensor(value, dtype):
    # A Python number converted to `dtype` as PyTorch converts a wrapped scalar operand. PyTorch
    # wraps a bool as a Boolean, an int as int64 (uint64 from 2 ** 63) and a float as binary64,
    # and c10::convert then rounds that value once, through binary32 for binary16 and bfloat16.
    # An int beyond 2 ** 53 therefore rounds once to binary32, not through binary64.
    if isinstance(value, bool):
        wrapped = torch.tensor(value, dtype=torch.bool)
    elif isinstance(value, int):
        if not -2 ** 63 <= value < 2 ** 64:
            raise NotImplementedError(f'PyTorch cannot wrap the Python int {value} as a scalar')
        wrapped = torch.tensor(value, dtype=torch.int64 if value < 2 ** 63 else torch.uint64)
    else:
        wrapped = torch.tensor(value, dtype=torch.float64)
    if dtype in _REDUCED_FLOATS:
        wrapped = wrapped.to(torch.float32)
    return wrapped.to(_torch_dtype(dtype))

def _scalar_constant(nodes, value, dtype):
    return _append_node(nodes, {'parents': [], **_constant(_scalar_tensor(value, dtype))})

def _one_element(value, dtype):
    # Whether TensorIterator's is_scalar(2) holds for this right operand when the common dtype
    # is `dtype`: a Python scalar, a one-element tensor, or a view with stride 0 along every
    # dimension longer than one. Conversion to the common dtype copies a longer operand to
    # contiguous storage, so such a view qualifies only when it already has that dtype.
    if not isinstance(value, torch.fx.Node):
        return True
    meta = value.meta.get('val', value.meta.get('tensor_meta'))
    if not hasattr(meta, 'shape'):
        raise NotImplementedError('a binary operation needs the shape of its right operand')
    sizes = [int(size) for size in meta.shape]
    if all(size == 1 for size in sizes):
        return True
    strides = meta.stride() if callable(getattr(meta, 'stride', None)) else meta.stride
    return _dtype_of(meta) == dtype and all(
        size == 1 or int(step) == 0 for size, step in zip(sizes, strides))

def _arithmetic_kind(node, op):
    # `add`, `sub` or `mul`. With two tensor operands this is the ordinary IR node. A Python
    # scalar operand becomes a constant: add and sub convert it to the result dtype, while
    # mul_kernel's reduced-float path multiplies by a right scalar's own value in binary32, as it
    # does for a one-element right operand of another dtype. Python's `s * x` calls
    # `x.__rmul__(s)`, which is `x * s`; FX records it as operator.mul(s, x) and torch.export as
    # mul.Tensor(x, s). An explicit torch.mul(s, x) keeps the scalar on the left, where
    # mul_kernel converts it to the tensor's dtype and multiplies there.
    left, right = _argument(node, 0, 'input', 'self'), _argument(node, 1, 'other')
    scalars = [not isinstance(value, torch.fx.Node) for value in (left, right)]
    for value, scalar in zip((left, right), scalars):
        if scalar and not isinstance(value, (bool, int, float)):
            raise NotImplementedError(f'unsupported {op} operand {value!r}')
    if all(scalars):
        raise NotImplementedError(f'{op} needs a tensor operand')
    if op == 'mul' and scalars[0] and node.target is operator.mul:
        left, right = right, left
    dtype = _node_dtype(node)
    wide = (op == 'mul' and dtype in _REDUCED_FLOATS and _one_element(right, dtype)
            and (not isinstance(right, torch.fx.Node) or _node_dtype(right) != dtype))
    if not any(scalars) and not wide:
        return {'kind': _ARITHMETIC_KINDS[op]}
    return {'kind': _ARITHMETIC_KINDS[op],
            '_plan': {'op': op, 'wide': wide, 'operands': [left, right]}}

def _pointwise_node(nodes, kind, parents):
    first = nodes[parents[0]]
    return _append_node(nodes, {**kind, 'parents': parents, 'shape': first['shape'],
                                'dtype': first['dtype']})

def _emit_pointwise(plan, nodes, node_to_id, shape, dtype):
    # Append the IR nodes the vectorized loops of PyTorch's CPU kernels compute for one pointwise
    # call, conversion, or arithmetic with a Python scalar, and return the last. Binary16 and
    # bfloat16 compute rsqrt, erf, gelu and pow in binary32 (PyTorch's opmath type), and division
    # and multiplication too when the right operand is one broadcast element.
    op, operands = plan['op'], plan['operands']
    reduced = dtype in _REDUCED_FLOATS
    opmath = 'float32' if reduced else dtype
    def operand(value, *dtypes):
        # A tensor converted to each dtype in turn, or a Python scalar made at the first one.
        if isinstance(value, torch.fx.Node):
            parent = node_to_id.get(value)
            if parent is None:
                raise NotImplementedError('pointwise operand has no tensor lowering')
        else:
            parent = _scalar_constant(nodes, value, dtypes[0])
        for target in dtypes:
            parent = _convert(nodes, parent, target)
        return _broadcast_to(nodes, parent, shape)
    def apply(name, parents, **attributes):
        kind = {'kind': _POINTWISE_KINDS[name]}
        kind.update({key: value for key, value in attributes.items() if value is not None})
        return _pointwise_node(nodes, kind, parents)
    def other(kind, parents):
        return _pointwise_node(nodes, {'kind': kind}, parents)
    if op == 'convert':
        return operand(operands[0], dtype)
    if op in _ARITHMETIC_KINDS:
        left, right = operands
        if plan['wide']:
            # mul_kernel's reduced-float path: the left operand in the common dtype and the right
            # operand's own value, both in binary32, with the product rounded once.
            parents = [operand(left, dtype, opmath), operand(right, opmath)]
            return _cast_to(nodes, other(_ARITHMETIC_KINDS[op], parents), dtype)
        return other(_ARITHMETIC_KINDS[op], [operand(left, dtype), operand(right, dtype)])
    if op == 'neg':
        return apply('neg', [operand(operands[0], dtype)])
    if op == 'clamp_min':
        # The tensor and the bound in the result dtype, which clamp_min promotes: an int32 tensor
        # with bound 0.5 is clamped in binary32. The kernel returns the bound where the tensor is
        # below it and keeps the tensor's value, a negative zero included, everywhere else.
        return apply('clamp_min', [operand(operands[0], dtype), operand(operands[1], dtype)])
    if op in ('rsqrt', 'erf', 'gelu'):
        attributes = {_GELU_FIELD: plan['approximate']} if op == 'gelu' else {}
        result = apply(op, [operand(operands[0], dtype, opmath)], **attributes)
        return _cast_to(nodes, result, dtype)
    if op == 'div':
        dividend, divisor = operands
        mode = {_DIV_FIELD: plan['rounding_mode']}
        if reduced and _one_element(divisor, dtype):
            # The reduced-float path of div_true_kernel, div_trunc_kernel and div_floor_kernel:
            # the dividend in the common dtype, the divisor's original value, both in binary32.
            parents = [operand(dividend, dtype, opmath), operand(divisor, opmath)]
            return _cast_to(nodes, apply('div', parents, **mode), dtype)
        return apply('div', [operand(dividend, dtype), operand(divisor, dtype)], **mode)
    if op == 'rdiv':
        # Tensor.__rtruediv__ is reciprocal(x) * scalar, and mul_kernel multiplies a reduced-float
        # tensor by the scalar's original value in binary32.
        tensor, scalar = operands
        reciprocal = other("inv", [operand(tensor, dtype)])
        product = other("mul_elem", [_cast_to(nodes, reciprocal, opmath), operand(scalar, opmath)])
        return _cast_to(nodes, product, dtype)
    base, exponent = operands
    if isinstance(exponent, torch.fx.Node):
        if not isinstance(base, (torch.fx.Node, bool)) and base == 1:
            # pow_Scalar_out fills ones for the base 1.
            return _broadcast_to(nodes, _scalar_constant(nodes, 1, dtype), shape)
        # pow.Tensor_Tensor, which pow.Scalar calls with its base as a wrapped scalar: both
        # operands in the common dtype, then in the opmath type.
        result = apply('pow', [operand(base, dtype, opmath), operand(exponent, dtype, opmath)])
        return _cast_to(nodes, result, dtype)
    # pow.Tensor_Scalar. pow_Tensor_Scalar_out fills ones for the exponent 0 and copies for 1.
    # PowKernel computes 0.5, -0.5 and -1 with the sqrt, rsqrt and reciprocal kernels in binary32,
    # binary64 and bfloat16. These comparisons use the Python value.
    if exponent == 0:
        return _broadcast_to(nodes, _scalar_constant(nodes, 1, dtype), shape)
    x = operand(base, dtype)
    if exponent == 1:
        return x
    if isinstance(dtype, dict):
        return apply('pow', [x, operand(exponent, dtype)])
    special = dtype in ('float32', 'float64', 'bfloat16')
    if special and exponent == 0.5:
        return other("sqrt", [x])
    if special and exponent == -0.5:
        return _cast_to(nodes, apply('rsqrt', [_cast_to(nodes, x, opmath)]), dtype)
    if special and exponent == -1:
        return other("inv", [x])
    # PowKernel then works with the exponent in the tensor dtype for bfloat16 and binary16. It
    # computes 2, 3 and -2 by multiplication in binary32, binary64 and bfloat16, and calls pow in
    # the opmath type otherwise.
    rounded = _scalar_tensor(exponent, dtype).item() if reduced else exponent
    if special and rounded == 2:
        return other("mul_elem", [x, x])
    if special and rounded == 3:
        return other("mul_elem", [other("mul_elem", [x, x]), x])
    if special and rounded == -2:
        return other("inv", [other("mul_elem", [x, x])])
    result = apply('pow', [_cast_to(nodes, x, opmath), operand(exponent, dtype, opmath)])
    return _cast_to(nodes, result, dtype)

def _lower_kind(node, model=None):
    name = _target_name(node.target)
    args = node.args
    kwargs = dict(node.kwargs)
    if kwargs.get("out", None) is not None:
        raise NotImplementedError("out= mutation is outside the pure graph contract")
    axis = kwargs.get("dim", kwargs.get("axis", None))
    if axis is None and len(args) > 1: axis = args[1]
    pointwise = _pointwise_kind(node, model)
    if pointwise is not None: return pointwise
    if node.op == "call_module" and model is not None:
        mod = model.get_submodule(str(node.target))
        if isinstance(mod, nn.Linear):
            return {"kind": "linear", **_linear_payload(mod.weight, mod.bias)}
        if isinstance(mod, nn.ReLU):
            if mod.inplace: raise NotImplementedError("in-place nn.ReLU mutation is outside the pure TorchLean graph contract")
            return {"kind": "relu"}
        if isinstance(mod, nn.Tanh): return {"kind": "tanh"}
        if isinstance(mod, nn.Sigmoid): return {"kind": "sigmoid"}
        if isinstance(mod, nn.Softmax):
            return {"kind": "softmax", "axis": _normalize_axis(mod.dim, len(_node_shape(node)))}
        if isinstance(mod, nn.Flatten):
            return _flatten_kind(_shape_from_arg(args[0]) if args else [], _node_shape(node))
        if isinstance(mod, nn.LayerNorm):
            rank = len(_node_shape(node))
            norm_rank = len(tuple(mod.normalized_shape))
            gamma = mod.weight if mod.weight is not None else torch.ones(mod.normalized_shape)
            beta = mod.bias if mod.bias is not None else torch.zeros(mod.normalized_shape)
            return {"kind": "layernorm", "axis": max(0, rank - norm_rank), "eps": float(mod.eps), "gamma": _tensor_values(gamma), "beta": _tensor_values(beta)}
        if isinstance(mod, (nn.Conv1d, nn.Conv2d, nn.Conv3d)):
            rank = len(tuple(mod.kernel_size))
            input_rank = len(_shape_from_arg(args[0])) if args else 0
            if input_rank not in (rank + 1, rank + 2): raise NotImplementedError(f"convolution expected rank {rank + 1} or {rank + 2}, got {input_rank}")
            channel_axis = input_rank - rank - 1
            if mod.padding_mode != "zeros": raise NotImplementedError(f"convolution padding_mode={mod.padding_mode!r} is outside the current TorchLean IR import subset")
            padding = _spatial_tuple(mod.padding, rank, 0)
            input_shape = _shape_from_arg(args[0])
            return {"kind": "conv", "spatial_rank": rank, "kernel": _spatial_tuple(mod.kernel_size, rank, 1), "stride": _spatial_tuple(mod.stride, rank, 1), "padding": padding, "padding_after": padding, "dilation": _spatial_tuple(mod.dilation, rank, 1), "groups": int(mod.groups), "channel_axis": channel_axis, "input_spatial": input_shape[channel_axis + 1:], **_convolution_payload(mod.weight, mod.bias, mod.groups)}
        if isinstance(mod, (nn.BatchNorm1d, nn.BatchNorm2d, nn.BatchNorm3d)):
            if mod.training:
                raise NotImplementedError("training-mode BatchNorm needs batch statistics and running-statistic updates")
            if not mod.track_running_stats or mod.running_mean is None or mod.running_var is None:
                raise NotImplementedError("nn.BatchNorm2d without running statistics has batch-dependent semantics, not TorchLean eval-mode BatchNorm semantics")
            gamma = mod.weight if mod.weight is not None else torch.ones(mod.num_features)
            beta = mod.bias if mod.bias is not None else torch.zeros(mod.num_features)
            return {"kind": "batch_norm_eval", "channel_axis": 1, "channels": int(mod.num_features), "eps": float(mod.eps), "gamma": _tensor_values(gamma), "beta": _tensor_values(beta), "mean": _tensor_values(mod.running_mean), "var": _tensor_values(mod.running_var)}
        if isinstance(mod, (nn.MaxPool1d, nn.MaxPool2d, nn.MaxPool3d)):
            rank = 1 if isinstance(mod, nn.MaxPool1d) else 2 if isinstance(mod, nn.MaxPool2d) else 3
            if _spatial_tuple(mod.dilation, rank, 1) != [1] * rank: raise NotImplementedError("dilated max pooling is outside the current TorchLean IR import subset")
            if mod.ceil_mode: raise NotImplementedError("max pooling with ceil_mode=True is outside the current TorchLean IR import subset")
            if mod.return_indices: raise NotImplementedError("max pooling with return_indices=True produces a tuple outside the current pooling lowering")
            return {"kind": "max_pool", "spatial_rank": rank, "kernel": _spatial_tuple(mod.kernel_size, rank, 1), "stride": _spatial_tuple(mod.stride if mod.stride is not None else mod.kernel_size, rank, 1), "padding": _spatial_tuple(mod.padding, rank, 0)}
        if isinstance(mod, (nn.AvgPool1d, nn.AvgPool2d, nn.AvgPool3d)):
            rank = 1 if isinstance(mod, nn.AvgPool1d) else 2 if isinstance(mod, nn.AvgPool2d) else 3
            if mod.ceil_mode: raise NotImplementedError("average pooling with ceil_mode=True is outside the current TorchLean IR import subset")
            if getattr(mod, 'divisor_override', None) is not None: raise NotImplementedError("average pooling with divisor_override is outside the current TorchLean IR import subset")
            padding = _spatial_tuple(mod.padding, rank, 0)
            if any(padding) and not mod.count_include_pad: raise NotImplementedError("padded average pooling with count_include_pad=False is outside the current TorchLean IR import subset")
            return {"kind": "avg_pool", "spatial_rank": rank, "kernel": _spatial_tuple(mod.kernel_size, rank, 1), "stride": _spatial_tuple(mod.stride if mod.stride is not None else mod.kernel_size, rank, 1), "padding": padding}
    if _is_op(node, ('aten._to_copy.default', 'aten.to.dtype'), (),
              ('to', 'float', 'double', 'half', 'bfloat16')):
        if _node_dtype(node) == _node_dtype(args[0]): return {"kind": "detach"}
        return {"kind": "custom", '_plan': {'op': 'convert', 'operands': [args[0]]}}
    if _is_op(node, ("aten.add.Tensor",), (operator.add, torch.add), ("add",)):
        alpha = kwargs.get("alpha", args[2] if len(args) > 2 else 1)
        if alpha != 1:
            raise NotImplementedError("torch.add with alpha != 1 is unsupported")
        return _arithmetic_kind(node, 'add')
    if _is_op(node, ("aten.sub.Tensor",), (operator.sub, torch.sub, torch.subtract), ("sub", "subtract")):
        alpha = kwargs.get("alpha", args[2] if len(args) > 2 else 1)
        if alpha != 1:
            raise NotImplementedError("torch.sub with alpha != 1 is unsupported")
        return _arithmetic_kind(node, 'sub')
    if _is_op(node, ("aten.mul.Tensor",), (operator.mul, torch.mul, torch.multiply), ("mul", "multiply")):
        return _arithmetic_kind(node, 'mul')
    if _top_k_mask(node) is not None:
        return {"kind": "top_k_mask", "k": _top_k_mask(node)[1]}
    if _is_op(node, ("aten.relu.default",), (torch.relu, F.relu), ("relu",)):
        inplace = kwargs.get("inplace", args[1] if len(args) > 1 else False)
        if inplace: raise NotImplementedError("in-place relu is outside the pure graph contract")
        return {"kind": "relu"}
    if _is_op(node, ("aten.tanh.default",), (torch.tanh,), ("tanh",)):
        return {"kind": "tanh"}
    if _is_op(node, ("aten.sigmoid.default",), (torch.sigmoid,), ("sigmoid",)):
        return {"kind": "sigmoid"}
    if _is_op(node, ("aten.exp.default",), (torch.exp,), ("exp",)):
        return {"kind": "exp"}
    if _is_op(node, ("aten.log.default",), (torch.log,), ("log",)):
        return {"kind": "log"}
    if _is_op(node, ("aten.sin.default",), (torch.sin,), ("sin",)):
        return {"kind": "sin"}
    if _is_op(node, ("aten.cos.default",), (torch.cos,), ("cos",)):
        return {"kind": "cos"}
    if _is_op(node, ("aten.abs.default",), (torch.abs, torch.absolute), ("abs", "absolute")):
        return {"kind": "abs"}
    if _is_op(node, ("aten.sqrt.default",), (torch.sqrt,), ("sqrt",)):
        source = args[0] if args else kwargs.get('input', kwargs.get('self'))
        if not _nonnegative(source, model):
            raise NotImplementedError('raw sqrt requires a structurally nonnegative operand; TorchLean sqrt clamps negative values')
        return {"kind": "sqrt"}
    if _is_op(node, ("aten.reciprocal.default",), (torch.reciprocal,), ("reciprocal",)):
        return {"kind": "inv"}
    if _is_op(node, ("aten.maximum.default",), (torch.maximum,), ("maximum",)):
        return {"kind": "max_elem"}
    if _is_op(node, ("aten.minimum.default",), (torch.minimum,), ("minimum",)):
        return {"kind": "min_elem"}
    if _is_op(node, ("aten.matmul.default", "aten.mm.default"), (torch.matmul, torch.mm, operator.matmul), ("matmul", "mm")):
        return {"kind": "matmul"}
    is_sum = _is_op(node, ("aten.sum.default", "aten.sum.dim_IntList"), (torch.sum,), ("sum",))
    if is_sum or _is_op(node, ("aten.mean.default", "aten.mean.dim"), (torch.mean,), ("mean",)):
        input_rank = len(_shape_from_arg(args[0])) if args else len(_node_shape(node))
        reduction_axes = _reduction_axes(axis, input_rank, name)
        keepdim = bool(kwargs.get("keepdim", args[2] if len(args) > 2 else False))
        dtype = kwargs.get("dtype", args[3] if len(args) > 3 else None)
        if dtype is not None:
            raise NotImplementedError("reduction dtype casts are outside the scalar contract")
        return {"kind": "reduce_sum" if is_sum else "reduce_mean",
                "axes": reduction_axes, "keepdim": keepdim}
    if _is_op(node, ("aten.softmax.int", "aten._softmax.default"), (torch.softmax, F.softmax), ("softmax",)):
        if _is_op(node, ("aten._softmax.default",)):
            if kwargs.get("half_to_float", args[2] if len(args) > 2 else False):
                raise NotImplementedError("softmax half_to_float changes scalar semantics")
            dtype = None
        else:
            dtype_pos = 3 if node.target is F.softmax else 2
            dtype = kwargs.get("dtype", args[dtype_pos] if len(args) > dtype_pos else None)
        if dtype is not None:
            raise NotImplementedError("softmax dtype casts are outside the scalar contract")
        rank = len(_node_shape(node))
        if axis is None and node.target is F.softmax:
            axis = 0 if rank in (0, 1, 3) else 1
        if axis is None: raise NotImplementedError("softmax requires a static dimension")
        return {"kind": "softmax", "axis": _normalize_axis(axis, rank)}
    if _is_op(node, ("aten.reshape.default", "aten.view.default"), (torch.reshape,), ("reshape", "view")):
        if node.op == "call_method" and node.target == "view":
            if isinstance(kwargs.get("dtype", args[1] if len(args) > 1 else None), torch.dtype):
                raise NotImplementedError("view(dtype) changes scalar semantics")
        return {"kind": "reshape", "in_shape": _shape_from_arg(args[0]),
                "out_shape": _node_shape(node)}
    if _is_op(node, ("aten.contiguous.default",), (), ("contiguous",)):
        return {"kind": "reshape", "in_shape": _shape_from_arg(args[0]),
                "out_shape": _node_shape(node)}
    if _is_op(node, ("aten.flatten.using_ints",), (torch.flatten,), ("flatten",)):
        return _flatten_kind(_shape_from_arg(args[0]), _node_shape(node))
    if _is_op(node, ("aten.permute.default",), (torch.permute,), ("permute",)):
        dims = kwargs.get("dims", args[1] if len(args) > 1 else ())
        perm = list(dims) if isinstance(dims, (tuple, list)) else list(args[1:])
        rank = len(_node_shape(node))
        return {"kind": "permute", "perm": [_normalize_axis(x, rank) for x in perm]}
    if _is_op(node, ("aten.cat.default",), (torch.cat, torch.concat, torch.concatenate), ()):
        rank = len(_node_shape(node))
        dim = _normalize_axis(kwargs.get("dim", args[1] if len(args) > 1 else 0), rank)
        return {"kind": "concat", "axis": dim}
    if _is_op(node, ("aten.slice.Tensor",)):
        source = _shape_from_arg(args[0])
        dim = args[1] if len(args) > 1 else kwargs.get('dim', 0)
        dim = _normalize_axis(dim, len(source))
        start = args[2] if len(args) > 2 else kwargs.get('start', None)
        end = args[3] if len(args) > 3 else kwargs.get('end', None)
        step = args[4] if len(args) > 4 else kwargs.get('step', 1)
        if step != 1:
            raise NotImplementedError('strided slices are outside the TorchLean IR subset')
        start, end, _ = slice(start, end).indices(source[dim])
        return {"kind": "slice", "axis": dim, "start": start, "length": max(0, end - start)}
    if _is_op(node, ("aten.transpose.int",), (torch.transpose,), ("transpose",)):
        rank = len(_shape_from_arg(args[0]))
        d0 = _normalize_axis(kwargs.get("dim0", args[1] if len(args) > 1 else None), rank)
        d1 = _normalize_axis(kwargs.get("dim1", args[2] if len(args) > 2 else None), rank)
        return {"kind": "transpose", "axis1": d0, "axis2": d1}
    if _is_op(node, ("aten.layer_norm.default",), (F.layer_norm, torch.layer_norm), ()):
        weight = kwargs.get("weight", args[2] if len(args) > 2 else None)
        bias = kwargs.get("bias", args[3] if len(args) > 3 else None)
        eps = kwargs.get("eps", args[4] if len(args) > 4 else 1e-5)
        input_rank = len(_shape_from_arg(args[0])) if args else len(_node_shape(node))
        normalized_shape = args[1] if len(args) > 1 else kwargs.get("normalized_shape", ())
        normalized_rank = len(normalized_shape) if isinstance(normalized_shape, (tuple, list, torch.Size)) else 1
        weight = _resolve_tensor(weight) if weight is not None else torch.ones(normalized_shape)
        bias = _resolve_tensor(bias) if bias is not None else torch.zeros(normalized_shape)
        return {"kind": "layernorm", "axis": max(0, input_rank - normalized_rank), "eps": float(eps), "gamma": _tensor_values(weight), "beta": _tensor_values(bias)}
    if _is_op(node, ("aten.linear.default",), (F.linear,), ()):
        weight = kwargs.get("weight", args[1] if len(args) > 1 else None)
        bias = kwargs.get("bias", args[2] if len(args) > 2 else None)
        return {"kind": "linear", **_linear_payload(weight, bias)}
    if _is_op(node, ("aten.batch_norm.default",), (F.batch_norm, torch.batch_norm), ()):
        training = bool(kwargs.get("training", args[5] if len(args) > 5 else False))
        if training: raise NotImplementedError("training-mode batch_norm is outside the eval-mode TorchLean IR operation")
        is_aten = node.target is not F.batch_norm
        mean_pos, var_pos = (3, 4) if is_aten else (1, 2)
        weight_pos, bias_pos = (1, 2) if is_aten else (3, 4)
        running_mean = kwargs.get("running_mean", args[mean_pos] if len(args) > mean_pos else None)
        running_var = kwargs.get("running_var", args[var_pos] if len(args) > var_pos else None)
        if running_mean is None or running_var is None: raise NotImplementedError("batch_norm without running statistics is outside the eval-mode TorchLean IR operation")
        shape = _node_shape(node)
        channels = int(shape[1]) if len(shape) >= 2 else 0
        eps = float(kwargs.get("eps", args[7] if len(args) > 7 else 1e-5))
        running_mean = _resolve_tensor(running_mean)
        running_var = _resolve_tensor(running_var)
        weight = kwargs.get("weight", args[weight_pos] if len(args) > weight_pos else None)
        bias = kwargs.get("bias", args[bias_pos] if len(args) > bias_pos else None)
        weight = _resolve_tensor(weight) if weight is not None else torch.ones(channels)
        bias = _resolve_tensor(bias) if bias is not None else torch.zeros(channels)
        return {"kind": "batch_norm_eval", "channel_axis": 1, "channels": channels, "eps": eps, "gamma": _tensor_values(weight), "beta": _tensor_values(bias), "mean": _tensor_values(running_mean), "var": _tensor_values(running_var)}
    if _is_op(node, ("aten.conv1d.default", "aten.conv2d.default", "aten.conv3d.default"), (F.conv1d, F.conv2d, F.conv3d), ()):
        weight_arg = kwargs.get("weight", args[1] if len(args) > 1 else None)
        bias_arg = kwargs.get("bias", args[2] if len(args) > 2 else None)
        weight = _resolve_tensor(weight_arg)
        wshape = _shape_of(weight)
        rank = max(0, len(wshape) - 2)
        input_rank = len(_shape_from_arg(args[0])) if args else 0
        if input_rank not in (rank + 1, rank + 2): raise NotImplementedError(f"convolution expected rank {rank + 1} or {rank + 2}, got {input_rank}")
        channel_axis = input_rank - rank - 1
        stride = _spatial_tuple(kwargs.get("stride", args[3] if len(args) > 3 else 1), rank, 1)
        padding = _spatial_tuple(kwargs.get("padding", args[4] if len(args) > 4 else 0), rank, 0)
        dilation = _spatial_tuple(kwargs.get("dilation", args[5] if len(args) > 5 else 1), rank, 1)
        groups = int(kwargs.get("groups", args[6] if len(args) > 6 else 1))
        input_shape = _shape_from_arg(args[0])
        return {"kind": "conv", "spatial_rank": rank, "kernel": [int(v) for v in wshape[2:]], "stride": stride, "padding": padding, "padding_after": padding, "dilation": dilation, "groups": groups, "channel_axis": channel_axis, "input_spatial": input_shape[channel_axis + 1:], **_convolution_payload(weight, bias_arg, groups)}
    if _is_op(node, ("aten.max_pool1d.default", "aten.max_pool2d.default", "aten.max_pool3d.default"), (F.max_pool1d, F.max_pool2d, F.max_pool3d), ()):
        rank = next(d for d in (1, 2, 3) if _is_op(node, (f"aten.max_pool{d}d.default",), (getattr(F, f"max_pool{d}d"),)))
        kernel = _spatial_tuple(args[1] if len(args) > 1 else kwargs.get("kernel_size", 1), rank, 1)
        stride_arg = kwargs.get("stride", args[2] if len(args) > 2 else None)
        stride = _spatial_tuple(stride_arg if stride_arg not in (None, []) else kernel, rank, 1)
        padding = _spatial_tuple(kwargs.get("padding", args[3] if len(args) > 3 else 0), rank, 0)
        dilation = _spatial_tuple(kwargs.get("dilation", args[4] if len(args) > 4 else 1), rank, 1)
        ceil_mode = bool(kwargs.get("ceil_mode", args[5] if len(args) > 5 else False))
        if kwargs.get("return_indices", args[6] if len(args) > 6 else False):
            raise NotImplementedError("max pooling indices need tuple lowering")
        if dilation != [1] * rank: raise NotImplementedError("dilated max pooling is outside the current TorchLean IR import subset")
        if ceil_mode: raise NotImplementedError("max pooling with ceil_mode=True is outside the current TorchLean IR import subset")
        return {"kind": "max_pool", "spatial_rank": rank, "kernel": kernel, "stride": stride, "padding": padding}
    if _is_op(node, ("aten.avg_pool1d.default", "aten.avg_pool2d.default", "aten.avg_pool3d.default"), (F.avg_pool1d, F.avg_pool2d, F.avg_pool3d), ()):
        rank = next(d for d in (1, 2, 3) if _is_op(node, (f"aten.avg_pool{d}d.default",), (getattr(F, f"avg_pool{d}d"),)))
        kernel = _spatial_tuple(args[1] if len(args) > 1 else kwargs.get("kernel_size", 1), rank, 1)
        stride_arg = kwargs.get("stride", args[2] if len(args) > 2 else None)
        stride = _spatial_tuple(stride_arg if stride_arg not in (None, []) else kernel, rank, 1)
        padding = _spatial_tuple(kwargs.get("padding", args[3] if len(args) > 3 else 0), rank, 0)
        ceil_mode = bool(kwargs.get("ceil_mode", args[4] if len(args) > 4 else False))
        count_include_pad = bool(kwargs.get("count_include_pad", args[5] if len(args) > 5 else True))
        divisor_override = kwargs.get("divisor_override", args[6] if len(args) > 6 else None)
        if ceil_mode: raise NotImplementedError("average pooling with ceil_mode=True is outside the current TorchLean IR import subset")
        if any(padding) and not count_include_pad: raise NotImplementedError("padded average pooling with count_include_pad=False is outside the current TorchLean IR import subset")
        if divisor_override is not None: raise NotImplementedError("average pooling with divisor_override is outside the current TorchLean IR import subset")
        return {"kind": "avg_pool", "spatial_rank": rank, "kernel": kernel, "stride": stride, "padding": padding}
    if _is_getitem(node):
        shapes = _tuple_shapes_of_node(args[0]) if args else []
        if not shapes or len(args) < 2 or type(args[1]) is not int:
            raise NotImplementedError("getitem requires a tuple and a static integer")
        idx = args[1] if args[1] >= 0 else len(shapes) + args[1]
        if not 0 <= idx < len(shapes):
            raise NotImplementedError("tuple index is out of range")
        return {"kind": "tuple_getitem", "index": idx}
    if _is_getattr(node):
        attr = args[1] if len(args) > 1 else "<unknown>"
        raise NotImplementedError(f"unsupported PyTorch attribute projection: {attr}. If this came from a tuple-returning op such as torch.sort(...).values, add a value-graph/lowering rule for that producer instead of treating the attribute as an ordinary tensor op.")
    raise NotImplementedError(f"unsupported PyTorch op: {name}")

def _metadata_constraint(node):
    if not _is_op(node, ('aten._assert_tensor_metadata.default',)): return None
    if len(node.args) != 1 or set(node.kwargs) - {'dtype', 'device', 'layout'}:
        raise NotImplementedError('unsupported tensor metadata assertion')
    dtype = node.kwargs.get('dtype')
    value = node.args[0].meta.get('val', node.args[0].meta.get('tensor_meta'))
    if dtype is not None and dtype != getattr(value, 'dtype', None):
        raise NotImplementedError('metadata assertion conflicts with the captured dtype')
    return {'value': node.args[0].name,
            **{key: str(value) for key, value in node.kwargs.items()}}

def _check_graph_operators(graph, model):
    helpers = _routing_helpers(graph)
    for node in graph.nodes:
        if node.op in ("placeholder", "get_attr", "output"):
            continue
        if node in helpers or _zeros_like(node): continue
        if _metadata_constraint(node) is not None: continue
        if _tuple_shapes_of_node(node):
            _tuple_kind(node, model)
        elif node.op in ("call_function", "call_method", "call_module"):
            _lower_kind(node, model)
        else:
            raise NotImplementedError(f"unsupported FX node op: {node.op}")

def _zeros_like(node):
    return _is_op(node, ('aten.zeros_like.default',)) and not node.kwargs.get('dtype')

def _graph_is_lowerable(graph, model):
    try:
        _check_graph_operators(graph, model)
    except NotImplementedError:
        return False
    return True

def _capture(model, example, require_torch_export=False):
    global _TENSOR_VALUES
    _TENSOR_VALUES = {}
    examples = (example,) if isinstance(example, torch.Tensor) else example
    if not isinstance(examples, tuple) or not examples or not all(
        isinstance(value, torch.Tensor) for value in examples):
        raise NotImplementedError('capture requires positional tensor arguments')
    if True:
        try:
            ep = torch.export.export(model, examples)
            # Functionalization can turn mutation into ordinary tensor operations.
            # Extra outputs describe state updates that need their own replay rules.
            if any(spec.kind.name != "USER_OUTPUT" for spec in ep.graph_signature.output_specs):
                raise NotImplementedError("exported mutation/effects are outside the pure graph contract")
            # ExportedProgram lifts parameters, buffers, and constants into graph
            # placeholders. Their order is not a user-input contract, so classify
            # placeholders through graph_signature rather than taking the first one.
            user_input_names = [
                spec.arg.name
                for spec in ep.graph_signature.input_specs
                if getattr(spec.kind, "name", "") == "USER_INPUT"
            ]
            state = dict(ep.state_dict)
            constants = dict(getattr(ep, 'constants', {}))
            for spec in ep.graph_signature.input_specs:
                if getattr(spec.kind, 'name', '') != 'USER_INPUT':
                    target = getattr(spec, 'target', None)
                    value = state.get(target, constants.get(target, None))
                    if isinstance(value, torch.Tensor): _TENSOR_VALUES[spec.arg.name] = value
            if _graph_is_lowerable(ep.graph, model):
                return ep.graph, user_input_names
            if require_torch_export: _check_graph_operators(ep.graph, model)
        except Exception:
            if require_torch_export:
                raise
    if require_torch_export:
        raise NotImplementedError("torch.export produced operators outside the current TorchLean import subset")
    from torch.fx import symbolic_trace
    from torch.fx.passes.shape_prop import ShapeProp
    gm = symbolic_trace(model)
    ShapeProp(gm).propagate(*examples)
    for node in gm.graph.nodes:
        if node.op == 'get_attr':
            value = gm
            for part in str(node.target).split('.'): value = getattr(value, part)
            if isinstance(value, torch.Tensor): _TENSOR_VALUES[node.name] = value
    _check_graph_operators(gm.graph, model)
    return gm.graph, [node.name for node in gm.graph.nodes if node.op == 'placeholder']

def export_torchlean_graph_json(model, example, json_path: str, require_torch_export=False):
    graph, user_input_names = _capture(model, example, require_torch_export)
    input_names = set(user_input_names)
    nodes = []
    promotion_cache = {}
    broadcast_cache = {}
    node_to_id = {}
    output_ids = None
    input_nodes = {}
    capture_constraints = []
    helpers = _routing_helpers(graph)
    for node in graph.nodes:
        if node in helpers:
            node_to_id[node] = None
            continue
        constraint = _metadata_constraint(node)
        if constraint is not None:
            capture_constraints.append(constraint)
            continue
        if node.op == "output":
            refs = _node_refs(node.args, node_to_id, strict=True)
            if not refs:
                raise NotImplementedError("TorchLean graph import requires at least one tensor output")
            output_ids = refs
            continue
        is_input = node.op == 'placeholder' and node.name in input_names
        if len(getattr(node, "users", {})) == 0 and not is_input:
            # FX often leaves dead tuple projections behind, e.g. attention weights from
            # `y, _ = mha(...)`. They are not part of the exported value, so we omit them
            # instead of forcing every unused container projection to have a tensor lowering.
            node_to_id[node] = None
            continue
        if _zeros_like(node):
            value = node.meta.get('val', node.meta.get('tensor_meta'))
            node_to_id[node] = len(nodes)
            nodes.append({'id': len(nodes), 'parents': [], **_constant(torch.zeros(_node_shape(node), dtype=value.dtype))})
            continue
        if node.op == 'get_attr' or (node.op == 'placeholder' and
            not is_input):
            value = _TENSOR_VALUES.get(node.name)
            if value is None: raise NotImplementedError('non-tensor lifted input')
            node_to_id[node] = len(nodes)
            nodes.append({'id': len(nodes), 'parents': [], **_constant(value)})
            continue
        node_id = len(nodes)
        node_to_id[node] = node_id
        shape = _node_shape(node)
        tuple_shapes = _tuple_shapes_of_node(node)
        value_meta = {"value_kind": "tuple", "tuple_shapes": tuple_shapes} if tuple_shapes else {"value_kind": "tensor", "shape": shape}
        if node.op == "placeholder":
            kind = {"kind": "input"}
            parents = []
            input_nodes[node.name] = node_id
        elif tuple_shapes:
            # The importer expands known tuple producers into tensor dataflow.
            kind = _tuple_kind(node, model)
            call = inspect.signature(model.get_submodule(str(node.target)).forward)
            parents = _node_refs(tuple(call.bind(*node.args, **node.kwargs).arguments.values()),
                                 node_to_id)
        elif node.op in ("call_function", "call_method", "call_module"):
            kind = _lower_kind(node, model)
            if '_plan' in kind:
                first = len(nodes)
                result = _emit_pointwise(kind['_plan'], nodes, node_to_id, shape, _node_dtype(node))
                if True and result >= first:
                    nodes[result]['debug_target'] = _target_name(node.target)
                node_to_id[node] = result
                continue
            parents = _node_refs((node.args, node.kwargs), node_to_id)
            if kind['kind'] == "top_k_mask":
                source, k = _top_k_mask(node)
                parents = _node_refs((source,), node_to_id)
                source_shape = _shape_from_arg(source)
                width = source_shape[-1] if source_shape else 1
                if 0 < k < width:
                    capture_constraints.append({
                        'kind': 'top_k_mask_agreement', 'node': node_id,
                        'value': source.name, 'value_id': parents[0], 'k': k,
                        'axis': len(source_shape) - 1, 'status': 'not_checked',
                        'assumptions': ['finite_scores', 'strict_cutoff_gap'],
                        'message': 'Each score row is finite, with its k-th largest score '
                                   'strictly above its (k+1)-th. PyTorch tie selection may '
                                   'otherwise differ from the IR lower-index rule.'})
            elif kind['kind'] in ('add', 'sub', 'mul_elem', 'max_elem', 'min_elem', 'matmul'):
                left = node.args[0] if node.args else node.kwargs.get('input', node.kwargs.get('self'))
                right = node.args[1] if len(node.args) > 1 else node.kwargs.get('other', node.kwargs.get('mat2'))
                parents = _node_refs((left, right), node_to_id)
            if kind['kind'] in ('linear', 'conv', 'layernorm', 'batch_norm_eval'):
                # These existing operators own their parameter payload, not extra edges.
                first = node.args[0] if node.args else node.kwargs.get('input')
                parents = _node_refs((first,), node_to_id)
        else:
            raise NotImplementedError(f"unsupported FX node op: {node.op}")
        if not tuple_shapes:
            dtype = _node_dtype(node)
            if kind['kind'] in ('add', 'sub', 'mul_elem', 'div', 'max_elem', 'min_elem'):
                promoted = []
                for parent in parents:
                    source = nodes[parent]
                    if source['dtype'] != dtype:
                        key = (parent, json.dumps(dtype, sort_keys=True))
                        if key not in promotion_cache:
                            # As c10::convert, through binary32 for binary16 and bfloat16.
                            promotion_cache[key] = _convert(nodes, parent, dtype)
                        parent = promotion_cache[key]
                    if source['shape'] != shape:
                        key = (parent, tuple(shape))
                        if key not in broadcast_cache:
                            nodes.append({'id': len(nodes), "kind": "broadcast_to", 'parents': [parent],
                                          'shape': shape, 'dtype': dtype,
                                          'from_shape': source['shape'], 'to_shape': shape})
                            broadcast_cache[key] = len(nodes) - 1
                        parent = broadcast_cache[key]
                    promoted.append(parent)
                parents = promoted
        node_id = len(nodes)
        node_to_id[node] = node_id
        entry = {"id": node_id, "parents": parents, **value_meta, **kind}
        if not tuple_shapes: entry['dtype'] = _node_dtype(node)
        if True:
            entry["debug_target"] = _target_name(node.target)
        nodes.append(entry)
    if len(input_nodes) != len(user_input_names) or output_ids is None:
        raise RuntimeError("could not identify graph input/output")
    input_ids = [input_nodes[name] for name in user_input_names]
    payload = {"format": FORMAT, "frontend": "pytorch", "input_ids": input_ids, "output_ids": output_ids, "nodes": nodes}
    payload['capture_constraints'] = capture_constraints
    try:
        text = json.dumps(payload, indent=2, sort_keys=True, allow_nan=False)
    except ValueError as exc:
        raise ValueError("TorchLean IR JSON cannot carry NaN or infinite values; check the model parameters and constants") from exc
    with open(json_path, "w", encoding="utf-8") as f:
        f.write(text)
    return payload

def main():
    parser = argparse.ArgumentParser(description="Export a PyTorch nn.Module to TorchLean IR JSON")
    parser.add_argument("module", help="Python file containing the model class/constructor")
    parser.add_argument("ctor", help="Zero-argument model class or constructor name")
    parser.add_argument("json", help="Output graph JSON path")
    parser.add_argument("--example-shape", required=True, help="Comma-separated example input shape, e.g. 1,4")
    parser.add_argument("--require-torch-export", action="store_true", help="Fail instead of falling back to FX")
    args = parser.parse_args()
    shape = tuple(int(x) for x in args.example_shape.split(',') if x)
    model = _load_model(args.module, args.ctor)
    example = torch.randn(*shape)
    payload = export_torchlean_graph_json(model, example, args.json, args.require_torch_export)
    print(f"wrote {len(payload['nodes'])} TorchLean IR nodes to {args.json}")

if __name__ == "__main__":
    main()