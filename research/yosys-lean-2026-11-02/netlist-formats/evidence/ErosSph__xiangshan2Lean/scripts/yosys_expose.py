"""Make explicit observation/input cuts using official Yosys passes only."""
from __future__ import annotations


def bulk_plan(top):
    """One traversal per pass, independent of the number of observation cells."""
    effects = 't:$print t:$assert t:$assume t:$cover'
    selected = effects + ' t:$anyseq'
    return '; '.join([
        'cd \\' + top,
        'select -assert-none w:c2l_effect_* c:c2l_effect_* w:c2l_undefined_* c:c2l_undefined_*',
        'rename -hide ' + selected,
        'rename -enumerate -pattern c2l_effect_% ' + effects,
        'rename -enumerate -pattern c2l_undefined_% t:$anyseq',
        # Keep the renamed cells' parameters and exact signal IDs for metadata.
        'write_json /work/named.json',
        'expose -evert -sep _ ' + selected,
        'delete w:c2l_effect_*_TRG',
        'delete w:c2l_effect_* s:0 %i',
        'cd ..', 'select \\' + top])


def cell_ids(cells):
    return {name: name if cell.get('hide_name', int(name.startswith('$'))) else '\\' + name
            for name, cell in cells.items() if cell['type'] in {'$print', '$assert', '$assume', '$cover', '$anyseq'}}


def write_plan(path, top, effects, arbitrary_cells, arbitrary, identifiers):
    def token(value):
        if not value or any(c.isspace() or ord(c) < 32 or c in ';"' for c in value):
            raise ValueError('Cannot safely name this RTLIL observation cell in a Yosys script: ' + repr(value))
        return value
    commands = ['cd ' + token('\\' + top),
                'select -assert-none w:c2l_bridge_* c:c2l_bridge_*']
    renames = []
    for index, effect in enumerate(effects):
        base = 'c2l_bridge_effect_' + str(index)
        commands.append('rename ' + token(identifiers[effect['cell']]) + ' \\' + base)
        for role, original_port in [('enable', 'EN'), ('condition', 'A'), ('arguments', 'ARGS')]:
            if role in effect['ports']:
                renames.append('rename \\' + base + '_' + original_port + ' \\' + effect['ports'][role])
    if len(arbitrary_cells) != len(arbitrary):
        raise ValueError('Arbitrary-value cell plan differs from exported input metadata')
    for index, (name, field) in enumerate(zip(arbitrary_cells, arbitrary)):
        base = 'c2l_bridge_undefined_' + str(index)
        commands.append('rename ' + token(identifiers[name]) + ' \\' + base)
        renames.append('rename \\' + base + '_Y \\' + field['name'])
    commands += ['expose -evert -sep _ c:c2l_bridge_effect_* c:c2l_bridge_undefined_*', *renames,
                 'delete w:c2l_bridge_effect_*_TRG', 'delete w:c2l_bridge_* s:0 %i', 'cd ..', 'select \\' + top]
    path.write_text('\n'.join(commands) + '\n', encoding='utf-8')
