"""No loaded numerical acceptance survives missing or destructive inputs."""
import importlib.util
from pathlib import Path
import unittest
from compiler.stages import digest
import test_graph_build_core as core_tests


class LoadedGenerationGateTests(unittest.TestCase):
    setUp=core_tests.BuildCoreBoundaryTests.setUp

    def verifier(self):
        name='scripts.comparison.verify_graph_loaded_generation'
        self.assertIsNotNone(importlib.util.find_spec(name),'loaded numerical verifier missing')
        return __import__(name,fromlist=['verify_loaded_generation']).verify_loaded_generation

    def verify(self,report,work):
        return self.verifier()(self.output,self.profile,self.root/'transport',self.root/'build',
            snapshot=self.root/'snapshot',package=self.root/'package',
            reference_source=self.root/'reference',work=work,report=report)

    def test_missing_simulator_invalidates_report_before_creating_reference_or_runtime(self):
        report=self.root/'report.json';report.write_text('old acceptance')
        work=self.root/'runtime'
        with self.assertRaisesRegex(ValueError,'stage'):
            self.verify(report,work)
        self.assertFalse(report.exists())
        self.assertFalse(work.exists())

    def test_report_cannot_replace_compilation_input(self):
        self.output.mkdir();report=self.output/'core.json';report.write_text('preserve input')
        with self.assertRaisesRegex(ValueError,'overlap'):
            self.verify(report,self.root/'runtime')
        self.assertEqual(report.read_text(),'preserve input')

    def test_frame_serialization_keeps_low_address_word_in_low_bits(self):
        self.verifier()
        from scripts.comparison.verify_graph_loaded_generation import write_runtime_frames
        words=[0x1122334455667788,0xFEDCBA9876543210]+[0]*7
        image=self.root/'images.bin';image.write_bytes(b''.join(w.to_bytes(8,'little') for w in words))
        layout={'frames':1,'frame_bytes':72,'data_width':576,'address_width':25,
                'byte_order':'little','image_sha256':digest(image)}
        output=self.root/'frames.hex';write_runtime_frames(layout,image,output)
        self.assertEqual(output.read_text(),'0'*112+'fedcba98765432101122334455667788\n')

    def test_report_cannot_replace_external_numerical_contract(self):
        from dataclasses import replace
        contract=self.root/'contract.json';contract.write_bytes(self.profile.numerical_contract.read_bytes())
        self.profile=replace(self.profile,numerical_contract=contract)
        with self.assertRaisesRegex(ValueError,'overlap'):
            self.verify(contract,self.root/'runtime')
        self.assertTrue(contract.is_file())
