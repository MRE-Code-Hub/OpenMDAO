import unittest

from contextlib import redirect_stdout, contextmanager
import io
import os
import shutil
import sys

import openmdao.api as om
from openmdao.utils.testing_utils import use_tempdirs


@contextmanager
def _replace_stdin(target):
    orig = sys.stdin
    sys.stdin = target
    yield
    sys.stdin = orig


@use_tempdirs
class TestCleanOutputs(unittest.TestCase):

    def test_specify_prob(self):

        p1 = om.Problem()
        p1.model.add_subsystem('exec', om.ExecComp('y = a + b'))
        p1.setup()
        p1.run_model()

        p2 = om.Problem()
        p2.model.add_subsystem('exec', om.ExecComp('z = a * b'))
        p2.setup()
        p2.run_model()

        # First Test that a dryrun on p1 works as expected.
        ss = io.StringIO()
        with redirect_stdout(ss):
            om.clean_outputs(p1, dryrun=True)

        expected1 = 'Found 1 OpenMDAO output directories:\n'
        expected2 = 'Would remove 1 output directories (dryrun = True).\n'
        
        self.assertIn(expected1, ss.getvalue())
        self.assertIn(expected2, ss.getvalue())

        # Now actually do it.
        ss = io.StringIO()
        with redirect_stdout(ss):
            om.clean_outputs(p1)

        self.assertEqual(len(os.listdir(os.getcwd())), 1)

        # Now specify p2 with the output directory.
        ss = io.StringIO()
        with redirect_stdout(ss):
            om.clean_outputs(p2)
        
        self.assertEqual(len(os.listdir(os.getcwd())), 0)

    def test_specify_non_output_dir_no_prompt(self):

        p1 = om.Problem(name='foo')
        p1.model.add_subsystem('exec', om.ExecComp('y = a + b'))
        p1.setup()
        p1.run_model()

        p2 = om.Problem(name='bar')
        p2.model.add_subsystem('exec', om.ExecComp('z = a * b'))
        p2.setup()
        p2.run_model()

        # First Test that a dryrun on p1 works as expected.
        ss = io.StringIO()
        with redirect_stdout(ss):
            om.clean_outputs('.', dryrun=True)

        expected = ('Found 2 OpenMDAO output directories:\n'
                    '  bar_out\n'
                    '  foo_out\n'
                    'Would remove 2 output directories (dryrun = True).')
        
        self.assertIn(expected, ss.getvalue())

        # Test that no specified path gives the same result.
        ss = io.StringIO()
        with redirect_stdout(ss):
            om.clean_outputs(dryrun=True)

        expected = ('Found 2 OpenMDAO output directories:\n'
                    '  bar_out\n'
                    '  foo_out\n'
                    'Would remove 2 output directories (dryrun = True).')
        
        self.assertIn(expected, ss.getvalue())

        # Now remove the files
        ss = io.StringIO()
        with redirect_stdout(ss):
            om.clean_outputs(prompt=False)
        
        expected = ('Found 2 OpenMDAO output directories:\n'
                    '  bar_out\n'
                    '  foo_out\n'
                    'Removed 2 OpenMDAO output directories.\n')
        
        self.assertIn(expected, ss.getvalue())

        self.assertNotIn('foo_out', os.listdir(os.getcwd()))
        self.assertNotIn('bar_out', os.listdir(os.getcwd()))

    @unittest.skipIf(sys.version_info  < (3, 9, 0))
    def test_specify_non_output_dir_prompt(self):

        for recurse in (True, False):
            with self.subTest(f'{recurse=}'):
                p1 = om.Problem()
                p1.model.add_subsystem('exec', om.ExecComp('y = a + b'))
                p1.setup()
                p1.run_model()

                p2 = om.Problem()
                p2.model.add_subsystem('exec', om.ExecComp('z = a * b'))
                p2.setup()
                p2.run_model()

                output_dirs = [p1.get_outputs_dir(), p2.get_outputs_dir()]

                os.mkdir('temp')
                for od in output_dirs:
                    shutil.move(od, 'temp')

                # First, respond in the negative
                ss = io.StringIO()
                with redirect_stdout(ss):
                    with _replace_stdin(io.StringIO('n')):
                        om.clean_outputs(recurse=recurse)

                if recurse:
                    expected = ('Found 2 OpenMDAO output directories:\n')
                    self.assertIn(expected, ss.getvalue())

                    outdirs = [d for d in os.listdir('temp') if d.endswith('_out')]           
                    self.assertEqual(len(outdirs), 2)

                    # Respond in the positive to actually remove them.
                    ss = io.StringIO()
                    with redirect_stdout(ss):
                        with _replace_stdin(io.StringIO('y')):
                            om.clean_outputs(recurse=recurse)

                    expected = ('Removed 2 OpenMDAO output directories.\n')
                    
                    self.assertIn(expected, ss.getvalue())

                    outdirs = [d for d in os.listdir('temp') if d.endswith('_out')]           
                    self.assertEqual(len(outdirs), 0)
                else:
                    self.assertIn('No OpenMDAO output directories found.', ss.getvalue())
                
                shutil.rmtree('temp')
