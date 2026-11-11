"""Private v9 VPI replay configuration; all observations stay exact and streamed."""
from pathlib import Path
import hashlib
import io
import json
import gzip
import os
import stat
from fourstate_scalable_bench import rails
from fourstate_assertions import FIELDS


def config(mapping,trace,path):
    path=Path(path)
    inputs=[p for p in mapping['ports'] if p['direction']=='input']
    outputs=[(i,p) for i,p in enumerate(mapping['ports']) if p['direction']=='output']
    states=mapping['states'];processes=mapping['events']['processes'];diagnostics=mapping.get('assertions',[])
    def name(value):
        if not isinstance(value,str) or not value or len(value)>1023 or any(c.isspace() for c in value):raise ValueError('Replay identifier outside exact frozen ABI')
        return value
    with path.open('x',encoding='ascii') as f:
        f.write(f'F57VPI1 {name(mapping["encoded_top"])} {len(trace)} {len(inputs)} {len(outputs)} {len(states)} {len(processes)} {len(mapping["events"]["monitors"])} {len(diagnostics)} {mapping["events"]["max_waves"]}\n')
        for p in inputs:f.write(f'{p["width"]} {name(p["tag"])} {name(p["value"])}\n')
        for i,p in outputs:f.write(f'{i} {p["width"]} {name(p["tag"])} {name(p["value"])}\n')
        for s in states:
            m=s.get('memory',{})
            if not 0<=s['process']<len(processes):raise ValueError('Invalid owner in observation ABI')
            f.write(f'{s["object_id"]} {s["process"]} {s["width"]} {m.get("depth",0)} {m.get("offset",0)} {m.get("physical_depth",0)} {name(s["bank_tag"])} {name(s["bank_value"])}\n')
        for p in processes:f.write(str(int(p['kind']=='edge'))+'\n')
        for a in diagnostics:
            f.write(f'{a["process"]} {len(a.get("arguments",[]))} '+' '.join(name(a[k]) for k in FIELDS)+'\n')
            for arg in a.get('arguments',[]):f.write(f'{arg["width"]} {name(arg["tag"])} {name(arg["value"])}\n')
    with Path(str(path)+'.frames').open('x',encoding='ascii') as f:
        previous=None
        for row in trace:
            if set(row['inputs'])!={p['name'] for p in inputs}:raise ValueError('Exact frozen input set required')
            if previous is not None and sum(row['inputs'][p['name']]!=previous[p['name']] for p in inputs)>1:raise ValueError('Original event contract violated')
            chunks=[]
            for p in inputs:
                value=row['inputs'][p['name']]
                if len(value)!=p['width']:raise ValueError('Input width mismatch')
                chunks.extend(rails(value))
            f.write(''.join(chunks)+'\n');previous=row['inputs']
    return dict(config_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),frames_sha256=hashlib.sha256(Path(str(path)+'.frames').read_bytes()).hexdigest(),samples=len(trace),scope='All outputs, all original scalar states, every logical RAM word, edge events, monitors and per-activation old state; no indexed observation-port switching')


def lowered_json(model, manifest, selection=None):
    """Read the original public artifact bytes, including private gzip storage.

    Check the public manifest hash before interpreting the artifact. Chunked
    decoding avoids retaining a second complete uncompressed byte buffer.
    """
    model=Path(model).absolute()
    expected=manifest['artifact_sha256']['lowered.json']
    if not isinstance(expected,str) or len(expected)!=64 or set(expected)-set('0123456789abcdef'):
        raise ValueError('Invalid lowered artifact SHA256')
    for parent in reversed([model,*model.parents]):
        info=parent.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info,'st_file_attributes',0)&0x400 or not stat.S_ISDIR(info.st_mode):
            raise ValueError('Unsafe model artifact directory')
    source=model/'lowered.json'
    try:source.lstat();compressed=False
    except FileNotFoundError:source=model/'lowered.json.gz';compressed=True
    before=source.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_nlink!=1 or before.st_dev!=model.stat().st_dev:
        raise ValueError('Single-link regular lowered artifact required')
    fields=('st_dev','st_ino','st_size','st_mtime_ns','st_ctime_ns')
    def identity(info):return tuple(getattr(info,k) for k in fields)
    value=hashlib.sha256();count=0
    descriptor=os.open(source,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0))
    with os.fdopen(descriptor,'rb') as raw:
        if identity(os.fstat(raw.fileno()))!=identity(before):raise ValueError('Lowered artifact changed before read')
        stream=gzip.GzipFile(fileobj=raw,mode='rb') if compressed else raw
        try:
            for block in iter(lambda:stream.read(1024*1024),b''):
                value.update(block);count+=len(block)
        finally:
            if compressed:stream.close()
        if identity(os.fstat(raw.fileno()))!=identity(before):raise ValueError('Lowered artifact changed during read')
        if value.hexdigest()!=expected:raise ValueError('Lowered artifact differs from public manifest SHA256')
        if selection is None:
            raw.seek(0)
            stream=gzip.GzipFile(fileobj=raw,mode='rb') if compressed else raw
            text=io.TextIOWrapper(stream,encoding='utf-8')
            try:data=json.load(text)
            finally:
                text.detach()
                if compressed:stream.close()
        else:
            # Preserve precisely the fields lean_plan already uses. Scan both
            # sections completely, including ignored values and trailing JSON.
            # Hash verification above precedes any interpretation in either mode.
            top=selection['top'];module={'cells':{},'netnames':{}}
            for section in ('cells','netnames'):
                raw.seek(0)
                stream=gzip.GzipFile(fileobj=raw,mode='rb') if compressed else raw
                try:
                    for name,entry in selection['reader'].kvitems(stream,'modules.'+top+'.'+section):
                        if name in selection[section] and (section!='cells' or entry['type']=='$ff'):
                            module[section][name]=entry
                finally:
                    if compressed:stream.close()
            data={'modules':{top:module}}
        if identity(os.fstat(raw.fileno()))!=identity(before):raise ValueError('Lowered artifact changed during parse')
    if identity(source.lstat())!=identity(before):raise ValueError('Lowered artifact path changed')
    return data,dict(path=str(source),compressed=compressed,logical_bytes=count,sha256=value.hexdigest(),verified_against='public manifest artifact_sha256.lowered.json')


def lean_plan(model,mapping,trace,folder):
    """Map physical banks to official clk2fflogic sampled-Q state by exact bits.

    Every matched FF captures the exact complete bank vector. No name heuristic
    or guessed state initialisation is used. Memory fields retain native sorts.
    """
    folder=Path(folder);folder.mkdir()
    manifest=json.loads((model/'manifest.json').read_bytes())
    interface=model/(manifest.get('interface') or manifest['ir'])
    interface_bytes=interface.read_bytes()
    ir=json.loads(interface_bytes)
    from fourstate_unused_inputs import prove_unused
    unused_inputs,unused_proof=prove_unused(model,manifest,ir,hashlib.sha256(interface_bytes).hexdigest())
    del interface_bytes
    state_indices={s['name']:i for i,s in enumerate(ir['states'])}
    output_indices={s['name']:i for i,s in enumerate(ir['outputs'])}
    input_names={s['name'] for s in ir['inputs']}
    from fourstate_json_stream import build
    stream_reader,_=build(folder)
    selection=dict(reader=stream_reader,top=mapping['encoded_top'],cells=set(state_indices),
                   netnames={s[k] for s in mapping['states'] if 'memory' not in s for k in ('bank_tag','bank_value')})
    lowered,artifact=lowered_json(model,manifest,selection)
    artifact.update(read_mode='streamed-selected-replay-fields',selection_counts={k:len(lowered['modules'][mapping['encoded_top']][k]) for k in ('cells','netnames')})
    module=lowered['modules'][mapping['encoded_top']]
    original_inputs=[p for p in mapping['ports'] if p['direction']=='input']
    expected_inputs={p[k] for p in original_inputs for k in ('tag','value')}|{'exec_clk','step_valid','obs_object','obs_index'}
    if expected_inputs & unused_inputs or input_names!=expected_inputs|unused_inputs or ir['clock']!='exec_clk':raise ValueError('Replay input ABI differs')
    captured={}
    for name,cell in module['cells'].items():
        if cell['type']=='$ff' and name in state_indices:captured.setdefault(tuple(cell['connections']['D']),[]).append(state_indices[name])
    objects=[];aliases=[];banks={};witnesses=[]
    for s in mapping['states']:
        indices=[]
        for key in ('bank_tag','bank_value'):
            name=s[key]
            if 'memory' in s:
                candidates=[state_indices[name]]
                sort=ir['states'][candidates[0]]['sort']
                if sort['kind']!='memory' or sort['width']!=s['width'] or 1<<sort['address_width']!=s['memory']['physical_depth']:raise ValueError('Native memory bank sort mismatch')
            else:
                candidates=captured.get(tuple(module['netnames'][name]['bits']),[])
                if not candidates:raise ValueError('No exact sampled-Q state for '+name)
                for i in candidates:
                    if ir['states'][i]['sort']!={'kind':'bits','width':s['width']}:raise ValueError('Sampled-Q sort mismatch')
                if len(candidates)>1:aliases.append(candidates)
            for i in candidates:
                if i in banks:raise ValueError('Distinct physical bank states overlap')
                banks[i]=(s,key)
            indices.append(candidates[0]);witnesses.append(dict(object=s['object_id'],bank=name,fields=candidates,names=[ir['states'][i]['name'] for i in candidates],rule='native memory field' if 'memory' in s else 'official $ff D vector equals every bit of the physical bank'))
        mem=s.get('memory',{})
        objects.append([s['object_id'],s['process'],s['width'],mem.get('depth',0),mem.get('offset',0),*indices])
    plan=dict(objects=objects,outputs=[[i,p['width'],output_indices[p['tag']],output_indices[p['value']]] for i,p in enumerate(mapping['ports']) if p['direction']=='output'],processes=len(mapping['events']['processes']),edges=[p['id'] for p in mapping['events']['processes'] if p['kind']=='edge'],monitors=len(mapping['events']['monitors']),waves=mapping['events']['max_waves'],aliases=aliases,protocol={n:output_indices[n] for n in ('step_ready','pre_valid','post_valid','fault','fired','observed_monitors','wave_valid')},clock=next(i for i,p in enumerate(ir['inputs']) if p['name']=='exec_clk'),diagnostics=[dict(process=a['process'],flags=[output_indices[a[k]] for k in FIELDS],arguments=[[v['width'],output_indices[v['tag']],output_indices[v['value']]] for v in a.get('arguments',[])]) for a in mapping.get('assertions',[])])
    (folder/'plan.json').write_text(json.dumps(plan,separators=(',',':')))
    (folder/'bank-witnesses.json').write_text(json.dumps(witnesses,separators=(',',':')))
    for code,fill in enumerate('01xz'):
        initial={}
        for i,s in enumerate(ir['states']):
            known=s['initial'];bank=banks.get(i)
            if s['sort']['kind']=='memory':
                values=[w['value'] for w in known['words']]
                if bank:
                    obj,key=bank;logical=obj['memory']['depth'];bit=int(code>=2) if key=='bank_tag' else code&1
                    values=[((1<<obj['width'])-1 if bit else 0) if j<logical else 0 for j in range(len(values))]
                if any((v&w['mask'])!=w['value'] for v,w in zip(values,known['words'])):raise ValueError('Memory seed violates known constraints')
                initial[s['name']]=values
            else:
                value=known['value']
                if bank:
                    obj,key=bank;bit=int(code>=2) if key=='bank_tag' else code&1
                    value=(1<<obj['width'])-1 if bit else 0
                if value&known['mask']!=known['value']:raise ValueError('Scalar seed violates known constraints')
                initial[s['name']]=value
        frames=[]
        for row in trace:
            data={n:0 for n in input_names}
            for p in original_inputs:
                t,v=rails(row['inputs'][p['name']]);data[p['tag']]=int(t,2);data[p['value']]=int(v,2)
            frames.append(dict(inputs=data))
        with (folder/('request-'+fill+'.json')).open('x') as f:json.dump(dict(initial=initial,trace=frames),f,separators=(',',':'))
    return dict(interface=str(interface),model_manifest_sha256=hashlib.sha256((model/'manifest.json').read_bytes()).hexdigest(),unused_input_proof=unused_proof,lowered_artifact=artifact,objects=len(objects),native_memories=sum(s['sort']['kind']=='memory' for s in ir['states']),bank_fields=len(banks),samples=len(trace)*4,binary=str(model/manifest['table']['binary']),plan_sha256=hashlib.sha256((folder/'plan.json').read_bytes()).hexdigest(),witness_sha256=hashlib.sha256((folder/'bank-witnesses.json').read_bytes()).hexdigest())


def normalize_numeric(source,target):
    def pair(width,tag,value):
        if width<1 or tag<0 or value<0 or tag.bit_length()>width or value.bit_length()>width:raise ValueError('Numeric observation outside exact width')
        return format(tag,'0'+str(width)+'b')+format(value,'0'+str(width)+'b')
    with gzip.open(source,'rt') as src,gzip.open(target,'xt',compresslevel=1) as dst:
        for line in src:
            p=line.split()
            if not p:continue
            if p[0]=='C2LN':
                if len(p)!=9:raise ValueError('Malformed numeric snapshot')
                width,tag,value=map(int,p[6:])
                v=str(value) if p[3]=='E' and width==0 and tag==0 else pair(width,tag,value)
                dst.write(' '.join(['C2L',*p[1:6],v])+'\n')
            elif p[0]=='A2LN':
                if len(p)!=10:raise ValueError('Malformed numeric activation')
                dst.write(' '.join(['A2L',*p[1:7],pair(*map(int,p[7:]))])+'\n')
            elif p[0]=='F57_ARGNUM':
                if len(p)!=8:raise ValueError('Malformed numeric diagnostic argument')
                dst.write(' '.join(['F56_ARG',*p[1:5],pair(*map(int,p[5:]))])+'\n')
            else:dst.write(line)
