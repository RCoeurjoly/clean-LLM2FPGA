#!/usr/bin/env python3
"""Read-only GitHub search/source survey; uses gh's existing authentication."""
import argparse, base64, concurrent.futures, csv, datetime, gzip, hashlib, io, json, pathlib, re, subprocess, tarfile, time, urllib.parse
BASE=pathlib.Path(__file__).resolve().parent
BASE.mkdir(parents=True,exist_ok=True)
def api(endpoint, dest):
    dest=BASE/dest
    if dest.exists(): return json.loads(dest.read_text())
    compressed=pathlib.Path(str(dest)+'.gz')
    if compressed.exists():
        with gzip.open(compressed,'rt') as stream:return json.load(stream)
    p=subprocess.run(['gh','api',endpoint],capture_output=True,text=True)
    log={'utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'endpoint':endpoint,'destination':str(dest.relative_to(BASE)),'exit_code':p.returncode}
    if p.returncode: log['error']=p.stderr[:1500]
    with (BASE/'requests.jsonl').open('a') as f:f.write(json.dumps(log)+'\n')
    if p.returncode: return {'error':p.stderr}
    d=json.loads(p.stdout);dest.parent.mkdir(parents=True,exist_ok=True);dest.write_text(json.dumps(d,indent=2)+'\n');return d
def searches():
    queries=[
      'language:Lean yosys in:name,description,readme fork:true',
      'language:Lean verilog in:name,description,readme fork:true',
      'language:Lean SystemVerilog in:name,description,readme fork:true',
      'language:Lean netlist in:name,description,readme fork:true',
      'language:Lean BLIF in:name,description,readme fork:true',
      'language:Lean EBLIF in:name,description,readme fork:true',
      'language:Lean EDIF in:name,description,readme fork:true',
      'language:Lean CXXRTL in:name,description,readme fork:true',
      'language:Lean intersynth in:name,description,readme fork:true',
      'language:Lean simplec in:name,description,readme fork:true',
      'language:Lean "write_json" in:name,description,readme fork:true',
      'Lean yosys in:name,description,readme fork:true',
      'Lean BLIF in:name,description,readme fork:true',
      'Lean EDIF in:name,description,readme fork:true',
      'Lean CXXRTL in:name,description,readme fork:true',
      'Lean "write_jny" in:name,description,readme fork:true',
      'hardwareLoom in:name,description,readme fork:true',
    ]
    results=[]
    for i,q in enumerate(queries):
        page=1
        while True:
            d=api('search/repositories?'+urllib.parse.urlencode({'q':q,'per_page':100,'page':page}),f'searches/repo-{i:02}-{page}.json')
            items=d.get('items',[])
            results.append({'kind':'repository','query':q,'page':page,'per_page':100,'returned':len(items),'total_count':d.get('total_count'),'incomplete_results':d.get('incomplete_results'),'names':[x['full_name'] for x in items],'error':d.get('error')})
            print(i,page,d.get('total_count'),len(items),flush=True)
            time.sleep(7)
            if len(items)<100 or page*100>=min(d.get('total_count',0),1000):break
            page+=1
    (BASE/'search-index.json').write_text(json.dumps(results,indent=2)+'\n')
def trees():
    repos=['thomasnormal/lean-surfaces','Verilean/sparkle','nathsou/HDLean','Easyoakland/HDLean','deepai-org/loom','verilog-proof/VerilLean','ErosSph/xiangshan2Lean','muzafferkal/circuitprover-sv','shizhilvren/VLean','soohyuk-cho/COS598B-Final-Project-Verilog-To-Lean','formalized-si/redstone-verify','atvrager/Shoumei-RTL','deepai-org/lean_hw','stephenneuendorffer/CircuitHDL','rewire-hardware/ReWire']
    def one(repo):
        slug=repo.replace('/','__')
        m=api(f'repos/{repo}',f'repos/{slug}/metadata.json')
        d=api(f'repos/{repo}/git/trees/{m.get("default_branch","main")}?recursive=1',f'repos/{slug}/tree.json')
        print(repo,d.get('sha'),len(d.get('tree',[])),d.get('truncated'),flush=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:list(ex.map(one,repos))
def codes():
    queries=['yosys language:Lean','BLIF language:Lean','EBLIF language:Lean','EDIF language:Lean','CXXRTL language:Lean','intersynth language:Lean','simplec language:Lean','write_jny language:Lean','write_table language:Lean','write_spice language:Lean','"$dff" language:Lean','"netnames" language:Lean','"write_json" language:Lean','"yosys" "lean" extension:py','"cxxrtl" "lean"','"blif" "lean"']
    rows=[]
    for i,q in enumerate(queries):
        page=1
        while True:
            cached=(BASE/f'searches/code-{i:02}-{page}.json').exists()
            d=api('search/code?'+urllib.parse.urlencode({'q':q,'per_page':100,'page':page}),f'searches/code-{i:02}-{page}.json')
            items=d.get('items',[]);rows.append({'kind':'code','query':q,'page':page,'per_page':100,'returned':len(items),'total_count':d.get('total_count'),'incomplete_results':d.get('incomplete_results'),'items':[{'repo':x['repository']['full_name'],'path':x['path'],'url':x['html_url']} for x in items],'error':d.get('error')})
            print('code',i,page,d.get('total_count'),len(items),flush=True)
            if not cached:time.sleep(7)
            # BLIF/EDIF substring matches include bLift/edifice math identifiers;
            # retain first page and explicitly report the capped broad probes.
            cap=100 if i in {1,3,6,13,14,15} else 1000
            if len(items)<100 or page*100>=min(d.get('total_count',0),cap):break
            page+=1
    (BASE/'code-index.json').write_text(json.dumps(rows,indent=2)+'\n')
def sources():
    def one(task):
        repo,path,sha=task;slug=repo.replace('/','__');dest=BASE/f'repos/{slug}/source'/path
        if dest.exists():return
        d=api(f'repos/{repo}/git/blobs/{sha}',f'repos/{slug}/blobs/{sha}.json')
        if 'content' not in d:return
        dest.parent.mkdir(parents=True,exist_ok=True);dest.write_bytes(base64.b64decode(d['content']))
    tasks=[]
    for p in (BASE/'repos').glob('*/tree.json'):
        repo=p.parent.name.replace('__','/');d=json.loads(p.read_text())
        for f in d.get('tree',[]):
            path=f['path']
            if f['type']!='blob' or f.get('size',0)>250000:continue
            keep=path.endswith(('.lean','.py','.cpp','.h','.toml')) or re.search(r'README|lean-toolchain|TRUST|STATUS|SVParser|[Ii]mport|[Pp]arser|yosys|[Ss]emantic',path)
            if keep and not any(s in path.split('/') for s in ['.lake','node_modules','lake-packages']):tasks.append((repo,path,f['sha']))
    print('source requests',len(tasks),flush=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:list(ex.map(one,tasks))
def archives():
    def one(p):
        repo=p.parent.name.replace('__','/');tree=json.loads(p.read_text());sha=tree['sha'];dest=p.parent/'source'
        if (p.parent/'archive-manifest.json').exists():return
        endpoint=f'repos/{repo}/tarball/{sha}'
        proc=subprocess.run(['gh','api',endpoint],capture_output=True)
        log={'utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'endpoint':endpoint,'exit_code':proc.returncode,'archive_bytes':len(proc.stdout),'sha256':hashlib.sha256(proc.stdout).hexdigest()}
        files=[]
        if proc.returncode:log['error']=proc.stderr.decode()[:1000]
        else:
            with tarfile.open(fileobj=io.BytesIO(proc.stdout),mode='r:gz') as tar:
                for m in tar:
                    path=pathlib.PurePosixPath(*pathlib.PurePosixPath(m.name).parts[1:]);s=str(path)
                    if not m.isfile() or m.size>1000000 or '..' in path.parts:continue
                    if any(x in path.parts for x in ['.lake','node_modules','lake-packages','experiments','.git','__pycache__']):continue
                    if not (s.endswith(('.lean','.py','.cpp','.c','.h','.hpp','.toml','.md')) or path.name=='lean-toolchain'):continue
                    data=tar.extractfile(m).read();out=dest/path;out.parent.mkdir(parents=True,exist_ok=True);out.write_bytes(data)
                    files.append({'path':s,'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()})
        log['files']=files;(p.parent/'archive-manifest.json').write_text(json.dumps(log,indent=2)+'\n')
        print(repo,proc.returncode,'files',len(files),'archive_bytes',len(proc.stdout),flush=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:list(ex.map(one,(BASE/'repos').glob('*/tree.json')))
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('mode',choices=['search','trees','sources','archives','code']);a=p.parse_args()
    {'search':searches,'trees':trees,'sources':sources,'archives':archives,'code':codes}[a.mode]()
