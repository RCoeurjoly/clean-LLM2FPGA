"""Qualify exported memory domains from native elaborated wire identities.

This downstream check never modifies compiler output. It proves direct aliases
only; unrelated computation, CDC timing and physical clock routing are outside
its scope. Missing or nontrivial clock/reset connections fail conservatively.
"""
import json
from pathlib import Path
import re
from compiler.stages import StageSpec,digest,run_stage,verify_stage_receipt,_write_json


def qualify_external_clock_contract(source: Path, interface: Path, output: Path,
                                    yosys: Path, *, timeout_seconds=120):
    source,interface,output,yosys=(Path(p).resolve() for p in (source,interface,output,yosys))
    output.mkdir(parents=True,exist_ok=True)
    (output/'qualification.json').unlink(missing_ok=True)
    (output/'elaboration/success.json').unlink(missing_ok=True)
    contract=json.loads(interface.read_text())
    top=contract.get('entrypoint')
    if contract.get('schema_version')!=1 or not isinstance(top,str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*',top):
        raise ValueError('invalid typed interface entrypoint/schema')
    declared={port['name']:port for port in contract['ports']}
    for role in ('clk','reset'):
        if declared.get(role)!={'name':role,'width':1,'direction':'input'}:
            raise ValueError('core domain requires declared scalar input '+role)
    ports=[]
    for memory in contract['memories']:
        domains=[p for p in memory['ports'] if p['role'] in ('clk','reset')]
        if len(domains)!=2 or {p['role'] for p in domains}!={'clk','reset'}:
            raise ValueError('memory requires unique clock and reset ports')
        for port in domains:
            if port['width']!=1 or port['direction']!='output':
                raise ValueError('memory domain requires scalar output')
            ports.append((port['name'],port['role']))
    if not ports or len({name for name,role in ports})!=len(ports):
        raise ValueError('empty or duplicate exported memory domains')
    if any(c in str(source) for c in ('"','\\','\n','\r')):
        raise ValueError('source path is not representable in native Yosys command')
    dependencies={str(p):digest(p) for p in (source,interface,Path(__file__).resolve())}
    receipt=run_stage(StageSpec('external-domain-elaboration',
        (str(yosys),'-p',f'read_verilog -sv "{source}"; proc; write_json connectivity.json'),
        dependencies,{str(yosys):digest(yosys)},
        {'scope':'Top-level direct exported memory clock/reset aliases only'},
        ('connectivity.json',),timeout_seconds),output/'elaboration')
    verify_stage_receipt(output/'elaboration')
    design=json.loads((output/'elaboration/connectivity.json').read_text())
    module=design['modules'][top]
    native=module['ports']
    for role in ('clk','reset'):
        port=native.get(role,{})
        if port.get('direction')!='input' or len(port.get('bits',[]))!=1 or type(port['bits'][0]) is not int:
            raise ValueError('native core domain is not a scalar input '+role)
    if native['clk']['bits']==native['reset']['bits']:
        raise ValueError('native core clock and reset alias each other')
    for name,role in ports:
        port=native.get(name,{})
        if port.get('direction')!='output' or port.get('bits')!=native[role]['bits']:
            raise ValueError(name+' does not alias core '+role)
    result={'schema_version':1,'entrypoint':top,'method':'native Yosys net identity',
        'checked_memory_ports':len(ports),'elaboration_identity':receipt.identity_sha256,
        'input_hashes':dependencies,'scope':'Direct memory clock/reset aliases only; no computation or physical acceptance'}
    _write_json(output/'qualification.json',result)
    return result
