"""Exact v9 observations plus native-rendered, per-activation diagnostics."""
from pathlib import Path
import collections,gzip,json,re,subprocess,time
from fourstate_eventful_bench import benches as event_benches,compare_activations
from fourstate_scalable_bench import compare_streams
from fourstate_assertions import FIELDS


def benches(mapping,trace,symbol,binary,**options):
    text,data=event_benches(mapping,trace,symbol,binary,**options)
    if binary:
        declarations=[];bindings=[]
        for i,a in enumerate(mapping.get('assertions',[])):
            for j,arg in enumerate(a.get('arguments',[])):
                stem=f'diag{i}_{j}';w=arg['width']
                declarations.append(f'wire [{w-1}:0] {stem}_tag,{stem}_value;')
                for key in ('tag','value'):bindings.append(f'.{arg[key]}({stem}_{key})')
                declarations.append(f'function [{w-1}:0] {stem}_decode;input [{w-1}:0] t,v;integer k;begin for(k=0;k<{w};k=k+1){stem}_decode[k]=t[k]?(v[k]?1\'bz:1\'bx):v[k];end endfunction')
        needle=mapping['encoded_top']+' dut('
        text=text.replace(needle,'\n'.join(declarations)+'\n'+needle,1)
        start=text.index(needle);end=text.index(');',start)
        if bindings:text=text[:end]+','+','.join(bindings)+text[end:]
        for p in mapping['events']['processes']:
            if p['kind']!='edge':continue
            pid=p['id'];code=[]
            for i,a in enumerate(mapping.get('assertions',[])):
                if a['process']!=pid:continue
                values=[]
                payload_records=[]
                for j,arg in enumerate(a.get('arguments',[])):
                    stem=f'diag{i}_{j}';v=f'{stem}_decode({stem}_tag,{stem}_value)'
                    values.append('$signed('+v+')' if arg['signed'] else v)
                    payload_records.append(f'$display("F56_ARG %0d {i} %0d {j} %b%b",sample,count{pid},{stem}_tag,{stem}_value);')
                args=','.join([json.dumps(a['message']),*values])
                condition=f'(a{i}_enable_tag===0 && a{i}_enable_value===1 && a{i}_pass_tag===0 && a{i}_pass_value===0)'
                code.append(f'if{condition}begin '+''.join(payload_records)+f'$display("F56_BEGIN %0d {i}",sample);$write({args});$display("\\nF56_END");end')
            # The hook executes once at each fired wave before its NBA commit.
            needle=f'task activation{pid};begin'
            if needle not in text:raise ValueError('Activation diagnostic hook missing')
            text=text.replace(needle,needle+'\n'+'\n'.join(code)+'\n',1)
    else:
        # stdout/stderr share one descriptor in run_stream. Flush the step marker
        # before changing an input, so native stderr writes keep their sample.
        text=text.replace('$display("F47_STEP %0d",sample);','$display("F47_STEP %0d",sample);$fwrite(32\'h80000002,"F56_STDERR_STEP %0d\\n",sample);$fflush(32\'h80000002);$fflush();')
        text=text.replace('observe(0);#1;observe(1);','$fflush(32\'h80000002);$fflush();observe(0);#1;$fflush(32\'h80000002);$fflush();observe(1);')
    return text,data


def run_stream(folder,label,argv):
    import resource
    folder=Path(folder);d=folder/label;d.mkdir();start=time.monotonic();count=0
    with gzip.open(d/'stdout.log.gz','wb') as output:
        p=subprocess.Popen([str(x) for x in argv],cwd=folder,stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
        while True:
            b=p.stdout.read(1024*1024)
            if not b:break
            output.write(b);count+=len(b)
        code=p.wait()
    result=dict(argv=[str(x) for x in argv],cwd=str(folder),exit_code=code,seconds=time.monotonic()-start,stdout_bytes=count,stderr='Merged into the same observation stream',cumulative_child_peak_rss_bytes=resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss*1024)
    (d/'command.json').write_text(json.dumps(result,indent=2));return result


def encoded_diagnostics(path,mapping):
    result=[]
    with gzip.open(path,'rt') as stream:
        for line in stream:
            if line.startswith('# '):line=line[2:]
            m=re.fullmatch(r'F56_BEGIN (\d+) (\d+)\n',line)
            if not m:continue
            sample,index=map(int,m.groups());payload=[]
            for line in stream:
                if line.startswith('# '):line=line[2:]
                if line=='F56_END\n':break
                payload.append(line)
            else:raise ValueError('Unterminated encoded diagnostic')
            value=''.join(payload)
            if not value.endswith('\n'):raise ValueError('Missing diagnostic framing newline')
            a=mapping['assertions'][index]
            result.append(dict(sample=sample,site=a['id'],process=a['process'],message=value[:-1].rstrip('\n'),severity=a['severity']))
    return result


def encoded_payloads(path,mapping):
    from fourstate_scalable_bench import decode
    rows=[]
    with gzip.open(path,'rt') as f:
        for line in f:
            if line.startswith('# '):line=line[2:]
            if not line.startswith('F56_ARG '):continue
            fields=line.split()
            if len(fields)!=6:raise ValueError('Malformed diagnostic payload record')
            sample,site,ordinal,arg=map(int,fields[1:5]);value=decode(fields[5])
            if len(value)!=mapping['assertions'][site]['arguments'][arg]['width']:raise ValueError('Diagnostic payload width')
            rows.append((sample,site,ordinal,arg,value))
    if len({r[:-1] for r in rows})!=len(rows):raise ValueError('Repeated diagnostic payload record')
    indices={a['id']:i for i,a in enumerate(mapping.get('assertions',[]))}
    if len(indices)!=len(mapping.get('assertions',[])):raise ValueError('Duplicate diagnostic site identity')
    occurrences=collections.Counter((r['sample'],indices[r['site']]) for r in encoded_diagnostics(path,mapping))
    groups=collections.defaultdict(set)
    for sample,site,ordinal,arg,value in rows:groups[(sample,site,ordinal)].add(arg)
    observed=collections.Counter()
    for (sample,site,ordinal),indices in groups.items():
        if indices!=set(range(len(mapping['assertions'][site]['arguments']))):raise ValueError('Incomplete diagnostic arguments')
        observed[(sample,site)]+=1
    for key,n in occurrences.items():
        if mapping['assertions'][key[1]]['arguments'] and observed[key]!=n:raise ValueError('Missing exact hardware diagnostic payload')
    return sorted(rows)


def compare_encodings(a,b,mapping,count,*,same_renderer=False):
    """Both are encoded executions: compare all hardware bits and payload ABI.

    Reference-tool formatting is compared in original vs encoded on that same
    native tool. Cross-tool default decimal field padding is not hardware data.
    """
    import itertools
    from fourstate_scalable_bench import records
    from fourstate_eventful_bench import activation_records
    sentinel=object();n=bits=0;seen=set()
    for x,y in itertools.zip_longest(records(a,True),records(b,True),fillvalue=sentinel):
        if x!=y:raise ValueError('Encoded hardware differs across tools '+repr((x,y))[:1000])
        seen.add(x[:2]);n+=1
        if x[2]!='E':bits+=len(x[-1])
    if seen!={(i,p) for i in range(count) for p in (0,1)}:raise ValueError('Encoded tool bridge missing samples')
    def grouped(path):
        for sample,rows in itertools.groupby(activation_records(path,True),lambda r:r[0]):yield sample,sorted(rows)
    for x,y in itertools.zip_longest(grouped(a),grouped(b),fillvalue=sentinel):
        if x!=y:raise ValueError('Encoded activation states differ across tools')
    left,right=encoded_payloads(a,mapping),encoded_payloads(b,mapping)
    if left!=right:raise ValueError('Encoded diagnostic payload differs across tools')
    def events(path):
        groups=collections.defaultdict(list)
        for r in encoded_diagnostics(path,mapping):groups[(r['sample'],r['process'],r['severity'])].append((r['site'],r['severity'],r['message'] if same_renderer else None))
        return dict(groups)
    if events(a)!=events(b):raise ValueError('Encoded diagnostic emissions differ across tools')
    return dict(status='ENCODED_HARDWARE_AND_PAYLOAD_PASS',records=n,bit_positions=bits,diagnostic_arguments=len(left),diagnostic_bits=sum(len(r[-1]) for r in left),renderer_policy='Full exact original/encoded message comparison on ModelSim; all encoded hardware observations and typed diagnostic arguments compared exactly between Linux Icarus and ModelSim')


def native_diagnostics(path,mapping):
    sites=mapping.get('assertions',[]);result=[];sample=None;stderr_sample=None;stderr_markers=[];stdout_markers=[];initial=0
    def write_matches(text):
        found=[]
        for a in sites:
            if a['severity']!='write':continue
            parts=re.split(r'(%(?:[0-9]+)?[bdhxo%])',a['message'].rstrip('\n'))
            pattern=''.join(re.escape(p) if i%2==0 else '%' if p=='%%' else r'[0-9a-fA-FxXzZ+? \-]+' for i,p in enumerate(parts))
            if re.fullmatch(pattern,text):found.append(a)
        return found
    with gzip.open(path,'rt') as stream:
        lines=iter(stream)
        for raw in lines:
            line=(raw[2:] if raw.startswith('# ') else raw).rstrip('\r\n')
            m=re.fullmatch(r'F47_STEP (\d+)',line)
            if m:sample=int(m[1]);stdout_markers.append(sample);continue
            m=re.fullmatch(r'F56_STDERR_STEP (\d+)',line)
            if m:stderr_sample=int(m[1]);stderr_markers.append(stderr_sample);continue
            if sample is None and stderr_sample is None and re.fullmatch(r'\*\* Warning: Design size of \d+ instances exceeds ModelSim ALTERA recommended capacity\.',line):
                trailer=[]
                for _ in range(2):
                    raw=next(lines);trailer.append((raw[2:] if raw.startswith('# ') else raw).rstrip('\r\n'))
                if trailer!=['This may because you are loading cell libraries which are not recommended with','the ModelSim Altera version. Expect performance to be adversely affected.']:raise ValueError('Unexpected ModelSim capacity warning context')
                continue
            native=re.match(r'^ERROR: (.+):(\d+):\s?(.*)$',line);model=re.match(r'^\*\* Error: (.*)$',line)
            if native or model:
                file=native[1] if native else None;number=int(native[2]) if native else None
                payload=[native[3] if native else model[1]];scope=None
                for raw in lines:
                    s=(raw[2:] if raw.startswith('# ') else raw).rstrip('\r\n')
                    if 'Time:' in s and 'Scope:' in s:
                        if native:
                            m=re.search(r'Scope:\s*(\S+)',s);scope=m[1] if m else None
                        else:
                            m=re.search(r'Scope:\s*(\S+)\s+File:\s*(.*?)\s+Line:\s*(\d+)',s)
                            if m:scope,file,number=m[1],m[2],int(m[3])
                        break
                    if s.startswith(('C2L ','A2L ','F47_STEP ','ERROR:','** Error:')):raise ValueError('Incomplete native diagnostic context')
                    payload.append(s)
                value='\n'.join(payload).rstrip('\n')
                # ModelSim inserts source-line-based names for anonymous blocks.
                # Preserve all actual named scopes, file and exact action range.
                scope='.'.join(p for p in (scope or '').split('.') if not re.fullmatch(r'#ublk#\d+#\d+',p))
                candidates=[a for a in sites if a['severity']=='error' and scope=='tb.dut'+('.'+a['rtl_scope'] if a['rtl_scope'] else '') and number is not None and a['source']['line']<=number<=a.get('action_end_source',a['action_source'])['line'] and file and str(file).replace('\\','/').rsplit('/',1)[-1]==str(a['source']['file']).replace('\\','/').rsplit('/',1)[-1]]
                if len(candidates)!=1:raise ValueError('Unmatched native error site '+repr((file,number,scope,value))[:1200])
                a=candidates[0];active_sample=sample
            elif line.startswith(('C2L ','A2L ','F56_')) or not line or '$finish called at' in line:continue
            elif re.match(r'^(?:Reading |10\.1d\s*$|vsim |Loading |do |onerror |Warning: onerror command for use within macro|run |force -freeze |noforce |quit -code |quit -f\s*$|Break in Module tb |Stopped at |Time:.*Instance: /tb|End time:|Errors: \d+, Warnings: \d+|\*\* Note: \$finish)',line.strip()):
                continue
            else:
                candidates=write_matches(line)
                if len(candidates)!=1:raise ValueError('Unmatched/ambiguous native stderr diagnostic '+repr(line))
                a=candidates[0];value=line;active_sample=stderr_sample
            if active_sample is None:initial+=1;continue
            result.append(dict(sample=active_sample,site=a['id'],process=a['process'],message=value,severity=a['severity']))
    if any(a['severity']=='write' for a in sites) and (not stderr_markers or stderr_markers!=stdout_markers):raise ValueError('Exact matching stdout/stderr step markers required for independent stream buffering')
    return result,initial


def compare(original,encoded,mapping,count):
    # The v9 diagnostic protocol permits repeated process activations. Preserve
    # the old field comparator but use the explicit per-activation ABI here.
    result=compare_streams(original,encoded,dict(mapping,assertions=[]),count)
    result['activation_states']=compare_activations(original,encoded)
    native,initial=native_diagnostics(original,mapping);expected=encoded_diagnostics(encoded,mapping)
    def normalize(rows):
        grouped=collections.defaultdict(list)
        for r in rows:grouped[(r['sample'],r['process'],r['severity'])].append(r)
        return dict(grouped)
    a,b=normalize(native),normalize(expected)
    if a!=b:
        key=next(k for k in sorted(set(a)|set(b)) if a.get(k)!=b.get(k))
        raise ValueError('Exact diagnostic mismatch at '+str(key)+': '+repr((a.get(key),b.get(key)))[:2000])
    result['diagnostics']=dict(status='NATIVE_DIAGNOSTICS_PASS',events=len(native),initialization_events_excluded=initial,process_order='Lexical order preserved per process/sample/output stream; repeated activations retained. Independent stdout-console/stderr streams have independent sample markers and no assumed merged-buffer order.',message_normalization='Native context separated; only trailing message newlines normalized, as in the existing assertion ABI')
    return result


def remainder_oracle(path,trace,symbol,binary):
    """Literal signed/unsigned remainder oracle independent of IR/operator code."""
    from fourstate_scalable_bench import records
    def expected(a,b,signed):
        if set(a+b)-set('01'):return 'x'*5
        x,y=int(a,2),int(b,2)
        if signed:
            if x&16:x-=32
            if y&16:y-=32
        if y==0:return 'x'*5
        v=abs(x)%abs(y)
        if x<0:v=-v
        return format(v&31,'05b')
    old=symbol*5;table={};previous=trace[0]['inputs']['clock']
    for i,row in enumerate(trace):
        p=row['inputs'];s=expected(p['a'],p['b'],True);u=expected(p['a'],p['b'],False)
        after=s if i and previous=='0' and p['clock']=='1' else old
        table[i]=(s,u,old,after);old=after;previous=p['clock']
    count=0
    for sample,phase,kind,obj,index,value in records(path,binary):
        a,b,pre,post=table[sample];target=a if kind=='O' and obj==3 else b if kind=='O' and obj==4 else (pre if phase==0 else post) if kind=='S' else None
        if target is not None:
            if value!=target:raise ValueError('Independent literal remainder oracle '+repr((sample,phase,kind,obj,value,target)))
            count+=1
    return dict(status='LITERAL_REMAINDER_PASS',observations=count,includes='All 32x32 signed and unsigned known input pairs, divide by zero, X/Z and old-state NBA')
