"""Bounded immediate-assert diagnostic ABI; no general assertion scheduler.

Success is silent. An active failed assert emits its literal $error diagnostic.
Ordering is lexical within one process activation; independent processes are
normalized by process/ordinal, without claiming a cross-process active order.
"""
import json
import re

FIELDS=('enable_tag','enable_value','pass_tag','pass_value')


def event(site,sample):
    return dict(site=site['id'],sample=sample,process=site['process'],ordinal=site['ordinal'],
                phase='active_before_nba',status='fail',severity='error',message=site['message'])


def normalize(events):
    return sorted(events,key=lambda e:(e['sample'],e['process'],e['ordinal']))


def validate_mapping(mapping):
    sites=mapping.get('assertions')
    if not isinstance(sites,list) or not sites or len({a['id'] for a in sites})!=len(sites):
        raise ValueError('Invalid immediate assertion site inventory')
    used=set();ordinals={}
    for a in sites:
        p=a['process']
        if (type(p) is not int or not 0<=p<len(mapping['events']['processes']) or
                mapping['events']['processes'][p]['kind']!='edge' or
                a['ordinal']!=ordinals.get(p,0) or a['severity']!='error' or
                not isinstance(a['message'],str) or '%' in a['message']):
            raise ValueError('Invalid immediate assertion contract')
        ordinals[p]=ordinals.get(p,0)+1
        for key in FIELDS:
            if not isinstance(a.get(key),str) or a[key] in used:raise ValueError('Invalid assertion rail inventory')
            used.add(a[key])


def flags_events(mapping,sample,fired,flags):
    result=[]
    if len(flags)!=len(mapping['assertions']):raise ValueError('Incomplete assertion flags')
    for a,f in zip(mapping['assertions'],flags):
        if len(f)!=4 or any(type(x) is not int or x not in (0,1) for x in f) or f[0] or f[2]:
            raise ValueError('Assertion control flags must be fully known Boolean rails')
        if a['process'] in fired and f[1] and not f[3]:result.append(event(a,sample))
    return normalize(result)


def adapt_runner(source,ir,mapping):
    """Only append private observation code; hardware backend remains pure."""
    from lean_records import access_paths
    paths=access_paths((p['field'] for p in ir['outputs']),ir.get('lean_record_group_size',128))
    names={p['name']:'o.'+paths[p['field']]+'.toNat' for p in ir['outputs']}
    lines=['def assertionEvents (o : Outputs) (ids : Array Nat) (sample : Nat) : Except String Lean.Json := do',
           '  let mut events : Array Lean.Json := #[]']
    for a in mapping['assertions']:
        et,ev,pt,pv=[names[a[k]] for k in FIELDS]
        values=[('site',json.dumps(a['id'],ensure_ascii=False)),('sample','sample'),('process',str(a['process'])+' : Nat'),
                ('ordinal',str(a['ordinal'])+' : Nat'),('phase','"active_before_nba"'),('status','"fail"'),('severity','"error"'),('message',json.dumps(a['message'],ensure_ascii=False))]
        fields=', '.join('('+json.dumps(k)+', Lean.toJson ('+v+'))' for k,v in values)
        lines += [f'  if {et} != 0 || {pt} != 0 then throw "Unknown assertion flags"',
                  f'  if ids.contains {a["process"]} && {ev} == 1 && {pv} == 0 then',
                  '    events := events.push (Lean.Json.mkObj ['+fields+'])']
    lines+=['  return Lean.toJson events','']
    marker='def eventStep ('
    assert source.count(marker)==1
    source=source.replace(marker,'\n'.join(lines)+'\n'+marker,1)
    source=source.replace('    let pre := observed before regs','    let pre := observed before regs\n    let diagnostics ← unwrap (assertionEvents before ids sample)',1)
    source=source.replace('("pre", pre), ("post", post)])','("pre", pre), ("post", post), ("assertions", diagnostics)])',1)
    return source


def reference_events(mapping,stdout,trace_count):
    """Parse native Icarus/ModelSim $error records, never recompute conditions.

    Markers before each input step bind diagnostics to a sampled event. Source
    scope, line and static payload identify the actual site. Unknown errors,
    missing locations and ambiguous sites are rejected, including pre-seed errors.
    """
    lines=[l[2:] if l.startswith('# ') else l for l in stdout.splitlines()]
    sample=None;out=[[] for _ in range(trace_count)];i=0;markers=[];seen_pre=False;order={}
    while i<len(lines):
        line=lines[i]
        m=re.fullmatch(r'F47_STEP (\d+)',line)
        if m:sample=int(m[1]);markers.append(sample);seen_pre=False;order={};i+=1;continue
        if re.match(r'^\d+ pre ',line):seen_pre=True
        native=re.match(r'^ERROR: (.+):(\d+):\s?(.*)$',line)
        model=re.match(r'^\*\* Error: (.*)$',line)
        if native or model:
            if sample is None or not 0<=sample<trace_count:raise ValueError('Assertion diagnostic outside seeded trace')
            if seen_pre:raise ValueError('Native assertion occurred after pre-NBA observation')
            start=i;message=(native.group(3) if native else model.group(1));file=native.group(1) if native else None
            number=int(native.group(2)) if native else None;scope=None;i+=1
            payload=[message]
            while i<len(lines):
                text=lines[i]
                if 'Time:' in text and 'Scope:' in text:
                    if native:
                        match=re.search(r'Scope:\s*(\S+)',text);scope=match.group(1) if match else None
                    else:
                        match=re.search(r'Scope:\s*(\S+)\s+File:\s*(.*?)\s+Line:\s*(\d+)',text)
                        if match:scope,file,number=match.group(1),match.group(2),int(match.group(3))
                    i+=1;break
                if re.match(r'^(?:F47_STEP|ERROR:|\*\* Error:|\d+ (?:pre|post))',text):break
                payload.append(text);i+=1
            payload='\n'.join(payload).rstrip()
            matches=[]
            for a in mapping['assertions']:
                expected='tb.dut'+('.'+a['rtl_scope'] if a['rtl_scope'] else '')
                low,high=a['source']['line'],a['action_source']['line']
                if scope==expected and number is not None and low<=number<=high and payload==a['message'].rstrip():matches.append(a)
            if not file or len(matches)!=1:raise ValueError('Unmatched/ambiguous native diagnostic: '+repr(lines[start:i]))
            a=matches[0]
            if order.get(a['process'],-1)>=a['ordinal']:
                raise ValueError('Repeated/out-of-order native assertion diagnostic')
            order[a['process']]=a['ordinal']
            out[sample].append(event(a,sample));continue
        if re.search(r'^(?:FATAL:|WARNING:|\*\* (?:Fatal|Failure|Error):)',line):raise ValueError('Unexpected simulator diagnostic: '+line)
        i+=1
    if markers!=list(range(trace_count)):raise ValueError('Missing/reordered assertion step markers')
    return [normalize(events) for events in out]
