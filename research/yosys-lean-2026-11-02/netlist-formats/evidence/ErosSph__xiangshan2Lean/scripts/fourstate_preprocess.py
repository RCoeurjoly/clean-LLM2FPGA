"""Bounded official slang preprocessing, with physical source provenance.

This is a file admission policy, not a SystemVerilog preprocessor. slang alone
expands macros and selects conditional branches. Literal quoted includes only;
macro quoting / token pasting and computed include names are deliberately out
of scope so that all possible file opens can be checked before parsing.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import stat

from bootstrap_fourstate import load_parser

MAX_FILES = 512
MAX_BYTES = 64 * 1024 * 1024
DIRECTIVES = {'DefineDirective', 'UndefDirective', 'UndefineAllDirective',
              'IfDefDirective', 'IfNDefDirective', 'ElsIfDirective',
              'ElseDirective', 'EndIfDirective', 'IncludeDirective', 'MacroUsage'}


def checked_path(path, *, directory=False):
    path = Path(os.path.abspath(path))
    for part in [*reversed(path.parents), path]:
        info = part.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValueError('preprocessing path is a link/reparse point: ' + str(part))
    if (directory and not path.is_dir()) or (not directory and not path.is_file()):
        raise ValueError('preprocessing requires an ordinary ' + ('directory: ' if directory else 'file: ') + str(path))
    return path


def stable_bytes(path):
    path = checked_path(path)
    before = path.stat()
    if before.st_size > MAX_BYTES:
        raise ValueError('preprocessing file exceeds byte budget: ' + str(path))
    data = path.read_bytes()
    after = path.stat()
    fields = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns')
    if any(getattr(before, k) != getattr(after, k) for k in fields):
        raise ValueError('preprocessing input changed during read: ' + str(path))
    if b'\0' in data:
        raise ValueError('NUL in preprocessing input: ' + str(path))
    data.decode('utf-8')  # no implicit replacement or newline conversion
    return data


def location(sm, pos):
    expanded = sm.getFullyExpandedLoc(pos)
    original = sm.getFullyOriginalLoc(pos)
    def physical(p):
        raw = str(sm.getRawFileName(p.buffer))
        return dict(file='<fourstate-command-line>' if raw.startswith('<unnamed_buffer') else str(Path(sm.getFullPath(p.buffer)).absolute()),
                    line=sm.getLineNumber(p), column=sm.getColumnNumber(p))
    result = physical(expanded)
    if original != expanded:
        result.update(macro=sm.getMacroName(pos), definition=physical(original))
    return result


def parse_sources(source, defines=(), include_dirs=(), report=None):
    s = load_parser()
    report = {} if report is None else report
    report.update(format='fourstate-preprocessing-v1', status='CHECKING', parser=s.__version__,
                  compilation_units='one independent unit per .sv/.v root; source macros do not leak between units',
                  implicit_synthesis_define=False,
                  path_policy='literal quoted includes; no links, absolute/traversal paths, computed include, macro quoting or token pasting; ambiguous include candidates rejected even in inactive text',
                  max_files=MAX_FILES, max_bytes=MAX_BYTES, max_include_depth=32)
    try:
        source = checked_path(source, directory=Path(source).is_dir())
        base = source if source.is_dir() else source.parent
        roots = []
        if source.is_dir():
            # Do not rglob through directory links before admitting them.
            def walk(folder):
                for child in sorted(folder.iterdir()):
                    checked_path(child, directory=child.is_dir())
                    if child.is_dir():
                        walk(child)
                    elif child.suffix.lower() in ('.sv', '.v'):
                        roots.append(child)
            walk(source)
        else:
            roots = [source]
        if not roots or len(roots) > MAX_FILES:
            raise ValueError('preprocessing root count outside budget')
        include_dirs = list(dict.fromkeys(checked_path(p, directory=True) for p in include_dirs))
        declared_roots = list(dict.fromkeys([base, *include_dirs]))
        predefines = []
        for value in defines:
            if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_$]*(?:=[^`\r\n\x00]*)?', value):
                raise ValueError('invalid command-line macro definition: ' + repr(value))
            predefines.append(value if '=' in value else value + '=1')
        if len({v.split('=', 1)[0] for v in predefines}) != len(predefines):
            raise ValueError('duplicate command-line macro definition')
        report.update(predefines=predefines, include_dirs=[str(p) for p in include_dirs],
                      roots=[str(p) for p in roots], source_roots=[str(p) for p in declared_roots])
        sm = s.SourceManager()
        report['diagnostics'] = []
        def diagnose(items):
            text = s.DiagnosticEngine.reportAll(sm, items)
            p = location(sm, items[0].location)
            return f"{p['file']}:{p['line']}:{p['column']}: official preprocessing diagnostics\n" + text
        admitted, buffers, requests = {}, {}, set()
        search = set([p.parent for p in roots] + include_dirs)
        def admit(path):
            if path in admitted:
                return
            if not any(path.is_relative_to(r) for r in declared_roots):
                raise ValueError('include outside declared source roots: ' + str(path))
            data = stable_bytes(path)
            if len(admitted) >= MAX_FILES or sum(len(b) for b in admitted.values()) + len(data) > MAX_BYTES:
                raise ValueError('preprocessing source closure exceeds budget')
            admitted[path] = data
            buffer = sm.assignText(str(path), data.decode('utf-8'))
            buffers[path] = buffer
            diagnostics, alloc = s.Diagnostics(), s.BumpAllocator()
            lexer = s.parsing.Lexer(buffer, alloc, diagnostics, sm)
            while True:
                token = lexer.lex()
                k = str(token.kind).split('.')[-1]
                if k == 'EndOfFile':
                    break
                if 'MacroQuote' in k or k == 'MacroPaste':
                    raise ValueError(f'{path}:{sm.getLineNumber(token.location)}: macro quoting/token pasting outside file-admission subset')
                if k == 'Directive' and token.rawText == '`include':
                    name = lexer.lex()
                    if str(name.kind).split('.')[-1] != 'StringLiteral':
                        raise ValueError(f'{path}:{sm.getLineNumber(token.location)}: literal quoted include required')
                    text = name.rawText[1:-1]
                    rel = PurePosixPath(text)
                    if (not text or '\\' in text or ':' in text or rel.is_absolute() or
                            any(p in ('..', '.') for p in text.split('/'))):
                        raise ValueError(f'{path}:{sm.getLineNumber(token.location)}: unsafe include path {text!r}')
                    requests.add(text)
            if any(d.isError() for d in diagnostics):
                raise ValueError(diagnose(diagnostics))
        for path in roots:
            admit(path)
        # Finite closure of literal include candidates; no directory content scan.
        while True:
            before = (len(admitted), len(requests), len(search))
            for name in sorted(requests):
                candidates = set()
                for folder in sorted(search):
                    path = folder / name
                    # Validate every existing component, including a broken link.
                    for parent in reversed([path, *path.parents]):
                        if parent.exists() or parent.is_symlink():
                            checked_path(parent, directory=parent.is_dir())
                    if path.exists():
                        candidates.add(checked_path(path))
                if len(candidates) > 1:
                    raise ValueError('ambiguous include search: ' + name)
                for path in candidates:
                    admit(path)
                    search.add(path.parent)
            if before == (len(admitted), len(requests), len(search)):
                break
        # Only the admitted search directories are visible to the official PP.
        sm.setDisableLocalIncludes(True)
        for folder in sorted(search):
            sm.addUserDirectories(str(folder))
        options = s.parsing.PreprocessorOptions()
        options.predefines = predefines
        options.predefineSource = '<fourstate-command-line>'
        options.maxIncludeDepth = 32
        trees, directives, active_counts, active_texts = [], [], {}, []
        seen = set()
        for path in roots:
            tree = s.syntax.SyntaxTree.fromBuffer(buffers[path], sm, s.Bag([options]))
            trees.append(tree)
            # A missing final newline cannot change four-state semantics. Keep
            # that diagnostic as evidence; all other PP warnings fail closed.
            report['diagnostics'].extend(dict(code=str(d.code), location=location(sm, d.location),
                                              error=d.isError()) for d in tree.diagnostics)
            fatal = [d for d in tree.diagnostics if d.isError() or str(d.code) != 'DiagCode(NewlineEOF)']
            if fatal:
                raise ValueError(diagnose(fatal))
            def visit(node):
                if not isinstance(node, s.syntax.SyntaxNode):
                    return
                k = str(node.kind).split('.')[-1]
                active_counts[k] = active_counts.get(k, 0) + 1
                for token in node:
                    if not isinstance(token, s.parsing.Token):
                        continue
                    for trivia in token.trivia:
                        if str(trivia.kind).split('.')[-1] != 'Directive':
                            continue
                        directive = trivia.syntax()
                        dk = str(directive.kind).split('.')[-1]
                        pos = directive.sourceRange.start
                        key = (dk, str(pos.buffer), pos.offset, str(path))
                        if key in seen:
                            continue
                        seen.add(key)
                        entry = dict(kind=dk, location=location(sm, pos), compilation_unit=str(path))
                        if hasattr(directive, 'name'):
                            entry['name'] = str(directive.name).strip()
                        if dk == 'DefineDirective':
                            entry['definition_text'] = str(directive).strip()
                        directives.append(entry)
                        if dk not in DIRECTIVES:
                            p = entry['location']
                            raise ValueError(f"{p['file']}:{p['line']}:{p['column']}: unsupported active preprocessor directive {dk}")
            tree.root.visit(visit)
            printer = s.syntax.SyntaxPrinter()
            printer.setExpandIncludes(True); printer.setExpandMacros(True)
            printer.setIncludeDirectives(False); printer.setIncludeSkipped(False); printer.setIncludeComments(False)
            printer.print(tree.root)
            active_texts.append(dict(root=str(path), text=printer.str()))
        actual = set(roots)
        includes = []
        for tree in trees:
            for include in tree.getIncludeDirectives():
                includes.append(dict(path=str(include.path), is_system=include.isSystem,
                                     location=location(sm, include.syntax.sourceRange.start),
                                     resolved=str(Path(sm.getFullPath(include.buffer.id)).absolute())))
        for buffer_id in sm.getAllBuffers():
            if str(sm.getBufferKind(buffer_id)).split('.')[-1] in ('IncludeFile', 'DesignFile'):
                file = Path(sm.getFullPath(buffer_id)).absolute()
                content = sm.getSourceText(buffer_id)
                if (str(sm.getRawFileName(buffer_id)).startswith('<unnamed_buffer') and
                        content in {'`define ' + v.replace('=', ' ', 1) + '\n\0' for v in predefines}):
                    continue  # slang's in-memory command-line definition buffer
                # Synthetic API buffers have no physical file; only our roots
                # and admitted include files can participate in compilation.
                if file not in admitted:
                    raise ValueError('official preprocessor read unadmitted file: ' + str(file))
                if content.endswith('\0'):
                    content = content[:-1]
                if content.encode('utf-8') != admitted[file]:
                    raise ValueError('official source buffer differs from admitted bytes: ' + str(file))
                if str(sm.getBufferKind(buffer_id)).split('.')[-1] == 'IncludeFile':
                    actual.add(file)
        entries = []
        for file, data in sorted(admitted.items()):
            if stable_bytes(file) != data:
                raise ValueError('preprocessing input changed: ' + str(file))
            index = next(i for i, root in enumerate(declared_roots) if file.is_relative_to(root))
            entries.append(dict(path=str(file), sha256=hashlib.sha256(data).hexdigest(), bytes=len(data),
                                snapshot=(Path('original') / ('root-' + str(index)) / file.relative_to(declared_roots[index])).as_posix(),
                                role='root' if file in roots else 'active-include' if file in actual else 'admission-only'))
        report.update(status='PREPROCESSED', files=entries, directives=directives, active_includes=includes,
                      active_syntax_counts=active_counts, active_units=active_texts,
                      effective_include_search=[str(p) for p in sorted(search)],
                      macro_inventory_scope='explicit command-line values and observed source directive events, not a fabricated final macro table')
        return s, sm, trees, sorted(actual)
    except Exception as error:
        report.update(status='REJECTED', error=str(error))
        raise
