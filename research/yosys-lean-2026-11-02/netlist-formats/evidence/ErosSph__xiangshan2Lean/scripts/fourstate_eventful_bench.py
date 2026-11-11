"""v8 benches: pre/post NBA plus every activation's complete owned old state."""
import gzip,itertools,re
from fourstate_scalable_bench import make_bench,decode,rails,compare_streams


def benches(mapping,trace,symbol,binary,*,initial_overrides=None):
    text,data=make_bench(mapping,trace,symbol,binary)
    edges=[p for p in mapping['events']['processes'] if p['kind']=='edge'];states=mapping['states'];tasks=[]
    for p in edges:
        pid=p['id'];lines=[f'task activation{pid};begin']
        for s in states:
            if s['process']!=pid:continue
            obj=s['object_id'];w=s['width'];m=s.get('memory')
            if m:lines.append(f'for(idx={m["offset"]};idx<{m["offset"]+m["depth"]};idx=idx+1)begin')
            if binary:
                lines.append(f'obs_object={obj};obs_index='+('idx-'+str(m['offset']) if m else '0')+';#0;')
                value=f'obs_tag[{w-1}:0],obs_value[{w-1}:0]'
            else:value='dut.'+s['rtl_path']+('[idx]' if m else '')
            lines.append(f'$display("A2L %0d {pid} %0d S {obj} %0d '+('%b%b' if binary else '%b')+f'",sample,count{pid},'+('idx' if m else '0')+','+value+');')
            if m:lines.append('end')
        tasks+=lines+['end endtask']
    text=text.replace('task observe;','\n'.join(tasks)+'\ntask observe;',1)
    if binary:
        width=mapping['protocol']['wave_id_width']
        text=text.replace('reg exec_clk=0,step_valid=0;',f'wire wave_valid;wire [{width-1}:0] wave_id;reg exec_clk=0,step_valid=0;',1)
        text=text.replace('.exec_clk(exec_clk)', '.wave_valid(wave_valid),.wave_id(wave_id),.exec_clk(exec_clk)',1)
        for p in edges:
            pid=p['id'];old=f'if(fired[{pid}])count{pid}=count{pid}+1;';new=f'if(fired[{pid}])begin count{pid}=count{pid}+1;activation{pid};end'
            if old not in text:raise ValueError('Unknown existing bench activation shape')
            text=text.replace(old,new)
        increments='\n'.join(f'if(fired[{p["id"]}])begin count{p["id"]}=count{p["id"]}+1;activation{p["id"]};end' for p in edges)
        text=text.replace('observe(0);tick;while(!post_valid)tick;observe(1);tick;',f'observe(0);tick;while(!post_valid)begin if(wave_valid)begin {increments} end tick;end observe(1);tick;')
        text=text.replace('if(cycles>16)',f'if(cycles>{8*mapping["protocol"]["max_waves"]})')
    else:
        text=text.replace('module tb;','module tb;reg observe_events=0;',1)
        for p in edges:
            pid=p['id'];old=f'count{pid}=count{pid}+1;';text=text.replace(old,f'begin count{pid}=count{pid}+1;if(observe_events)activation{pid};end',1)
        text=text.replace('for(sample=0;','observe_events=1;for(sample=0;',1)
    byname={s['name']:s for s in states}
    for name,bits in (initial_overrides or {}).items():
        s=byname[name]
        if 'memory' in s or len(bits)!=s['width']:raise ValueError('Exact-width scalar override required')
        t,v=rails(bits);w=s['width']
        if binary:
            for bank,value,old in [('bank_tag',t,rails(symbol*w)[0]),('bank_value',v,rails(symbol*w)[1])]:
                before=f'dut.{s[bank]}={w}\'b{old};';after=f'dut.{s[bank]}={w}\'b{value};'
                if before not in text:raise ValueError('Missing scalar initialization')
                text=text.replace(before,after,1)
        else:
            before=f'$fourstate_deposit(dut.{s["rtl_path"]},{w}\'b{symbol*w});';after=f'$fourstate_deposit(dut.{s["rtl_path"]},{w}\'b{bits});'
            if before not in text:raise ValueError('Missing native initialization')
            text=text.replace(before,after,1)
    if not binary:
        # Initial deposits into a reset producer can queue downstream NBA that
        # overwrite later deposits. Hold all scalars while this initialization
        # activity drains, restore RAM, then release a stable explicit seed.
        # Variables retain the forced value after release until their next RTL
        # assignment (IEEE procedural force/release). No original RTL is edited.
        release=[];restore=[]
        for s in states:
            m=s.get('memory');w=s['width']
            if m:
                restore.append(f'for(idx={m["offset"]};idx<{m["offset"]+m["depth"]};idx=idx+1)dut.{s["rtl_path"]}[idx]={w}\'b{symbol*w};')
            else:
                bits=(initial_overrides or {}).get(s['name'],symbol*w)
                before=f'$fourstate_deposit(dut.{s["rtl_path"]},{w}\'b{bits});'
                if before not in text:raise ValueError('Native explicit-state seed missing')
                text=text.replace(before,f'force dut.{s["rtl_path"]}={w}\'b{bits};',1)
                release.append('release dut.'+s['rtl_path']+';')
        anchor='#1;\n'
        if anchor not in text:raise ValueError('Native seed drain point missing')
        text=text.replace(anchor,'#1;\n'+'\n'.join(restore+release)+'\n#0;\n',1)
    return text,data


def activation_records(path,binary):
    with gzip.open(path,'rt') as f:
        for line in f:
            line=line.lstrip('# ')
            if not line.startswith('A2L '):continue
            p=line.split()
            if len(p)!=8:raise ValueError('Malformed activation observation')
            a,proc,ordinal,obj,index=(int(p[i]) for i in (1,2,3,5,6));bits=decode(p[7]) if binary else p[7].lower()
            if set(bits)-set('01xz'):raise ValueError('Invalid activation bits')
            yield a,proc,ordinal,obj,index,bits


def compare_activations(original,encoded):
    count=bits=0;sentinel=object()
    def groups(path,binary):
        for sample,records in itertools.groupby(activation_records(path,binary),lambda r:r[0]):
            rows=sorted(records);keys=[r[:-1] for r in rows]
            if len(keys)!=len(set(keys)):raise ValueError('Duplicate activation record')
            yield sample,rows
    for a,b in itertools.zip_longest(groups(original,False),groups(encoded,True),fillvalue=sentinel):
        if a is sentinel or b is sentinel or a!=b:
            if a is sentinel or b is sentinel:raise ValueError('Missing activation observation group')
            difference=next(((x,y) for x,y in itertools.zip_longest(a[1],b[1]) if x!=y),None)
            raise ValueError('Activation old-state mismatch: '+repr(difference)[:1000])
        count+=len(a[1]);bits+=sum(len(r[-1]) for r in a[1])
    return dict(status='ACTIVATION_STATE_PASS',records=count,bit_positions=bits)


def compare(original,encoded,mapping,count):
    result=compare_streams(original,encoded,mapping,count)
    result['activation_states']=compare_activations(original,encoded)
    return result
