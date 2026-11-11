"""Bound generated record arity while retaining stable field identifiers."""

GROUP_SIZE = 128


def group_span(length, group_size=GROUP_SIZE):
    if group_size < 2:
        raise ValueError('Record group size must be at least two')
    span = group_size
    while (length + span - 1) // span > group_size:
        span *= group_size
    return span


def access_paths(names, group_size=GROUP_SIZE):
    names = list(names)
    if len(names) <= group_size:
        return {name: name for name in names}
    span = group_span(len(names), group_size)
    return {name: f'g{i // span}.' + path for i in range(0, len(names), span)
            for name, path in access_paths(names[i:i + span], group_size).items()}


def record_expression(bindings, group_size=GROUP_SIZE):
    bindings = list(bindings)
    if not bindings:
        return '⟨⟩'
    if len(bindings) > group_size:
        span = group_span(len(bindings), group_size)
        bindings = [(f'g{i // span}', record_expression(bindings[i:i + span], group_size))
                    for i in range(0, len(bindings), span)]
    return '{ ' + ', '.join(name + ' := ' + value for name, value in bindings) + ' }'


def record_structures(name, fields, group_size=GROUP_SIZE):
    fields = list(fields)
    lines = []
    if len(fields) > group_size:
        span = group_span(len(fields), group_size)
        groups = []
        for i in range(0, len(fields), span):
            subtype = name + 'Group' + str(i // span)
            lines += record_structures(subtype, fields[i:i + span], group_size)
            groups.append(('g' + str(i // span), subtype))
        fields = groups
    return lines + [f'structure {name} where'] + ['  ' + field + ' : ' + typ for field, typ in fields] + ['']


def balanced_and(conditions):
    conditions = list(conditions)
    if not conditions:
        return 'true'
    if len(conditions) <= GROUP_SIZE:
        return ' &&\n  '.join(conditions)
    middle = len(conditions) // 2
    return '(' + balanced_and(conditions[:middle]) + ') &&\n  (' + balanced_and(conditions[middle:]) + ')'


def record_update(value, path, expression):
    head, *tail = path.split('.')
    nested = record_update(value + '.' + head, '.'.join(tail), expression) if tail else expression
    return '{ ' + value + ' with ' + head + ' := ' + nested + ' }'
