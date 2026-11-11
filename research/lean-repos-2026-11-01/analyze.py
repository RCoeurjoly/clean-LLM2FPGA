#!/usr/bin/env python3
"""Render an offline, auditable Lean repository survey from collector outputs.

Usage: python3 analyze.py [--out DIRECTORY]
The collector must have written coverage.json before this script is run.
Optional source-scan.json supplies separately labeled filename inventories.
No network requests or third-party packages are used.
"""

import argparse
from collections import Counter
import csv
import datetime as dt
import json
from pathlib import Path
import urllib.parse

REQUESTED = ['Verilog', 'SystemVerilog', 'VHDL', 'yosys', 'RTL', 'MLIR', 'PyTorch', 'Python']
SUPPLEMENTAL = ['CIRCT', 'Calyx', 'FPGA', 'hardware', 'tensor', 'quantization',
                'bitvector', 'floating-point', 'SMT', 'SAT', 'compiler', 'equivalence']
CUTOFF = '2026-09-01T00:00:00Z'
COLUMNS = ['full_name', 'html_url', 'stars', 'pushed_at', 'archived', 'fork',
           'active_2months', 'in_census', 'requested_keyword_matches',
           'supplemental_keyword_matches', 'query_keyword_matches', 'file_keyword_matches',
           'extension_counts', 'sample_paths', 'tree_sha', 'tree_truncated',
           'filename_inspection_kind', 'filename_inspection_errors', 'category',
           'priority', 'relevance', 'capability', 'limitations', 'proof_status',
           'source_roles', 'source_evidence', 'description', 'language', 'id']


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8'))


def save_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def unique(values):
    return list(dict.fromkeys(values))


def is_recent(row):
    # ISO 8601 collector timestamps are UTC; parsing also handles curated offsets.
    pushed = row.get('pushed_at')
    if not pushed:
        return None
    try:
        value = dt.datetime.fromisoformat(pushed.replace('Z', '+00:00'))
        threshold = dt.datetime.fromisoformat(CUTOFF.replace('Z', '+00:00'))
        return value >= threshold
    except (TypeError, ValueError):
        return None


def automatic_assessment(terms):
    """A screening bucket, deliberately not an inferred implementation claim."""
    matches = set(terms)
    if matches & {'Verilog', 'SystemVerilog', 'VHDL', 'yosys', 'RTL', 'FPGA', 'hardware'}:
        category = 'direct hardware'
    elif matches & {'MLIR', 'CIRCT', 'Calyx', 'compiler'}:
        category = 'compiler IR'
    elif matches & {'PyTorch', 'tensor', 'quantization'}:
        category = 'NN semantics'
    elif matches & {'bitvector', 'floating-point', 'SMT', 'SAT', 'equivalence'}:
        category = 'numeric + solver'
    elif matches == {'Python'}:
        category = 'Python-only'
    else:
        category = 'adjacent'
    result = {
        'category': category,
        'priority': 4,
        'relevance': ('Low-specificity Python-only indexed match; requires source review.'
                      if category == 'Python-only' else
                      'Screening lead based on indexed keyword evidence; requires source review.'),
        'capability': 'No implementation capability established by keyword screening.',
        'limitations': 'Name, description or indexed README match may be incidental; code not audited.',
        'proof_status': 'Indexed keyword evidence only; no proof or correctness assessment.',
    }
    if not terms:
        result.update(relevance='No requested or supplemental indexed keyword match recorded.',
                      capability='No implementation capability established by metadata collection.',
                      limitations='Repository metadata collected; code not audited.',
                      proof_status='Metadata only; no proof or correctness assessment.')
    return result


def curated_entries(directory):
    result = {}
    files = []
    for path in sorted(directory.glob('curated-*.json')):
        files.append(path.name)
        value = read_json(path)
        if isinstance(value, dict):
            value = value.get('repositories', value.get('entries', value.get('candidates', [])))
        if not isinstance(value, list):
            raise ValueError(f'{path.name}: expected an array of source assessments')
        for item in value:
            name = item.get('full_name')
            if not name:
                raise ValueError(f'{path.name}: source assessment lacks full_name')
            entry = dict(item)
            entry['_file'] = path.name
            result.setdefault(name, []).append(entry)
    return result, files


def make_row(repo, hits, census, curated, source_scan=None):
    name = repo['full_name']
    scan = (source_scan or {}).get(name)
    terms = sorted(hits.get(name, []), key=lambda s: (REQUESTED + SUPPLEMENTAL).index(s)
                   if s in REQUESTED + SUPPLEMENTAL else len(REQUESTED + SUPPLEMENTAL))
    # These suffix-supported labels are supplied by the separate filename inspector.
    # In particular, .v can be Coq or Verilog and never establishes a Verilog match.
    file_terms = [term for term in REQUESTED if scan and
                  term in {'Python', 'SystemVerilog', 'VHDL', 'MLIR'} and
                  term in scan.get('file_keyword_matches', [])]
    requested = [term for term in REQUESTED if term in terms or term in file_terms]
    supplemental = [term for term in terms if term not in REQUESTED]
    assessment = automatic_assessment(unique(terms + file_terms))
    if file_terms:
        assessment.update(
            relevance=('Low-specificity Python-only screening evidence; requires source review.'
                       if assessment['category'] == 'Python-only' else
                       'Screening lead based on indexed keywords and/or filename evidence; requires source review.'),
            limitations='Indexed matches and filename extensions may be incidental; filenames do not establish code semantics.',
            proof_status='Keyword/filename screening only; no source or proof audit.')
    roles = ['GitHub repository metadata' if repo.get('id') is not None else
             'Curated lead; repository metadata absent from collector']
    evidence = [repo.get('html_url') or f'https://github.com/{name}']
    if terms:
        roles.append('GitHub indexed name/description/README keyword search')
        evidence.extend('https://github.com/search?' + urllib.parse.urlencode({
            'q': f'language:lean {term} in:name,description,readme', 'type': 'repositories'})
                        for term in terms)
    if scan:
        roles.append('Filename inspection, not source/proof audit: ' + scan.get('inspection_kind', 'unspecified'))
        if scan.get('tree_sha'):
            evidence.append(f'https://github.com/{name}/tree/{urllib.parse.quote(scan["tree_sha"], safe="")}')
    # Apply weaker records first so the strongest source assessment wins scalar fields.
    audits = sorted(curated.get(name, []), key=lambda item: int(item.get('priority') or 4), reverse=True)
    for audit in audits:
        for key in assessment:
            if audit.get(key) is not None and audit.get(key) != '':
                assessment[key] = audit[key]
        roles.append('Curated source assessment: ' + audit['_file'])
        urls = audit.get('evidence_urls', [])
        if isinstance(urls, str):
            urls = [urls]
        evidence.extend(urls)
    recent = is_recent(repo)
    active = recent and not repo.get('archived') if repo.get('archived') is not None else None
    return {
        'full_name': name, 'html_url': repo.get('html_url') or f'https://github.com/{name}',
        'stars': repo.get('stargazers_count'), 'pushed_at': repo.get('pushed_at'),
        'archived': repo.get('archived'), 'fork': repo.get('fork'), 'active_2months': active,
        'in_census': str(repo.get('id')) in census,
        'requested_keyword_matches': requested, 'supplemental_keyword_matches': supplemental,
        'query_keyword_matches': terms, 'file_keyword_matches': file_terms,
        'extension_counts': scan.get('extension_counts', {}) if scan else {},
        'sample_paths': scan.get('sample_paths', {}) if scan else {},
        'tree_sha': scan.get('tree_sha') if scan else None,
        'tree_truncated': scan.get('tree_truncated') if scan else None,
        'filename_inspection_kind': scan.get('inspection_kind') if scan else None,
        'filename_inspection_errors': scan.get('errors', []) if scan else [],
        'filename_inspected': scan is not None,
        **assessment, 'source_roles': unique(roles),
        'source_evidence': unique(evidence), 'description': repo.get('description') or '',
        'language': repo.get('language'), 'id': repo.get('id'), 'source_reviewed': bool(audits),
    }


def write_csv(path, rows):
    with path.open('w', newline='', encoding='utf-8') as stream:
        writer = csv.DictWriter(stream, fieldnames=COLUMNS, extrasaction='ignore')
        writer.writeheader()
        for row in rows:
            values = {}
            for key in COLUMNS:
                value = row.get(key)
                if isinstance(value, list):
                    value = ' | '.join(str(item) for item in value)
                elif isinstance(value, dict):
                    value = json.dumps(value, ensure_ascii=False, sort_keys=True)
                elif isinstance(value, bool):
                    value = 'true' if value else 'false'
                elif value is None:
                    value = ''
                # Keep repository-controlled strings inert when opened in a spreadsheet.
                if isinstance(value, str) and value.startswith(('=', '+', '-', '@', '\t', '\r')):
                    value = "'" + value
                values[key] = value
            writer.writerow(values)


def star_key(row):
    return (-(row.get('stars') or 0), row['full_name'].lower())


def script_json(value):
    return (json.dumps(value, ensure_ascii=False).replace('&', '\\u0026')
            .replace('<', '\\u003c').replace('>', '\\u003e')
            .replace('\u2028', '\\u2028').replace('\u2029', '\\u2029'))


HTML = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Lean repositories for LLM2FPGA · 1 November 2026</title>
<style>
:root{font:15px/1.5 system-ui,sans-serif;color:#172638;background:#f5f7fa}body{margin:0;padding:28px;max-width:1800px;margin:auto}h1{font-size:30px;margin:0 0 10px}h2{font-size:20px}a{color:#075e9b}p{max-width:1150px}.muted{color:#526476}.cards{display:flex;gap:12px;flex-wrap:wrap;margin:22px 0}.card{background:white;border:1px solid #d9e1ea;border-radius:8px;padding:15px;min-width:170px}.card b{display:block;font-size:26px}.controls{background:#fff;padding:16px;border:1px solid #d9e1ea;border-radius:8px;display:flex;flex-wrap:wrap;gap:14px;align-items:center}.controls input[type=search]{width:320px;max-width:90vw;padding:8px}select{padding:6px}fieldset{border:0;padding:0;margin:0;display:flex;gap:10px;flex-wrap:wrap}legend{font-size:12px;color:#526476}label{white-space:nowrap}.table-wrap{overflow:auto;background:white;border:1px solid #d9e1ea;border-radius:8px;margin-top:12px}table{width:100%;border-collapse:collapse;min-width:1350px}th,td{text-align:left;padding:10px;border-bottom:1px solid #e6ebf0;vertical-align:top}th{background:#eaf0f6;white-space:nowrap}th button{border:0;background:none;font:inherit;font-weight:650;cursor:pointer;padding:0;color:inherit}td:nth-child(1){min-width:230px;max-width:340px}td:nth-child(6){min-width:340px;max-width:600px}.badge{display:inline-block;font-size:11px;padding:2px 6px;margin:2px;background:#edf2f7;border-radius:4px}.p1{background:#d8f4e4}.p2{background:#e4edf9}.p3{background:#fff0cf}.downloads{display:flex;gap:15px;flex-wrap:wrap}details{margin-top:7px}summary{cursor:pointer;color:#075e9b}.note{border-left:4px solid #e2ab3d;padding:10px 14px;background:#fff8e8}#count{margin:12px 0 4px}code{overflow-wrap:anywhere}#term-counts{min-width:0;max-width:700px;margin-bottom:20px}#term-counts th,#term-counts td{padding:6px 12px}.pagination{display:flex;align-items:center;gap:12px;margin:12px 0}button:disabled{opacity:.5}.downloads a{white-space:nowrap}footer{margin:24px 0;color:#526476;font-size:13px}
</style></head><body>
<h1>Lean repositories for LLM2FPGA</h1>
<p class="muted">Public GitHub repository survey · 1 November 2026 · offline report</p>
<div id="cards" class="cards"></div>
<p id="coverage"></p>
<p class="note">This is an indexed metadata and README screen, with filename inventories and separately labeled source assessments for selected projects. Filename inspection is not a source or proof audit. A keyword or filename hit does not establish hardware generation, interoperability, numerical fidelity, or a proved theorem. Read each candidate’s evidence and limitations.</p>
<p>“Active in two calendar months” means <code>archived:false</code> and <code>pushed_at ≥ 2026-09-01T00:00:00Z</code>. Activity filters also offer six months (1 May 2026), twelve months (1 November 2025), or all dates. GitHub’s push timestamp can reflect activity on any branch and does not establish substantive maintenance. Older and archived leads remain available by changing the filters.</p>
<p id="method"></p>
<p id="scan-scope"></p>
<div class="downloads">
<a href="all-repositories.csv" download>All census repositories CSV</a><a href="top100-all.csv" download>Original top 100 CSV</a><a href="top100-active.csv" download>Active top 100 CSV</a><a href="keyword-matches.csv" download>Requested matches union CSV</a><a href="active-keyword-matches.csv" download>Active requested matches CSV</a><a href="source-file-matches.csv" download>Filename matches CSV</a><a href="candidates.csv" download>All candidate leads CSV</a><a href="analysis.json" download>Analysis JSON</a><a href="coverage.json" download>Collector coverage JSON</a>
<a href="shortlist-all.csv" download>Reviewed priority 1–2 shortlist · all dates</a><a href="shortlist-active.csv" download>Reviewed priority 1–2 shortlist · active</a>
<a href="top100-github-query.csv" download>Top 100 from GitHub indexed pushed query</a><a href="ranking-validation.json" download>Ranking comparison audit</a>
</div>
<h2>Requested term coverage · indexed and filename evidence union</h2><p class="muted">Repositories may match multiple terms; counts overlap. Union includes each term’s indexed query hits and inspected filename matches, including archived and older projects. Filename-only means the term has filename evidence but no indexed hit for that same term. Filename inspections cover a selected subset; they are not a corpus-wide content search. A .v suffix is ambiguous between Coq and Verilog and is not counted as a Verilog filename match.</p>
<table id="term-counts"><thead><tr><th>Term</th><th>Union</th><th>Indexed</th><th>Filename-only</th><th>Active union</th><th>Archived</th><th>In census</th></tr></thead><tbody></tbody></table>
<h2>Candidate explorer</h2><p class="muted">Includes all requested and supplemental indexed keyword hits, requested filename matches, and curated source leads. Categories assigned without source review are screening buckets. Priority 1 marks highest relevance to LLM2FPGA, not proof completeness or implementation maturity; priority 4 includes unreviewed leads. Recent activity and priority are independent.</p>
<div class="controls">
<input id="search" type="search" aria-label="Search candidates" placeholder="Search names, descriptions, assessments…">
<label>Push activity <select id="activity"><option value="2026-09-01T00:00:00Z">2 months · since 1 Sep 2026</option><option value="2026-05-01T00:00:00Z">6 months · since 1 May 2026</option><option value="2025-11-01T00:00:00Z">12 months · since 1 Nov 2025</option><option value="">All dates</option></select></label>
<label><input id="archived" type="checkbox"> Include archived</label>
<label>Category <select id="category"><option value="">All categories</option></select></label>
<label>Priority <select id="priority"><option value="">All priorities</option><option value="1">1 · strongest</option><option value="2">2</option><option value="3">3</option><option value="4">4 · indirect / unreviewed</option></select></label>
<label><input id="reviewed" type="checkbox"> Source assessments only</label>
<fieldset id="terms"><legend>Any selected requested term; no selection shows all candidates</legend></fieldset>
</div>
<p id="count" aria-live="polite"></p>
<div class="pagination"><button id="prev">Previous</button><span id="page"></span><button id="next">Next</button><label>Rows <select id="pagesize"><option>50</option><option>100</option><option>250</option><option value="1000000">All</option></select></label></div>
<div class="table-wrap"><table><thead><tr><th><button data-sort="full_name">Repository ↕</button></th><th><button data-sort="stars">Stars ↕</button></th><th><button data-sort="pushed_at">Pushed ↕</button></th><th><button data-sort="priority">Priority ↕</button></th><th><button data-sort="category">Category ↕</button></th><th>Relevance, capability and evidence</th></tr></thead><tbody id="rows"></tbody></table></div>
<footer>All filters and sorting run locally. No external scripts, fonts or analytics. CSV string values beginning with spreadsheet formula characters receive a leading apostrophe; JSON retains original values. Unknown metadata is blank in CSV and shown as “unknown” here. Repository metadata and source assessments have distinct evidence roles.</footer>
<script type="application/json" id="survey-data">__DATA__</script>
<script>
'use strict';
const data=JSON.parse(document.getElementById('survey-data').textContent), $=id=>document.getElementById(id);
const fmt=n=>Number(n).toLocaleString('en-US');
function node(tag,text,cls){const e=document.createElement(tag);if(text!==undefined)e.textContent=text;if(cls)e.className=cls;return e;}
function link(url,text){const a=node('a',text);try{const u=new URL(url);if(u.protocol==='https:'||u.protocol==='http:'){a.href=u.href;a.target='_blank';a.rel='noopener noreferrer';}}catch(e){}return a;}
for(const [label,value] of [['Initial search denominator',data.analysis.initial_search_total],['Collected census repositories',data.analysis.census_unique_count],['Active census repositories',data.analysis.active_census_count],['All candidate leads',data.rows.length],['Filename inventories',data.analysis.filename_inspected_count],['Curated source assessments',data.analysis.source_reviewed_candidate_count]]){const card=node('div',undefined,'card');card.append(node('b',fmt(value)),node('span',label));$('cards').append(card);}
$('coverage').textContent=data.analysis.coverage_summary;
$('method').textContent=data.analysis.method_summary;
$('scan-scope').textContent=data.analysis.filename_inspection_summary;
if(data.analysis.filename_inspection_source){const raw=node('a','Raw filename inventories JSON');raw.href='source-scan.json';raw.download='';document.querySelector('.downloads').append(raw);}
for(const r of data.analysis.requested_keyword_counts){const tr=node('tr');for(const key of ['term','all','indexed','filename_only','active','archived','in_census'])tr.append(node('td',typeof r[key]==='number'?fmt(r[key]):r[key]));$('term-counts').querySelector('tbody').append(tr);}
for(const term of data.analysis.requested_keywords){const label=node('label'),input=node('input');input.type='checkbox';input.value=term;label.append(input,document.createTextNode(' '+term));$('terms').append(label);}
for(const category of [...new Set(data.rows.map(r=>r.category))].sort()){const o=node('option',category);o.value=category;$('category').append(o);}
let sort='priority',ascending=true,page=0;
const haystacks=data.rows.map(r=>JSON.stringify(r).toLowerCase());
function inventoryDetails(r){
 const detail=node('details');detail.append(node('summary','Filename inventory · not a source/proof audit'),node('p','Inspection: '+(r.filename_inspection_kind||'unspecified')+'; tree SHA: '+(r.tree_sha||'unknown')+'; tree truncated: '+(r.tree_truncated==null?'unknown':r.tree_truncated)),node('p','Extension counts: '+JSON.stringify(r.extension_counts)),node('p','Filename term matches: '+(r.file_keyword_matches.join(', ')||'none')+'. A .v suffix alone is not a Verilog match.'));
 for(const [suffix,paths] of Object.entries(r.sample_paths)){const group=node('div');group.append(node('b',suffix+' sample paths'));for(const path of paths){const entry=node('div'),url=r.html_url+'/blob/'+encodeURIComponent(r.tree_sha||'HEAD')+'/'+path.split('/').map(encodeURIComponent).join('/');entry.append(link(url,path));group.append(entry);}detail.append(group);}
 if(r.filename_inspection_errors.length)detail.append(node('p','Inspection errors: '+JSON.stringify(r.filename_inspection_errors)));
 return detail;
}
function render(){
 const q=$('search').value.trim().toLowerCase(),cutoff=$('activity').value,archived=$('archived').checked,category=$('category').value,priority=$('priority').value,reviewed=$('reviewed').checked,terms=[...$('terms').querySelectorAll('input:checked')].map(e=>e.value);
 const selected=data.rows.filter((r,i)=>(!q||haystacks[i].includes(q))&&(!cutoff||(r.pushed_at&&Date.parse(r.pushed_at)>=Date.parse(cutoff)))&&(archived||r.archived!==true)&&(!category||r.category===category)&&(!priority||String(r.priority)===priority)&&(!reviewed||r.source_reviewed)&&(!terms.length||terms.some(t=>r.requested_keyword_matches.includes(t))));
 selected.sort((a,b)=>{let x=a[sort],y=b[sort],c;if(sort==='stars'||sort==='priority')c=(x??-1)-(y??-1);else c=String(x??'').localeCompare(String(y??''));return (ascending?c:-c)||((b.stars??0)-(a.stars??0))||a.full_name.localeCompare(b.full_name);});
 const size=Number($('pagesize').value),pages=Math.max(1,Math.ceil(selected.length/size));page=Math.min(page,pages-1);
 $('count').textContent=fmt(selected.length)+' of '+fmt(data.rows.length)+' candidate leads match these filters. '+(cutoff?'Push activity cutoff: '+cutoff.slice(0,10)+'.':'All push dates, including unknown dates, are included.');
 $('page').textContent='Page '+(page+1)+' / '+pages;$('prev').disabled=page===0;$('next').disabled=page>=pages-1;
 $('rows').replaceChildren();
 for(const r of selected.slice(page*size,(page+1)*size)){
  const tr=node('tr'),repo=node('td');repo.append(link(r.html_url,r.full_name));if(r.description)repo.append(node('div',r.description,'muted'));const badges=node('div');for(const t of r.query_keyword_matches)badges.append(node('span',t+' · indexed','badge'));for(const t of r.file_keyword_matches)badges.append(node('span',t+' · filename','badge'));if(r.archived===true)badges.append(node('span','archived','badge'));if(r.fork===true)badges.append(node('span','fork','badge'));if(!r.in_census)badges.append(node('span','outside census snapshot','badge'));repo.append(badges);tr.append(repo,node('td',r.stars==null?'unknown':fmt(r.stars)),node('td',r.pushed_at?r.pushed_at.slice(0,10):'unknown'));
  const priority=node('td');priority.append(node('span',String(r.priority),'badge p'+r.priority));tr.append(priority,node('td',r.category));const info=node('td');info.append(node('div',r.relevance),node('p','Capability: '+r.capability),node('p','Limitations: '+r.limitations,'muted'));const details=node('details');details.append(node('summary',r.source_reviewed?'Source assessment and evidence':'Screening evidence'),node('p','Proof status: '+r.proof_status),node('p','Source roles: '+r.source_roles.join('; ')));for(const url of r.source_evidence){const div=node('div');div.append(link(url,url));details.append(div);}info.append(details);if(r.filename_inspected)info.append(inventoryDetails(r));tr.append(info);$('rows').append(tr);
 }
}
for(const id of ['search','activity','archived','category','priority','reviewed','terms','pagesize'])$(id).addEventListener(id==='search'?'input':'change',()=>{page=0;render();});
for(const b of document.querySelectorAll('[data-sort]'))b.addEventListener('click',()=>{if(sort===b.dataset.sort)ascending=!ascending;else{sort=b.dataset.sort;ascending=sort!=='stars'&&sort!=='pushed_at';}page=0;render();});
$('prev').addEventListener('click',()=>{page--;render();});$('next').addEventListener('click',()=>{page++;render();});render();
</script></body></html>
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, default=Path(__file__).resolve().parent)
    args = parser.parse_args()
    directory = args.out
    coverage = read_json(directory / 'coverage.json')
    repos = {}
    for line in (directory / 'repositories.jsonl').read_text(encoding='utf-8').splitlines():
        if line.strip():
            repo = json.loads(line)
            repos[repo['full_name']] = repo
    hits = read_json(directory / 'keyword-hits.json')
    census = {str(value) for value in read_json(directory / 'census-ids.json')}
    original = read_json(directory / 'top100-original-search.json')
    indexed_active_path = directory / 'active-validation-query.json'
    indexed_active = read_json(indexed_active_path) if indexed_active_path.exists() else None
    curated, curated_files = curated_entries(directory)
    scan_path = directory / 'source-scan.json'
    source_scan = read_json(scan_path) if scan_path.exists() else {}
    if not isinstance(source_scan, dict):
        raise ValueError('source-scan.json must map repository names to inspection records')
    for name, scan in source_scan.items():
        if name not in repos:
            repos[name] = dict(scan.get('metadata') or {})
            repos[name].setdefault('full_name', name)
            repos[name].setdefault('html_url', f'https://github.com/{name}')
    # Source leads outside language:lean can be included, but must not alter census totals.
    for name, audits in curated.items():
        if name not in repos:
            repo = {'full_name': name, 'html_url': f'https://github.com/{name}'}
            for audit in audits:
                for key in ['id', 'html_url', 'description', 'stargazers_count', 'pushed_at',
                            'archived', 'fork', 'language']:
                    if audit.get(key) is not None:
                        repo[key] = audit[key]
            repos[name] = repo
    rows = {name: make_row(repo, hits, census, curated, source_scan) for name, repo in repos.items()}
    missing_hits = sorted(set(hits) - set(rows))
    if missing_hits:
        raise ValueError(f'Keyword hits lack repository metadata: {missing_hits[:10]}')
    census_rows = sorted((row for row in rows.values() if row['in_census']), key=star_key)
    missing_ids = census - {str(row['id']) for row in census_rows}
    if missing_ids:
        raise ValueError(f'{len(missing_ids)} census IDs lack repository metadata')
    if len(census) != coverage['census_unique_count']:
        raise ValueError('census-ids.json and coverage.json counts disagree')
    candidates = sorted((row for row in rows.values()
                         if row['query_keyword_matches'] or row['file_keyword_matches']
                         or row['full_name'] in curated),
                        key=lambda row: (int(row['priority']), *star_key(row)))
    requested_rows = sorted((row for row in candidates if row['requested_keyword_matches']), key=star_key)
    active_census = [row for row in census_rows if row['active_2months'] is True]
    top_original = [make_row(repo, hits, census, curated, source_scan) for repo in original['items'][:100]]
    exports = {
        'all-repositories.csv': census_rows,
        'top100-all.csv': top_original,
        'top100-active.csv': active_census[:100],
        'keyword-matches.csv': requested_rows,
        'active-keyword-matches.csv': [row for row in requested_rows if row['active_2months'] is True],
        'source-file-matches.csv': sorted((row for row in candidates if row['file_keyword_matches']),
                                          key=star_key),
        'candidates.csv': candidates,
        'shortlist-all.csv': [row for row in candidates if row['source_reviewed'] and int(row['priority']) <= 2],
        'shortlist-active.csv': [row for row in candidates if row['source_reviewed'] and int(row['priority']) <= 2
                                 and row['active_2months'] is True],
    }
    if indexed_active is not None:
        exports['top100-github-query.csv'] = [make_row(repo, hits, census, curated, source_scan)
                                             for repo in indexed_active['items'][:100]]
    leaves = coverage.get('leaves', [])
    balanced = all(leaf['expected_count'] == leaf['unique_count'] for leaf in leaves)
    discrepancies = [leaf for leaf in leaves if leaf['expected_count'] != leaf['unique_count']]
    documented_queries = {entry.get('query') for entry in coverage.get('errors', [])}
    differences_documented = all(leaf['query'] in documented_queries for leaf in discrepancies)
    census_count_only = sum(max(0, leaf['expected_count'] - leaf['unique_count'])
                            for leaf in leaves if leaf['base_query'] == 'language:lean')
    under_limit = all(leaf['expected_count'] < 1000 for leaf in leaves)
    keyword_counts = []
    for term in REQUESTED:
        matches = [row for row in candidates if term in row['requested_keyword_matches']]
        keyword_counts.append({'term': term, 'all': len(matches),
                               'indexed': sum(term in row['query_keyword_matches'] for row in matches),
                               'filename': sum(term in row['file_keyword_matches'] for row in matches),
                               'filename_only': sum(term in row['file_keyword_matches'] and
                                                    term not in row['query_keyword_matches'] for row in matches),
                               'active': sum(row['active_2months'] is True for row in matches),
                               'archived': sum(row['archived'] is True for row in matches),
                               'in_census': sum(row['in_census'] for row in matches)})
    initial = coverage['initial_search_total']
    difference = len(census) - initial
    summary = (f"The initial language:lean search reported {initial:,} repositories; "
               f"the partitioned census collected {len(census):,} distinct IDs "
               f"(difference {difference:+,}). GitHub repository search excludes forks by default. "
               f"{len(leaves):,} completed search leaves; "
               f"leaf counts {'all reconcile' if balanced else 'have ' + str(len(discrepancies)) + ' documented count discrepancies' if differences_documented else 'have UNDOCUMENTED discrepancies'}, "
               f"{'all below' if under_limit else 'some reach or exceed'} the 1,000-result limit.")
    method = ('Creation-time partitions avoid the search API’s 1,000-result pagination cap. '
              'Keyword screening uses GitHub-indexed names, descriptions and READMEs for all eight requested terms '
              'plus supplemental hardware, compiler and numeric terms. This is not a full-code audit of every repository. '
              'The live GitHub index is mutable rather than an atomic snapshot; missing, inaccessible or unindexed '
              'repositories and unindexed text cannot be ruled out. The original top 100 preserves the initial '
              'GitHub star-sorted response, including archived projects; active rankings use collected census metadata.')
    if discrepancies:
        method += (f' GitHub reports {census_count_only} census match(es) with no retrievable record even after '
                   'single-timestamp isolation. These are explicit coverage gaps, not invented repository rows; '
                   'see coverage.json errors and discrepancy-investigation.json.')
    if indexed_active is not None:
        metadata_names = {row['full_name'] for row in active_census[:100]}
        indexed_names = {repo['full_name'] for repo in indexed_active['items'][:100]}
        ranking_same = metadata_names == indexed_names
        method += (f' A separate GitHub indexed query language:lean archived:false pushed:>=2026-09-01 '
                   f'reports {indexed_active["total_count"]:,} matches. Its top-100 set '
                   f'{"agrees" if ranking_same else "differs"} with the saved pushed_at-field ranking. '
                   'Both are exported separately; the activity filters in this report use saved pushed_at fields. '
                   'See ranking-validation.json for the observed difference.')
    scan_errors = sum(bool(scan.get('errors')) for scan in source_scan.values())
    scan_truncated = sum(scan.get('tree_truncated') is True for scan in source_scan.values())
    scan_summary = (f'Optional filename inspection covers {len(source_scan):,} selected repositories, '
                    f'with {len(exports["source-file-matches.csv"]):,} having requested-term filename evidence; '
                    f'{scan_truncated:,} report truncated trees and {scan_errors:,} report inspection errors. '
                    'This selected inventory is not a source/proof audit or exhaustive repository content search. '
                    'Inspections do not increase the count of curated source assessments. '
                    'The source-scan.json raw artifact preserves inventory scope and per-repository errors.')
    analysis = {
        'report_date': '2026-11-01', 'generated_at': dt.datetime.now(dt.timezone.utc).isoformat(),
        'initial_search_total': initial, 'census_unique_count': len(census),
        'census_difference_from_initial': difference,
        'all_collected_unique_count': coverage.get('all_collected_unique_count'),
        'active_census_count': len(active_census),
        'github_indexed_recent_query_total': indexed_active['total_count'] if indexed_active else None,
        'github_indexed_top100_agrees_with_metadata': ranking_same if indexed_active else None,
        'archived_census_count': sum(row['archived'] is True for row in census_rows),
        'fork_census_count': sum(row['fork'] is True for row in census_rows),
        'requested_keywords': REQUESTED, 'supplemental_keywords': SUPPLEMENTAL,
        'requested_keyword_counts': keyword_counts, 'requested_union_count': len(requested_rows),
        'active_requested_union_count': len(exports['active-keyword-matches.csv']),
        'candidate_count': len(candidates),
        'source_reviewed_candidate_count': sum(row['source_reviewed'] for row in candidates),
        'source_code_inspected_candidate_count': sum(row['proof_status'] == 'source_inspected'
                                                    for row in candidates),
        'documentation_only_candidate_count': sum(row['proof_status'] == 'documentation_only'
                                                  for row in candidates),
        'curated_candidates_outside_original_top100': sum(row['source_reviewed'] and row['full_name'] not in
            {repo['full_name'] for repo in original['items']} for row in candidates),
        'filename_inspected_count': len(source_scan),
        'filename_match_count': len(exports['source-file-matches.csv']),
        'filename_only_candidate_count': sum(bool(row['file_keyword_matches']) and
                                             not row['query_keyword_matches'] for row in candidates),
        'filename_inspection_truncated_count': scan_truncated,
        'filename_inspection_error_repository_count': scan_errors,
        'filename_inspection_kind_counts': dict(Counter(scan.get('inspection_kind', 'unspecified')
                                                        for scan in source_scan.values())),
        'filename_inspection_summary': scan_summary,
        'filename_inspection_source': 'source-scan.json' if source_scan else None,
        'candidate_category_counts': dict(Counter(row['category'] for row in candidates)),
        'candidate_priority_counts': dict(Counter(str(row['priority']) for row in candidates)),
        'older_or_inactive_curated_count': sum(row['source_reviewed'] and row['active_2months'] is not True
                                               for row in candidates),
        'curated_files': curated_files, 'recent_activity_threshold': CUTOFF,
        'activity_filter_thresholds': {'2_months': CUTOFF, '6_months': '2026-05-01T00:00:00Z',
                                       '12_months': '2025-11-01T00:00:00Z', 'all_dates': None},
        'activity_definition': coverage.get('activity_definition'),
        'completed_leaf_count': len(leaves), 'leaf_counts_reconcile': balanced,
        'documented_count_discrepancy_leaves': len(discrepancies),
        'all_count_differences_documented': differences_documented,
        'count_only_census_matches': census_count_only,
        'all_leaves_below_search_cap': under_limit, 'collector_errors': coverage.get('errors', []),
        'collection_started_at': coverage.get('started_at'),
        'collection_finished_at': coverage.get('finished_at'),
        'export_counts': {name: len(value) for name, value in exports.items()},
        'coverage_summary': summary, 'method_summary': method,
        'screening_limitations': [
            'A language:lean search is not a census of every repository containing a Lean file.',
            'GitHub indexed keyword matches do not establish implementation or proof capability.',
            'Filename inspection covers selected repositories only; suffixes do not establish code semantics.',
            '.v is ambiguous between Coq and Verilog and is never promoted to a Verilog filename match.',
            'Curated source assessments are scoped to their cited evidence, not exhaustive certification.',
            'Supplemental or curated leads can fall outside the original live census snapshot.',
            'Active means nonarchived and pushed since cutoff, not substantive default-branch commits.',
        ],
    }
    for filename, values in exports.items():
        write_csv(directory / filename, values)
    save_json(directory / 'analysis.json', analysis)
    rendered = HTML.replace('__DATA__', script_json({'analysis': analysis, 'rows': candidates}))
    (directory / 'report.html').write_text(rendered, encoding='utf-8')
    print(json.dumps({'exports': analysis['export_counts'], 'report': str(directory / 'report.html')}, indent=2))


if __name__ == '__main__':
    main()
