"""Lower four-state IR to fully defined tag/value Boolean RTL."""
from __future__ import annotations


def encode(ir):
    if ir['format'].endswith('-v9'):
        raise ValueError('Private extended v9 requires the explicit encode_extended entry')
    if ir['format'].endswith('-v8'):
        raise ValueError('Private eventful v8 requires the explicit encode_eventful entry')
    if ir['format'].endswith('-v7'):
        raise ValueError('Private scalable v7 requires the explicit encode_scalable entry')
    if ir['format'].endswith('-v6'):
        raise ValueError('Compact v6 requires explicit private small-model lowering before encoding')
    sequential = ir.get('mode') == 'sequential'
    hierarchical = ir['format'].endswith(('v4', 'v5'))
    clocks = set(ir.get('clock_signals', [ir.get('clock')])) if sequential else set()
    signals = {x['name']: x for x in ir['signals'] if x['name'] not in clocks}
    used = {x['name'] for x in ir['signals']} | {x.get('port_name', x['name'].rsplit('.', 1)[-1]) for x in ir['signals']}
    def fresh(stem):
        while stem in used:
            stem += '_'
        used.add(stem)
        return stem
    pairs = {}
    for index, (name, signal) in enumerate(signals.items()):
        if hierarchical and signal['direction'] == 'internal':
            pairs[name] = (fresh(f'c2l4_sig{index}_tag'), fresh(f'c2l4_sig{index}_value'))
        else:
            stem = signal.get('port_name', name)
            pair = (stem + '__tag', stem + '__value')
            if any(p in used for p in pair):
                pos = signal['source']
                raise ValueError(f"{pos['file']}:{pos['line']}:{pos['column']}: encoded port name collision: {stem}")
            pairs[name] = pair; used.update(pair)
    states = []
    for index, register in enumerate(ir.get('states', [])):
        entry = dict(register)
        for field, suffix in [('current_tag', '__state_tag'), ('current_value', '__state_value'),
                              ('next_tag', '__next_tag'), ('next_value', '__next_value')]:
            name = fresh(f'c2l4_state{index}' + suffix) if hierarchical else register['name'] + suffix
            if hierarchical:
                entry[field] = name
                continue
            if name in used or name == ir.get('clock'):
                pos = register['source']
                raise ValueError(f"{pos['file']}:{pos['line']}:{pos['column']}: state encoding name collision: {name}")
            used.add(name); entry[field] = name
        states.append(entry)
    node_pairs = {}
    for node in ir['nodes']:
        stem = 'c2l4_n' + str(node['id'])
        while any(stem + suffix in used for suffix in ('_t', '_v')):
            stem += '_'
        node_pairs[node['id']] = (stem + '_t', stem + '_v')
        used.update(node_pairs[node['id']])
    top = ir['top'] + '__fourstate'
    ports = [x for x in signals.values() if x['direction'] != 'internal']
    monitors = [dict(name=n, width=1, tag=fresh('c2l4_monitor'+str(i)+'_tag'),
                     value=fresh('c2l4_monitor'+str(i)+'_value'))
                for i,n in enumerate(ir.get('events', {}).get('monitors', []))]
    extra_ports = [r[k] for r in states for k in ('current_tag', 'current_value', 'next_tag', 'next_value')]
    extra_ports += [m[k] for m in monitors for k in ('tag', 'value')]
    assertions=[]
    if ir.get('assertions'):
        from fourstate_assertions import FIELDS
        for i,a in enumerate(ir['assertions']):
            entry=dict(a)
            for key in FIELDS:entry[key]=fresh(f'c2l4_assert{i}_'+key)
            if 'arguments' in a:
                entry['arguments']=[dict(x,tag=fresh(f'c2l4_assert{i}_arg{j}_tag'),value=fresh(f'c2l4_assert{i}_arg{j}_value')) for j,x in enumerate(a['arguments'])]
            assertions.append(entry)
        extra_ports += [a[k] for a in assertions for k in FIELDS]
        extra_ports += [v[k] for a in assertions for v in a.get('arguments',[]) for k in ('tag','value')]
    lines = ['module ' + top + '(' + ', '.join([p for x in ports for p in pairs[x['name']]] + extra_ports) + ');']
    for x in signals.values():
        declaration = 'wire' if x['direction'] == 'internal' else x['direction'] + ' wire'
        lines.append(f"{declaration} [{x['width']-1}:0] " + ', '.join(pairs[x['name']]) + ';')
    for r in states:
        lines += [f"input wire [{r['width']-1}:0] {r['current_tag']}, {r['current_value']};",
                  f"output wire [{r['width']-1}:0] {r['next_tag']}, {r['next_value']};",
                  f"assign {pairs[r['name']][0]} = {r['current_tag']};",
                  f"assign {pairs[r['name']][1]} = {r['current_value']};"]
    for m in monitors:
        lines.append(f"output wire {m['tag']}, {m['value']};")
        lines.extend(f"assign {m[k]} = {pairs[m['name']][i]};" for i,k in enumerate(('tag','value')))
    for a in assertions:
        lines.append('output wire '+', '.join(a[k] for k in FIELDS)+';')
        for arg in a.get('arguments',[]):lines.append(f'output wire [{arg["width"]-1}:0] {arg["tag"]}, {arg["value"]};')
    for n in ir['nodes']:
        lines.append(f"wire [{n['width']-1}:0] " + ', '.join(node_pairs[n['id']]) + ';')
    for a in assertions:
        for key,value in zip(FIELDS,(*node_pairs[a['enable_node']],*node_pairs[a['pass_node']])):
            lines.append(f'assign {a[key]} = {value};')
        for arg in a.get('arguments',[]):
            for key,value in zip(('tag','value'),node_pairs[arg['node']]):lines.append(f'assign {arg[key]} = {value};')
    for n in ir['nodes']:
        op, w = n['op'], n['width']
        args = [node_pairs[i] for i in n['args']]
        if op == 'ref':
            t, v = pairs[n['name']]
        elif op == 'cast_sign':
            t, v = args[0]
        elif op == 'resize':
            at, av = args[0]
            source_width = n['from_width']
            if w < source_width:
                t, v = f'{at}[{w-1}:0]', f'{av}[{w-1}:0]'
            elif n['extension'] == 'sign':
                t = f'{{{{{w-source_width}{{{at}[{source_width-1}]}}}}, {at}}}'
                v = f'{{{{{w-source_width}{{{av}[{source_width-1}]}}}}, {av}}}'
            else:
                t = f'{{{{{w-source_width}{{1\'b0}}}}, {at}}}'
                v = f'{{{{{w-source_width}{{1\'b0}}}}, {av}}}'
        elif op == 'select':
            t, v = ('{' + ', '.join(f'{p}[{b}]' for b in n['bits']) + '}' for p in pairs[n['name']])
        elif op == 'const':
            t = f"{w}'b" + ''.join('1' if c in 'xz' else '0' for c in n['bits'])
            v = f"{w}'b" + ''.join('1' if c in '1z' else '0' for c in n['bits'])
        elif op == 'concat':
            t, v = ('{' + ', '.join(a[i] for a in args) + '}' for i in (0, 1))
        elif op == 'repeat':
            t, v = ('{' + str(n['count']) + '{' + args[0][i] + '}}' for i in (0, 1))
        elif op == 'slice':
            t, v = ('{' + ', '.join(f'{p}[{b}]' for b in n['bits']) + '}' for p in args[0])
        elif op == 'packed_select':
            at, av = args[0]; it, iv = args[1]
            iw = ir['nodes'][n['args'][1]]['width']
            negative = f'{iv}[{iw-1}]' if n['index_signed'] else "1'b0"
            low, high = min(n['left'], n['right']), max(n['left'], n['right'])
            bad = f'((|{it}) | {negative} | ({iv} < {low}) | ({iv} > {high}))'
            distance = f'({iv} - {n["right"]})' if n['left'] >= n['right'] else f'({n["right"]} - {iv})'
            offset = f'({distance} * {w})'
            t = f'({bad} ? {{{w}{{1\'b1}}}} : ({at} >> {offset}))'
            v = f'({bad} ? {w}\'b0 : ({av} >> {offset}))'
        elif op in ('reduce_and', 'reduce_or'):
            at, av = args[0]
            if op == 'reduce_and':
                zero, one = f'(|(~{at} & ~{av}))', f'(& (~{at} & {av}))'
            else:
                zero, one = f'(~(|({at} | {av})))', f'(|(~{at} & {av}))'
            t, v = f'~({zero} | {one})', one
        elif op in ('reduce_xor','reduce_xnor'):
            at,av=args[0];unknown=f'(|{at})';parity=f'(^{av})'
            if op=='reduce_xnor':parity=f'~{parity}'
            t,v=unknown,f'(~{unknown} & {parity})'
        elif op == 'not':
            a, b = args[0]; t, v = a, f'(~{a} & ~{b})'
        elif op in ('logical_not', 'logical_and', 'logical_or'):
            def truth(pair):
                tag, value = pair
                return f'(|(~{tag} & {value}))', f'(~(|({tag} | {value})))'
            one_a, zero_a = truth(args[0])
            if op == 'logical_not':
                known_one, known_zero = zero_a, one_a
            else:
                one_b, zero_b = truth(args[1])
                if op == 'logical_and':
                    known_one, known_zero = f'({one_a} & {one_b})', f'({zero_a} | {zero_b})'
                else:
                    known_one, known_zero = f'({one_a} | {one_b})', f'({zero_a} & {zero_b})'
            t, v = f'~({known_one} | {known_zero})', known_one
        elif op in ('eq', 'ne', 'case_eq', 'case_ne', 'lt', 'le', 'gt', 'ge'):
            at, av = args[0]; bt, bv = args[1]
            if op in ('eq', 'ne'):
                mismatch = f'(|(~{at} & ~{bt} & ({av} ^ {bv})))'
                unknown = f'(|({at} | {bt}))'
                t = f'(~{mismatch} & {unknown})'
                v = mismatch if op == 'ne' else f'(~{mismatch} & ~{unknown})'
            elif op in ('case_eq', 'case_ne'):
                same = f'(~(|(({at} ^ {bt}) | ({av} ^ {bv}))))'
                t, v = "1'b0", f'~{same}' if op == 'case_ne' else same
            else:
                signed = n['compare_signed']
                left = f'$signed({av})' if signed else av
                right = f'$signed({bv})' if signed else bv
                operator = {'lt': '<', 'le': '<=', 'gt': '>', 'ge': '>='}[op]
                t = f'(|({at} | {bt}))'
                v = f'(~{t} & ({left} {operator} {right}))'
        elif op == 'pos':
            t, v = args[0]
        elif op in ('neg', 'add', 'sub'):
            at, av = args[0]
            if op == 'neg':
                unknown = f'(|{at})'
                calculation = f'(-{av})'
            else:
                bt, bv = args[1]
                unknown = f'(|({at} | {bt}))'
                calculation = f'({av} {"+" if op == "add" else "-"} {bv})'
            t = f'({unknown} ? {w}\'b' + '1' * w + f' : {w}\'b0)'
            v = f'({unknown} ? {w}\'b0 : {calculation})'
        elif op=='mod':
            at,av=args[0];bt,bv=args[1]
            bad=f'((|{at}) | (|{bt}) | ({bv}==0))'
            denominator=f'({bad}?{w}\'d1:{bv})'
            expr=f'({av} % {denominator})'
            # Use the existing unsigned-remainder backend operation. Magnitudes
            # fit in w unsigned bits, including the minimum signed value. Restore
            # the dividend sign after remainder; protect the actual denominator.
            # This also isolates 1 % -1 from unsigned ternary context propagation.
            calculated=fresh(node_pairs[n['id']][1]+'_remainder')
            if n['signed']:
                magnitude_a=fresh(calculated+'_a');magnitude_b=fresh(calculated+'_b');magnitude_r=fresh(calculated+'_r')
                lines += [f'wire [{w-1}:0] {magnitude_a}={av}[{w-1}]?(~{av}+{w}\'d1):{av};',
                    f'wire [{w-1}:0] {magnitude_b}={bv}[{w-1}]?(~{bv}+{w}\'d1):{bv};',
                    f'wire [{w-1}:0] {magnitude_r}={magnitude_a}%({bad}?{w}\'d1:{magnitude_b});']
                expr=f'({av}[{w-1}]?(~{magnitude_r}+{w}\'d1):{magnitude_r})'
            lines.append(f'wire [{w-1}:0] {calculated}={expr};')
            t,v=f'({bad}?{w}\'b'+('1'*w)+f':{w}\'b0)',f'({bad}?{w}\'b0:{calculated})'
        elif op in ('shl', 'shr', 'ashl', 'ashr'):
            at, av = args[0]; bt, bv = args[1]
            unknown_amount = f'(|{bt})'
            operator = '<<' if op in ('shl', 'ashl') else '>>>' if op == 'ashr' and n['signed'] else '>>'
            if operator == '>>>':
                stem = 'c2l4_shift' + str(n['id'])
                while stem + '_t' in used or stem + '_v' in used:
                    stem += '_'
                used.update((stem + '_t', stem + '_v'))
                lines += [f'wire signed [{w-1}:0] {stem}_t, {stem}_v;',
                          f'assign {stem}_t = $signed({at}) >>> {bv};',
                          f'assign {stem}_v = $signed({av}) >>> {bv};']
                shifted_t, shifted_v = stem + '_t', stem + '_v'
            else:
                shifted_t, shifted_v = f'({at} {operator} {bv})', f'({av} {operator} {bv})'
            t = f'({unknown_amount} ? {{{w}{{1\'b1}}}} : {shifted_t})'
            v = f'({unknown_amount} ? {w}\'b0 : {shifted_v})'
        elif op in ('and', 'or', 'xor', 'xnor'):
            at, av = args[0]; bt, bv = args[1]
            if op in ('xor', 'xnor'):
                t = f'({at} | {bt})'
                value = f'({av} ^ {bv})'
                v = f'(~{t} & ' + ('~' if op == 'xnor' else '') + value + ')'
            else:
                zero_a, zero_b = f'(~{at} & ~{av})', f'(~{bt} & ~{bv})'
                one_a, one_b = f'(~{at} & {av})', f'(~{bt} & {bv})'
                zero = f'({zero_a} ' + ('|' if op == 'and' else '&') + f' {zero_b})'
                one = f'({one_a} ' + ('&' if op == 'and' else '|') + f' {one_b})'
                t, v = f'~({zero} | {one})', one
        elif op == 'proc_mux':
            ct, cv = args[0]; at, av = args[1]; bt, bv = args[2]
            t = f'((~{ct} & {cv}) ? {at} : {bt})'
            v = f'((~{ct} & {cv}) ? {av} : {bv})'
        elif op == 'mux':
            ct, cv = args[0]; at, av = args[1]; bt, bv = args[2]
            # IEEE 1800-2017 11.4.11 / Table 11-20: an ambiguous
            # predicate preserves only equal *known* bits. Z/Z merges to X;
            # case equality of the dual rails is not logical equivalence.
            same = f'(~({at} | {bt} | ({av} ^ {bv})))'
            t = f'({ct} ? ~{same} : ({cv} ? {at} : {bt}))'
            v = f'({ct} ? ({same} & {av}) : ({cv} ? {av} : {bv}))'
        else:
            raise ValueError('Unsupported four-state IR operation: ' + op)
        nt, nv = node_pairs[n['id']]
        lines.extend([f'assign {nt} = {t};', f'assign {nv} = {v};'])
    for name in ir['order']:
        for dest, value in zip(pairs[name], node_pairs[ir['assignments'][name]]):
            lines.append(f'assign {dest} = {value};')
    for r in states:
        for field, value in zip(('next_tag', 'next_value'), node_pairs[ir['next_state'][r['name']]]):
            lines.append(f"assign {r[field]} = {value};")
    lines.append('endmodule')
    version = ir['format'].rsplit('-', 1)[-1]
    mapping = {'format': 'chisel2lean-fourstate-ports-' + version, 'mode': ir.get('mode', 'combinational'),
               'top': ir['top'], 'encoded_top': top,
               'encoding': {'0': '00', '1': '01', 'x': '10', 'z': '11'}, 'ports': []}
    for port in ports:
        p = dict(port)
        p.update(tag=pairs[p['name']][0], value=pairs[p['name']][1],
                 bit_indices=[p['right'] + (i if p['left'] >= p['right'] else -i) for i in range(p['width'])])
        if hierarchical:
            p['name'] = p['port_name']
        mapping['ports'].append(p)
    if sequential:
        mapping.update(clock=ir['clock'], states=states)
        if 'memories' in ir:
            mapping['memories'] = ir['memories']
        if 'events' in ir:
            mapping['events'] = ir['events']
        if monitors:
            mapping['monitors'] = monitors
        if assertions:
            mapping['assertions'] = assertions
        if 'bounded_events' not in ir.get('capabilities', []):
            clock_entry = dict(next(x for x in ir['signals'] if x['name'] == ir.get('clock_signal', ir['clock'])), role='clock')
            if hierarchical:
                clock_entry['name'] = clock_entry['port_name']
            mapping['ports'].append(clock_entry)
    return '\n'.join(lines) + '\n', mapping
