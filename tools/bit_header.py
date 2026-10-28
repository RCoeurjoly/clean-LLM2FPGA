"""Freeze only the two timestamp fields in the Xilinx .bit container header."""
from pathlib import Path
import hashlib

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
    path=Path(path);before=path.read_bytes();after=normalized(before)
    raw=path.with_name('model.wallclock.bit')
    if raw.exists():raise ValueError('Timestamp normalization already performed')
    raw.write_bytes(before);path.write_bytes(after)
    p=fields(before);start,end=p['e']
    return dict(scope='Xilinx container timestamp only; configuration payload unchanged',
                old_date=before[slice(*p['c'])].rstrip(b'\0').decode('ascii'),
                old_time=before[slice(*p['d'])].rstrip(b'\0').decode('ascii'),
                fixed_date=DATE.rstrip(b'\0').decode(),fixed_time=TIME.rstrip(b'\0').decode(),
                payload_sha256=hashlib.sha256(before[start:end]).hexdigest())
