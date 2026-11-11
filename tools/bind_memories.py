import json
from pathlib import Path
import re
import struct
import sys


def image_bits(value, format):
    width = format['width']
    if format['numeric_type'] == 'ieee754_float':
        return int.from_bytes(struct.pack('<' + {32: 'f', 64: 'd'}[width], value), 'little')
    if format['numeric_type'] != 'bitnum':
        raise ValueError('unsupported memory format')
    return int(value) & ((1 << width) - 1)


def bind(program, images, output):
    memories = re.findall(r'@external\(1\) (mem_\d+) = seq_mem_d1\((\d+), (\d+), (\d+)\)',
                          program)
    memories = {n: tuple(map(int, (w, s, a))) for n, w, s, a in memories}
    if memories.get('mem_0') != (64, 32, 5) or memories.get('mem_1') != (32, 1608224, 21):
        raise ValueError('expected TinyStories token and logits buffers')
    if set(memories) != set(images):
        raise ValueError('memory image/cell mismatch')
    output.mkdir(parents=True, exist_ok=True)
    (output / 'images').mkdir(exist_ok=True)
    body, ports = [], ['.clk(clk)', '.reset(reset)', '.go(go)', '.done(done)']
    for name, (width, size, address) in memories.items():
        image = images[name]
        if image['format']['width'] != width or len(image['data']) != size:
            raise ValueError(f'{name}: image size or width mismatch')
        path = (output / 'images' / f'{name}.hex').resolve()
        with path.open('w') as stream:
            for value in image['data']:
                stream.write(f"{image_bits(value, image['format']):0{(width+3)//4}x}\n")
        signals = {'clk': 1, 'reset': 1, 'addr0': address, 'content_en': 1,
                   'write_en': 1, 'write_data': width, 'read_data': width, 'done': 1}
        for signal, bits in signals.items():
            body.append(f'wire [{bits-1}:0] {name}_{signal};')
            ports.append(f'.{name}_{signal}({name}_{signal})')
        connections = {s: f'{name}_{s}' for s in signals}
        connections.update(clk='clk', reset='reset')
        if name == 'mem_0':
            connections.update(addr0='tokens_write && !go ? tokens_address : mem_0_addr0',
                content_en='mem_0_content_en || (tokens_write && !go)',
                write_en='mem_0_write_en || (tokens_write && !go)',
                write_data='tokens_write && !go ? tokens_data : mem_0_write_data')
        if name == 'mem_1':
            connections.update(addr0='go ? mem_1_addr0 : logits_address',
                content_en='go ? mem_1_content_en : logits_read',
                write_en='go && mem_1_write_en')
        args = ','.join(f'.{key}({value})' for key, value in connections.items())
        body.append(f'model_memory #({width},{size},{address},"{path}") {name}({args});')
    (output / 'model.sv').write_text('\n'.join(['''module model_top(
input clk,reset,go,tokens_write,logits_read,
input [4:0] tokens_address,input [63:0] tokens_data,
input [20:0] logits_address,output [31:0] logits_data,
output reg logits_valid,output done);
always @(posedge clk) logits_valid<=!reset && !go && logits_read;
assign logits_data=mem_1_read_data;''', *body,
        'main kernel(' + ','.join(ports) + ');', 'endmodule']))
    bits = sum(width * size for width, size, _ in memories.values())
    print(f'{len(memories)} memories; {bits} bits; {bits/8/2**20:.3f} MiB allocated')


if __name__ == '__main__':
    bind(Path(sys.argv[1]).read_text(), json.loads(Path(sys.argv[2]).read_text()),
         Path(sys.argv[3]))
