"""DDR image layout retains every declared word across 576-bit frames."""
import hashlib,json,tempfile,unittest
from pathlib import Path

class DDRImageTests(unittest.TestCase):
    def prepare(self,root,words,*,cell='weights',width=64):
        image=root/(cell+'.hex');image.write_text(''.join(f'{word:0{width//4}x}\n' for word in words))
        return dict(component='main',cell=cell,width=width,depth=len(words),address_width=max(1,(len(words)-1).bit_length()),implementation='ddr3',read_latency=0,writable=False,init_file=image.name,init_sha256=hashlib.sha256(image.read_bytes()).hexdigest())
    def build(self,root,bindings,**kwargs):
        from compiler.ddr_images import materialize_ddr_images
        manifest=root/'bindings.json';manifest.write_text(json.dumps({'schema_version':1,'bindings':bindings}))
        return materialize_ddr_images(manifest,root/'output',data_width=576,address_width=25,**kwargs)
    def test_nine_word_frames_preserve_boundary_words_and_disjoint_images(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);words=list(range(19));words[8]=0xffffffffffffffff;words[9]=0x123456789abcdef0
            result=self.build(root,[self.prepare(root,words),self.prepare(root,[0xff,0x80],cell='second',width=8)])
            payload=(root/'output/images.bin').read_bytes()
            self.assertEqual(len(payload),4*72)
            records={row['cell']:row for row in result['memories']}
            self.assertEqual(records['weights']['base_frame'],0)
            self.assertEqual(records['second']['base_frame'],3)
            for cell,expected in [('weights',words),('second',[0xff,0x80])]:
                record=records[cell]
                for index,word in enumerate(expected):
                    offset=record['base_frame']*72+index*8
                    self.assertEqual(int.from_bytes(payload[offset:offset+8],'little'),word)
            self.assertEqual(payload[19*8:3*72],bytes(3*72-19*8))
            self.assertEqual(result['words_per_frame'],9)
            self.assertEqual(result['image_sha256'],hashlib.sha256(payload).hexdigest())
    def test_changed_image_and_unqualified_binding_remove_stale_acceptance(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);binding=self.prepare(root,[1,2]);self.build(root,[binding])
            (root/'weights.hex').write_text('0000000000000003\n0000000000000002\n')
            with self.assertRaisesRegex(ValueError,'identity'):self.build(root,[binding])
            self.assertFalse((root/'output/layout.json').exists())
            binding=self.prepare(root,[1,2]);binding['storage_width']=8
            with self.assertRaisesRegex(ValueError,'full-width'):self.build(root,[binding])
            self.assertFalse((root/'output/layout.json').exists())
    def test_capacity_and_mutable_offchip_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);binding=self.prepare(root,list(range(19)))
            from compiler.ddr_images import materialize_ddr_images
            manifest=root/'bindings.json';manifest.write_text(json.dumps({'schema_version':1,'bindings':[binding]}))
            with self.assertRaisesRegex(ValueError,'capacity'):
                materialize_ddr_images(manifest,root/'output',data_width=576,address_width=1)
            binding['writable']=True
            with self.assertRaisesRegex(ValueError,'immutable'):self.build(root,[binding])
    def test_output_receipt_cannot_overwrite_input_manifest(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);manifest=root/'layout.json'
            manifest.write_text(json.dumps({'schema_version':1,'bindings':[]}))
            original=manifest.read_bytes()
            from compiler.ddr_images import materialize_ddr_images
            with self.assertRaisesRegex(ValueError,'overlap'):
                materialize_ddr_images(manifest,root,data_width=576,address_width=25)
            self.assertEqual(manifest.read_bytes(),original)
