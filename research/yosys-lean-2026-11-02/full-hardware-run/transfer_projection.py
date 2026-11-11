#!/usr/bin/env python3
"""Copy the pinned serializer's exact transfer prefix, retaining its full export.

The upstream importer discards declare-const/assert roots and restores initial
constraints from JSON. The native serializer puts that tail after its sole
transfer function. Fail closed if that expected marker/ordering is absent.
No bytes inside a declaration or transfer function are rewritten.
"""

import hashlib
from pathlib import Path


def project(source: Path, target: Path, top: str):
    before = source.stat()
    marker = ('(declare-const ' + top + '-initial ').encode()
    declarations = functions = copied = 0
    digest = hashlib.sha256()
    found = False
    with source.open('rb') as incoming, target.open('wb') as outgoing:
        for line in incoming:
            if line.startswith(b'(declare-const '):
                if not line.startswith(marker) or functions != 1:
                    raise ValueError('Unexpected serializer initialization boundary')
                found = True
                break
            if line.startswith(b'(assert'):
                raise ValueError('Initialization assertion precedes the expected boundary')
            declarations += line.startswith(b'(declare-datatype ')
            functions += bool(line.split()) and line.split()[0] == b'(define-fun'
            outgoing.write(line)
            digest.update(line)
            copied += len(line)
    after = source.stat()
    if not found or declarations < 3 or functions != 1:
        raise ValueError('Expected one complete native transfer and record declarations')
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError('Native serialization changed while projecting')
    return {'full_export': str(source), 'full_bytes': before.st_size,
            'transfer': str(target), 'transfer_bytes': copied,
            'transfer_sha256': digest.hexdigest(), 'record_declarations': declarations,
            'functions': functions, 'projection': 'exact prefix before native initializer roots',
            'initialization_source': 'unchanged upstream JSON known-bit constraints'}
