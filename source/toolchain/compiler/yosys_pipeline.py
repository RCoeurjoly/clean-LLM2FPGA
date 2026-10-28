"""Explicit native Yosys pass policies; never rewrite serialized hardware."""
import re


def top_flatten_passes(top):
    """Flatten the whole hierarchy into top without flattening intermediate copies.

    Yosys recursively replaces instances in the selected module. Other module
    definitions remain unexpanded until native hierarchy cleanup removes those
    no longer reachable. Black-box primitives retain their normal contracts.
    This changes allocation order, not the selected computation or model scope.
    """
    if type(top) is not str or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*',top):
        raise ValueError('top requires a simple public compiler symbol')
    return (f'flatten -noscopeinfo {top}','select *',
            f'hierarchy -check -top {top}','opt_clean')


def coarse_preparation_passes(top):
    """Retire slice aliases through native RTLIL passes before XC7 mapping.

    Only structurally proven connection-only slice helpers are removed; model and memory hierarchy
    stay intact. Native cleanup cannot operate on modules containing processes,
    so process lowering must precede alias retirement. The standard prepare
    stage remains responsible for all subsequent synthesis transformations.
    Its repeated process pass sees already lowered RTLIL.
    """
    if type(top) is not str or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', top):
        raise ValueError('top requires a simple public compiler symbol')
    synthesis = f'synth_xilinx -family xc7 -top {top} -noiopad -noclkbuf'
    return (
        synthesis + ' -run begin:begin',
        'proc',
        'opt_connection_helpers */t:*std_bit_slice*',
        'select *',
        f'hierarchy -check -top {top}',
        'opt_whole_wire_aliases',
        'opt_clean -purge',
        # Comparator mapping expands generic wide operands into carry/lookup
        # logic. Inline these pure primitives while parent constants and input
        # gating can still specialize them; retain compute and memory modules.
        'flatten -noscopeinfo */t:*std_slt* */t:*std_sle* */t:*std_sgt* */t:*std_sge*',
        'select *',
        f'hierarchy -check -top {top}',
        'opt',
        synthesis + ' -run prepare:map_memory',
    )
