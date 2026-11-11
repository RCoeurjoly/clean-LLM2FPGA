"""Explicit structural inlining of the private combinational scalar kernel.

The named kernel scope and all nets are retained. Ordinary continuous wire
connections replace module ports. No operator, state or protocol is changed.
Only the exact declaration/connection forms emitted by the private encoder are
accepted; this is not a general SystemVerilog text rewriter.
"""
import io
import re
import shutil

IDENT=r'[A-Za-z_$][A-Za-z0-9_$]*'
DECL=re.compile(r'^(input|output) wire (?:\[[0-9]+:[0-9]+\] )?('+IDENT+r'(?:, *'+IDENT+r')*);\n$')
BIND=re.compile(r'\.('+IDENT+r')\(('+IDENT+r')\)')


def inline_stream(source, body, output, encoded_top):
    header=source.readline()
    match=re.fullmatch(r'module ('+IDENT+r')\(('+IDENT+r'(?:, *'+IDENT+r')*)\);\n',header)
    if not match:raise ValueError('Expected generated scalar-kernel header')
    kernel=match[1];ports=[n.strip() for n in match[2].split(',')]
    if len(set(ports))!=len(ports):raise ValueError('Duplicate kernel header port')
    directions={};body_bytes=0
    for line in source:
        if line=='endmodule\n':break
        declaration=DECL.fullmatch(line)
        if line.startswith(('input ','output ')):
            if not declaration:raise ValueError('Unexpected generated port declaration')
            for name in declaration[2].split(','):
                name=name.strip()
                if name in directions:raise ValueError('Duplicate kernel port declaration')
                directions[name]=declaration[1]
            line=line[len(declaration[1])+1:]
        body.write(line);body_bytes+=len(line)
    else:raise ValueError('Unterminated scalar kernel')
    if set(ports)!=set(directions):raise ValueError('Kernel declarations/header differ')
    wrapper=source.readline()
    while wrapper=='\n':wrapper=source.readline()
    if not wrapper.startswith('module '+encoded_top+'('):raise ValueError('Expected admitted encoded wrapper')
    output.write(wrapper);replaced=False
    for line in source:
        if line.startswith(kernel+' kernel('):
            if replaced or not line.endswith(');\n'):raise ValueError('Unexpected kernel instance')
            text=line[len(kernel+' kernel('):-3]
            bindings=BIND.findall(text)
            if ','.join('.'+n+'('+v+')' for n,v in bindings)!=text:raise ValueError('Noncanonical kernel connection')
            if len(bindings)!=len(ports) or {n for n,v in bindings}!=set(ports):raise ValueError('Incomplete/duplicate kernel binding')
            output.write('generate if (1) begin: kernel\n')
            body.seek(0);shutil.copyfileobj(body,output)
            output.write('end endgenerate\n')
            for name,value in bindings:
                left,right=('kernel.'+name,value) if directions[name]=='input' else (value,'kernel.'+name)
                output.write('assign '+left+' = '+right+';\n')
            replaced=True
        else:output.write(line)
    if not replaced:raise ValueError('Generated kernel instance missing')
    return dict(kernel=kernel,scope='kernel',kernel_ports_removed=len(ports),inputs=sum(v=='input' for v in directions.values()),outputs=sum(v=='output' for v in directions.values()),body_bytes=body_bytes,semantics='Same scoped wires, expressions, states and directional continuous connections')


def inline_text(rtl,encoded_top):
    output=io.StringIO()
    with io.StringIO(rtl) as source,io.StringIO() as body:
        stats=inline_stream(source,body,output,encoded_top)
    return output.getvalue(),stats
