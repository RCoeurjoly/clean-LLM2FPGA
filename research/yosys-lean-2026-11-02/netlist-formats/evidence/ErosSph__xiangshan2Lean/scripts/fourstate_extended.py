"""Explicit private v9 admission; old v4-v8 entrypoints remain opt-in compatible."""
from pathlib import Path


def validate_extended(ir):
    if ir['format']!='chisel2lean-fourstate-ir-v9':raise ValueError('Explicit private v9 IR required')
    if ir['mode']=='combinational':return
    from fourstate_scalable import validate_scalable
    from fourstate_eventful import dependency_plan
    from fourstate_diagnostics import format_plan
    validate_scalable(ir,_eventful=True,_extensions=True)
    e=ir['events']
    def fail(p,msg):raise ValueError(str(e['processes'][p]['source'])+': '+msg)
    graph=dependency_plan(e['processes'],e['reset_dependency_edges'],fail)
    if graph['order']!=e['reset_topology'] or e['max_waves']!=graph['depth']+2:raise ValueError('Derived reset graph/bound mismatch')
    for p in e['processes']:
        for t in p['triggers']:
            s=t['source']
            if s['form']=='state_bit_reset' and (s.get('producer_nba_bound')!=1 or ir['scalar_nba_bounds'].get(s['state'])!=1):fail(p['id'],'unsafe intermediate NBA reset source')
    for a in ir.get('assertions',[]):
        if ir['nodes'][a['enable_node']]['width']!=1 or ir['nodes'][a['pass_node']]['width']!=1:raise ValueError('Diagnostic flags must be one bit')
        if a['specifiers']!=format_plan(a['message'],len(a['arguments'])):raise ValueError('Diagnostic format mismatch')
        for v in a['arguments']:
            if ir['nodes'][v['node']]['width']!=v['width']:raise ValueError('Diagnostic payload width mismatch')


def parse_extended(source,top,clock=None,*,event_clocks=(),immediate_assertions=True):
    if clock is None:
        from fourstate_elaborated import parse
        return parse(Path(source),top,_extensions=True)
    from fourstate_scalable import parse_scalable
    ir=parse_scalable(source,top,clock,event_clocks=event_clocks,immediate_assertions=immediate_assertions,_eventful=True,_extensions=True)
    validate_extended(ir)
    return ir


def encode_extended(ir, *, shallow_memory=False, inline_kernel=False, compact_observation=False, compact_forwarding=False):
    validate_extended(ir)
    if ir['mode']=='combinational':
        from fourstate_encode import encode
        return encode(dict(ir,format='chisel2lean-fourstate-ir-v5'))
    from fourstate_scalable import encode_scalable
    rtl,mapping=encode_scalable(ir,_eventful=True,_extensions=True,_shallow_memory=shallow_memory,_compact_forwarding=compact_forwarding)
    if inline_kernel:
        from fourstate_inline_kernel import inline_text
        rtl,_=inline_text(rtl,mapping['encoded_top'])
    if compact_observation:
        from fourstate_observation_layout import narrow_text
        rtl,_=narrow_text(rtl,mapping)
    return rtl,mapping
