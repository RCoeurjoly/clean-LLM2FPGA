"""Finite F53 server work: compact IR, explicit small lowerings, raw/encoded RTL."""
from pathlib import Path
import argparse,copy,hashlib,itertools,json,os,random,subprocess,sys,time,traceback
from collections import Counter
sys.path.insert(0,str(Path(__file__).resolve().parent/'verification'/'four_state'))

def write(p,v):
    p.parent.mkdir(parents=True,exist_ok=True)
    with p.open('x',encoding='utf8') as f:json.dump(v,f,indent=2);f.write('\n')
def digest(p):return hashlib.sha256(p.read_bytes()).hexdigest()

def command(folder,label,argv,*,required=True):
    import resource
    d=folder/label;d.mkdir();start=time.monotonic()
    with (d/'stdout.log').open('wb') as out,(d/'stderr.log').open('wb') as err:
        p=subprocess.run([str(x) for x in argv],cwd=folder,stdout=out,stderr=err)
    row=dict(argv=[str(x) for x in argv],cwd=str(folder),exit_code=p.returncode,seconds=time.monotonic()-start,
        cumulative_child_peak_rss_bytes=resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss*1024)
    write(d/'command.json',row)
    if required and p.returncode:raise RuntimeError(str(d)+' '+(d/'stderr.log').read_text()[:3000])
    return row,(d/'stdout.log').read_text()

def trace_for(mapping):
    ports=[p for p in mapping['ports'] if p['direction']=='input'];now={p['name']:'0'*p['width'] for p in ports}
    # Opaque clock-gate latch at explicit deposit; opening/closing follows later.
    for root in mapping['events']['roots']:now[root]='1'
    rows=[dict(inputs=dict(now))]
    def setone(n,v):
        if now[n]!=v:now[n]=v;rows.append(dict(inputs=dict(now)))
    roots=mapping['events']['roots'];symbols='01xz'
    # Every payload bit receives every symbol. Controls exercise empty/full,
    # asynchronous reset, enable/address ambiguity and all four-state edges.
    scalar=[p for p in ports if p['width']==1 and p['name'] not in roots]
    data=[p for p in ports if p['width']>1]
    for c in symbols:
        for root in roots:setone(root,'0')
        for p in data:setone(p['name'],c*p['width'])
        for p in scalar:setone(p['name'],'1' if 'reset' not in p['name'] else '0')
        for _ in range(5):
            for root in roots:setone(root,'1');setone(root,'0')
    rng=random.Random(530021)
    for i in range(160):
        p=rng.choice(ports);setone(p['name'],''.join(rng.choices(symbols,k=p['width'])))
        if i%2==0:
            for root in roots:setone(root,rng.choice(symbols))
    for root in roots:
        for a,b in itertools.product(symbols,repeat=2):setone(root,a);setone(root,b)
    return rows

def runtime(run,out):
    from fourstate_sync_common import WORKSPACE
    from fourstate_sync_remote import Guard
    g=Guard();ref='runs/f46-20261017t182304z-84b51ec0'
    try:
        receipt=json.loads(g.read(ref+'/reports/reference-runtime-v3.json',None))
        for row in receipt['files']:
            if hashlib.sha256(g.read(row['copy'],None)).hexdigest()!=row['sha256']:raise ValueError('Icarus runtime changed')
    finally:g.close()
    base=Path(WORKSPACE)/ref/'tool/iverilog-v3'
    command(out,'iverilog-version',[base/'bin/iverilog','-B',base/'lib/ivl','-V'])
    return base

def simulate_case(case,ir,originals,*,binary_only=False):
    from fourstate_stateflow import lower_small,signature
    from fourstate_encode import encode
    from verify_fourstate_clocks import benches,parse_rtl,compare
    case.mkdir();write(case/'compact.ir.json',ir)
    lowered=lower_small(ir,allowed_tops=[ir['top']]);rtl,mapping=encode(lowered)
    (case/'encoded.sv').write_text(rtl);write(case/'lowered.ir.json',lowered);write(case/'ports.json',mapping)
    for i,p in enumerate(originals):(case/f'original-{i}.sv').write_bytes(p.read_bytes())
    trace=trace_for(mapping);write(case/'trace.json',trace)
    initials=[{s['name']:c*s['width'] for s in mapping['states']} for c in '01xz'];write(case/'initials.json',initials)
    summaries=[];all_results=[]
    for i,initial in enumerate(initials):
        folder=case/f'initial-{i}';folder.mkdir();b=benches(mapping,trace,initial,compact=True)
        for role in ('original','encoded'):(folder/(role+'-tb.sv')).write_text(b[role])
        write(folder/'descriptors.json',{k:v for k,v in b.items() if k.endswith('_descriptors')})
        results={}
        for role in ('original','encoded'):
            sources=[case/'encoded.sv'] if role=='encoded' else [case/f'original-{j}.sv' for j in range(len(originals))]
            result,_=command(folder,role+'-compile',[IV/'bin/iverilog','-B',IV/'lib/ivl','-g2012','-s','tb','-o',folder/(role+'.vvp'),*sources,folder/(role+'-tb.sv')],required=role=='encoded')
            if result['exit_code']:continue
            args=[IV/'bin/vvp']+(['-M',str(out),'-m','deposit4'] if role=='original' else [])+[folder/(role+'.vvp')]
            _,stdout=command(folder,role+'-execute',args)
            results[role]=parse_rtl(mapping,stdout,b[role+'_descriptors'],len(trace),role=='encoded')
            write(folder/(role+'-result.json'),results[role])
        if len(results)==2:
            try:
                summary=compare(mapping,results['original'],results['encoded'],copy.deepcopy(results['encoded']));summary['status']='RAW_ENCODED_PASS'
            except ValueError as e:summary=dict(status='REFERENCE_DISAGREEMENT',error=str(e)[:3000])
        else:summary=dict(status='MODELSIM_REFERENCE_REQUIRED')
        summaries.append(summary);all_results.append(results)
    write(case/'plan.json',dict(top=ir['top'],original_sources=[f'original-{j}.sv' for j in range(len(originals))],compact_sha256=signature(ir)))
    result=dict(top=ir['top'],cases=summaries,samples=len(trace)*len(initials),nodes=len(ir['nodes']),lowered_nodes=len(lowered['nodes']),
        encoded_bytes=len(rtl.encode()),storage=ir['storage'],original_sha256=[digest(p) for p in originals])
    write(case/'result.json',result);return result

def probe(request,out):
    from fourstate_stateflow import parse_compact
    source=Path(request['input_run'])/'reports/inputs';scale=[]
    for name in ('array_0_6144x64','array_4096x96'):
        start=time.monotonic();ir=parse_compact(source/(name+'.sv'),name,'RW0_clk')
        write(out/(name+'.ir.json'),ir);scale.append(dict(top=name,nodes=len(ir['nodes']),objects=len(ir['states']),storage=ir['storage'],ir_bytes=(out/(name+'.ir.json')).stat().st_size,seconds=time.monotonic()-start))
    for depth in (2,32,1024,6144):
        p=out/f'scale-{depth}.sv';p.write_text(f'module Scale(input clk,en,input [12:0] a,input [63:0] d,output [63:0] q);reg [63:0] m[0:{depth-1}];always @(posedge clk)if(en)m[a]<=d;assign q=m[a];endmodule')
        start=time.monotonic();ir=parse_compact(p,'Scale','clk');write(out/f'scale-{depth}.ir.json',ir)
        scale.append(dict(depth=depth,nodes=len(ir['nodes']),objects=len(ir['states']),memory_bits=ir['storage']['memory_bits'],seconds=time.monotonic()-start))
    assert len({r['nodes'] for r in scale if 'depth' in r})==1
    write(out/'scale.json',scale)
    cases=[]
    for top,files,clock in [('ram_2x273',['ram_2x273'],'W0_clk'),('Queue2_AXI4BundleR',['Queue2_AXI4BundleR','ram_2x273'],'clock'),('Queue2_AXI4BundleAW_3',['Queue2_AXI4BundleAW_3','ram_2x57'],'clock')]:
        src=out/('input-'+top);src.mkdir()
        originals=[]
        for n in files:
            p=src/(n+'.sv');p.write_bytes((source/(n+'.sv')).read_bytes());originals.append(p)
        ir=parse_compact(src,top,clock);cases.append(simulate_case(out/top,ir,originals))
    wrapper=out/'GateRAM.sv';wrapper.write_text('''module GateRAM(input ck,cb,en,te,we,a,input [272:0] d,output [272:0] q,output [339:0] r);
    wire g,inv;assign inv=~cb;ClockGate gate(.TE(te),.E(en),.CK(ck),.Q(g));
    ram_2x273 x(.R0_addr(a),.R0_en(we),.R0_clk(cb),.R0_data(q),.W0_addr(a),.W0_en(we),.W0_clk(g),.W0_data(d));
    ram_2x340 y(.R0_addr(a),.R0_en(we),.R0_clk(ck),.R0_data(r),.W0_addr(a),.W0_en(we),.W0_clk(inv),.W0_data({67'b0,d}));endmodule''')
    src=out/'input-GateRAM';src.mkdir();originals=[]
    for p in [wrapper,*[source/(n+'.sv') for n in ('ClockGate','ram_2x273','ram_2x340')]]:
        q=src/p.name;q.write_bytes(p.read_bytes());originals.append(q)
    ir=parse_compact(src,'GateRAM','ck',event_clocks=('ck','cb'));cases.append(simulate_case(out/'GateRAM',ir,originals))
    # Same-process overlapping NBA, signed/nonzero ranges and old-memory reads.
    p=out/'MemoryOrder.sv';p.write_text('''module MemoryOrder(input clk,en,en2,input signed [2:0] a,input [3:0] d,output [3:0] q,r);
    reg [3:0] m[3:2];always @(posedge clk)begin if(en)begin m[a]<=d;m[3]<=m[2];end
    if(en2)begin m[2][2:1]<=d[1:0];m[2][1:0]<=m[3][1:0];end end assign q=m[a];assign r=m[2];endmodule''')
    cases.append(simulate_case(out/'MemoryOrder',parse_compact(p,'MemoryOrder','clk'),[p]))
    p=out/'MemorySlices.sv';p.write_text('''module MemorySlices(input clk,en,en2,input [2:0] a,input [7:0] d,output [7:0] q,r);
    reg [0:7] m[2:3];always @(posedge clk)begin if(en)m[a]<=d;
    if(en2)begin m[2][0 +: 3]<=m[3][5:7];m[2][7 -: 4]<=d[3:0];end end assign q=m[a];assign r=m[2];endmodule''')
    cases.append(simulate_case(out/'MemorySlices',parse_compact(p,'MemorySlices','clk'),[p]))
    write(out/'probe-summary.json',dict(scale=scale,cases=cases))

def compare_two_legacy(mapping,request,raw,encoded):
    """Strict output/state/event comparison, independent of frozen Lean data."""
    import re
    outputs=[p for p in mapping['ports'] if p['direction']=='output'];fields=outputs+mapping.get('states',[])
    phases=('pre','post') if mapping.get('mode')=='sequential' else ('comb',)
    def read(text,binary):
        lines=[]
        for line in text.splitlines():
            line=line.lstrip('# ')
            if line.startswith('F41 '):line=line[4:]
            if re.match(r'^\d+ ',line):lines.append(line.split())
        if len(lines)!=len(request['trace'])*len(phases):raise ValueError('Incomplete legacy observations')
        result=[]
        for i,line in enumerate(lines):
            prefix=[str(i//len(phases))]+([phases[i%len(phases)]] if len(phases)==2 else [])
            if line[:len(prefix)]!=prefix:raise ValueError('Legacy sample/phase mismatch')
            values=line[len(prefix):];events=[]
            if 'events' in mapping:events=values[:3];values=values[3:]
            if len(values)!=len(fields)*(2 if binary else 1):raise ValueError('Legacy observation field mismatch')
            decoded=[]
            for j,p in enumerate(fields):
                pair=values[2*j:2*j+2] if binary else [values[j]]
                if any(len(v)!=p['width'] or set(v)-set('01' if binary else '01xz') for v in pair):raise ValueError('Unknown rail or invalid width')
                decoded.append(''.join('01xz'[(int(a)<<1)|int(b)] for a,b in zip(*pair)) if binary else pair[0])
            result.append((events,decoded))
        return result
    a,b=read(raw,False),read(encoded,True)
    for i,(x,y) in enumerate(zip(a,b)):
        if x!=y:raise ValueError('Legacy raw/encoded mismatch '+str(i))
    return dict(status='RAW_ENCODED_PASS',samples=len(request['trace']),bit_positions=len(a)*sum(p['width'] for p in fields))

def regressions(request,out):
    from fourstate_sync_common import WORKSPACE
    from fourstate_sync_remote import Guard
    from fourstate_frontend import parse
    from fourstate_encode import encode
    from verify_fourstate_clocks import benches,parse_rtl,compare
    from verify_fourstate_clocks_regression import canonical,sha
    root=Path(__file__).resolve().parents[1];configs=json.loads((root/'verification/fourstate/f53-regressions.json').read_bytes());g=Guard();summary=[];missing=[]
    old='runs/f51a-20261018t041550z-80afd4fe/reports'
    resume=([request['resume_label']] if request.get('resume_label') else [])+request.get('resume_labels',[])
    previous_roots=[]
    for label in resume:
        from fourstate_sync_common import identifier
        previous=Path(request['run'])/'reports'/identifier(label);previous_roots.append(previous)
        machine=json.loads((previous/'machine.json').read_bytes());hashes={Path(n).name:h for n,h in machine['source_hashes'].items()}
        for name in ('fourstate_elaborated.py','fourstate_encode.py','fourstate_frontend.py','fourstate_bounded_events.py','fourstate_stateflow.py','fourstate_preprocess.py'):
            if hashes[name]!=digest(Path(__file__).parent/name):raise ValueError('Cannot resume with changed semantics')
    try:
        for c in configs:
            oldcase=old+'/input-'+c['case']
            if not g.exists(oldcase+'/plan.json'):missing.append(c['case']);continue
            checkpoint=next((p/c['case']/'result.json' for p in previous_roots if (p/c['case']/'result.json').is_file()),None)
            if checkpoint:
                path=checkpoint;r=json.loads(path.read_bytes());r['checkpoint']=dict(path=str(path),sha256=digest(path),semantic_sources_verified=True)
                summary.append(r);continue
            f=out/c['case'];f.mkdir();plan=json.loads(g.read(oldcase+'/plan.json',None));payload={}
            for row in plan['files']:
                data=g.read(oldcase+'/'+row['path'],None)
                if hashlib.sha256(data).hexdigest()!=row['sha256']:raise ValueError('Frozen asset hash mismatch')
                payload[row['path']]=data
            src=f/'source';src.mkdir()
            for name,data in payload.items():
                if name.endswith(('.sv','.v','.svh','.vh')) and name!='encoded.sv' and not name.endswith('-tb.sv'):
                    p=src/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(data)
            paths=[src/n for n in c['source_files']]
            source=Path(os.path.commonpath([str(p.parent) for p in paths])) if c['source_is_dir'] else paths[0]
            options=dict(c['options']);includes=[]
            if plan.get('tests'):
                includes=[src/n[2:] for n in plan['tests'][0]['original_args'] if n.startswith('-I')]
            options['include_dirs']=includes
            start=time.monotonic();ir=parse(source,c['top'],**options);rtl,mapping=encode(ir)
            if rtl.replace('\r\n','\n')!=payload['encoded.sv'].decode().replace('\r\n','\n'):raise ValueError('Regenerated encoding differs: '+c['case'])
            oldmapping=json.loads(payload['ports.json'])
            for key in ('ports','states'):
                keys=('name','width','tag','value') if key=='ports' else ('name','width','process','current_tag','current_value','next_tag','next_value')
                if [{k:p.get(k) for k in keys} for p in mapping.get(key,[])]!=[{k:p.get(k) for k in keys} for p in oldmapping.get(key,[])]:raise ValueError('Regenerated interface differs')
            mapping=oldmapping;(f/'encoded.sv').write_text(rtl);write(f/'regenerated.ir.json',ir);write(f/'ports.json',mapping)
            tests=[]
            if 'trace.json' in payload:
                trace=json.loads(payload['trace.json']);initials=json.loads(payload['initials.json'])
                for i,initial in enumerate(initials):
                    d=f/f'initial-{i}';d.mkdir();b=benches(mapping,trace,initial,compact=True);(d/'encoded-tb.sv').write_text(b['encoded'])
                    command(d,'compile',[IV/'bin/iverilog','-B',IV/'lib/ivl','-g2012','-s','tb','-o',d/'sim.vvp',f/'encoded.sv',d/'encoded-tb.sv'])
                    _,text=command(d,'execute',[IV/'bin/vvp',d/'sim.vvp']);encoded=parse_rtl(mapping,text,b['encoded_descriptors'],len(trace),True)
                    ref='reference-'+str(i)+'.log'
                    if ref in payload:rawtext=payload[ref].decode();reference='SHA256-verified frozen native reference'
                    else:
                        (d/'original-tb.sv').write_text(b['original'])
                        command(d,'original-compile',[IV/'bin/iverilog','-B',IV/'lib/ivl','-g2012','-s','tb','-o',d/'original.vvp',*[src/n for n in plan['original_sources']],d/'original-tb.sv'])
                        _,rawtext=command(d,'original-execute',[IV/'bin/vvp','-M',str(out),'-m','deposit4',d/'original.vvp']);reference='Fresh untouched raw RTL / Icarus'
                    original=parse_rtl(mapping,rawtext,b['original_descriptors'],len(trace));r=compare(mapping,original,encoded,copy.deepcopy(encoded));r.update(status='RAW_ENCODED_PASS',reference=reference);tests.append(r)
            else:
                for t in plan['tests']:
                    d=f/t['name'];d.mkdir();bench=payload[t['name']+'/encoded-tb.sv'];(d/'encoded-tb.sv').write_bytes(bench)
                    command(d,'compile',[IV/'bin/iverilog','-B',IV/'lib/ivl','-g2012','-s','tb','-o',d/'sim.vvp',f/'encoded.sv',d/'encoded-tb.sv'])
                    _,text=command(d,'execute',[IV/'bin/vvp',d/'sim.vvp'])
                    ref=t['name']+'/reference.log'
                    supplement='runs/'+Path(request['run']).name+'/reports/reference-supplement-v1/'+c['case']+'/'+t['name']+'.log'
                    selected=t.get('windows_reference')
                    # Retained failed ModelSim trials may still be in the
                    # archive. The accepted plan chooses the reference, not
                    # the mere presence of a file named reference.log.
                    if selected:
                        chosen=selected.get('file',ref)
                        if chosen in payload:raw=payload[chosen].decode()
                        elif g.exists(supplement):
                            raw=g.read(supplement,None).decode()
                            if sha(canonical(raw,'original'))!=t['expected']['original']:
                                raise ValueError('Selected native reference supplement digest differs')
                            write(d/'reference-selection.json',dict(source=supplement,accepted_selection=selected,
                                canonical_sha256=sha(canonical(raw,'original')),reason='Accepted native reference stored separately from archived asset bundle'))
                        else:raise ValueError('Selected native reference is unavailable: '+chosen)
                    elif not plan.get('fresh_comparison') and g.exists(supplement):raw=g.read(supplement,None).decode()
                    else:
                        (d/'original-tb.sv').write_bytes(payload[t['name']+'/original-tb.sv'])
                        args=[str(src/n) if n.endswith(('.sv','.v')) else '-I'+str(src/n[2:]) if n.startswith('-I') else n for n in t['original_args'] if n!=t['name']+'/original-tb.sv']
                        command(d,'original-compile',[IV/'bin/iverilog','-B',IV/'lib/ivl','-o',d/'original.vvp',*args,d/'original-tb.sv'])
                        _,raw=command(d,'original-execute',[IV/'bin/vvp','-M',str(out),'-m','deposit4',d/'original.vvp'])
                    if (selected or not plan.get('fresh_comparison')) and sha(canonical(raw,'original'))!=t['expected']['original']:raise ValueError('Frozen native reference digest differs')
                    tests.append(compare_two_legacy(mapping,json.loads(payload[t['name']+'/request.json']),raw,text))
            r=dict(case=c['case'],top=c['top'],status='REGENERATED_RAW_ENCODED_PASS',tests=tests,seconds=time.monotonic()-start,encoded_sha256=digest(f/'encoded.sv'),frozen_assets_sha256={n:hashlib.sha256(v).hexdigest() for n,v in payload.items() if n!='model/FourState/Model.lean' and not n.endswith('.lean')})
            summary.append(r);write(f/'result.json',r);write(out/('progress-'+str(len(summary))+'.json'),dict(last=c['case'],samples=sum(t['samples'] for v in summary for t in v['tests'])))
    finally:g.close()
    write(out/'regression-summary.json',dict(cases=summary,absent_superseded_inputs=missing,samples=sum(t['samples'] for v in summary for t in v['tests']),scope='Fresh RTL parse and encoding; actual encoded RTL execution, full frozen raw references or fresh native reference; no FunctionalIR/Lean execution'))

def faults(request,out):
    from fourstate_stateflow import lower_small
    from fourstate_encode import encode
    from verify_fourstate_clocks import benches,parse_rtl,compare
    good=Path(request['run'])/'reports/probe-v1/MemoryOrder';ir=json.loads((good/'compact.ir.json').read_bytes());mapping=json.loads((good/'ports.json').read_bytes())
    trace=json.loads((good/'trace.json').read_bytes());initial=json.loads((good/'initials.json').read_bytes())[3]
    reference=json.loads((good/'initial-3/original-result.json').read_bytes());rows=[]
    for fault in ('wrong_address','omit_write','early_commit','wrong_trigger','xz_confusion'):
        f=out/fault;f.mkdir();mut=copy.deepcopy(ir)
        if fault=='wrong_address':
            n=next(n for n in mut['nodes'] if n['op']=='memory_write');a=mut['nodes'][n['args'][1]];a.update(op='const',args=[],bits='010')
        lowered=lower_small(mut,allowed_tops=[ir['top']])
        if fault=='omit_write':
            name=lowered['states'][0]['name'];id=len(lowered['nodes']);lowered['nodes'].append(dict(id=id,op='ref',name=name,width=4,signed=False,args=[],source=lowered['states'][0]['source']));lowered['next_state'][name]=id
        rtl,m=encode(lowered);b=benches(mapping,trace,initial,compact=True);bench=b['encoded']
        if fault=='early_commit':
            import re
            bench=re.sub(r'(if\(fire\d+\) s\d+_current_(?:tag|value)) <= ([^;]+);',r'\1 = \2; #0;',bench)
        if fault=='wrong_trigger':bench=bench.replace('rise4=(a==0 && b!=0)||((a==2||a==3)&&b==1);','rise4=(a==0 && b==1);')
        if fault=='xz_confusion':
            import re
            s=m['states'][0];rtl=re.sub(r'(assign '+re.escape(s['next_value'])+r' = )([^;]+);',r'\g<1>('+s['next_tag']+r' ? 0 : \2);',rtl)
        (f/'encoded.sv').write_text(rtl);(f/'tb.sv').write_text(bench)
        command(f,'compile',[IV/'bin/iverilog','-B',IV/'lib/ivl','-g2012','-s','tb','-o',f/'sim.vvp',f/'encoded.sv',f/'tb.sv'])
        _,text=command(f,'execute',[IV/'bin/vvp',f/'sim.vvp']);bad=parse_rtl(mapping,text,b['encoded_descriptors'],len(trace),True)
        try:compare(mapping,reference,bad,copy.deepcopy(bad))
        except ValueError as e:rows.append(dict(fault=fault,status='DETECTED',diagnostic=str(e)[:2500]))
        else:raise ValueError('Fault escaped '+fault)
    gate=Path(request['run'])/'reports/probe-v1/GateRAM';gm=json.loads((gate/'ports.json').read_bytes())
    gt=json.loads((gate/'trace.json').read_bytes());gi=json.loads((gate/'initials.json').read_bytes())[1]
    for fault in ('inconsistent_latch_deposit','delta_nonconvergence_guard'):
        f=out/fault;f.mkdir();fault_trace=copy.deepcopy(gt)
        if fault=='inconsistent_latch_deposit':
            fault_trace[0]['inputs']['ck']='0'
        b=benches(gm,fault_trace,gi,compact=True);text=b['encoded'];write(f/'trace.json',fault_trace)
        if fault=='delta_nonconvergence_guard':
            needle='if(changed) $fatal(1,"delta budget");'
            if needle not in text:raise ValueError('Settlement guard anchor absent')
            text=text.replace(needle,'changed=1; '+needle)
        (f/'tb.sv').write_text(text)
        command(f,'compile',[IV/'bin/iverilog','-B',IV/'lib/ivl','-g2012','-s','tb','-o',f/'sim.vvp',gate/'encoded.sv',f/'tb.sv'])
        row,stdout=command(f,'execute',[IV/'bin/vvp',f/'sim.vvp'],required=False)
        expected='inconsistent initial latch' if fault=='inconsistent_latch_deposit' else 'delta budget'
        if row['exit_code']==0 or expected not in stdout:raise ValueError('Runtime guard did not reject '+fault)
        rows.append(dict(fault=fault,status='REJECTED',diagnostic=expected,exit_code=row['exit_code'],scope='Injected harness/deposit fault; not an admitted feedback circuit'))
    write(out/'faults.json',rows)

def main():
    import resource
    global IV,out
    os.umask(0o077);p=argparse.ArgumentParser();p.add_argument('--request',type=Path,required=True);p.add_argument('--out',type=Path,required=True);a=p.parse_args()
    request=json.loads(a.request.read_bytes());out=a.out
    from audit_fourstate_f52_worker import setup
    setup(Path(request['run']))
    source=Path(request['input_run'])/'reports/inputs';receipt=json.loads((Path(request['run'])/'reports/input-receipt.json').read_bytes())
    for row in receipt['files']:
        f=source/row['name']
        if f.is_symlink() or digest(f)!=row['sha256']:raise ValueError('Frozen input changed')
    source_root=Path(__file__).resolve().parent
    write(out/'machine.json',dict(hostname=os.uname().nodename,python=sys.version,source_hashes={str(f):digest(f) for f in source_root.rglob('*.py') if f.is_file()},inherited_limits={n:resource.getrlimit(getattr(resource,n)) for n in ('RLIMIT_CPU','RLIMIT_AS','RLIMIT_FSIZE')}))
    mode=request.get('mode','probe')
    if mode=='probe':
        IV=runtime(Path(request['run']),out)
        root=Path(__file__).resolve().parents[1];(out/'deposit4.c').write_bytes((root/'verification/fourstate/deposit4.c').read_bytes())
        command(out,'vpi-compile',['/usr/bin/gcc','-shared','-fPIC','-O2','-Wall','-Wextra','-Werror','-I/usr/include/iverilog',out/'deposit4.c','-o',out/'deposit4.vpi'])
        probe(request,out)
    elif mode in ('regressions','compare'):
        IV=runtime(Path(request['run']),out)
        root=Path(__file__).resolve().parents[1];(out/'deposit4.c').write_bytes((root/'verification/fourstate/deposit4.c').read_bytes())
        command(out,'vpi-compile',['/usr/bin/gcc','-shared','-fPIC','-O2','-Wall','-Wextra','-Werror','-I/usr/include/iverilog',out/'deposit4.c','-o',out/'deposit4.vpi'])
        (regressions if mode=='regressions' else faults)(request,out)
    elif mode=='inventory':
        from audit_fourstate_stateflow import inventory
        inventory(request,out)
    elif mode=='tests':
        import unittest
        from audit_fourstate_f52_worker import focused_tests
        old=out/'historical';old.mkdir();focused_tests(Path(request['run']),old)
        root=old/'project';sys.path.insert(0,str(root/'tests'))
        suite=unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromName(name) for name in
            ('test_fourstate_stateflow','test_fourstate_events','test_fourstate_procedural','test_fourstate_packed_procedural','test_fourstate_prioritymux','test_fourstate_preprocess','test_fourstate_blockers'))
        result=unittest.TextTestRunner(verbosity=2).run(suite);write(out/'tests.json',dict(tests=result.testsRun,failures=len(result.failures),errors=len(result.errors)))
        if not result.wasSuccessful():raise ValueError('F53 tests failed')
    else:raise ValueError('Mode not implemented yet: '+mode)
if __name__=='__main__':main()
