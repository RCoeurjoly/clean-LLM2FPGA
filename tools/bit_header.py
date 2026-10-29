"""Freeze only the two timestamp fields in the Xilinx .bit container header."""
from pathlib import Path

DATE=b'2026/10/28\x00'
TIME=b'10:02:51\x00'

def fields(data):
    def integer(position,width):
        if position+width>len(data):raise ValueError('Truncated bit header')
        return int.from_bytes(data[position:position+width],'big')
    n=integer(0,2)
    if data[2:2+n]!=bytes.fromhex('0ff00ff00ff00ff000'):raise ValueError('Wrong bit header magic')
    pos=2+n
    if integer(pos,2)!=1:raise ValueError('Wrong first field marker')
    pos+=2;result={}
    for tag in b'abcde':
        if pos>=len(data) or data[pos]!=tag:raise ValueError('Unexpected header tag')
        pos+=1;width=4 if tag==ord('e') else 2;n=integer(pos,width);pos+=width
        if pos+n>len(data):raise ValueError('Truncated bit field')
        result[chr(tag)]=(pos,pos+n);pos+=n
    if pos!=len(data):raise ValueError('Unexpected trailing bit data')
    return result

def normalized(data):
    positions=fields(data);out=bytearray(data)
    for tag,value in [('c',DATE),('d',TIME)]:
        start,end=positions[tag]
        if end-start!=len(value):raise ValueError('Unexpected timestamp field size')
        out[start:end]=value
    start,end=positions['e']
    assert bytes(out[start:end])==data[start:end]
    return bytes(out)

def normalize_file(path):
    path = Path(path)
    path.write_bytes(normalized(path.read_bytes()))
