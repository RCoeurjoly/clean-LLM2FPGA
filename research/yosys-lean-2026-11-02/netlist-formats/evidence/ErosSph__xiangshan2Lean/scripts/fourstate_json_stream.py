"""Private exact-number Linux JSON selector; ijson-shaped IO, no hardware logic."""
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading


class Reader:
    def __init__(self, executable):
        self.executable = str(executable)

    def _read(self, stream, mode, prefix):
        errors = []
        # A file prevents a stderr pipe deadlock, including compiler/parser faults.
        with tempfile.TemporaryFile() as diagnostic:
            process = subprocess.Popen([self.executable, mode, prefix], stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE, stderr=diagnostic)
            def feed():
                try:
                    for block in iter(lambda: stream.read(1024 * 1024), b''):
                        process.stdin.write(block)
                except BrokenPipeError:
                    pass
                except BaseException as error:
                    errors.append(error)
                finally:
                    try:
                        process.stdin.close()
                    except BrokenPipeError:
                        pass
            producer = threading.Thread(target=feed, daemon=True)
            producer.start()
            complete = False
            try:
                for line in process.stdout:
                    value = json.loads(line, parse_float=Decimal)
                    yield tuple(value) if mode in ('kvitems', 'basic_parse') else value
                code = process.wait()
                producer.join()
                complete = True
                if errors:
                    raise errors[0]
                if code:
                    diagnostic.seek(0)
                    raise ValueError('Private streaming JSON parser failed: ' + diagnostic.read().decode(errors='replace'))
            finally:
                if not complete:
                    # next(items(...)) intentionally stops after ports. Close the
                    # helper and join before the caller closes its gzip stream.
                    if process.poll() is None:
                        process.terminate()
                    process.wait()
                    producer.join()
                process.stdout.close()

    def items(self, stream, prefix):
        return self._read(stream, 'items', prefix)

    def kvitems(self, stream, prefix):
        return self._read(stream, 'kvitems', prefix)

    def basic_parse(self, stream):
        return self._read(stream, 'basic_parse', '')


def build(directory):
    """Build only from the receipted private snapshot, inside fresh report output."""
    if sys.platform != 'linux':
        raise RuntimeError('The private JSON selector targets the Linux worker')
    directory = Path(directory)
    source = Path(__file__).with_name('fourstate_json_select.cpp')
    executable = directory/'fourstate-json-select'
    compiler = shutil.which('g++')
    if not compiler:
        raise RuntimeError('Existing g++ is required; no installation attempted')
    argv = [compiler, '-std=c++11', '-O3', '-Wall', '-Wextra', str(source), '-o', str(executable)]
    result = subprocess.run(argv, capture_output=True)
    report = dict(argv=argv, exit_code=result.returncode,
                  source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
                  stdout=result.stdout.decode(errors='replace'), stderr=result.stderr.decode(errors='replace'))
    if result.returncode == 0:
        report['executable_sha256'] = hashlib.sha256(executable.read_bytes()).hexdigest()
    (directory/'json-stream-build.json').write_text(json.dumps(report, indent=2))
    if result.returncode:
        raise RuntimeError('Private JSON selector build failed')
    return Reader(executable), report
