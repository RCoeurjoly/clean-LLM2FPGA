"""Exact optional narrowing of unsigned, zero-extended observation muxes.

The tree shape, selector bits, leaves and root ABI stay unchanged. Each subtree
only contains words up to its own maximum width; all omitted high bits are zero,
including for native unknown selector bits (SV conditional bitwise merging).
"""
import io
import re


def widths(mapping):
    level = [s['width'] for s in mapping['states']]
    level += [1] * ((1 << mapping['observation']['object_width']) - len(level))
    result = {}
    bit = 0
    while len(level) > 1:
        level = [max(level[i:i+2]) for i in range(0, len(level), 2)]
        for i, width in enumerate(level):
            for key in ('tag', 'value'):
                result[f'observe_{key}_{bit}_{i}'] = width
        bit += 1
    return result


def narrow_stream(source, output, mapping):
    expected = widths(mapping)
    seen = set()
    old_bits = new_bits = 0
    pattern = re.compile(r'wire \[(\d+):0\] (observe_(?:tag|value)_\d+_\d+)=(.*)\n\Z')
    for line in source:
        match = pattern.fullmatch(line)
        if match:
            high, name, expression = match.groups()
            if name not in expected or name in seen or int(high)+1 != mapping['observation']['width']:
                raise ValueError('Unexpected generated observation wire: ' + name)
            width = expected[name]
            old_bits += int(high)+1
            new_bits += width
            seen.add(name)
            line = f'wire [{width-1}:0] {name}={expression}\n'
        output.write(line)
    if seen != set(expected):
        raise ValueError('Incomplete generated observation tree')
    return dict(wires=len(seen), original_wire_bits=old_bits, narrowed_wire_bits=new_bits,
                semantics='Identical unsigned mux topology; only provably zero high bits removed')


def narrow_text(rtl, mapping):
    output = io.StringIO()
    stats = narrow_stream(io.StringIO(rtl), output, mapping)
    return output.getvalue(), stats


def buffer_selector_stream(source, output, mapping, group_wires=512):
    """Partition Icarus's selector nexus; buf Z->X is identical as ?: control.

    These nets are used only as single-bit conditional controls. Every original
    leaf, condition bit, data expression and root assignment stays in place.
    A primitive prevents early alias collapse during Icarus elaboration; normal
    synthesis removes it. This is explicitly opt-in, never a default encoding.
    """
    if type(group_wires) is not int or group_wires<1:
        raise ValueError('Positive selector partition size required')
    expected=widths(mapping);seen=set();count=0;inserted=False
    ow=mapping['observation']['object_width'];groups=(len(expected)+group_wires-1)//group_wires
    pattern=re.compile(r'wire \[(\d+):0\] (observe_(?:tag|value)_\d+_\d+)=(.*)\n\Z')
    for line in source:
        match=pattern.fullmatch(line)
        if match:
            _,name,expression=match.groups()
            if name not in expected or name in seen:raise ValueError('Unexpected or duplicate observation wire')
            bit=int(name.split('_')[2])
            prefix=f'obs_object[{bit}]?'
            if not expression.startswith(prefix) or expression.count('obs_object')!=1:
                raise ValueError('Selector may only occur as the canonical ternary condition')
            if not inserted:
                for group in range(groups):
                    output.write(f'wire [{ow-1}:0] c2l_obs_select_{group};\n')
                    output.write(f'buf c2l_obs_buf_{group}[{ow-1}:0](c2l_obs_select_{group},obs_object);\n')
                inserted=True
            line=line.replace('=obs_object[',f'=c2l_obs_select_{count//group_wires}[')
            seen.add(name);count+=1
        elif 'c2l_obs_select_' in line or 'c2l_obs_buf_' in line:
            raise ValueError('Selector buffering was already applied')
        output.write(line)
    if seen!=set(expected):raise ValueError('Incomplete observation selector tree')
    return dict(wires=count,groups=groups,group_wires=group_wires,selector_bits=ow,
        semantics='Only ternary control distribution changed; 0/1 unchanged and native X/Z controls both perform the same bitwise conditional merge')


def buffer_selector_text(rtl,mapping,group_wires=512):
    output=io.StringIO()
    stats=buffer_selector_stream(io.StringIO(rtl),output,mapping,group_wires)
    return output.getvalue(),stats


def balance_guards_stream(source,output):
    """Factor only canonical, pure controller OR lists into named balanced trees.

    Logical booleanization preserves multi-bit and X/Z operands. Logical OR on
    these boolean values is associative. No term or guard is omitted.
    """
    pattern=re.compile(r'\bwire (bad_latch|bad_repeat|bad_clock|more_work)=(?:seeded && )?\(')
    seen=set();rows=[]
    for line in source:
        if 'c2l_guard_' in line:raise ValueError('Controller guard factoring was already applied')
        replacements=[];declarations=[]
        for match in pattern.finditer(line):
            name=match[1]
            if name in seen:raise ValueError('Duplicate controller guard')
            seen.add(name);start=match.end();depth=1;end=start
            while end<len(line) and depth:
                if line[end]=='(':depth+=1
                elif line[end]==')':depth-=1
                if depth:end+=1
            if depth:raise ValueError('Unbalanced generated guard')
            body=line[start:end];terms=[];begin=0;depth=0;index=0
            while index<len(body):
                char=body[index]
                if char=='(':depth+=1
                elif char==')':depth-=1
                elif depth==0 and body[index:index+2]=='||':
                    terms.append(body[begin:index].strip());begin=index+2;index+=1
                index+=1
            terms.append(body[begin:].strip())
            if depth or any(not term for term in terms):raise ValueError('Malformed generated OR list')
            if len(terms)==1:continue
            level=[]
            for index,term in enumerate(terms):
                wire=f'c2l_guard_{name}_0_{index}'
                declarations.append(f'wire {wire}=!(!({term}));\n');level.append(wire)
            tree_depth=0
            while len(level)>1:
                tree_depth+=1;next_level=[]
                for index in range(0,len(level),2):
                    if index+1==len(level):next_level.append(level[index]);continue
                    wire=f'c2l_guard_{name}_{tree_depth}_{index//2}'
                    declarations.append(f'wire {wire}={level[index]} || {level[index+1]};\n');next_level.append(wire)
                level=next_level
            replacements.append((start,end,level[0]));rows.append(dict(guard=name,terms=len(terms),depth=tree_depth))
        for start,end,wire in reversed(replacements):line=line[:start]+wire+line[end:]
        output.writelines(declarations);output.write(line)
    if seen!={'bad_latch','bad_repeat','bad_clock','more_work'}:raise ValueError('Missing canonical controller guards')
    return dict(guards=rows,semantics='All pure OR operands retained in original order; exact four-state booleanization and balanced logical OR')


def balance_guards_text(rtl):
    output=io.StringIO();stats=balance_guards_stream(io.StringIO(rtl),output)
    return output.getvalue(),stats
