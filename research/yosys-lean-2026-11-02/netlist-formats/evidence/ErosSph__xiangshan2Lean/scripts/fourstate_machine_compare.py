"""Exact streamed large-model observations, activations and diagnostic ABI."""
import gzip
import itertools
import hashlib
from fourstate_scalable_bench import records
from fourstate_eventful_bench import activation_records


def native_prefix(source,target,count):
    """Derive an explicit initial sample prefix from an already sealed reference.

    The full frozen source remains intact. Only original C2L/A2L lines are
    copied; no values, objects or words within the selected samples are cut.
    """
    if type(count) is not int or count<1:raise ValueError('Positive explicit prefix length required')
    rows=0;last=-1;cutoff=False;digest=hashlib.sha256()
    with gzip.open(source,'rt') as src,open(target,'xb') as raw:
        with gzip.GzipFile(filename='',fileobj=raw,mode='wb',mtime=0,compresslevel=1) as dst:
            for line in src:
                line=line.lstrip('# ');p=line.split()
                if not p or p[0] not in ('C2L','A2L'):continue
                if len(p)!=(7 if p[0]=='C2L' else 8):raise ValueError('Malformed native prefix observation')
                sample=int(p[1])
                if sample<last or sample<0:raise ValueError('Unordered native reference samples')
                if sample>=count:cutoff=True;break
                last=sample;data=line.encode('utf-8');dst.write(data);digest.update(data);rows+=1
    if not cutoff or last!=count-1:raise ValueError('Native prefix boundary missing')
    return dict(samples=count,records=rows,logical_sha256=digest.hexdigest(),
                source=str(source),derived=str(target),scope='Explicit prefix only; complete frozen source retained')


def stream_complete(path,mapping,count,*,binary=True,protocol=True):
    """Check an exact streamed observation matrix; this is not equivalence."""
    def matrix():
        for sample in range(count):
            for phase in (0,1):
                for i,p in enumerate(mapping['ports']):
                    if p['direction']=='output':yield sample,phase,'O',i,0,p['width']
                for state in mapping['states']:
                    memory=state.get('memory')
                    for index in range(memory['offset'],memory['offset']+memory['depth']) if memory else (0,):
                        yield sample,phase,'S',state['object_id'],index,state['width']
                for i in range(len(mapping['events']['monitors'])):yield sample,phase,'M',i,0,1
                for process in mapping['events']['processes']:
                    if process['kind']=='edge':yield sample,phase,'E',process['id'],0,0
    expected=matrix();flags=iter((s,a) for s in range(count) for a in range(len(mapping.get('assertions',[]))))
    steps=end=records_count=flag_count=0
    with gzip.open(path,'rt') as stream:
        for line in stream:
            p=line.split()
            if not p:continue
            if p[0]=='F57_ERROR':raise ValueError('Replay reported F57_ERROR: '+line[:300])
            if p[0]=='F47_STEP':
                if len(p)!=2 or int(p[1])!=steps or steps>=count:raise ValueError('Missing/repeated/out-of-order replay step')
                steps+=1
            elif p[0]=='F57_END':
                if len(p)!=2 or int(p[1])!=count or end:raise ValueError('Invalid/repeated replay end marker')
                end+=1
            elif p[0]=='C2L':
                key=next(expected,None)
                if len(p)!=7 or key is None:raise ValueError('Unexpected or excess snapshot record')
                actual=(int(p[1]),int(p[2]),p[3],int(p[4]),int(p[5]))
                if actual!=key[:5]:raise ValueError('Missing/repeated/out-of-order snapshot object: '+repr((actual,key[:5])))
                if end:raise ValueError('Snapshot after end marker')
                if p[3]=='E':
                    if not p[6].isdigit():raise ValueError('Invalid event counter')
                elif len(p[6])!=key[5]*(2 if binary else 1) or set(p[6].lower())-set('01' if binary else '01xz'):
                    raise ValueError('Snapshot rail or width mismatch')
                records_count+=1
            elif p[0]=='AFLAG':
                if len(p)!=4 or (int(p[1]),int(p[2]))!=next(flags,None) or len(p[3])!=4 or set(p[3])-set('01'):
                    raise ValueError('Incomplete/repeated diagnostic flags')
                flag_count+=1
    if next(expected,None) is not None:raise ValueError('Truncated snapshot observation matrix')
    if protocol and (steps!=count or end!=1 or next(flags,None) is not None):raise ValueError('Replay did not complete all steps/diagnostic flags/end marker')
    return dict(status='STREAM_COMPLETE',samples=count,snapshot_records=records_count,diagnostic_flags=flag_count,steps=steps,end_markers=end,behavior='NOT_COMPARED')


def diagnostics(path, mapping):
    rows=[];arguments={};flags={}
    with gzip.open(path,'rt') as stream:
        for line in stream:
            p=line.split()
            if not p:continue
            if p[0]=='F57_DIAG':
                if len(p)!=5:raise ValueError('Diagnostic event arity')
                sample,site,process,ordinal=map(int,p[1:])
                if mapping['assertions'][site]['process']!=process:raise ValueError('Diagnostic owner differs')
                rows.append((sample,site,process,ordinal))
            elif p[0]=='F56_ARG':
                if len(p)!=6:raise ValueError('Diagnostic argument arity')
                key=tuple(map(int,p[1:5]));width=mapping['assertions'][key[1]]['arguments'][key[3]]['width']
                if len(p[5])!=2*width or set(p[5])-set('01') or key in arguments:raise ValueError('Invalid/repeated exact argument')
                arguments[key]=p[5]
            elif p[0]=='AFLAG':
                if len(p)!=4 or len(p[3])!=4 or set(p[3])-set('01'):raise ValueError('Invalid diagnostic flags')
                key=tuple(map(int,p[1:3]))
                if key in flags:raise ValueError('Repeated diagnostic flags')
                flags[key]=p[3]
    if len(set(rows))!=len(rows):raise ValueError('Repeated diagnostic activation')
    expected={(sample,site,ordinal,j) for sample,site,_,ordinal in rows for j in range(len(mapping['assertions'][site]['arguments']))}
    if set(arguments)!=expected:raise ValueError('Incomplete diagnostic argument payloads')
    return dict(events=sorted(rows),arguments=arguments,flags=flags)


def encoded_pair(a,b,mapping,count):
    sentinel=object();n=bits=0;seen=set()
    for x,y in itertools.zip_longest(records(a,True),records(b,True),fillvalue=sentinel):
        if x!=y:raise ValueError('Encoded/Lean exact snapshot mismatch: '+repr((x,y))[:1500])
        n+=1;seen.add(x[:2])
        if x[2]!='E':bits+=len(x[-1])
    expected=2*count*(sum(p['direction']=='output' for p in mapping['ports'])+sum(s['memory']['depth'] if 'memory' in s else 1 for s in mapping['states'])+len(mapping['events']['monitors'])+sum(p['kind']=='edge' for p in mapping['events']['processes']))
    if n!=expected or seen!={(i,p) for i in range(count) for p in (0,1)}:raise ValueError('Incomplete complete-object observation matrix')
    def groups(path):
        for sample,items in itertools.groupby(activation_records(path,True),lambda r:r[0]):
            rows=sorted(items)
            if len({r[:-1] for r in rows})!=len(rows):raise ValueError('Repeated activation state')
            yield sample,rows
    activations=0
    for x,y in itertools.zip_longest(groups(a),groups(b),fillvalue=sentinel):
        if x!=y:raise ValueError('Encoded/Lean activation old-state mismatch')
        activations+=len(x[1])
    da,db=diagnostics(a,mapping),diagnostics(b,mapping)
    if da!=db:raise ValueError('Encoded/Lean exact diagnostic events, parameters or flags differ')
    if len(da['flags'])!=count*len(mapping.get('assertions',[])):raise ValueError('Missing diagnostic flags')
    return dict(status='EXACT_ENCODED_LEAN_PASS',samples=count,snapshot_records=n,bit_positions=bits,activation_records=activations,diagnostic_events=len(da['events']),diagnostic_arguments=len(da['arguments']))
