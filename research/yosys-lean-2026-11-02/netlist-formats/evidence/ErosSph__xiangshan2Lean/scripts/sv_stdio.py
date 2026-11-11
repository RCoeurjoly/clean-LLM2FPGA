"""Narrow compatibility for fixed stdout/stderr $fwrite observations.

The source copy is retained. Only static standard streams and literal formats
are adapted to supported $write cells; a reserved format tag carries the exact
file descriptor through Yosys. The effect bridge removes it from observations.
Comments, strings and escaped identifiers are scanned rather than regex-edited.
"""
from __future__ import annotations

import hashlib
import re

MARKER_PREFIX = '__C2L_STDIO_'
STREAMS = {0x80000001: 'stdout', 0x80000002: 'stderr', 1: 'stdout'}
NUMBER = re.compile(r"(?:(\d[\d_]*)'([sS]?)([bBoOdDhH])([0-9a-fA-F_xXzZ?]+)|(\d[\d_]*))")


def normalize_stdio(text, source):
    if '$fwrite' not in text:
        return text, []
    if MARKER_PREFIX in text:
        raise ValueError(source + ': reserved stdio marker occurs in source')
    changes, events = [], []
    length = len(text)
    fingerprint = hashlib.sha256(text.encode('utf-8')).hexdigest()[:16]
    def error(pos, message):
        raise ValueError(f'{source}:{text.count(chr(10), 0, pos) + 1}: {message}')
    def trivia(pos):
        while pos < length:
            if text[pos].isspace():
                pos += 1
            elif text.startswith('//', pos):
                end = text.find('\n', pos + 2)
                pos = length if end < 0 else end
            elif text.startswith('/*', pos):
                end = text.find('*/', pos + 2)
                if end < 0:
                    error(pos, 'Unterminated comment')
                pos = end + 2
            else:
                break
        return pos
    def quoted(pos):
        end = pos + 1
        while end < length:
            if text[end] == '\\':
                end += 2
            elif text[end] == '"':
                return end + 1
            else:
                end += 1
        error(pos, 'Unterminated string literal')
    pos = 0
    while pos < length:
        next_pos = trivia(pos)
        if next_pos != pos:
            pos = next_pos
            continue
        if text[pos] == '"':
            pos = quoted(pos)
            continue
        if text[pos] == '\\':
            pos += 1
            while pos < length and not text[pos].isspace():
                pos += 1
            continue
        if text[pos] == '$' and (pos == 0 or not (text[pos - 1].isalnum() or text[pos - 1] in '_$')) and text.startswith('$fwrite', pos) and (pos + 7 == length or
                not (text[pos + 7].isalnum() or text[pos + 7] in '_$')):
            opening = trivia(pos + 7)
            if opening >= length or text[opening] != '(':
                error(pos, '$fwrite compatibility requires a call')
            fd_start = trivia(opening + 1)
            number = NUMBER.match(text, fd_start)
            if not number:
                error(pos, '$fwrite requires a static standard-stream descriptor')
            try:
                if number[5] is not None:
                    descriptor = int(number[5].replace('_', ''))
                else:
                    if int(number[1].replace('_', '')) != 32:
                        error(pos, '$fwrite compatibility requires a 32-bit descriptor')
                    descriptor = int(number[4].replace('_', ''), {'b': 2, 'o': 8, 'd': 10, 'h': 16}[number[3].lower()])
            except ValueError:
                error(pos, '$fwrite descriptor must be a known 32-bit constant')
            if descriptor not in STREAMS:
                error(pos, '$fwrite descriptor is not a supported standard stream')
            comma = trivia(number.end())
            if comma >= length or text[comma] != ',':
                error(pos, '$fwrite descriptor must be a single literal')
            fmt = trivia(comma + 1)
            if fmt >= length or text[fmt] != '"':
                error(pos, '$fwrite compatibility requires a literal format string')
            fmt_end = quoted(fmt)
            marker = MARKER_PREFIX + fingerprint + '_' + str(len(events)) + '__'
            changes += [(pos, pos + 7, '$write '),
                        (fd_start, comma + 1, ''.join(c if c in '\r\n' else ' ' for c in text[fd_start:comma + 1])),
                        (fmt + 1, fmt + 1, marker)]
            events.append({'marker': marker, 'file_descriptor': descriptor, 'stream': STREAMS[descriptor],
                           'source': source, 'line': text.count('\n', 0, pos) + 1,
                           'original_format_literal': text[fmt:fmt_end]})
            pos += 7
        else:
            pos += 1
    pieces, previous = [], 0
    for start, end, replacement in sorted(changes):
        if start < previous:
            raise ValueError('Overlapping stdio compatibility edits')
        pieces += [text[previous:start], replacement]
        previous = end
    pieces.append(text[previous:])
    return ''.join(pieces), events


def restore_stdio(effect, markers):
    if effect['kind'] != 'print' or not effect['format'].startswith(MARKER_PREFIX):
        return
    matches = [marker for marker in markers if effect['format'].startswith(marker)]
    if len(matches) != 1:
        raise ValueError('Cannot recover standard-stream observation from Yosys format tag')
    marker = matches[0]
    event = markers[marker]
    effect['format'] = effect['format'][len(marker):]
    effect['file_descriptor'] = event['file_descriptor']
    effect['stream'] = event['stream']
    effect['original_format_literal'] = event['original_format_literal']
