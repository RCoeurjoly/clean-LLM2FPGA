"""Compare every native assembled frame word with independently decoded bitstream data."""
import argparse
import json
from pathlib import Path
import re


def verify_frames(frames,decoded):
    expected={}
    for line in Path(frames).read_text().splitlines():
        if not line.strip():continue
        fields=line.split()
        if len(fields)!=2:raise ValueError('Malformed assembled frame')
        address=int(fields[0],16)
        words=[int(w,16) for w in fields[1].split(',')]
        if address in expected or not 0<=address<2**32 or len(words)!=101:
            raise ValueError('Duplicate address or invalid assembled XC7 frame')
        if any(not 0<=w<2**32 for w in words):raise ValueError('Invalid assembled frame word')
        expected[address]=words
    observed={};address=None
    for line in Path(decoded).read_text().splitlines():
        line=line.strip()
        if not line.strip():continue
        if line.startswith('.frame '):
            match=re.fullmatch(r'\.frame 0x([0-9a-fA-F]{1,8})',line)
            if not match:raise ValueError('Malformed decoded frame address')
            address=int(match[1],16)
            if address in observed:raise ValueError('Duplicate decoded frame')
            observed[address]=[]
        else:
            if address is None or not re.fullmatch(r'[0-9a-fA-F]{8}(?: [0-9a-fA-F]{8})*',line):
                raise ValueError('Malformed decoded frame words')
            observed[address].extend(int(w,16) for w in line.split())
    if not expected or not expected.keys()<=observed.keys():raise ValueError('Missing decoded frames')
    extra=observed.keys()-expected.keys()
    # The native bitstream writer fills unused part frames with zero words.
    # They must have the full frame width and may contain no configuration bits.
    if any(len(words)!=101 for words in observed.values()):raise ValueError('Invalid decoded XC7 frame width')
    if any(any(observed[a]) for a in extra):raise ValueError('Unrequested nonzero decoded frame')
    nonzero=setbits=0
    for address,words in expected.items():
        if observed[address]!=words:raise ValueError('Decoded frame payload differs at '+hex(address))
        nonzero+=sum(w!=0 for w in words);setbits+=sum(w.bit_count() for w in words)
    if nonzero==0:raise ValueError('All-zero bitstream configuration')
    return dict(frame_count=len(observed),assembled_frame_count=len(expected),zero_filled_frames=len(extra),word_count=len(observed)*101,
        nonzero_words=nonzero,set_configuration_bits=setbits,all_frame_words_match=True,
        hardware_programmed=False)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--frames',type=Path,required=True)
    parser.add_argument('--decoded',type=Path,required=True)
    args=parser.parse_args();print(json.dumps(verify_frames(args.frames,args.decoded)))


if __name__=='__main__':main()
