"""F56 finite probes of hierarchy, expressions, process scopes and diagnostics."""
from pathlib import Path
import argparse,hashlib,json,os,resource,sys,time,traceback
sys.path.insert(0,str(Path(__file__).resolve().parent/'verification'/'four_state'))
from verify_fourstate_f53 import write,digest,runtime,command
from verify_fourstate_f54 import selected_sources

TOPS=['SubDirectory_4','ArrayMulDataModule','AXI4UserYanker','BlockCipherModule','MSHR','Directory_Anon_4','HashModule','HPerfCounter','TLXbar','AgeDetector','CSRCacheErrorDecoder','DebugTransportModuleJTAG','HPerfMonitor','CryptoModule']


def fixtures(out):
    base=out/'fixtures';base.mkdir();result={}
    texts=dict(
        ExprTypes="module ExprTypes(input clock,input [3:0] a,b,input signed [2:0] i,input [2:0] c,output s,output [3:0] p,m,output reg [7:0] q);wire [1:0][3:0] v='{a,b};assign s=a[i];assign p=v[i];assign m=c?a:b;always @(posedge clock)if(c)q<=a+1;else q<=b-1;endmodule",
        Remainder="module Remainder(input clock,input signed [4:0] a,b,output signed [4:0] s,output [4:0] u,output reg signed [4:0] q);assign s=a%b;assign u=$unsigned(a)%$unsigned(b);always @(posedge clock)q<=a%b;endmodule",
        Reduction="module Reduction(input clock,input [5:0] d,output p,n,output reg q);assign p=^d;assign n=~^d;always @(posedge clock)q<=^d;endmodule",
        LiteralTypes="module LiteralTypes(input clock,input [3:0] a,output [7:0] ones,zeros,xs,zs,output reg [7:0] q,u);assign ones='1;assign zeros='0;assign xs='x;assign zs='z;always @(posedge clock)begin q<=$signed(a)+-1;u<=a+1;end endmodule",
        OpenPorts="module OC(input [2:0] d,output [2:0] q,unused);assign q=d;assign unused=~d;endmodule module OpenPorts(input clock,input [2:0] d,output [2:0] q,z,output reg [2:0] r);OC used(.d(d),.q(q),.unused());OC floating(.d(),.q(z),.unused());always @(posedge clock)r<=z;endmodule",
        LocalScope="module LocalScope(input clock,input [2:0] d,output reg [2:0] q,r);always @(posedge clock)begin:a automatic logic [2:0] x=d;begin:b automatic logic [2:0] x=~d;q<=x;end end always @(negedge clock)begin:c automatic logic [2:0] x;if(d[0])x=d+1;r<=x;end endmodule",
        DiagOrder="module DiagOrder(input clock,reset,d,output reg [2:0] pipe,output reg [3:0] q);wire dr=pipe[2];always @(posedge clock or posedge reset)if(reset)pipe<=3'b111;else pipe<={pipe[1:0],d};always @(posedge clock or posedge dr)begin:consumer automatic logic signed [3:0] old=q;if(dr)q<=0;else q<=q+1;assert(q==4'h0)else $error(\"old=%b hex=%x dec=%d\\n\\n\",q,q,old);if(d)$fwrite(32'h80000002,\"writer old=%b\\n\",q);end endmodule")
    for top,text in texts.items():
        target=base/top;target.mkdir();(target/(top+'.sv')).write_text(text);result[top]=target
    return result


def trace_for(mapping):
    from fourstate_scalable_bench import rails
    ports=[p for p in mapping['ports'] if p['direction']=='input'];now={p['name']:'0'*p['width'] for p in ports}
    roots=mapping['events']['roots']
    for root in roots:now[root]='1'
    rows=[dict(inputs=dict(now))]
    def change(name,bits):
        if bits!=now[name]:now[name]=bits;rows.append(dict(inputs=dict(now)))
    def cycle():
        for root in roots:change(root,'0');change(root,'1')
    if mapping['top']=='Remainder':
        for a in range(32):
            change('a',format(a,'05b'))
            for b in range(32):change('b',format(b,'05b'));cycle()
        for a in ('xxxxx','zzzzz','1x001','0z010'):
            change('a',a)
            for b in ('00000','00001','xxxxx','zzzzz','1x001'):change('b',b);cycle()
        return rows
    if mapping['top']=='Reduction':
        for d in [format(i,'06b') for i in range(64)]+['xxxxxx','zzzzzz','00000x','11111z','1x0z01']:
            change('d',d);cycle()
        return rows
    if mapping['top']=='ExprTypes':
        for a in ('0000','0001','1010','1111','xxxx','zzzz','1x0z'):
            change('a',a);change('b',a[::-1])
            for index in ('000','001','011','100','111','xxx','zzz'):
                change('i',index)
                for c in ('000','001','010','1xz','0xx','zzz'):change('c',c);cycle()
        return rows
    # Visit each actual input port under known and unknown values, with a
    # process activation after each change. Port widths are never truncated.
    data=[p for p in ports if p['name'] not in roots]
    for symbol in '01xz':
        for p in data:change(p['name'],symbol*p['width']);cycle()
    # Exact edge classes on every admitted root, including X/Z transitions.
    for root in roots:
        for a in '01xz':
            for b in '01xz':change(root,a);change(root,b)
    return rows


def behavior(request,out,iv):
    from fourstate_extended_bench import benches,run_stream,compare
    from fourstate_scalable_bench import records
    base=Path(request['run'])/'reports'/request['probe_label'];results=[]
    for top in request['tops']:
        src=base/top;d=out/top;d.mkdir();m=json.loads((src/'mapping.json').read_bytes());trace=trace_for(m)
        (d/'encoded.sv').write_bytes((src/'encoded.sv').read_bytes());write(d/'mapping.json',m);write(d/'trace.json',trace)
        (d/'original').mkdir()
        for f in (src/'original').glob('*.sv'):(d/'original'/f.name).write_bytes(f.read_bytes())
        initials=[dict(symbol=s,overrides={}) for s in '01xz']
        if top=='DiagOrder':
            initials.extend(dict(symbol='0',overrides={top+'.pipe':b}) for b in ('010','0x0','0z0'))
        write(d/'initial-plan.json',initials);rows=[]
        for i,seed in enumerate(initials):
            f=d/f'initial-{i}';f.mkdir();write(f/'initial.json',seed);write(f/'trace.json',trace);streams={}
            if request.get('encoded_only'):
                tb,data=benches(m,trace,seed['symbol'],False,initial_overrides=seed['overrides']);(f/'original-tb.sv').write_text(tb);(f/'original-inputs.mem').write_text(data)
            for binary in ((True,) if request.get('encoded_only') else (True,False)):
                role='encoded' if binary else 'original';tb,data=benches(m,trace,seed['symbol'],binary,initial_overrides=seed['overrides']);(f/(role+'-tb.sv')).write_text(tb);(f/(role+'-inputs.mem')).write_text(data)
                files=[d/'encoded.sv'] if binary else sorted((d/'original').glob('*.sv'))
                cc,_=command(f,role+'-compile',[iv/'bin/iverilog','-B',iv/'lib/ivl','-g2012','-s','tb','-o',f/(role+'.vvp'),*files,f/(role+'-tb.sv')],required=False)
                if cc['exit_code']:
                    if binary:raise ValueError('Encoded compile failure '+str(f))
                    continue
                rr=run_stream(f,role+'-execute',[iv/'bin/vvp',f/(role+'.vvp')])
                if rr['exit_code']:raise ValueError('Execution failure '+str(f)+' '+role)
                streams[role]=f/(role+'-execute/stdout.log.gz')
            result=dict(status='ENCODED_EXECUTED' if request.get('encoded_only') else 'REFERENCE_REQUIRED')
            if len(streams)==2:
                try:
                    initial_words=0
                    for sample,phase,kind,obj,index,value in records(streams['original'],False):
                        if sample!=0 or phase!=0:break
                        if kind=='S':
                            s=m['states'][obj];expected=seed['overrides'].get(s['name'],seed['symbol']*s['width'])
                            if value!=expected:raise ValueError('Lost explicit initial state '+s['name'])
                            initial_words+=1
                    result=compare(streams['original'],streams['encoded'],m,len(trace));result['checked_initial_words']=initial_words
                    if top=='Remainder':
                        from fourstate_extended_bench import remainder_oracle
                        result['literal_original']=remainder_oracle(streams['original'],trace,seed['symbol'],False)
                        result['literal_encoded']=remainder_oracle(streams['encoded'],trace,seed['symbol'],True)
                except ValueError as e:result=dict(status='DISAGREEMENT',error=str(e))
            result['initial']=i;write(f/'comparison.json',result);rows.append(result);print(top,i,result['status'],result.get('error',''),flush=True)
        results.append(dict(top=top,initials=rows,samples=len(trace)*len(initials)));write(out/('progress-'+top+'.json'),results)
    write(out/'summary.json',results)
    allowed={'ENCODED_EXECUTED'} if request.get('encoded_only') else {'RAW_ENCODED_PASS'}
    if any(x['status'] not in allowed for r in results for x in r['initials']):raise ValueError('Unresolved real behavior reference')


def inventory(request,out):
    from collections import Counter
    from fourstate_preprocess import parse_sources,location
    import fourstate_preprocess as pp
    source=Path(request['input_run'])/'reports/inputs';receipt=json.loads((Path(request['run'])/'reports/input-receipt.json').read_bytes())
    pp.MAX_FILES=len(receipt['files']);pp.MAX_BYTES=sum(r['bytes'] for r in receipt['files'])
    s,sm,trees,files=parse_sources(source)
    opts=s.ast.CompilationOptions();opts.topModules={'XSTop'};opts.maxInstanceDepth=128;opts.maxGenerateSteps=65536
    c=s.ast.Compilation(s.Bag([opts]))
    for t in trees:c.addSyntaxTree(t)
    root=c.getRoot();diags=c.getAllDiagnostics()
    if any(d.isError() for d in diags):raise ValueError(s.DiagnosticEngine.reportAll(sm,diags))
    seen=set();calls=[];expr=Counter();instances=Counter();locals_=[]
    def k(n):return str(n.kind).split('.')[-1]
    def walk(inst):
        name=inst.definition.name;instances[name]+=1
        if name not in seen:
            seen.add(name)
            def collect(n):
                if isinstance(n,s.ast.Expression):
                    expr[k(n)]+=1
                    if k(n)=='Call':
                        args=[]
                        for a in n.arguments:
                            row=dict(kind=k(a),width=a.type.bitWidth,type=str(a.type),signed=bool(a.type.isSigned))
                            if k(a)=='StringLiteral':row['value']=a.value
                            if k(a)=='IntegerLiteral':row['value']=str(a.value)
                            args.append(row)
                        calls.append(dict(module=name,system=n.isSystemCall,name=n.subroutineName,args=args,source=location(sm,n.sourceRange.start)))
                if isinstance(n,s.ast.Statement) and k(n)=='VariableDeclaration':locals_.append(dict(module=name,name=n.symbol.name,path=n.symbol.hierarchicalPath,lifetime=str(n.symbol.lifetime),source=location(sm,n.sourceRange.start)))
            for n in inst.body:
                if k(n)=='ProceduralBlock':n.body.visit(collect)
                elif k(n)=='ContinuousAssign':n.assignment.visit(collect)
                elif k(n) in ('Net','Variable') and n.initializer is not None:n.initializer.visit(collect)
        for n in inst.body:
            if k(n)=='Instance':walk(n)
    walk(root.topInstances[0])
    write(out/'inventory.json',dict(definitions=len(seen),instances=sum(instances.values()),module_counts=dict(instances),expressions=dict(expr),calls=calls,locals=locals_,semantic_conversion=False))


def tests(request,out):
    import unittest
    from verify_fourstate_f54 import tests as old_tests
    old_tests(request,out)
    sys.path.insert(0,str(Path(__file__).parent.parent/'tests'))
    suite=unittest.defaultTestLoader.loadTestsFromNames(['test_fourstate_eventful','test_fourstate_extended'])
    result=unittest.TextTestRunner(verbosity=2).run(suite)
    write(out/'extended-tests.json',dict(tests=result.testsRun,failures=len(result.failures),errors=len(result.errors)))
    if not result.wasSuccessful():raise ValueError('Extended tests failed')


def audit(request,out):
    """Freshly bind/import every real closure; do not emit a giant XSTop RTL."""
    import gzip
    from audit_fourstate_f52_worker import closure,profile,diagnostic
    from fourstate_extended import parse_extended
    import fourstate_preprocess as pp
    source=Path(request['input_run'])/'reports/inputs'
    modules={m['name']:m for m in json.loads(gzip.decompress((Path(request['input_run'])/'reports/frozen/modules-v3.json.gz').read_bytes()))}
    receipt=json.loads((Path(request['run'])/'reports/input-receipt.json').read_bytes())
    pp.MAX_FILES=len(receipt['files']);pp.MAX_BYTES=sum(r['bytes'] for r in receipt['files'])
    prep={};s,sm,trees,files=pp.parse_sources(source,report=prep)
    table={Path(p).name:(Path(p),t) for p,t in zip(prep['roots'],trees)};active=set();original=pp.parse_sources
    def cached(path,defines=(),include_dirs=(),report=None):
        if defines or include_dirs:raise ValueError('Only frozen default macro profile admitted')
        names=sorted(active)
        if report is not None:report.update(status='PREPROCESSED',source='Verified frozen syntax cache',files=prep['files'],directives=prep['directives'])
        return s,sm,[table[n][1] for n in names],[table[n][0] for n in names]
    rows=[];beg=time.monotonic();pp.parse_sources=cached
    try:
        for index,name in enumerate(sorted(request.get('tops') or modules)):
            names=closure(modules,name);active.clear();active.update(modules[n]['file'] for n in names)
            options,features=profile(modules,name);clock=options['clock'];roots=tuple(features['clock_roots'])
            if clock and clock not in roots:roots=(clock,*roots)
            start=time.monotonic()
            try:
                ir=parse_extended(source,name,clock,event_clocks=roots)
                row=dict(top=name,status='IR_IMPORTED',nodes=len(ir['nodes']),states=len(ir.get('states',[])),instances=len(ir['elaboration']['instances']),memory_objects=len(ir.get('memories',[])),processes=len(ir.get('events',{}).get('processes',[])),assertions=len(ir.get('assertions',[])),scalar_bits=sum(s['width'] for s in ir.get('states',[]) if not s.get('memory_object')),memory_bits=sum(m['depth']*m['width'] for m in ir.get('memories',[])))
                del ir
            except Exception as e:row=dict(top=name,status='REJECTED' if isinstance(e,ValueError) else 'TOOL_FAILURE',error=str(e),diagnostic=diagnostic(e),traceback=traceback.format_exc())
            row.update(seconds=time.monotonic()-start,closure_definitions=len(names),clock=clock,roots=roots,encoded_rtl='NOT_RUN',behavior='NOT_RUN',peak_rss_bytes=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024)
            rows.append(row);write(out/(name+'-result.json'),row)
            if index%25==0:print(json.dumps(dict(completed=len(rows),top=name,seconds=time.monotonic()-beg,last=row['status'])),flush=True)
    finally:pp.parse_sources=original
    write(out/'ledger.json',rows);write(out/'summary.json',dict(definitions=len(rows),imported=sum(r['status']=='IR_IMPORTED' for r in rows),rejected=sum(r['status']=='REJECTED' for r in rows),tool_failures=sum(r['status']=='TOOL_FAILURE' for r in rows),seconds=time.monotonic()-beg,scope='Fresh complete dependency closure IR import. No whole-XSTop encoding, FunctionalIR, Lean or behavioral claim.'))


def regenerate(request,out):
    from fourstate_extended import parse_extended,encode_extended
    run=Path(request['run']);rows=[]
    for label,top in request['models']:
        source=run/'reports'/label/top;old=json.loads((source/'ir.json').read_bytes());start=time.monotonic()
        ir=parse_extended(source/'original',top,old['clock'],event_clocks=old.get('events',{}).get('roots',()))
        rtl,m=encode_extended(ir)
        if rtl!=(source/'encoded.sv').read_text():raise ValueError('Current encoding differs '+top)
        f=out/top;f.mkdir();write(f/'current.ir.json',ir);write(f/'current.mapping.json',m)
        rows.append(dict(top=top,status='CURRENT_SOURCE_EXACT_ENCODING',source_label=label,encoded_sha256=hashlib.sha256(rtl.encode()).hexdigest(),seconds=time.monotonic()-start))
    write(out/'summary.json',rows)
    from verify_fourstate_f55 import regenerate as previous
    f=out/'v8';f.mkdir();previous(dict(request,run=str(run.parent/'f55-20261018t081308z-8d0a6cc2'),models=[('probe-v1',t) for t in ('ResetGen','SRAMWrapper_32','ResetBanked')]+[('probe-extra-v5',t) for t in ('ResetCascade','ResetInvert','DualStateGate')]),f)


def faults(request,out,iv):
    import copy,re
    from fourstate_extended import parse_extended,encode_extended
    from fourstate_extended_bench import benches,run_stream,compare_encodings
    from fourstate_scalable_bench import records
    run=Path(request['run']);rows=[]
    specs=[('floating_z_to_x','fixtures-v6','OpenPorts',0),('multibit_low_bit','fixtures-v6','ExprTypes',0),
        ('automatic_local_zero','fixtures-v6','LocalScope',0),('stateless_lost_event','real-v6','TLXbar',0),
        ('unsigned_remainder','mod-behavior-v12','Remainder',0),('parity_inverted','reduction-behavior-v11','Reduction',0),
        ('unsized_fill_truncated','literal-behavior-v10','LiteralTypes',0),
        ('drop_error','payload-fixtures-v8','DiagOrder',4),('drop_stderr','payload-fixtures-v8','DiagOrder',0),
        ('payload_reads_next','payload-fixtures-v8','DiagOrder',4),('wrong_format_metadata','payload-fixtures-v8','DiagOrder',4)]
    for name,label,top,initial in specs:
        base=run/'reports'/label/top;old=base/f'initial-{initial}';m=json.loads((base/'mapping.json').read_bytes());trace=json.loads((old/'trace.json').read_bytes());seed=json.loads((old/'initial.json').read_bytes())
        ir=parse_extended(base/'original',top,m['events']['roots'][0],event_clocks=m['events']['roots']);rtl,current=encode_extended(ir)
        if rtl!=(base/'encoded.sv').read_text():raise ValueError('Fault baseline no longer current '+top)
        if name=='floating_z_to_x':next(n for n in ir['nodes'] if n['op']=='const' and n['bits']=='zzz')['bits']='xxx'
        elif name=='multibit_low_bit':
            n=next(n for n in ir['nodes'] if n['op']=='reduce_or');n.update(op='slice',bits=[0])
        elif name=='automatic_local_zero':
            active=set();todo=[ir['next_state'][top+'.r']]
            while todo:
                i=todo.pop()
                if i in active:continue
                active.add(i);todo.extend(ir['nodes'][i]['args'])
            next(ir['nodes'][i] for i in active if ir['nodes'][i]['op']=='const' and ir['nodes'][i]['bits']=='xxx')['bits']='000'
        elif name=='unsigned_remainder':next(n for n in ir['nodes'] if n['op']=='mod')['signed']=False
        elif name=='parity_inverted':next(n for n in ir['nodes'] if n['op']=='reduce_xor')['op']='reduce_xnor'
        elif name=='unsized_fill_truncated':next(n for n in ir['nodes'] if n['op']=='const' and n['width']==8 and n['bits']=='11111111')['bits']='00000001'
        elif name in ('drop_error','drop_stderr'):
            site=next(a for a in ir['assertions'] if a['severity']==('error' if name=='drop_error' else 'write'))
            node=dict(id=len(ir['nodes']),op='const',width=1,signed=False,args=[],bits='0',source=site['source']);ir['nodes'].append(node);site['enable_node']=node['id']
        elif name=='payload_reads_next':ir['assertions'][0]['arguments'][0]['node']=ir['next_state'][top+'.q']
        elif name=='wrong_format_metadata':ir['assertions'][0]['message']='CORRUPTED '+ir['assertions'][0]['message']
        mutant,mm=encode_extended(ir)
        if name=='stateless_lost_event':
            pid=next(p['id'] for p in ir['events']['processes'] if not p['states']);mutant,n=re.subn(r'(?m)^fired\['+str(pid)+r'\]<=.*?;$',f'fired[{pid}]<=0;',mutant)
            if n!=1:raise ValueError('Stateless mutation not applied')
        f=out/name;f.mkdir();(f/'mutant.sv').write_text(mutant);write(f/'mapping.json',mm);tb,data=benches(mm,trace,seed['symbol'],True,initial_overrides=seed['overrides']);(f/'encoded-tb.sv').write_text(tb);(f/'encoded-inputs.mem').write_text(data)
        command(f,'compile',[iv/'bin/iverilog','-B',iv/'lib/ivl','-g2012','-s','tb','-o',f/'mutant.vvp',f/'mutant.sv',f/'encoded-tb.sv'])
        rr=run_stream(f,'execute',[iv/'bin/vvp',f/'mutant.vvp'])
        if rr['exit_code']:raise ValueError('Mutant must complete actual behavior '+name)
        baseline=old/'encoded-execute/stdout.log.gz';reason=None
        try:compare_encodings(baseline,f/'execute/stdout.log.gz',m,len(trace),same_renderer=True)
        except ValueError as e:reason=str(e)
        if reason is None:raise ValueError('Mutation escaped exact comparison '+name)
        prepost_equal=all(a==b for a,b in __import__('itertools').zip_longest(records(baseline,True),records(f/'execute/stdout.log.gz',True)))
        if name in ('drop_error','drop_stderr','payload_reads_next','wrong_format_metadata') and not prepost_equal:raise ValueError('Diagnostic-only mutation changed hardware outputs/state')
        rows.append(dict(fault=name,status='DETECTED',exit_code=rr['exit_code'],reason=reason,pre_post_observations_equal=prepost_equal,mutant_sha256=digest(f/'mutant.sv'),mapping_sha256=digest(f/'mapping.json'),scope='Diagnostic ABI metadata' if name=='wrong_format_metadata' else 'Compiled encoding behavior'))
        write(f/'result.json',rows[-1]);print(name,'DETECTED',flush=True)
    write(out/'summary.json',rows)


def probe(request,out,iv):
    import re,gzip
    from audit_fourstate_f52_worker import profile
    from fourstate_eventful import parse_eventful,encode_eventful
    from fourstate_elaborated import parse
    from fourstate_encode import encode
    if request.get('extended'):
        from fourstate_extended import parse_extended,encode_extended
    source=Path(request['input_run'])/'reports/inputs';rows=[];extra=fixtures(out)
    modules={m['name']:m for m in json.loads(gzip.decompress((Path(request['input_run'])/'reports/frozen/modules-v3.json.gz').read_bytes()))}
    for top in request.get('tops',TOPS+list(extra)):
        d=out/top;d.mkdir();files=selected_sources(extra.get(top,source),top,d/'original');text=(extra.get(top,source)/(top+'.sv')).read_text()
        options,features=profile(modules,top) if top in modules else (dict(clock='clock'),dict(clock_roots=['clock']))
        clock=options['clock'];roots=tuple(features['clock_roots'])
        if clock and clock not in roots:roots=(clock,*roots)
        start=time.monotonic()
        try:
            ir=parse_extended(d/'original',top,clock,event_clocks=roots) if request.get('extended') else parse_eventful(d/'original',top,clock,event_clocks=roots,immediate_assertions=True) if clock else parse(d/'original',top)
            rtl,m=encode_extended(ir) if request.get('extended') else encode_eventful(ir) if clock else encode(ir)
            write(d/'ir.json',ir);write(d/'mapping.json',m);(d/'encoded.sv').write_text(rtl)
            row=dict(top=top,status='ENCODED',nodes=len(ir['nodes']),rtl_bytes=len(rtl.encode()),clock=clock)
        except Exception as e:row=dict(top=top,status='REJECTED' if isinstance(e,ValueError) else 'TOOL_FAILURE',error=str(e),traceback=traceback.format_exc(),clock=clock)
        row.update(seconds=time.monotonic()-start,source_hashes={f.name:digest(f) for f in files});rows.append(row);write(d/'result.json',row)
        print(top,row['status'],row.get('error',''),flush=True)
    write(out/'summary.json',rows)


def main():
    from audit_fourstate_f52_worker import setup
    p=argparse.ArgumentParser();p.add_argument('--request',type=Path,required=True);p.add_argument('--out',type=Path,required=True);a=p.parse_args();r=json.loads(a.request.read_bytes());out=a.out
    os.umask(0o077);setup(Path(r['run']))
    receipt=json.loads((Path(r['run'])/'reports/input-receipt.json').read_bytes())
    for row in receipt['files']:
        f=Path(r['input_run'])/'reports/inputs'/row['name']
        if f.is_symlink() or digest(f)!=row['sha256']:raise ValueError('Frozen input differs')
    source_root=Path(__file__).resolve().parent
    write(out/'machine.json',dict(hostname=os.uname().nodename,python=sys.version,source_hashes={f.relative_to(source_root).as_posix():digest(f) for f in source_root.rglob('*.py') if f.is_file()},inherited_limits={n:resource.getrlimit(getattr(resource,n)) for n in ('RLIMIT_CPU','RLIMIT_AS','RLIMIT_FSIZE')}))
    private_runtime=Path(r['run'])/'reports/icarus-runtime.json'
    if private_runtime.exists():
        runtime_receipt=json.loads(private_runtime.read_bytes());workspace=Path(r['run']).parents[1];iv=workspace/runtime_receipt['base']
        for row in runtime_receipt['files']:
            f=iv/row['path']
            if f.is_symlink() or digest(f)!=row['sha256']:raise ValueError('Private Icarus runtime changed')
        command(out,'iverilog-version',[iv/'bin/iverilog','-B',iv/'lib/ivl','-V'])
    else:iv=runtime(Path(r['run']),out)
    mode=r['mode']
    if mode=='probe':probe(r,out,iv)
    elif mode=='inventory':inventory(r,out)
    elif mode=='tests':
        tests(r,out)
        if r.get('native_print_probe'):
            text='module tb;reg signed [3:0] v;initial begin $display("BEGIN_PROTOCOL");$display("F47_STEP 0");v=4\'b0001;$fwrite(32\'h80000002,"STDERR_STAGE_ZERO\\n");$fflush(32\'h80000002);$display("AFTER_FLUSH");$display("DECIMAL<%d>|BINARY<%b>|HEX<%x>",v,v,v);#1;$display("END_PROTOCOL");$finish;end endmodule\n'
            (out/'StandardPrint.sv').write_text(text)
            command(out,'native-print-compile',[iv/'bin/iverilog','-B',iv/'lib/ivl','-g2012','-s','tb','-o',out/'native-print.vvp',out/'StandardPrint.sv'])
            command(out,'native-print-execute',[iv/'bin/vvp',out/'native-print.vvp'])
    elif mode=='audit':audit(r,out)
    elif mode=='compare':behavior(r,out,iv)
    elif mode=='regressions':
        from verify_fourstate_f54 import legacy_regressions
        legacy_regressions(r,out,iv)
    elif mode=='compat':
        from verify_fourstate_f54 import compat
        compat(r,out)
    elif mode=='regenerate':regenerate(r,out)
    elif mode=='faults':faults(r,out,iv)
    else:raise ValueError('Unimplemented F56 mode')
    write(out/'resources-self.json',dict(worker_peak_rss_bytes=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024,cumulative_child_peak_rss_bytes=resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss*1024))


if __name__=='__main__':main()
