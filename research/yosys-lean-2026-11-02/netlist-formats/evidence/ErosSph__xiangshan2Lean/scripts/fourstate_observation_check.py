"""Native four-state exhaustive selector check of the optional mux layout."""


def bench(mapping, flat, compact):
    def tree(text, prefix):
        lines=[line for line in text.splitlines() if (line.startswith('wire [') and (' observe_' in line or ' c2l_obs_select_' in line)) or line.startswith(('assign obs_','buf c2l_obs_buf_'))]
        return '\n'.join(line.replace('observe_',prefix+'_').replace('c2l_obs_select_',prefix+'_selector_').replace('c2l_obs_buf_',prefix+'_buffer_').replace('assign obs_tag=',f'assign {prefix}_tag=').replace('assign obs_value=',f'assign {prefix}_value=') for line in lines)
    ob=mapping['observation'];w=ob['width'];ow=ob['object_width'];iw=ob['index_width']
    lines=['module tb;',f'reg [{ow-1}:0] obs_object;',f'reg [{iw-1}:0] obs_index;',f'wire [{w-1}:0] a_tag,a_value,b_tag,b_value;',
           'integer fill,j,k,o,i;',
           "function symbol4;input integer c;begin case(c)0:symbol4=0;1:symbol4=1;2:symbol4=1'bx;3:symbol4=1'bz;endcase end endfunction"]
    seeds=[]
    for s in mapping['states']:
        for key in ('bank_tag','bank_value'):
            name=s[key];width=s['width'];m=s.get('memory')
            lines.append(f'reg [{width-1}:0] {name}'+(f' [0:{m["physical_depth"]-1}]' if m else '')+';')
            if m:seeds.append(f'for(j=0;j<{m["physical_depth"]};j=j+1)for(k=0;k<{width};k=k+1){name}[j][k]=symbol4((fill+j+k)%4);')
            else:seeds.append(f'for(k=0;k<{width};k=k+1){name}[k]=symbol4((fill+k)%4);')
    lines += [tree(flat,'a'),tree(compact,'b'),'initial begin for(fill=0;fill<4;fill=fill+1)begin',*seeds,
              f'for(o=0;o<{4**ow};o=o+1)begin for(k=0;k<{ow};k=k+1)obs_object[k]=symbol4((o>>(2*k))%4);',
              f'for(i=0;i<{4**iw};i=i+1)begin for(k=0;k<{iw};k=k+1)obs_index[k]=symbol4((i>>(2*k))%4);',
              '#1;if(a_tag!==b_tag || a_value!==b_value)$fatal(1,"Observation layout changed exact four-state values");end end end',
              f'$display("OBSERVATION_LAYOUT_PASS {4*4**ow*4**iw}");$finish;end endmodule']
    return '\n'.join(lines)+'\n'
