"""Allocate declared immutable input images in controller-native read frames.

One logical word occupies a full 64-bit slot. Every memory starts at a frame
boundary; there is no storage narrowing or image aliasing. Frame lane zero is
the low 64 bits, serialized first in little-endian order. The adapter must use
base_frame + address // words_per_frame and address % words_per_frame. This
module prepares inputs; it never reads generated RTL or expected answers.
"""
import json
from pathlib import Path
from compiler.stages import digest, _write_json
from scripts.pipeline.memory_policy import MemorySpec, validate_image


def materialize_ddr_images(manifest, output, *, data_width, address_width):
    output=Path(output).resolve();output.mkdir(parents=True,exist_ok=True)
    manifest=Path(manifest).resolve(strict=True)
    acceptance=output/'layout.json'
    if manifest in (acceptance, output/'images.bin', output/'images.bin.partial'):
        raise ValueError('DDR output and input manifest overlap')
    acceptance.unlink(missing_ok=True)
    if (type(data_width) is not int or data_width < 64 or data_width % 64 or
            type(address_width) is not int or address_width < 1):
        raise ValueError('invalid controller frame geometry')
    dependencies={str(manifest):digest(manifest),str(Path(__file__).resolve()):digest(Path(__file__).resolve())}
    document=json.loads(manifest.read_text())
    if document.get('schema_version') != 1 or not isinstance(document.get('bindings'),list):
        raise ValueError('invalid memory binding manifest')
    slots=data_width//64;frame_bytes=data_width//8;next_frame=0
    records=[];seen=set()
    for binding in document['bindings']:
        key=(binding['component'],binding['cell'])
        if key in seen:raise ValueError('duplicate memory binding')
        seen.add(key)
        if binding['implementation'] != 'ddr3':continue
        if binding['writable'] or binding['read_latency'] != 0:
            raise ValueError('DDR image binding requires immutable dynamic reads')
        if 'storage_width' in binding or 'sign_extend' in binding:
            raise ValueError('DDR image binding requires full-width storage')
        width,depth=binding['width'],binding['depth']
        if (type(width) is not int or width < 8 or width > 64 or width % 8 or
                type(depth) is not int or depth < 1):
            raise ValueError('unsupported logical image geometry')
        filename=binding.get('init_file')
        # Reference arguments have no physical image; their caller owns storage.
        if filename is None:continue
        if (not isinstance(filename,str) or not filename.endswith('.hex') or
                Path(filename).name != filename or '\\' in filename):
            raise ValueError('unsafe image binding')
        image=(manifest.parent/filename).resolve(strict=True)
        if image.parent != manifest.parent:raise ValueError('unsafe image binding')
        identity=digest(image)
        if identity != binding.get('init_sha256'):raise ValueError('image identity mismatch')
        validate_image(MemorySpec('image',width,depth,'bram',2,False,filename),manifest.parent)
        dependencies[str(image)]=identity
        frames=(depth+slots-1)//slots
        if next_frame+frames > 1 << address_width:raise ValueError('controller address capacity exceeded')
        records.append(dict(component=key[0],cell=key[1],width=width,depth=depth,
            base_frame=next_frame,frame_count=frames,input_file=str(image),input_sha256=identity))
        next_frame+=frames
    temporary=output/'images.bin.partial';image_output=output/'images.bin'
    try:
        with temporary.open('wb') as stream:
            for record in records:
                with Path(record['input_file']).open() as source:
                    for row in source:stream.write(int(row,16).to_bytes(8,'little'))
                stream.write(bytes((record['frame_count']*slots-record['depth'])*8))
        for path,identity in dependencies.items():
            if digest(Path(path)) != identity:raise ValueError('DDR input identity changed during materialization')
        temporary.replace(image_output)
    finally:
        temporary.unlink(missing_ok=True)
    result=dict(schema_version=1,data_width=data_width,address_width=address_width,
        slot_width=64,words_per_frame=slots,frame_bytes=frame_bytes,frames=next_frame,
        byte_order='little',lane_order='lowest-address-word-in-lowest-bits',
        memories=records,image_file='images.bin',image_sha256=digest(image_output),
        dependency_hashes=dependencies,
        scope='Declared DDR input layout only; controller loading and execution unverified')
    _write_json(acceptance,result)
    return result
