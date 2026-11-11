"""Private v9 observational diagnostics, sampled at each process activation.

Only integral payloads and literal formats are admitted. Formatting is performed
by the native simulator in the verification bench, never by the hardware backend.
"""
import re


def format_plan(text,count):
    specs=[];i=0
    while i<len(text):
        if text[i]!='%':i+=1;continue
        m=re.match(r'%(?:[0-9]+)?[bdhxo%]',text[i:])
        if not m:raise ValueError('unsupported diagnostic format at '+repr(text[i:]))
        token=m.group();i+=len(token)
        if token!='%%':specs.append(token)
    if len(specs)!=count:raise ValueError('diagnostic format argument count mismatch')
    return specs


def action(statement,kind,fail,integer,scope):
    assertion=kind(statement)=='ImmediateAssertion'
    node=statement
    if assertion:
        if (str(statement.assertionKind).split('.')[-1]!='Assert' or statement.isDeferred or statement.isFinal or statement.ifTrue is not None and kind(statement.ifTrue)!='Empty'):
            fail(statement,'v9 requires immediate assert and empty success action')
        node=statement.ifFalse
        if node is None or kind(node)!='ExpressionStatement':fail(statement,'v9 requires a direct diagnostic failure action')
    if kind(node)!='ExpressionStatement' or kind(node.expr)!='Call':fail(node,'v9 direct diagnostic call required')
    call=node.expr
    if not call.isSystemCall or call.subroutineName not in ('$error','$fwrite'):fail(call,'v9 supports only nonterminating $error and stderr $fwrite diagnostics')
    args=list(call.arguments);stream=None
    if call.subroutineName=='$fwrite':
        if assertion:fail(call,'assert failure action must use $error')
        if not args or integer(args[0],scope)!=0x80000002:fail(call,'only fixed stderr diagnostic descriptor supported')
        stream=0x80000002;args=args[1:]
    if not args or kind(args[0])!='StringLiteral':fail(call,'literal diagnostic format required')
    fmt=args[0].value;payload=args[1:]
    if stream is not None and (not fmt.endswith('\n') or '\n' in fmt[:-1]):fail(call,'stderr diagnostics require one complete literal-format line')
    try:specs=format_plan(fmt,len(payload))
    except ValueError as e:fail(call,str(e))
    for a in payload:
        if not a.type.isIntegral or a.type.bitWidth<1:fail(a,'fixed integral diagnostic payload required')
    return dict(call=call,condition=statement.cond if assertion else None,format=fmt,payload=payload,specifiers=specs,
        action='assert' if assertion else 'call',severity='error' if call.subroutineName=='$error' else 'write',stream=stream)
