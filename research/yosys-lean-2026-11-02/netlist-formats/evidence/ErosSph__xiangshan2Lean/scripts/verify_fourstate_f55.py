"""Finite F55 native subsystem probes and real raw/encoded executions on amax."""
from pathlib import Path
import argparse,gzip,hashlib,json,os,re,sys,time,traceback,resource
sys.path.insert(0,str(Path(__file__).resolve().parent/'verification'/'four_state'))
from verify_fourstate_f53 import write,digest,command,runtime
from verify_fourstate_f54 import selected_sources,legacy_regressions


def wrapper(source,target,module,top):
    data=(source/(module+'.sv')).read_text();clean=re.sub(r'//[^\n]*','',data)
    header=re.search(r'module\s+'+re.escape(module)+r'\s*\((.*?)\);',clean,re.S).group(1)
    names=[re.findall(r'[A-Za-z_][A-Za-z0-9_$]*',s)[-1] for s in header.split(',')]
    if 'clock' not in names or 'reset' not in names:raise ValueError('Selected boundary must expose its original clock and reset')
    bindings=','.join('.'+n+'('+('derived_reset' if n=='reset' else n)+')' for n in names)
    text=f'module {top}({header});wire derived_reset;ResetGen reset_generator(.clock(clock),.reset(reset),.o_reset(derived_reset));{module} subsystem({bindings});endmodule\n'
    target.mkdir();selected_sources(source,module,target/'selected');selected_sources(source,'ResetGen',target/'reset-selected')
    for folder in ('selected','reset-selected'):
        for p in (target/folder).glob('*.sv'):(target/p.name).write_bytes(p.read_bytes())
    (target/(top+'.sv')).write_text(text);return target


def trace_for(mapping):
    ports=[p for p in mapping['ports'] if p['direction']=='input'];now={p['name']:'0'*p['width'] for p in ports};now['clock']='1';rows=[dict(inputs=dict(now))]
    def setone(n,v):
        if now[n]!=v:now[n]=v;rows.append(dict(inputs=dict(now)))
    def edge():setone('clock','0');setone('clock','1')
    if mapping['top']=='DualStateGate':
        now['cb']='1';rows[0]['inputs']['cb']='1'
        setone('d','1'*273);setone('clock','0');setone('cb','0');setone('cb','1')
        setone('we','1')
        for c in '01xz':
            setone('d',c*273)
            for root in '01':
                setone('clock',root);setone('cb','0');setone('cb','1')
            edge();setone('a','1');edge();setone('a','0')
        for c in 'xz01':setone('reset',c);edge();setone('cb','0');setone('cb','1')
        for root in ('clock','cb'):
            for a in '01xz':
                for b in '01xz':setone(root,a);setone(root,b)
        return rows
    if 'd' in now:setone('d','1')
    edge()
    # Assert derived reset while the root is both low and high, synchronous
    # release, repeated clock/reset activation in the same ordered time slot.
    for root in '01':
        setone('clock',root);setone('reset','1');setone('reset','0')
        for _ in range(4):edge()
    if 'io_w_req_valid' in now:
        setone('io_w_req_valid','1')
        for bank,c in enumerate('01xz'):
            setone('io_w_req_bits_data_0',c*64)
            setone('io_w_req_bits_setIdx',format(bank,'015b'));edge();edge()
        setone('io_w_req_valid','0');setone('io_r_req_valid','1')
        for address in [0,1,2,3,24575,32767]:setone('io_r_req_bits_setIdx',format(address,'015b'));edge();edge()
        setone('io_r_req_bits_setIdx','x'*15);edge();setone('io_r_req_bits_setIdx','z'*15);edge()
        for c in 'xz':setone('io_r_req_valid',c);edge();setone('io_w_req_valid',c);edge()
        setone('io_r_req_valid','0');setone('io_w_req_valid','0')
    for c in 'xz01':setone('reset',c);edge()
    for a in '01xz':
        for b in '01xz':setone('clock',a);setone('clock',b)
    return rows


def prepared(request,out):
    source=Path(request['input_run'])/'reports/inputs';fixtures=out/'fixtures';fixtures.mkdir()
    extra=dict(ResetBanked=wrapper(source,fixtures/'ResetBanked','SRAMWrapper_32','ResetBanked'))
    texts=dict(
        ResetCascade="module ResetCascade(input clock,reset,d,output reg q,output r1,r2);ResetGen first(.clock(clock),.reset(reset),.o_reset(r1));ResetGen second(.clock(clock),.reset(r1),.o_reset(r2));always @(posedge clock or posedge r2)if(r2)q<=1'b0;else q<=d;endmodule",
        ResetInvert="module ResetInvert(input clock,reset,d,output reg q,output dr);wire r;ResetGen first(.clock(clock),.reset(reset),.o_reset(r));assign dr=~r;always @(posedge clock or posedge dr)if(dr)q<=1'b0;else q<=d;endmodule",
        DualStateGate="module DualStateGate(input clock,cb,reset,we,a,input [272:0] d,output [272:0] q,r);wire rst,g,inv;reg en;ResetGen generator(.clock(cb),.reset(reset),.o_reset(rst));always @(posedge cb or posedge rst)if(rst)en<=1'b0;else en<=d[0];ClockGate gate(.CK(clock),.TE(1'b0),.E(en),.Q(g));assign inv=~cb;ram_2x273 bank(.R0_addr(a),.R0_en(we),.R0_clk(cb),.R0_data(q),.W0_addr(a),.W0_en(we),.W0_clk(g),.W0_data(d));ram_2x273 isolated(.R0_addr(a),.R0_en(we),.R0_clk(clock),.R0_data(r),.W0_addr(a),.W0_en(we),.W0_clk(inv),.W0_data(d));endmodule")
    for top,text in texts.items():
        target=fixtures/top;target.mkdir();(target/(top+'.sv')).write_text(text)
        for name in (('ResetGen','ClockGate','ram_2x273') if top=='DualStateGate' else ('ResetGen',)):
            (target/(name+'.sv')).write_bytes((source/(name+'.sv')).read_bytes())
        extra[top]=target
    return source,extra


def probe(request,out,iv):
    from fourstate_eventful import parse_eventful,encode_eventful
    source,extra=prepared(request,out);rows=[]
    for top in request.get('tops',['ResetGen','SRAMWrapper_32','ResetBanked','SubDirectory_4','Directory_Anon_4']):
        f=out/top;f.mkdir()
        try:
            files=selected_sources(extra.get(top,source),top,f/'original');start=time.monotonic()
            ir=parse_eventful(f/'original',top,'clock',event_clocks=('clock','cb') if top=='DualStateGate' else (),immediate_assertions=True);rtl,m=encode_eventful(ir)
            write(f/'ir.json',ir);write(f/'mapping.json',m);(f/'encoded.sv').write_text(rtl)
            row=dict(top=top,status='GENERATED',nodes=len(ir['nodes']),processes=len(ir['events']['processes']),storage=ir['storage'],events=ir['events'],rtl_bytes=len(rtl.encode()),seconds=time.monotonic()-start,source_hashes={p.name:digest(p) for p in files})
        except Exception as e:row=dict(top=top,status='REJECTED',error=str(e),traceback=traceback.format_exc())
        write(f/'result.json',row);rows.append(row)
    write(out/'summary.json',rows)


def compare_cases(request,out,iv):
    from fourstate_eventful_bench import benches,compare
    source=Path(request['run'])/'reports'/request['probe_label'];rows=[]
    (out/'deposit4.c').write_bytes((Path(__file__).parent.parent/'verification/fourstate/deposit4.c').read_bytes())
    command(out,'vpi-compile',['/usr/bin/gcc','-shared','-fPIC','-O2','-Wall','-Wextra','-Werror','-I/usr/include/iverilog',out/'deposit4.c','-o',out/'deposit4.vpi'])
    from fourstate_scalable_bench import run_stream
    for top in request.get('tops',['ResetGen','SRAMWrapper_32','ResetBanked']):
        src=source/top;f=out/top;f.mkdir();m=json.loads((src/'mapping.json').read_bytes());trace=trace_for(m)
        if request.get('trace_prefix'):trace=trace[:request['trace_prefix']]
        (f/'encoded.sv').write_bytes((src/'encoded.sv').read_bytes());(f/'original').mkdir()
        for p in (src/'original').glob('*.sv'):(f/'original'/p.name).write_bytes(p.read_bytes())
        write(f/'mapping.json',m);write(f/'trace.json',trace);results=[]
        initials=[dict(symbol=c,overrides={},trace=trace) for c in '01xz']
        pipes=[s for s in m['states'] if s['name'].endswith('.pipe_reset')]
        patterns=['100','1x0','1z0'] if top=='ResetInvert' else ['010','0x0','0z0']
        witness=5 if top=='DualStateGate' else 4 if top in ('ResetCascade','ResetInvert') else 3
        for bits in patterns:
            if pipes:
                initials.append(dict(symbol='0',overrides={s['name']:('000' if top=='ResetCascade' and '.second.' in s['name'] else bits) for s in pipes},trace=trace[:witness]))
        write(f/'initial-plan.json',[{k:v for k,v in r.items() if k!='trace'}|dict(trace_points=len(r['trace'])) for r in initials])
        for i,initial in enumerate(initials):
            if request.get('initial_ids') is not None and i not in request['initial_ids']:continue
            symbol=initial['symbol'];current_trace=initial['trace'];d=f/f'initial-{i}';d.mkdir();write(d/'initial.json',dict(symbol=symbol,overrides=initial['overrides'],all_original_objects=[s['name'] for s in m['states']],padding='00'));write(d/'trace.json',current_trace)
            streams={}
            for binary in (True,False):
                role='encoded' if binary else 'original';tb,data=benches(m,current_trace,symbol,binary,initial_overrides=initial['overrides'])
                (d/(role+'-tb.sv')).write_text(tb);(d/(role+'-inputs.mem')).write_text(data)
                sources=[f/'encoded.sv'] if binary else sorted((f/'original').glob('*.sv'))
                cc,_=command(d,role+'-compile',[iv/'bin/iverilog','-B',iv/'lib/ivl','-g2012','-s','tb','-o',d/(role+'.vvp'),*sources,d/(role+'-tb.sv')],required=False)
                if cc['exit_code']:
                    if binary:raise ValueError('Encoded bench compile failed: '+str(d))
                    continue
                rr=run_stream(d,role+'-execute',[iv/'bin/vvp',*(['-M',str(out),'-m','deposit4'] if not binary else []),d/(role+'.vvp')])
                if rr['exit_code']:raise ValueError('Simulation failed: '+str(d)+' '+role)
                streams[role]=d/(role+'-execute/stdout.log.gz')
            try:
                r=compare(streams['original'],streams['encoded'],m,len(current_trace)) if len(streams)==2 else dict(status='REFERENCE_REQUIRED')
            except ValueError as e:r=dict(status='DISAGREEMENT',error=str(e))
            r['initial']=i;write(d/'comparison.json',r);results.append(r)
        rows.append(dict(top=top,samples=sum(len(initials[r['initial']]['trace']) for r in results),initials=results));write(out/('progress-'+top+'.json'),rows)
    write(out/'summary.json',rows)
    if any(r['status']!='RAW_ENCODED_PASS' for row in rows for r in row['initials']):raise ValueError('Unresolved raw/encoded behavior comparison')


def regenerate(request,out):
    from fourstate_eventful import parse_eventful,encode_eventful
    run=Path(request['run']);rows=[]
    for label,top in request.get('models',[(request.get('probe_label','probe-v1'),n) for n in ('ResetGen','SRAMWrapper_32','ResetBanked')]):
        src=run/'reports'/label/top;old=json.loads((src/'ir.json').read_bytes());start=time.monotonic()
        ir=parse_eventful(src/'original',top,'clock',event_clocks=old['events']['roots'],immediate_assertions=True);rtl,m=encode_eventful(ir)
        if rtl!=(src/'encoded.sv').read_text():raise ValueError('Current encoding differs: '+top)
        f=out/top;f.mkdir();write(f/'current.ir.json',ir);write(f/'current.mapping.json',m)
        rows.append(dict(top=top,status='CURRENT_SOURCE_EXACT_ENCODING',encoded_sha256=hashlib.sha256(rtl.encode()).hexdigest(),seconds=time.monotonic()-start))
    write(out/'summary.json',rows)
    from verify_fourstate_f54 import regenerate as old
    f=out/'v7';f.mkdir();old(dict(request,run=str(run.parent/'f54-20261018t071031z-20a88c07')),f)


def faults(request,out,iv):
    from fourstate_eventful_bench import compare,benches
    from fourstate_scalable_bench import run_stream,compare_streams
    run=Path(request['run']);base=run/'reports'/request.get('baseline_label','smoke-v4')/'ResetBanked';m=json.loads((base/'mapping.json').read_bytes())
    source=(base/'encoded.sv').read_text();raw=base/'initial-4/original-execute/stdout.log.gz';baseline=base/'initial-4/encoded-execute/stdout.log.gz'
    compare(raw,baseline,m,3);rows=[]
    def part(pid):
        return re.search(r'^(fired\['+str(pid)+r'\]<=.*?\(phase==1\)\?)(.*?)(:\(phase==3\)\?.*)$',source,re.M)
    zero=part(0);consumer=1;memproc=next(s['process'] for s in m['states'] if 'memory' in s)
    observer=re.search(r'assign obs_value=([^;]+);',source).group(1);tag=re.search(r'assign obs_tag=([^;]+);',source).group(1)
    reset=m['events']['processes'][consumer]['reset'];idx=m['events']['monitors'].index(reset)
    a=f'history[{2*idx} +: 2]';b=f'observed_monitors[{2*idx} +: 2]'
    positive=f'(({a}==0 && {b}!=0) || (({a}==2 || {a}==3) && {b}==1))';negative=f'(({b}==0 && {a}!=0) || (({b}==2 || {b}==3) && {a}==1))'
    mutations=dict(
        missing_reset_wave=re.sub(r'wire more_work=.*?;','wire more_work=bad_latch;',source,count=1),
        wrong_reset_edge=source.replace(positive,negative),
        early_commit=source.replace('phase==4 && fired[','phase==3 && fired['),
        skipped_first_commit=source.replace('phase==4 && fired[1]','phase==4 && fired[1] && wave_id!=0'),
        skipped_second_commit=source.replace('phase==4 && fired[1]','phase==4 && fired[1] && wave_id==0'),
        repeated_trigger=source[:zero.start(2)]+'(fired[0] || '+zero.group(2)+')'+source[zero.end(2):],
        wrong_clock_source=source[:part(memproc).start(2)]+zero.group(2)+source[part(memproc).end(2):],
        xz_confusion=source.replace('assign obs_value='+observer+';','assign obs_value=('+observer+') & ~('+tag+');'))
    for name,rtl in mutations.items():
        if rtl==source:raise ValueError('Fault injection missed: '+name)
        f=out/name;f.mkdir();initial=6 if name=='xz_confusion' else 4;old=base/f'initial-{initial}'
        (f/'encoded.sv').write_text(rtl);(f/'encoded-tb.sv').write_bytes((old/'encoded-tb.sv').read_bytes());(f/'encoded-inputs.mem').write_bytes((old/'encoded-inputs.mem').read_bytes())
        command(f,'compile',[iv/'bin/iverilog','-B',iv/'lib/ivl','-g2012','-s','tb','-o',f/'mutant.vvp',f/'encoded.sv',f/'encoded-tb.sv'])
        rr=run_stream(f,'execute',[iv/'bin/vvp',f/'mutant.vvp'])
        if rr['exit_code']:raise ValueError('Behavioral mutant failed before comparison: '+name)
        original=old/'original-execute/stdout.log.gz';encoded=f/'execute/stdout.log.gz';normal_equal=False
        try:compare_streams(original,encoded,m,3);normal_equal=True
        except ValueError:pass
        try:compare(original,encoded,m,3)
        except ValueError as e:
            rows.append(dict(fault=name,status='DETECTED',reason=str(e),pre_post_observations_equal=normal_equal,mutant_sha256=digest(f/'encoded.sv'),exit_code=rr['exit_code']))
        else:raise ValueError('Fault escaped: '+name)
    # Runtime contract guards are kept separate from successfully executed
    # behavioral mutants. Each one must identify its specific controller code.
    latch=next(s for s in m['states'] if m['events']['processes'][s['process']]['kind']=='latch')
    seed=json.loads((base/'trace.json').read_bytes())[0];guardtrace=[seed,dict(inputs=dict(seed['inputs'],clock='0')),dict(inputs=dict(seed['inputs'],clock='1')),dict(inputs=dict(seed['inputs'],clock='0'))]
    cases=[('input_race',source,[seed,dict(inputs=dict(seed['inputs'],clock='0',reset='1'))],{},1),
        ('illegal_initial',source,[dict(inputs=dict(seed['inputs'],clock='0'))],{latch['name']:'1'},2),
        ('latch_settlement',source.replace('phase==2 && (seeded','phase==7 && (seeded'),guardtrace,{},3),
        ('wave_bound',re.sub(r'wire more_work=.*?;','wire more_work=1;',source,count=1),[seed],{},7)]
    for name,rtl,trace,overrides,code in cases:
        f=out/name;f.mkdir();tb,data=benches(m,trace,'0',True,initial_overrides=overrides);(f/'encoded.sv').write_text(rtl);(f/'encoded-tb.sv').write_text(tb);(f/'encoded-inputs.mem').write_text(data)
        command(f,'compile',[iv/'bin/iverilog','-B',iv/'lib/ivl','-g2012','-s','tb','-o',f/'sim.vvp',f/'encoded.sv',f/'encoded-tb.sv'])
        rr=run_stream(f,'execute',[iv/'bin/vvp',f/'sim.vvp'])
        with gzip.open(f/'execute/stdout.log.gz','rt') as h:text=h.read()
        if not rr['exit_code'] or 'controller fault '+str(code) not in text:raise ValueError('Guard failed: '+name+' '+text[-500:])
        rows.append(dict(fault=name,status='REJECTED',controller_fault=code,exit_code=rr['exit_code']))
    if not next(r for r in rows if r['fault']=='skipped_first_commit')['pre_post_observations_equal']:raise ValueError('Intermediate-only fault witness lost')
    write(out/'summary.json',rows)


def reseed(request,out,iv):
    """Rerun a native reference with an atomic stable seed; encoded run retained.

    No time-zero initialization event is part of the ordered-step contract.
    Check the exact first-sample state as well as every later observation.
    """
    from fourstate_eventful_bench import benches,compare
    from fourstate_scalable_bench import run_stream,records
    from fourstate_eventful import parse_eventful,encode_eventful
    run=Path(request['run']);rows=[]
    for label,top,initial in request['cases']:
        src=run/'reports'/label/top;old=src/f'initial-{initial}';f=out/(top+'-'+str(initial));f.mkdir()
        m=json.loads((src/'mapping.json').read_bytes());trace=json.loads((old/'trace.json').read_bytes());seed=json.loads((old/'initial.json').read_bytes())
        ir=parse_eventful(src/'original',top,'clock',event_clocks=m['events']['roots'],immediate_assertions=True);rtl,current=encode_eventful(ir)
        if rtl!=(src/'encoded.sv').read_text():raise ValueError('Model changed before reference correction')
        binary,data=benches(m,trace,seed['symbol'],True,initial_overrides=seed['overrides'])
        if binary!=(old/'encoded-tb.sv').read_text() or data!=(old/'encoded-inputs.mem').read_text():raise ValueError('Encoded execution/trace changed')
        tb,data=benches(m,trace,seed['symbol'],False,initial_overrides=seed['overrides']);(f/'original-tb.sv').write_text(tb);(f/'original-inputs.mem').write_text(data)
        command(f,'compile',[iv/'bin/iverilog','-B',iv/'lib/ivl','-g2012','-s','tb','-o',f/'original.vvp',*sorted((src/'original').glob('*.sv')),f/'original-tb.sv'])
        rr=run_stream(f,'execute',[iv/'bin/vvp',f/'original.vvp'])
        if rr['exit_code']:raise ValueError('Native stable-seed execution failed')
        count=0
        for sample,phase,kind,obj,index,value in records(f/'execute/stdout.log.gz',False):
            if sample!=0 or phase!=0:break
            if kind=='S':
                s=m['states'][obj];expected=seed['overrides'].get(s['name'],seed['symbol']*s['width'])
                if value!=expected:raise ValueError('Requested explicit initial state not preserved: '+s['name'])
                count+=1
        if count!=sum(s['memory']['depth'] if 'memory' in s else 1 for s in m['states']):raise ValueError('Incomplete seed state check')
        result=compare(f/'execute/stdout.log.gz',old/'encoded-execute/stdout.log.gz',m,len(trace));result.update(top=top,initial=initial,original_case=str(src),checked_initial_objects_words=count,encoded_model_sha256=hashlib.sha256(rtl.encode()).hexdigest(),replacement_of=str(old/'original-execute/stdout.log.gz'),seed_policy='Hold scalars through initialization NBA; restore RAM; release stable exact seed; reset event counters; time-zero excluded')
        write(f/'comparison.json',result);rows.append(result)
    write(out/'summary.json',rows)


def audit(request,out,iv):
    """Original bound connection slice and a native intermediate-NBA witness."""
    from fourstate_eventful import parse_eventful
    run=Path(request['run']);old=run.parent/'f53-20261018t061308z-4f19d009/reports/inventory-v4/ownership.json.gz'
    if digest(old)!='691707a27bed47a301bcdd6b8e123d4313ec7516d7591544ce13f1a8cc40f8b6':raise ValueError('Frozen ownership inventory differs')
    with gzip.open(old,'rt') as f:inv=json.load(f)
    prefix='XSTop.l3cacheOpt.slices_0.dataStorage.bankedData_0';drivers=inv['drivers'];name=prefix+'.reset';chain=[];seen=set()
    while name not in seen:
        seen.add(name);items=drivers.get(name,[])
        if len(items)!=1:raise ValueError('Selected reset boundary has ambiguous drivers')
        edge=items[0];chain.append(dict(signal=name,**edge));expr=edge['expression']
        if expr['form']!='ref':break
        name=expr['name']
    selected=[p for p in inv['processes'] if p['instance']==prefix or p['instance'].startswith(prefix+'.')]
    gates=[dict(process=p['id'],module=p['module'],trigger=t) for p in selected for t in p['triggers'] if t['attribution'].get('form')=='structural_latched_and']
    if len(gates)!=4 or not name.endswith('.reset_sync_resetSync.o_reset'):raise ValueError('Selected real boundary differs')
    write(out/'bound-selection.json',dict(scope='Frozen bound XSTop connection slice; not a whole-XSTop semantic model',inventory_sha256=digest(old),prefix=prefix,module='SRAMWrapper_32',instance_count=sum(i['path']==prefix or i['path'].startswith(prefix+'.') for i in inv['instances']),processes=len(selected),reset_chain=chain,gated_endpoints=gates,
        wrapper='ResetBanked preserves the shared clock and ResetGen output connection at this boundary; only XSTop is the common original parent containing both. All real module definitions remain byte-identical.'))
    f=out/'intermediate-NBA';f.mkdir();source=f/'NBAIntermediate.sv'
    source.write_text("module NBAIntermediate(input clock,d,output reg r,q);always @(posedge clock)begin r<=1'b1;r<=1'b0;end always @(posedge clock or posedge r)if(r)q<=1'b0;else q<=d;endmodule\n")
    try:parse_eventful(source,'NBAIntermediate','clock')
    except ValueError as e:
        if 'intermediate NBA' not in str(e):raise
        rejection=str(e)
    else:raise ValueError('Intermediate reset producer was accepted')
    (f/'tb.sv').write_text('module tb;reg clock=0,d=1;wire r,q;integer events=0;NBAIntermediate dut(.clock(clock),.d(d),.r(r),.q(q));always @(posedge clock or posedge dut.r)events=events+1;initial begin dut.r=0;dut.q=0;#1;events=0;#1;clock=1;#1;$display("NBA_EDGE_WITNESS r=%b q=%b events=%0d",r,q,events);if(events!=2||r!==0)$fatal(1,"missing intermediate NBA edge");$finish;end endmodule\n')
    command(f,'compile',[iv/'bin/iverilog','-B',iv/'lib/ivl','-g2012','-s','tb','-o',f/'probe.vvp',source,f/'tb.sv'])
    _,text=command(f,'execute',[iv/'bin/vvp',f/'probe.vvp'])
    write(out/'standard-witness.json',dict(status='PASS',rejection=rejection,source_sha256=digest(source),observed=[l for l in text.splitlines() if l.startswith('NBA_EDGE_WITNESS')],contract='One effective producer NBA per activation; exclusive if branches use a maximum, sequential NBA statements add. No intermediate NBA reset edges are silently collapsed.'))


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
    iv=runtime(Path(r['run']),out);mode=r['mode']
    if mode=='probe':probe(r,out,iv)
    elif mode=='compare':compare_cases(r,out,iv)
    elif mode=='regressions':legacy_regressions(r,out,iv)
    elif mode=='compat':
        from verify_fourstate_f54 import compat
        compat(r,out)
    elif mode=='regenerate':regenerate(r,out)
    elif mode=='faults':faults(r,out,iv)
    elif mode=='reseed':reseed(r,out,iv)
    elif mode=='audit':audit(r,out,iv)
    elif mode=='tests':
        import unittest
        from verify_fourstate_f54 import tests
        tests(r,out);sys.path.insert(0,str(Path(__file__).parent.parent/'tests'))
        result=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromName('test_fourstate_eventful'))
        write(out/'eventful-tests.json',dict(tests=result.testsRun,errors=len(result.errors),failures=len(result.failures)))
        if not result.wasSuccessful():raise ValueError('Eventful tests failed')
    else:raise ValueError('Unimplemented F55 mode')
    write(out/'resources-self.json',dict(worker_peak_rss_bytes=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024,cumulative_child_peak_rss_bytes=resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss*1024))
if __name__=='__main__':main()
