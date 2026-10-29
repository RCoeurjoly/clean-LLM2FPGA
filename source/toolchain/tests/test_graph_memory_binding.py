"""Native binding validation is a prerequisite to changing memory cells."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


SOURCE = '''module attributes {calyx.entrypoint = "main"} {
  calyx.component @main(%clk: i1 {clk}, %reset: i1 {reset}, %go: i1 {go}) -> (%done: i1 {done}) {
    %false = hw.constant false
    %true = hw.constant true
    %addr = hw.constant 0 : i2
    %data = hw.constant 0 : i8
    %m.addr0, %m.clk, %m.reset, %m.content_en, %m.write_en, %m.write_data, %m.read_data, %m.done = calyx.seq_mem @m <[3] x 8> [2] {external = true} : i2, i1, i1, i1, i1, i8, i8, i1
    calyx.wires {
      calyx.group @read {
        calyx.assign %m.addr0 = %addr : i2
        calyx.assign %m.content_en = %true : i1
        calyx.assign %m.write_en = %false : i1
        calyx.assign %m.write_data = %data : i8
        calyx.group_done %m.done : i1
      }
    }
    calyx.control { calyx.enable @read }
  } {toplevel}
}
'''

MULTI_SOURCE = '''module attributes {calyx.entrypoint = "main"} {
  calyx.component @main(%clk: i1 {clk}, %reset: i1 {reset}, %go: i1 {go}) -> (%done: i1 {done}) {
    %m.addr0, %m.clk, %m.reset, %m.content_en, %m.write_en, %m.write_data, %m.read_data, %m.done = calyx.seq_mem @m <[3] x 8> [2] {external = true} : i2, i1, i1, i1, i1, i8, i8, i1
    %worker.clk, %worker.reset, %worker.go, %worker.done = calyx.instance @worker of @reader : i1, i1, i1, i1
    calyx.wires {}
    calyx.control { calyx.invoke @worker[weights = m]() -> () }
  } {toplevel}
  calyx.component @reader(%clk: i1 {clk}, %reset: i1 {reset}, %go: i1 {go}) -> (%done: i1 {done}) {
    %false = hw.constant false
    %true = hw.constant true
    %addr = hw.constant 0 : i2
    %data = hw.constant 0 : i8
    %weights.addr0, %weights.clk, %weights.reset, %weights.content_en, %weights.write_en, %weights.write_data, %weights.read_data, %weights.done = calyx.seq_mem @weights <[3] x 8> [2] : i2, i1, i1, i1, i1, i8, i8, i1
    calyx.wires {
      calyx.group @read {
        calyx.assign %weights.addr0 = %addr : i2
        calyx.assign %weights.content_en = %true : i1
        calyx.assign %weights.write_en = %false : i1
        calyx.assign %weights.write_data = %data : i8
        calyx.group_done %weights.done : i1
      }
    }
    calyx.control { calyx.enable @read }
  }
}
'''


class MemoryBindingTests(unittest.TestCase):
    def validate(self, changes=None, image=b'01\nff\n07\n', omit=False,
                 image_hash=None, manifest_hash=None, source=SOURCE, extra=(), bind=False,
                 primitive_hash=None, check_calyx=False,
                 narrow_reads=False, validate_first=True):
        tool = os.environ.get('LLM2FPGA_CALYX_PASS_TOOL')
        self.assertTrue(tool and Path(tool).is_file(), 'native Calyx driver required')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'weights.hex').write_bytes(image)
            binding = dict(component='main', cell='m', width=8, depth=3,
                           address_width=2, implementation='bram', read_latency=2,
                           writable=False, init_file='weights.hex',
                           init_sha256=image_hash or hashlib.sha256(image).hexdigest())
            binding.update(changes or {})
            data = json.dumps({'schema_version': 1,
                               'bindings': [] if omit else [binding, *extra]}).encode()
            manifest = root / 'bindings.json'
            manifest.write_bytes(data)
            digest = manifest_hash or hashlib.sha256(data).hexdigest()
            arguments = [tool, '--verify-each']
            if validate_first:
                arguments.append('--llm2fpga-validate-memory-bindings=manifest=' + str(manifest) +
                                 ' manifest-sha256=' + digest)
            if narrow_reads:
                arguments.append('--llm2fpga-narrow-immutable-read-values=manifest=' + str(manifest) +
                                 ' manifest-sha256=' + digest)
            if bind:
                from scripts.pipeline.memory_policy import MemorySpec, emit_memory_sources
                specs = [MemorySpec('fixture_0', 8, 3, 'bram', 2, False, 'weights.hex')]
                primitive = root / 'closure_memory.sv'
                primitive.write_text(emit_memory_sources(specs,
                    include_narrow_rom='storage_width' in binding)['sv'])
                sha = primitive_hash or hashlib.sha256(primitive.read_bytes()).hexdigest()
                arguments.append('--llm2fpga-bind-model-memories=primitive-source=' +
                                 str(primitive) + ' primitive-source-sha256=' + sha)
            result = subprocess.run(arguments, input=source, text=True,
                capture_output=True, timeout=30)
            if check_calyx and result.returncode == 0:
                translator = os.environ['LLM2FPGA_CALYX_TRANSLATE_TOOL']
                exported = subprocess.run([translator, '--export-calyx'], input=result.stdout,
                                          text=True, capture_output=True, timeout=30)
                self.assertEqual(exported.returncode, 0, exported.stderr)
                program = root / 'bound.futil'
                program.write_text(exported.stdout)
                compiler = os.environ['LLM2FPGA_CALYX_TOOL']
                library = Path(compiler).resolve().parents[1] / 'share/calyx'
                return subprocess.run([compiler, str(program), '-b', 'verilog',
                                       '-l', str(library)], cwd=root,
                                      text=True, capture_output=True, timeout=30)
            return result

    def rejects(self, diagnostic, **arguments):
        result = self.validate(**arguments)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(diagnostic, result.stderr)


    def test_narrow_rom_rejects_lossy_sign_or_zero_extension(self):
        for width, signed in ((3, True), (4, False)):
            with self.subTest(width=width, signed=signed):
                self.rejects('storage narrowing loses initializer bits',
                    changes={'storage_width': width, 'sign_extend': signed})

    def test_narrow_rom_rejects_mutable_storage_and_invalid_fields(self):
        self.rejects('storage narrowing requires immutable memory',
            changes={'storage_width': 4, 'sign_extend': True, 'writable': True})
        for changes in ({'storage_width': 0, 'sign_extend': True},
                        {'storage_width': 9, 'sign_extend': True},
                        {'storage_width': 4}, {'sign_extend': True}):
            with self.subTest(changes=changes):
                self.rejects('invalid storage narrowing fields', changes=changes)

    def test_narrow_rom_reference_contract_must_match(self):
        self.rejects('reference memory contract', source=MULTI_SOURCE,
            changes={'storage_width': 4, 'sign_extend': True},
            extra=[self.reference()])


    def test_truncated_initializer_is_rejected(self):
        self.rejects('initializer depth', image=b'01\nff\n')

    def test_initializer_width_is_rejected(self):
        self.rejects('initializer width', image=b'001\n0ff\n007\n')

    def test_image_hash_mismatch_is_rejected(self):
        self.rejects('initializer identity', image_hash='0' * 64)

    def test_manifest_hash_mismatch_is_rejected(self):
        self.rejects('manifest identity', manifest_hash='0' * 64)

    def test_geometry_must_match_typed_cell(self):
        for field, value in (('width', 16), ('depth', 4), ('address_width', 3)):
            with self.subTest(field=field):
                self.rejects('cell geometry', changes={field: value})

    def test_missing_binding_is_rejected(self):
        self.rejects('missing memory binding', omit=True)

    def test_latency_must_match_implementation(self):
        self.rejects('implementation/latency', changes={'read_latency': 1})

    def test_unknown_implementation_is_rejected(self):
        self.rejects('implementation/latency', changes={'implementation': 'magic'})

    def test_image_path_cannot_escape_manifest_directory(self):
        self.rejects('initializer filename', changes={'init_file': '../weights.hex'})

    def test_immutable_physical_memory_requires_image(self):
        self.rejects('immutable memory requires initializer',
                     changes={'init_file': None, 'init_sha256': None})

    def test_enabled_immutable_write_is_rejected(self):
        self.rejects('immutable memory write',
                     source=SOURCE.replace('%m.write_en = %false', '%m.write_en = %true'))

    def reference(self, **changes):
        reference = dict(component='reader', cell='weights', width=8, depth=3,
                         address_width=2, implementation='bram', read_latency=2,
                         writable=False, init_file=None, init_sha256=None)
        reference.update(changes)
        return reference


    def test_reference_latency_mismatch_is_rejected(self):
        self.rejects('reference memory contract', source=MULTI_SOURCE,
                     extra=[self.reference(implementation='register', read_latency=1)])

    def test_reference_mutability_mismatch_is_rejected(self):
        self.rejects('reference memory contract', source=MULTI_SOURCE,
                     extra=[self.reference(writable=True)])

    def test_unknown_reference_mapping_is_rejected(self):
        self.rejects('reference memory mapping',
                     source=MULTI_SOURCE.replace('[weights = m]', '[missing = m]'),
                     extra=[self.reference()])

    def test_reference_cannot_target_a_physical_callee_cell(self):
        self.rejects('reference memory mapping targets a physical cell',
                     source=MULTI_SOURCE.replace('@weights <[3] x 8> [2] :',
                                                 '@weights <[3] x 8> [2] {external = true} :'),
                     extra=[self.reference(init_file='weights.hex',
                        init_sha256=hashlib.sha256(b'01\nff\n07\n').hexdigest())])

    def test_every_callee_reference_must_be_passed(self):
        spare = '''%spare.addr0, %spare.clk, %spare.reset, %spare.content_en, %spare.write_en, %spare.write_data, %spare.read_data, %spare.done = calyx.seq_mem @spare <[3] x 8> [2] : i2, i1, i1, i1, i1, i8, i8, i1
    '''
        self.rejects('reference memory mapping leaves an unbound cell',
            source=MULTI_SOURCE.replace('calyx.wires {\n      calyx.group @read',
                                         spare + 'calyx.wires {\n      calyx.group @read'),
            extra=[self.reference(), self.reference(cell='spare')])


    def test_primitive_source_identity_is_required(self):
        self.rejects('primitive source identity', bind=True, primitive_hash='0' * 64)


if __name__ == '__main__':
    unittest.main()
