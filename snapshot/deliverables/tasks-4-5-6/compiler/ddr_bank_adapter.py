"""Lower typed external-memory contracts to a CIRCT HW bank join.

Only board wiring is constructed: the captured graph core remains an external
module. MLIR operations own the connections; native CIRCT verifies and emits
RTL downstream. No generated computation source or expected answers are read.
The bank uses the core domain; domain qualification and controller/CDC/image
loading are separate mandatory integration obligations.
"""
import re


def lower_ddr_bank_adapter(interface, layout, *, load_images=False, controller=None):
    from torch_mlir import ir
    if type(load_images) is not bool:
        raise ValueError('image loading policy must be boolean')
    if interface.get('schema_version')!=1 or layout.get('schema_version')!=1:
        raise ValueError('invalid interface/layout schema')
    top=interface['entrypoint']
    identifier=lambda s: isinstance(s,str) and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*',s)
    if not identifier(top) or top in ('closure_ddr_read_bank','graph_ddr_bank'):
        raise ValueError('invalid or conflicting core symbol')
    width,address,frames=(layout[k] for k in ('data_width','address_width','frames'))
    if (any(type(v) is not int for v in (width,address,frames)) or width<64 or width%64 or
            not 1<=address<=32 or not 1<=frames<=2**address or frames*(width//64)>2**32-1 or
            layout['slot_width']!=64 or layout['words_per_frame']!=width//64):
        raise ValueError('invalid controller geometry')
    clients=[m for m in interface['memories'] if m['implementation']=='ddr3']
    records=layout['memories']
    key=lambda m:(m['component'],m['cell'])
    if (not clients or len({key(m) for m in clients})!=len(clients) or
            len({key(m) for m in records})!=len(records) or {key(m) for m in clients}!={key(m) for m in records}):
        raise ValueError('interface/layout DDR client set differs')
    by_key={key(m):m for m in clients};clients=[by_key[key(m)] for m in records]
    expected={'addr0':('output',None),'clk':('output',1),'reset':('output',1),
              'content_en':('output',1),'write_en':('output',1),'write_data':('output',64),
              'read_data':('input',64),'done':('input',1)}
    allocated=[]
    for memory,record in zip(clients,records,strict=True):
        if memory['writable'] or memory['width']!=64 or memory['read_latency']!=0:
            raise ValueError('bank requires immutable full-width dynamic memory')
        depth,aw=memory['depth'],memory['address_width'];base=record['base_frame'];count=record['frame_count']
        if (memory['component']!=top or not identifier(memory['cell']) or
                any(type(v) is not int for v in (depth,aw,base,count)) or not 1<=aw<=32 or
                not 1<=depth<=min(2**aw,2**32-1) or record['width']!=64 or record['depth']!=depth or
                count!=(depth+width//64-1)//(width//64) or base<0 or base+count>frames):
            raise ValueError('DDR client geometry mismatch')
        if any(base<end and start<base+count for start,end in allocated):
            raise ValueError('overlapping DDR client geometry')
        allocated.append((base,base+count))
        ports=memory['ports']
        if len(ports)!=8 or {p['role'] for p in ports}!=set(expected):
            raise ValueError('invalid DDR memory port roles')
        for port in ports:
            direction,bits=expected[port['role']]
            if port['direction']!=direction or port['width']!=(aw if bits is None else bits):
                raise ValueError('DDR memory port geometry mismatch')
    core_ports=list(interface['ports'])+[p for m in interface['memories'] for p in m['ports']]
    if any(not identifier(p['name']) or p['direction'] not in ('input','output') or
           type(p['width']) is not int or p['width']<1 for p in core_ports) or len({p['name'] for p in core_ports})!=len(core_ports):
        raise ValueError('invalid/duplicate core ports')
    for role in ('clk','reset'):
        if {'name':role,'width':1,'direction':'input'} not in interface['ports']:
            raise ValueError('core requires scalar '+role+' input')
    ddr_names={p['name'] for m in clients for p in m['ports']}
    passthrough=[p for p in core_ports if p['name'] not in ddr_names]
    n=len(clients)
    bank_ports=[dict(name=name,width=bits,direction=direction) for name,bits,direction in (
        ('clk',1,'input'),('reset',1,'input'),('memory_ready',1,'input'),
        ('content_en',n,'input'),('write_en',n,'input'),('addr',n*32,'input'),
        ('read_data',n*64,'output'),('done',n,'output'),('fault',1,'output'),
        ('cmd_valid',1,'output'),('cmd_ready',1,'input'),('cmd_we',1,'output'),
        ('cmd_addr',address,'output'),('rdata_valid',1,'input'),('rdata_ready',1,'output'),('rdata',width,'input'))]
    loaded_ports=[dict(name=name,width=bits,direction=direction) for name,bits,direction in (
        ('core_clk',1,'input'),('ui_clk',1,'input'),('reset',1,'input'),('controller_ready',1,'input'),
        ('input_valid',1,'input'),('input_ready',1,'output'),('input_data',width,'input'),
        ('frame_index',address,'output'),('ui_loaded',1,'output'),('ui_load_fault',1,'output'),
        ('core_fault',1,'output'),('core_memory_ready',1,'output'),
        ('core_cmd_valid',1,'input'),('core_cmd_ready',1,'output'),('core_cmd_we',1,'input'),
        ('core_cmd_addr',address,'input'),('core_rdata_valid',1,'output'),('core_rdata_ready',1,'input'),
        ('core_rdata',width,'output'),('cmd_valid',1,'output'),('cmd_ready',1,'input'),
        ('cmd_we',1,'output'),('cmd_addr',address,'output'),('wdata_valid',1,'output'),
        ('wdata_ready',1,'input'),('wdata',width,'output'),('wdata_we',width//8,'output'),
        ('rdata_valid',1,'input'),('rdata_ready',1,'output'),('rdata',width,'input'))]
    internal_loaded={'core_clk','reset','core_cmd_valid','core_cmd_ready','core_cmd_we',
                     'core_cmd_addr','core_rdata_valid','core_rdata_ready','core_rdata'}
    ui=([p for p in bank_ports if p['name']=='fault']+
        [p for p in loaded_ports if p['name'] not in internal_loaded]) if load_images else [
        p for p in bank_ports if p['name'] not in ('clk','reset','content_en','write_en','addr','read_data','done')]
    controller_ports=[]
    controller_links={}
    if controller is not None:
        if not load_images:
            raise ValueError('controller requires loaded transport')
        if (controller.get('schema_version')!=1 or controller.get('data_width')!=width or
                controller.get('address_width')!=address or
                controller.get('physical_data_width',0)*8!=width):
            raise ValueError('controller geometry differs from DDR layout')
        controller_name=controller.get('module')
        if not identifier(controller_name) or controller_name in (top,'closure_ddr_read_bank','closure_ddr_loaded_port','graph_ddr_bank'):
            raise ValueError('invalid controller symbol')
        controller_ports=controller['ports']
        if (any(not identifier(p['name']) or p['direction'] not in ('input','output','inout') or
                type(p['width']) is not int or p['width']<1 for p in controller_ports) or
                len({p['name'] for p in controller_ports})!=len(controller_ports)):
            raise ValueError('invalid controller ports')
        declared={p['name']:p for p in controller_ports}
        requirements={name:(1,direction) for name,direction in (
            ('clk','input'),('rst','input'),('user_clk','output'),('user_rst','output'),
            ('init_done','output'),('init_error','output'),('pll_locked','output'))}
        for port in loaded_ports:
            role=port['name']
            if role in ('cmd_valid','cmd_ready','cmd_we','cmd_addr','wdata_valid','wdata_ready','wdata','wdata_we','rdata_valid','rdata_ready','rdata'):
                suffix=role+'_data' if role in ('wdata','rdata') else role
                name='user_port_native_0_'+suffix
                controller_links[role]=name
                requirements[name]=(port['width'],'input' if port['direction']=='output' else 'output')
        for name,(bits,direction) in requirements.items():
            if declared.get(name)!={'name':name,'width':bits,'direction':direction}:
                raise ValueError('controller port contract mismatch: '+name)
        internal_controller={'clk','rst',*controller_links.values()}
        ui=[p for p in ui if p['name'] not in {'ui_clk','controller_ready',*controller_links}]
        ui += [dict(name='ddr_ref_clk',width=1,direction='input'),
               dict(name='phy_calibrated',width=1,direction='input')]
        ui += [p for p in controller_ports if p['name'] not in internal_controller]
    if {p['name'] for p in ui}&{p['name'] for p in passthrough}:
        raise ValueError('board/core passthrough port collision')
    wrapper_ports=passthrough+ui
    if len({p['name'] for p in wrapper_ports})!=len(wrapper_ports):
        raise ValueError('duplicate board passthrough ports')
    parameters=[('CLIENTS',32,n),('FRAME_DATA_WIDTH',32,width),('FRAME_ADDRESS_WIDTH',32,address),
                ('FRAME_COUNT',32,frames),('DEPTHS',32*n,sum(m['depth']<<(32*i) for i,m in enumerate(clients))),
                ('BASE_FRAMES',32*n,sum(m['base_frame']<<(32*i) for i,m in enumerate(records)))]
    with ir.Context() as context,ir.Location.unknown():
        # Torch-MLIR supplies the MLIR object API. Native CIRCT registers and
        # verifies HW/Comb dialects after serialization; unknown dialects here
        # never count as a verified boundary.
        context.allow_unregistered_dialects=True
        module=ir.Module.create()
        integer=lambda bits:ir.IntegerType.get_signless(bits)
        strings=lambda names:ir.ArrayAttr.get([ir.StringAttr.get(s) for s in names])
        attrs=lambda ports:ir.TypeAttr.get(ir.Type.parse('!hw.modty<'+', '.join(
            p['direction']+' '+p['name']+' : i'+str(p['width']) for p in ports)+'>'))
        param_attrs=ir.ArrayAttr.get([ir.Attribute.parse(f'#hw.param.decl<"{name}": i{bits} = {value}> : i{bits}') for name,bits,value in parameters])
        param_declarations=ir.ArrayAttr.get([ir.Attribute.parse(f'#hw.param.decl<"{name}": i{bits}> : i{bits}') for name,bits,value in parameters])
        loaded_parameters=[p for p in parameters if p[0] in ('FRAME_DATA_WIDTH','FRAME_ADDRESS_WIDTH','FRAME_COUNT')]
        loaded_params=ir.ArrayAttr.get([ir.Attribute.parse(f'#hw.param.decl<"{name}": i{bits} = {value}> : i{bits}') for name,bits,value in loaded_parameters])
        loaded_declarations=ir.ArrayAttr.get([ir.Attribute.parse(f'#hw.param.decl<"{name}": i{bits}> : i{bits}') for name,bits,value in loaded_parameters])
        empty=ir.ArrayAttr.get([])
        def declaration(name,ports,params):
            return ir.Operation.create('hw.module.extern',attributes={'sym_name':ir.StringAttr.get(name),
                'module_type':attrs(ports),'parameters':params,'port_locs':ir.ArrayAttr.get([ir.Attribute.parse('loc(unknown)') for p in ports])},regions=1)
        with ir.InsertionPoint(module.body):
            declaration(top,core_ports,empty)
            declaration('closure_ddr_read_bank',bank_ports,param_declarations)
            if load_images:
                declaration('closure_ddr_loaded_port',loaded_ports,loaded_declarations)
            if controller is not None:
                declaration(controller_name,controller_ports,empty)
            wrapper=ir.Operation.create('hw.module',attributes={'sym_name':ir.StringAttr.get('graph_ddr_bank'),
                'module_type':attrs(wrapper_ports),'parameters':empty,
                'result_locs':ir.ArrayAttr.get([ir.Attribute.parse('loc(unknown)') for p in wrapper_ports if p['direction']=='output'])},regions=1)
        inputs=[p for p in wrapper_ports if p['direction']!='output']
        block=wrapper.regions[0].blocks.append(*[ir.Type.parse('!hw.inout<i'+str(p['width'])+'>')
              if p['direction']=='inout' else integer(p['width']) for p in inputs])
        values={p['name']:v for p,v in zip(inputs,block.arguments,strict=True)}
        def op(name,operands,bits,attributes=None):
            return ir.Operation.create(name,operands=operands,results=[integer(bits)],attributes=attributes or {}).result
        def instance(name,symbol,ports,arguments,params):
            incoming=[p for p in ports if p['direction']!='output'];outgoing=[p for p in ports if p['direction']=='output']
            instance=ir.Operation.create('hw.instance',operands=[arguments[p['name']] for p in incoming],
                results=[integer(p['width']) for p in outgoing],attributes={'instanceName':ir.StringAttr.get(name),
                'moduleName':ir.FlatSymbolRefAttr.get(symbol),'argNames':strings([p['name'] for p in incoming]),
                'resultNames':strings([p['name'] for p in outgoing]),'parameters':params})
            return instance,{p['name']:v for p,v in zip(outgoing,instance.results,strict=True)}
        with ir.InsertionPoint(block):
            controller_values={}
            controller_placeholders={}
            if controller is not None:
                controller_arguments={p['name']:values[p['name']] for p in controller_ports
                                      if p['direction']!='output' and p['name'] not in internal_controller}
                controller_arguments.update(clk=values['ddr_ref_clk'],rst=values['reset'])
                for port in controller_ports:
                    if port['direction']=='input' and port['name'] in controller_links.values():
                        placeholder=ir.Operation.create('builtin.unrealized_conversion_cast',results=[integer(port['width'])])
                        controller_placeholders[port['name']]=placeholder
                        controller_arguments[port['name']]=placeholder.result
                controller_op,controller_values=instance('controller',controller_name,controller_ports,controller_arguments,empty)
                one=op('hw.constant',[],1,{'value':ir.IntegerAttr.get(integer(1),1)})
                not_locked=op('comb.xor',[controller_values['pll_locked'],one],1)
                not_error=op('comb.xor',[controller_values['init_error'],one],1)
                not_ui_reset=op('comb.xor',[controller_values['user_rst'],one],1)
                # One graph/transport reset, derived from the controller domain.
                # External reset also resets the controller and must drain it.
                values['reset']=op('comb.or',[values['reset'],controller_values['user_rst'],not_locked],1)
                values['ui_clk']=controller_values['user_clk']
                values['controller_ready']=op('comb.and',[controller_values['init_done'],not_error,
                    controller_values['pll_locked'],not_ui_reset,values['phy_calibrated']],1)
                for role,name in controller_links.items():
                    if name in controller_values:values[role]=controller_values[name]
            placeholders={}
            for memory in clients:
                for port in memory['ports']:
                    if port['direction']=='input':
                        placeholder=ir.Operation.create('builtin.unrealized_conversion_cast',results=[integer(port['width'])])
                        placeholders[port['name']]=placeholder
                        values[port['name']]=placeholder.result
            core,core_values=instance('core',top,core_ports,values,empty)
            packed={}
            for role in ('content_en','write_en','addr0'):
                pieces=[]
                for memory in clients:
                    port=next(p for p in memory['ports'] if p['role']==role)
                    value=core_values[port['name']]
                    if role=='addr0' and port['width']<32:
                        pad=op('hw.constant',[],32-port['width'],{'value':ir.IntegerAttr.get(integer(32-port['width']),0)})
                        value=op('comb.concat',[pad,value],32)
                    pieces.append(value)
                packed[role]=pieces[0] if n==1 else op('comb.concat',list(reversed(pieces)),n*(32 if role=='addr0' else 1))
            bank_arguments={}
            transport_placeholders={}
            for port in bank_ports:
                if port['direction']!='input' or port['name'] in ('content_en','write_en','addr'):
                    continue
                if load_images and port['name'] in ('memory_ready','cmd_ready','rdata_valid','rdata'):
                    placeholder=ir.Operation.create('builtin.unrealized_conversion_cast',results=[integer(port['width'])])
                    transport_placeholders[port['name']]=placeholder
                    bank_arguments[port['name']]=placeholder.result
                else:
                    bank_arguments[port['name']]=values[port['name']]
            bank_arguments.update(content_en=packed['content_en'],write_en=packed['write_en'],addr=packed['addr0'])
            bank,bank_values=instance('bank','closure_ddr_read_bank',bank_ports,bank_arguments,param_attrs)
            loaded_values={}
            if load_images:
                loaded_arguments={p['name']:values[p['name']] for p in loaded_ports
                                  if p['direction']=='input' and p['name'] not in internal_loaded}
                loaded_arguments.update(core_clk=values['clk'],reset=values['reset'])
                for role in ('cmd_valid','cmd_we','cmd_addr','rdata_ready'):
                    loaded_arguments['core_'+role]=bank_values[role]
                loaded,loaded_values=instance('loaded_port','closure_ddr_loaded_port',loaded_ports,loaded_arguments,loaded_params)
                transport_roles={'memory_ready':'core_memory_ready','cmd_ready':'core_cmd_ready',
                                 'rdata_valid':'core_rdata_valid','rdata':'core_rdata'}
                for index,port in enumerate(p for p in bank_ports if p['direction']=='input'):
                    if port['name'] in transport_roles:
                        bank.operands[index]=loaded_values[transport_roles[port['name']]]
                for placeholder in transport_placeholders.values():placeholder.erase()
                if controller is not None:
                    reverse_links={name:role for role,name in controller_links.items()}
                    for index,port in enumerate(p for p in controller_ports if p['direction']!='output'):
                        if port['name'] in controller_placeholders:
                            controller_op.operands[index]=loaded_values[reverse_links[port['name']]]
                    for placeholder in controller_placeholders.values():placeholder.erase()
            replacements={}
            for index,memory in enumerate(clients):
                for role,bits in [('read_data',64),('done',1)]:
                    port=next(p for p in memory['ports'] if p['role']==role)
                    replacements[port['name']]=op('comb.extract',[bank_values[role]],bits,{'lowBit':ir.IntegerAttr.get(integer(32),bits*index)})
            for index,port in enumerate(p for p in core_ports if p['direction']=='input'):
                if port['name'] in replacements:core.operands[index]=replacements[port['name']]
            for placeholder in placeholders.values():placeholder.erase()
            public_outputs=dict(bank_values)
            public_outputs.update(loaded_values)
            public_outputs.update(core_values)
            public_outputs.update(controller_values)
            outputs=[public_outputs[p['name']] for p in wrapper_ports if p['direction']=='output']
            ir.Operation.create('hw.output',operands=outputs)
        return str(module)


def emit_ddr_bank_adapter_stage(interface,layout,output,tool,*,load_images=False,controller=None,timeout_seconds=120):
    """Authenticate metadata and native HW verification/emission boundaries."""
    import json
    from pathlib import Path
    from torch_mlir import ir
    from compiler.stages import StageSpec,digest,run_stage,verify_stage_receipt
    interface=Path(interface).resolve();layout=Path(layout).resolve();output=Path(output).resolve()
    output.mkdir(parents=True,exist_ok=True)
    stage=output/'emission'
    (stage/'success.json').unlink(missing_ok=True)
    controller=Path(controller).resolve() if controller is not None else None
    inputs=(interface,layout,*((controller,) if controller is not None else ()))
    if any(path.is_relative_to(output) for path in inputs):
        raise ValueError('adapter output overlaps input metadata')
    dependencies={str(path):digest(path) for path in
                  (*inputs,Path(__file__).resolve(),Path(ir.__file__).resolve())}
    raw=output/'adapter.mlir'
    raw.write_text(lower_ddr_bank_adapter(json.loads(interface.read_text()),json.loads(layout.read_text()),load_images=load_images,
        controller=json.loads(controller.read_text()) if controller is not None else None))
    dependencies[str(raw)]=digest(raw)
    specification=StageSpec('graph-ddr-bank-adapter',
        (str(tool.executable),'--verify-each','--export-split-verilog=dir-name=rtl',
         '-o','lowered.mlir',str(raw)),dependencies,tool.identities(),
        {'load_images':load_images,'controller_connected':controller is not None,
         'scope':('Board bank/CDC/loader/controller wiring; graph remains external; PHY calibration/physical acceptance open'
                  if controller is not None else 'Board bank with image readback and CDC; graph remains external; physical controller not connected'
                  if load_images else 'Core-domain board wiring only; graph remains external; CDC/controller not connected')},
        ('lowered.mlir','rtl/graph_ddr_bank.sv'),timeout_seconds)
    receipt=run_stage(specification,stage)
    verify_stage_receipt(stage)
    return receipt
