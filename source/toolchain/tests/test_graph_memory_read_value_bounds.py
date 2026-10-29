"""Native bindings validate read bounds independently of storage packing."""
import unittest
from pathlib import Path
import shutil
import subprocess
import tempfile
import test_graph_memory_binding as bindings


class NativeReadBoundsTests(unittest.TestCase):
    def validate(self, **arguments):
        return bindings.MemoryBindingTests().validate(**arguments)

    def test_rewriter_authenticates_images_without_prior_validator(self):
        result = self.validate(changes={'value_width': 4, 'value_sign_extend': True},
            image=b'80\n7f\n00\n', narrow_reads=True, validate_first=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('read value bounds lose initializer bits', result.stderr)

    def test_rewriter_validates_readonly_references_without_prior_validator(self):
        source = bindings.MULTI_SOURCE.replace(
            'calyx.assign %weights.write_en = %false', 'calyx.assign %weights.write_en = %true')
        reference = dict(component='reader', cell='weights', width=8, depth=3,
            address_width=2, implementation='bram', read_latency=2, writable=False,
            init_file=None, init_sha256=None, value_width=4, value_sign_extend=True)
        result = self.validate(source=source,
            changes={'value_width': 4, 'value_sign_extend': True}, extra=(reference,),
            narrow_reads=True, validate_first=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('immutable memory write is not proven disabled', result.stderr)


    def test_rewritten_rtl_returns_every_signed_and_unsigned_image_word(self):
        source = bindings.SOURCE.replace('%go: i1 {go}', '%go: i1 {go}, %index: i2')
        source = source.replace('(%done: i1 {done})', '(%done: i1 {done}, %out: i8)')
        source = source.replace('calyx.wires {', 'calyx.wires { calyx.assign %out = %m.read_data : i8')
        source = source.replace('calyx.assign %m.addr0 = %addr', 'calyx.assign %m.addr0 = %index')
        iverilog = shutil.which('iverilog')
        self.assertIsNotNone(iverilog, 'iverilog must be supplied by the Nix build')
        vvp = str(Path(iverilog).with_name('vvp'))
        for signed, image in ((True, b'01\nff\n07\n'), (False, b'01\n0f\n07\n')):
            with self.subTest(signed=signed), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                result = self.validate(source=source,
                    changes={'value_width': 4, 'value_sign_extend': signed}, image=image,
                    narrow_reads=True, bind=True, check_calyx=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                (root/'core.sv').write_text(result.stdout)
                (root/'weights.hex').write_bytes(image)
                (root/'tb.sv').write_text('''module tb;
                  reg clk=0, reset=1, go=0; reg [1:0] index=0;
                  wire done; wire [7:0] out;
                  always #5 clk=~clk;
                  main dut(.clk(clk),.reset(reset),.go(go),.index(index),.done(done),.out(out));
                  integer i;
                  initial begin
                    for(i=0;i<3;i=i+1) begin
                      reset=1;go=0;index=i;repeat(3)@(negedge clk);
                      reset=0;go=1;wait(done);@(negedge clk);
                      $display("WORD %0d %02h",i,out);
                    end
                    $finish;
                  end
                  initial begin repeat(200)@(posedge clk);$fatal(1,"timeout");end
                endmodule''')
                compiled = subprocess.run([iverilog,'-g2012','-s','tb','-o','sim','tb.sv','core.sv'],
                                          cwd=root,text=True,capture_output=True,timeout=30)
                self.assertEqual(compiled.returncode,0,compiled.stderr)
                simulated = subprocess.run([vvp,'sim'],cwd=root,text=True,capture_output=True,timeout=30)
                self.assertEqual(simulated.returncode,0,simulated.stdout+simulated.stderr)
                actual = [int(line.split()[2],16) for line in simulated.stdout.splitlines() if line.startswith('WORD ')]
                self.assertEqual(actual,[int(word,16) for word in image.splitlines()])

    def test_lossy_read_bounds_reject_image(self):
        result = self.validate(changes={'value_width': 4, 'value_sign_extend': True}, image=b'80\n7f\n00\n')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('read value', result.stderr)

    def test_mutable_memory_cannot_receive_read_bounds(self):
        result = self.validate(changes={'writable': True, 'value_width': 4, 'value_sign_extend': True})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('immutable', result.stderr)

    def test_reference_bounds_must_match_physical_source(self):
        reference = dict(component='reader', cell='weights', width=8, depth=3,
            address_width=2, implementation='bram', read_latency=2, writable=False,
            init_file=None, init_sha256=None, value_width=8, value_sign_extend=True)
        result = self.validate(source=bindings.MULTI_SOURCE,
            changes={'value_width': 4, 'value_sign_extend': True}, extra=(reference,))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('reference memory contract', result.stderr)
