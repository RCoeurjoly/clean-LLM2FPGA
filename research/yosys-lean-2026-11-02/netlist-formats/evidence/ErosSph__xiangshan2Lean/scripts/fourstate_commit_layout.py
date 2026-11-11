"""Opt-in grouping of generated, disjoint NBA bank writers by logical owner.

Only exact one-line bank blocks emitted by the private encoder are admitted.
The controller and all combinational expressions are untouched. Every original
body is copied verbatim, in encounter order within its owner's physical block.
All RHS operands still evaluate before the shared clock edge's NBA commit.
"""
import io
import re


def group_stream(source,output,mapping):
    states=mapping['states'];objects={s['object_id']:s for s in states}
    if len(objects)!=len(states):raise ValueError('Duplicate storage object identity')
    banks={s[key]:s['object_id'] for s in states for key in ('bank_tag','bank_value')}
    if len(banks)!=2*len(states):raise ValueError('Storage banks are aliased')
    assignment=re.compile(r'([A-Za-z_$][A-Za-z0-9_$]*)(\[[A-Za-z_$][A-Za-z0-9_$]*\])?\s*<=([^;]+)\Z')
    header='module '+mapping['encoded_top']+'('
    prefix='always @(posedge exec_clk) begin '
    inside=False;finished=False;seen=set();groups={};statements=0
    for number,line in enumerate(source,1):
        if line.startswith(header):
            if inside or finished:raise ValueError('Duplicate target module')
            inside=True
        if inside and line.startswith(prefix):
            if not line.endswith('end\n'):raise ValueError(f'Line {number}: noncanonical state block')
            body=line[len(prefix):-4].strip();parts=[p.strip() for p in body.split(';') if p.strip()]
            identities=set();lefts=set()
            for part in parts:
                match=assignment.fullmatch(part)
                if not match or match[1] not in banks:
                    raise ValueError(f'Line {number}: only known bank NBA statements may be grouped')
                obj=banks[match[1]];s=objects[obj]
                if bool(match[2])!=('memory' in s):raise ValueError('Bank array/scalar type differs')
                identities.add(obj);lefts.add(match[1])
            if len(identities)!=1:raise ValueError(f'Line {number}: mixed or empty storage writer')
            obj=identities.pop();s=objects[obj]
            if obj in seen or lefts!={s['bank_tag'],s['bank_value']}:
                raise ValueError(f'Line {number}: repeated or incomplete storage writer')
            if 'memory' not in s and len(parts)!=2:raise ValueError('Scalar bank has multiple updates')
            seen.add(obj);statements+=len(parts)
            groups.setdefault(s['process'],[]).append(body)
            continue
        if inside and line=='endmodule\n':
            if seen!=set(objects):raise ValueError('Missing canonical storage blocks')
            for owner,bodies in sorted(groups.items()):
                output.write('always @(posedge exec_clk) begin\n')
                for body in bodies:output.write(body+'\n')
                output.write('end\n')
            inside=False;finished=True
        output.write(line)
    if inside or not finished:raise ValueError('Target module was not closed')
    return dict(storage_objects=len(states),scalar_objects=sum('memory' not in s for s in states),
        memory_objects=sum('memory' in s for s in states),original_bank_blocks=len(seen),
        grouped_bank_blocks=len(groups),nba_statements=statements,clock='exec_clk',
        semantics='Same disjoint banks and verbatim ordered NBA bodies; grouped by existing logical process owner, with every RHS evaluated before commit')


def group_text(rtl,mapping):
    output=io.StringIO();stats=group_stream(io.StringIO(rtl),output,mapping)
    return output.getvalue(),stats
