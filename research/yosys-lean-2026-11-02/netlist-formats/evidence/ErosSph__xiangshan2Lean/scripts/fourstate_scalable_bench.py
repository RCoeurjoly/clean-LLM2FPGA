"""Streamed exact raw/native-array comparison; no checksum-only acceptance."""
from pathlib import Path
import gzip,itertools,json,re,subprocess,time
from fourstate_events import ALPHABET


def rails(bits):
    if not isinstance(bits,str) or not bits or set(bits)-set(ALPHABET):raise ValueError('Exact lowercase 01xz bits required')
    return ''.join('1' if c in 'xz' else '0' for c in bits),''.join('1' if c in '1z' else '0' for c in bits)


def decode(bits):
    if len(bits)%2 or set(bits)-set('01'):raise ValueError('Undefined or malformed encoded rails')
    w=len(bits)//2
    tag,value=bits[:w],bits[w:]
    if tag=='0'*w:return value
    if tag=='1'*w:return value.translate(str.maketrans('01','xz'))
    return ''.join(ALPHABET[(ord(t)&1)*2+(ord(v)&1)] for t,v in zip(tag,value))


def make_bench(mapping,trace,symbol,binary):
    inputs=[p for p in mapping['ports'] if p['direction']=='input'];outputs=[p for p in mapping['ports'] if p['direction']=='output']
    if symbol not in ALPHABET or len(symbol)!=1:raise ValueError('Explicit initial fill must be one 01xz symbol')
    if not trace:raise ValueError('Nonempty explicit trace required')
    for row in trace:
        if set(row)!={'inputs'} or set(row['inputs'])!={p['name'] for p in inputs}:raise ValueError('Exact original input set required')
        for p in inputs:
            value=row['inputs'][p['name']]
            if not isinstance(value,str) or len(value)!=p['width'] or set(value)-set(ALPHABET):raise ValueError('Exact-width original input bits required')
    states=mapping['states'];edges=[p for p in mapping['events']['processes'] if p['kind']=='edge'];connections=[]
    lines=['module tb;integer sample,idx,cycles;'];input_names=[]
    for i,p in enumerate(mapping['ports']):
        for key in (('tag','value') if binary else ('name',)):
            name=f'p{i}_{key}';lines.append(f'{"reg" if p["direction"]=="input" else "wire"} [{p["width"]-1}:0] {name};');connections.append(f'.{p[key]}({name})')
            if p['direction']=='input':input_names.append(name)
    width=sum(p['width'] for p in inputs)*(2 if binary else 1)
    lines.append(f'reg [{width-1}:0] vectors[0:{len(trace)-1}];')
    if binary:
        o=mapping['observation'];P=len(mapping['events']['processes']);M=len(mapping['events']['monitors'])
        lines+=['reg exec_clk=0,step_valid=0;wire step_ready,pre_valid,post_valid;wire [3:0] fault;',
            f'wire [{P-1}:0] fired;reg [{o["object_width"]-1}:0] obs_object=0;reg [{o["index_width"]-1}:0] obs_index=0;',
            f'wire [{o["width"]-1}:0] obs_tag,obs_value;wire [{2*M-1}:0] observed_monitors;']
        for n in ('exec_clk','step_valid','step_ready','pre_valid','post_valid','fault','fired','obs_object','obs_index','obs_tag','obs_value','observed_monitors'):connections.append(f'.{n}({n})')
        from fourstate_assertions import FIELDS
        for i,a in enumerate(mapping.get('assertions',[])):
            for k in FIELDS:lines.append(f'wire a{i}_{k};');connections.append(f'.{a[k]}(a{i}_{k})')
    lines.append(f'{mapping["encoded_top"] if binary else mapping["top"]} dut('+','.join(connections)+');')
    for p in edges:
        lines.append(f'integer count{p["id"]}=0;')
        if not binary:
            terms=['%s dut.%s'%('posedge' if t['edge']=='PosEdge' else 'negedge',t.get('original_signal',t['signal']).split('.',1)[1]) for t in p['triggers']]
            lines.append('always @('+ ' or '.join(terms)+f') count{p["id"]}=count{p["id"]}+1;')
    if binary:lines+=['task tick;begin #1;exec_clk=1;#1;exec_clk=0;if(fault!=0)$fatal(1,"controller fault %0d sample %0d",fault,sample);end endtask']
    lines+=['task observe;input integer phase;begin']
    for i,p in enumerate(mapping['ports']):
        if p['direction']!='output':continue
        value=f'p{i}_tag,p{i}_value' if binary else f'p{i}_name'
        lines.append(f'$display("C2L %0d %0d O {i} 0 '+('%b%b' if binary else '%b')+f'",sample,phase,{value});')
    for s in states:
        j=s['object_id'];w=s['width'];m=s.get('memory')
        if m:lines.append(f'for(idx={m["offset"]};idx<{m["offset"]+m["depth"]};idx=idx+1)begin')
        if binary:
            lines.append(f'obs_object={j};obs_index='+('idx-'+str(m['offset']) if m else '0')+';#0;')
            value=f'obs_tag[{w-1}:0],obs_value[{w-1}:0]'
        else:value='dut.'+s['rtl_path']+('[idx]' if m else '')
        lines.append(f'$display("C2L %0d %0d S {j} %0d '+('%b%b' if binary else '%b')+'",sample,phase,'+('idx' if m else '0')+','+value+');')
        if m:lines.append('end')
    for i,n in enumerate(mapping['events']['monitors']):
        value=f'observed_monitors[{2*i+1}],observed_monitors[{2*i}]' if binary else 'dut.'+n.split('.',1)[1]
        lines.append(f'$display("C2L %0d %0d M {i} 0 '+('%b%b' if binary else '%b')+f'",sample,phase,{value});')
    for p in edges:lines.append(f'$display("C2L %0d %0d E {p["id"]} 0 %0d",sample,phase,count{p["id"]});')
    if binary:
        for i,a in enumerate(mapping.get('assertions',[])):
            lines.append(f'if(phase==0)$display("AFLAG %0d {i} %b%b%b%b",sample,'+','.join(f'a{i}_{k}' for k in FIELDS)+');')
    lines+=['end endtask','initial begin','$readmemb("'+('encoded' if binary else 'original')+'-inputs.mem",vectors);',
        '{'+','.join(input_names)+'}=vectors[0];','#2;']
    for s in states:
        w=s['width'];m=s.get('memory');j=s['object_id']
        if m:
            if binary:
                t,v=rails(symbol*w)
                lines.append(f'for(idx=0;idx<{m["physical_depth"]};idx=idx+1)begin dut.{s["bank_tag"]}[idx]=(idx<{m["depth"]})?{w}\'b{t}:0;dut.{s["bank_value"]}[idx]=(idx<{m["depth"]})?{w}\'b{v}:0;end')
            else:lines.append(f'for(idx={m["offset"]};idx<{m["offset"]+m["depth"]};idx=idx+1)dut.{s["rtl_path"]}[idx]={w}\'b{symbol*w};')
        elif binary:
            t,v=rails(symbol*w);lines.append(f'dut.{s["bank_tag"]}={w}\'b{t};dut.{s["bank_value"]}={w}\'b{v};')
        else:lines.append(f'$fourstate_deposit(dut.{s["rtl_path"]},{w}\'b{symbol*w});')
    lines+=['#1;']
    for p in edges:lines.append(f'count{p["id"]}=0;')
    lines+=['for(sample=0;sample<'+str(len(trace))+';sample=sample+1)begin',
        '$display("F47_STEP %0d",sample);','{'+','.join(input_names)+'}=vectors[sample];']
    if binary:
        lines+=['while(!step_ready)tick;step_valid=1;tick;step_valid=0;cycles=0;',
            'while(!pre_valid)begin tick;cycles=cycles+1;if(cycles>16)$fatal(1,"protocol failed to reach pre");end']
        for p in edges:lines.append(f'if(fired[{p["id"]}])count{p["id"]}=count{p["id"]}+1;')
        lines+=['observe(0);tick;while(!post_valid)tick;observe(1);tick;']
    else:lines+=['#0;#0;#0;#0;observe(0);#1;observe(1);']
    lines+=['end','$finish;end endmodule']
    data=[]
    for row in trace:
        chunks=[]
        for p in inputs:chunks.extend(rails(row['inputs'][p['name']]) if binary else [row['inputs'][p['name']]])
        data.append(''.join(chunks))
    return '\n'.join(lines)+'\n','\n'.join(data)+'\n'


def run_stream(folder,label,argv):
    import resource
    folder=Path(folder);d=folder/label;d.mkdir();start=time.monotonic();count=0
    with gzip.open(d/'stdout.log.gz','wb') as output,(d/'stderr.log').open('wb') as error:
        p=subprocess.Popen([str(a) for a in argv],cwd=folder,stdout=subprocess.PIPE,stderr=error)
        while True:
            b=p.stdout.read(1024*1024)
            if not b:break
            output.write(b);count+=len(b)
        code=p.wait()
    result=dict(argv=[str(a) for a in argv],cwd=str(folder),exit_code=code,seconds=time.monotonic()-start,stdout_bytes=count,
        cumulative_child_peak_rss_bytes=resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss*1024)
    (d/'command.json').write_text(json.dumps(result,indent=2))
    return result


def records(path,binary):
    with gzip.open(path,'rt') as f:
        for line in f:
            line=line.lstrip('# ')
            if not line.startswith('C2L '):continue
            parts=line.split()
            if len(parts)!=7:raise ValueError('Malformed observation: '+line[:200])
            sample,phase=int(parts[1]),int(parts[2]);kind=parts[3];obj,index=int(parts[4]),int(parts[5]);value=parts[6].lower()
            if kind=='E':value=int(value)
            elif binary:value=decode(value)
            elif set(value)-set('01xz'):raise ValueError('Invalid original observation')
            yield sample,phase,kind,obj,index,value


def compare_streams(original,encoded,mapping,trace_count):
    count=bits=0;seen=set();symbols={s['object_id']:set() for s in mapping['states']}
    sentinel=object()
    for a,b in itertools.zip_longest(records(original,False),records(encoded,True),fillvalue=sentinel):
        if a is sentinel or b is sentinel or a!=b:raise ValueError('Exact raw/encoded mismatch: '+repr(a)+' != '+repr(b))
        count+=1;seen.add((a[0],a[1]))
        if a[2]!='E':bits+=len(a[-1])
        if a[2]=='S':symbols[a[3]].update(a[-1])
    if seen!={(i,p) for i in range(trace_count) for p in (0,1)}:raise ValueError('Incomplete phase/sample coverage')
    expected=2*trace_count*(sum(p['direction']=='output' for p in mapping['ports'])+
        sum(s['memory']['depth'] if 'memory' in s else 1 for s in mapping['states'])+
        len(mapping['events']['monitors'])+sum(p['kind']=='edge' for p in mapping['events']['processes']))
    if count!=expected:raise ValueError('Incomplete object/word observations')
    if mapping.get('assertions'):
        from fourstate_assertions import reference_events,flags_events
        with gzip.open(original,'rt') as f:diagnostic=''.join(l for l in f if not l.lstrip('# ').startswith('C2L '))
        expected=reference_events(mapping,diagnostic,trace_count);flags={};events={};previous={}
        for sample,phase,kind,obj,index,v in records(encoded,True):
            if phase==0 and kind=='E':
                delta=v-previous.get(obj,0)
                if delta not in (0,1):raise ValueError('Repeated encoded activation')
                if delta:events.setdefault(sample,[]).append(obj)
                previous[obj]=v
        with gzip.open(encoded,'rt') as f:
            for line in f:
                if line.startswith('AFLAG '):
                    _,sample,site,value=line.split();flags.setdefault(int(sample),{})[int(site)]=list(map(int,value))
        for sample in range(trace_count):
            actual=flags_events(mapping,sample,events.get(sample,[]),[flags[sample][i] for i in range(len(mapping['assertions']))])
            if actual!=expected[sample]:raise ValueError('Assertion diagnostic mismatch at '+str(sample))
    return dict(status='RAW_ENCODED_PASS',samples=trace_count,records=count,bit_positions=bits,state_symbols={str(k):''.join(sorted(v)) for k,v in symbols.items()})
