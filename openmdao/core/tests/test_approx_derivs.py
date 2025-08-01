""" Testing for group finite differencing."""
import time
import unittest
from io import StringIO

from packaging.version import Version

import numpy as np
from scipy import __version__ as scipy_version

import openmdao.api as om
from openmdao.test_suite.components.impl_comp_array import TestImplCompArray, TestImplCompArrayDense
from openmdao.test_suite.components.paraboloid import Paraboloid
from openmdao.test_suite.components.sellar import SellarDis1withDerivatives, \
    SellarDis2withDerivatives, SellarDis1CS, SellarDis2CS
from openmdao.test_suite.components.sellar_feature import SellarNoDerivativesCS
from openmdao.test_suite.components.simple_comps import DoubleArrayComp
from openmdao.test_suite.components.unit_conv import SrcComp, TgtCompC, TgtCompF, TgtCompK
from openmdao.test_suite.groups.parallel_groups import FanInSubbedIDVC
from openmdao.test_suite.parametric_suite import parametric_suite
from openmdao.utils.assert_utils import assert_near_equal, assert_check_partials, \
    assert_check_totals, assert_warnings
from openmdao.utils.general_utils import set_pyoptsparse_opt
from openmdao.utils.mpi import MPI
from openmdao.utils.rich_utils import strip_formatting
from openmdao.utils.testing_utils import use_tempdirs

try:
    from openmdao.parallel_api import PETScVector
    vector_class = PETScVector
except ImportError:
    vector_class = om.DefaultVector
    PETScVector = None

ScipyVersion = Version(scipy_version)

# check that pyoptsparse is installed
# if it is, try to use SNOPT but fall back to SLSQP
OPT, OPTIMIZER = set_pyoptsparse_opt('SNOPT')

if OPTIMIZER:
    from openmdao.drivers.pyoptsparse_driver import pyOptSparseDriver


@use_tempdirs
class TestGroupFiniteDifference(unittest.TestCase):

    def test_paraboloid(self):
        prob = om.Problem()
        model = prob.model
        model.add_subsystem('p1', om.IndepVarComp('x', 0.0), promotes=['x'])
        model.add_subsystem('p2', om.IndepVarComp('y', 0.0), promotes=['y'])
        model.add_subsystem('comp', Paraboloid(), promotes=['x', 'y', 'f_xy'])

        model.linear_solver = om.ScipyKrylov()
        model.approx_totals()

        prob.setup(check=False, mode='fwd')
        prob.set_solver_print(level=0)
        prob.run_model()

        of = ['f_xy']
        wrt = ['x', 'y']
        derivs = prob.compute_totals(of=of, wrt=wrt)

        assert_near_equal(derivs['f_xy', 'x'], [[-6.0]], 1e-6)
        assert_near_equal(derivs['f_xy', 'y'], [[8.0]], 1e-6)

        # 1 output x 2 inputs
        self.assertEqual(np.sum(list(v.size for v in derivs.values())), 2)

    def test_fd_count(self):
        # Make sure we aren't doing extra FD steps.

        class ParaboloidA(om.ExplicitComponent):
            def setup(self):
                self.add_input('x', val=0.0)
                self.add_input('y', val=0.0)

                self.add_output('f_xy', val=0.0)
                self.add_output('g_xy', val=0.0)

                # makes extra calls to the model with no actual steps
                self.declare_partials(of='*', wrt='*', method='fd', form='forward', step=1e-6)

                self.count = 0

            def compute(self, inputs, outputs):
                x = inputs['x']
                y = inputs['y']

                outputs['f_xy'] = (x-3.0)**2 + x*y + (y+4.0)**2 - 3.0
                g_xy = (x-3.0)**2 + x*y + (y+4.0)**2 - 3.0
                outputs['g_xy'] = g_xy * 3

                self.count += 1

        prob = om.Problem()
        model = prob.model
        model.add_subsystem('px', om.IndepVarComp('x', val=3.0))
        model.add_subsystem('py', om.IndepVarComp('y', val=5.0))
        model.add_subsystem('parab', ParaboloidA())

        model.connect('px.x', 'parab.x')
        model.connect('py.y', 'parab.y')

        model.add_design_var('px.x', lower=-50, upper=50)
        model.add_design_var('py.y', lower=-50, upper=50)
        model.add_objective('parab.f_xy')

        prob.setup()
        prob.run_model()
        prob.compute_totals(of=['parab.f_xy'], wrt=['px.x', 'py.y'])

        # 1. run_model; 2. step x; 3. step y
        self.assertEqual(model.parab.count, 3)
        self.assertEqual(model.parab.iter_count_without_approx, 1)
        self.assertEqual(model.parab.iter_count, 1)
        self.assertEqual(model.parab.iter_count_apply, 2)

    def test_fd_count_driver(self):
        # Make sure we aren't doing FD wrt any var that isn't in the driver desvar set.

        class ParaboloidA(om.ExplicitComponent):
            def setup(self):
                self.add_input('x', val=0.0)
                self.add_input('y', val=0.0)

                self.add_output('f_xy', val=0.0)
                self.add_output('g_xy', val=0.0)

                self.count = 0

            def compute(self, inputs, outputs):
                x = inputs['x']
                y = inputs['y']

                outputs['f_xy'] = (x-3.0)**2 + x*y + (y+4.0)**2 - 3.0
                g_xy = (x-3.0)**2 + x*y + (y+4.0)**2 - 3.0
                outputs['g_xy'] = g_xy * 3

                self.count += 1

        prob = om.Problem()
        model = prob.model
        model.add_subsystem('px', om.IndepVarComp('x', val=3.0))
        model.add_subsystem('py', om.IndepVarComp('y', val=5.0))
        model.add_subsystem('parab', ParaboloidA())

        model.connect('px.x', 'parab.x')
        model.connect('py.y', 'parab.y')

        model.add_design_var('px.x', lower=-50, upper=50)
        model.add_objective('parab.f_xy')

        model.approx_totals(method='fd')

        prob.setup()
        prob.run_model()

        prob.driver._compute_totals(of=['parab.f_xy'], wrt=['px.x'])

        # 1. run_model; 2. step x
        self.assertEqual(model.parab.count, 2)

    def test_paraboloid_subbed(self):
        prob = om.Problem()
        model = prob.model
        model.add_subsystem('p1', om.IndepVarComp('x', 0.0), promotes=['x'])
        model.add_subsystem('p2', om.IndepVarComp('y', 0.0), promotes=['y'])
        sub = model.add_subsystem('sub', om.Group(), promotes=['x', 'y', 'f_xy'])
        sub.add_subsystem('comp', Paraboloid(), promotes=['x', 'y', 'f_xy'])

        model.linear_solver = om.ScipyKrylov()
        sub.approx_totals()

        prob.setup(check=False, mode='fwd')
        prob.set_solver_print(level=0)
        prob.run_model()

        of = ['f_xy']
        wrt = ['x', 'y']
        derivs = prob.compute_totals(of=of, wrt=wrt)

        assert_near_equal(derivs['f_xy', 'x'], [[-6.0]], 1e-6)
        assert_near_equal(derivs['f_xy', 'y'], [[8.0]], 1e-6)

        Jfd = sub._jacobian
        assert_near_equal(Jfd['sub.comp.f_xy', 'sub.comp.x'], [[-6.0]], 1e-6)
        assert_near_equal(Jfd['sub.comp.f_xy', 'sub.comp.y'], [[8.0]], 1e-6)

        # 1 output x 2 inputs
        self.assertEqual(len(sub._approx_schemes['fd']._wrt_meta), 2)

    def test_paraboloid_subbed_in_setup(self):
        class MyModel(om.Group):

            def setup(self):
                self.add_subsystem('comp', Paraboloid(), promotes=['x', 'y', 'f_xy'])

                self.approx_totals()

        prob = om.Problem()
        model = prob.model
        model.add_subsystem('p1', om.IndepVarComp('x', 0.0), promotes=['x'])
        model.add_subsystem('p2', om.IndepVarComp('y', 0.0), promotes=['y'])
        sub = model.add_subsystem('sub', MyModel(), promotes=['x', 'y', 'f_xy'])

        model.linear_solver = om.ScipyKrylov()

        prob.setup(check=False, mode='fwd')
        prob.set_solver_print(level=0)
        prob.run_model()

        of = ['f_xy']
        wrt = ['x', 'y']
        derivs = prob.compute_totals(of=of, wrt=wrt)

        assert_near_equal(derivs['f_xy', 'x'], [[-6.0]], 1e-6)
        assert_near_equal(derivs['f_xy', 'y'], [[8.0]], 1e-6)

        Jfd = sub._jacobian
        assert_near_equal(Jfd['sub.comp.f_xy', 'sub.comp.x'], [[-6.0]], 1e-6)
        assert_near_equal(Jfd['sub.comp.f_xy', 'sub.comp.y'], [[8.0]], 1e-6)

        # 1 output x 2 inputs
        self.assertEqual(len(sub._approx_schemes['fd']._wrt_meta), 2)

    def test_paraboloid_subbed_with_connections(self):
        prob = om.Problem()
        model = prob.model
        model.add_subsystem('p1', om.IndepVarComp('x', 0.0))
        model.add_subsystem('p2', om.IndepVarComp('y', 0.0))
        sub = model.add_subsystem('sub', om.Group())
        sub.add_subsystem('bx', om.ExecComp('xout = xin'))
        sub.add_subsystem('by', om.ExecComp('yout = yin'))
        sub.add_subsystem('comp', Paraboloid())

        model.connect('p1.x', 'sub.bx.xin')
        model.connect('sub.bx.xout', 'sub.comp.x')
        model.connect('p2.y', 'sub.by.yin')
        model.connect('sub.by.yout', 'sub.comp.y')

        model.linear_solver = om.ScipyKrylov()
        sub.approx_totals()

        prob.setup(check=False, mode='fwd')
        prob.set_solver_print(level=0)
        prob.run_model()

        of = ['sub.comp.f_xy']
        wrt = ['p1.x', 'p2.y']
        derivs = prob.compute_totals(of=of, wrt=wrt)

        assert_near_equal(derivs['sub.comp.f_xy', 'p1.x'], [[-6.0]], 1e-6)
        assert_near_equal(derivs['sub.comp.f_xy', 'p2.y'], [[8.0]], 1e-6)

        Jfd = sub._jacobian
        assert_near_equal(Jfd['sub.comp.f_xy', 'sub.bx.xin'], [[-6.0]], 1e-6)
        assert_near_equal(Jfd['sub.comp.f_xy', 'sub.by.yin'], [[8.0]], 1e-6)

    def test_array_comp(self):

        class DoubleArrayFD(DoubleArrayComp):

            def compute_partials(self, inputs, partials):
                """
                Override deriv calculation.
                """
                pass

        prob = om.Problem()
        model = prob.model

        model.add_subsystem('p1', om.IndepVarComp('x1', val=np.ones(2)))
        model.add_subsystem('p2', om.IndepVarComp('x2', val=np.ones(2)))
        comp = model.add_subsystem('comp', DoubleArrayFD())
        model.connect('p1.x1', 'comp.x1')
        model.connect('p2.x2', 'comp.x2')

        model.linear_solver = om.ScipyKrylov()
        model.approx_totals()

        model.add_design_var('p1.x1')
        model.add_design_var('p2.x2')
        model.add_constraint('comp.y1')
        model.add_constraint('comp.y2')

        prob.setup()
        prob.run_model()
        model.run_linearize(driver=prob.driver)

        Jfd = model._jacobian.J_dict
        assert_near_equal(Jfd['comp.y1', 'p1.x1'], comp.JJ[0:2, 0:2], 1e-6)
        assert_near_equal(Jfd['comp.y1', 'p2.x2'], comp.JJ[0:2, 2:4], 1e-6)
        assert_near_equal(Jfd['comp.y2', 'p1.x1'], comp.JJ[2:4, 0:2], 1e-6)
        assert_near_equal(Jfd['comp.y2', 'p2.x2'], comp.JJ[2:4, 2:4], 1e-6)

    def test_implicit_component_fd(self):
        # Somehow this wasn't tested in the original fd tests (which are mostly feature tests.)

        class TestImplCompArrayDense(TestImplCompArray):

            def setup(self):
                super().setup()
                self.declare_partials('*', '*', method='fd')

        prob = om.Problem()
        model = prob.model

        model.add_subsystem('p_rhs', om.IndepVarComp('rhs', val=np.ones(2)))
        sub = model.add_subsystem('sub', om.Group())
        comp = sub.add_subsystem('comp', TestImplCompArrayDense())
        model.connect('p_rhs.rhs', 'sub.comp.rhs')

        model.linear_solver = om.ScipyKrylov()

        prob.setup()
        prob.run_model()
        model.run_linearize()

        Jfd = comp._jacobian
        assert_near_equal(Jfd['sub.comp.x', 'sub.comp.rhs'], -np.eye(2), 1e-6)
        assert_near_equal(Jfd['sub.comp.x', 'sub.comp.x'], comp.mtx, 1e-6)

    def test_around_newton(self):
        # For a group that is set to FD that has a Newton solver, make sure it doesn't
        # try to FD itself while solving.

        class TestImplCompArrayDenseNoSolve(TestImplCompArrayDense):
            def solve_nonlinear(self, inputs, outputs):
                """ Disable local solve."""
                pass

        prob = om.Problem()
        model = prob.model

        model.add_subsystem('p_rhs', om.IndepVarComp('rhs', val=np.array([2, 4])))
        model.add_subsystem('comp', TestImplCompArrayDenseNoSolve())
        model.connect('p_rhs.rhs', 'comp.rhs')

        model.nonlinear_solver = om.NewtonSolver(solve_subsystems=False)
        model.linear_solver = om.ScipyKrylov()
        model.approx_totals()

        prob.setup()
        prob.run_model()
        model.approx_totals()
        assert_near_equal(prob['comp.x'], [1.97959184, 4.02040816], 1e-5)

        model.run_linearize()

        of = ['comp.x']
        wrt = ['p_rhs.rhs']
        Jfd = prob.compute_totals(of=of, wrt=wrt)

        assert_near_equal(Jfd['comp.x', 'p_rhs.rhs'],
                         [[1.01020408, -0.01020408], [-0.01020408, 1.01020408]], 1e-5)

    def test_step_size(self):
        # Test makes sure option metadata propagates to the fd function
        prob = om.Problem()
        model = prob.model
        model.add_subsystem('p1', om.IndepVarComp('x', 0.0), promotes=['x'])
        model.add_subsystem('p2', om.IndepVarComp('y', 0.0), promotes=['y'])
        model.add_subsystem('comp', Paraboloid(), promotes=['x', 'y', 'f_xy'])

        model.linear_solver = om.ScipyKrylov()

        # Worse step so that our answer will be off a wee bit.
        model.approx_totals(step=1e-2)

        prob.setup(check=False, mode='fwd')
        prob.set_solver_print(level=0)
        prob.run_model()

        of = ['f_xy']
        wrt = ['x', 'y']
        derivs = prob.compute_totals(of=of, wrt=wrt)

        assert_near_equal(derivs['f_xy', 'x'], [[-5.99]], 1e-6)
        assert_near_equal(derivs['f_xy', 'y'], [[8.01]], 1e-6)

    def test_unit_conv_group(self):

        prob = om.Problem()
        prob.model.add_subsystem('px1', om.IndepVarComp('x1', 100.0), promotes=['x1'])
        sub1 = prob.model.add_subsystem('sub1', om.Group())
        sub2 = prob.model.add_subsystem('sub2', om.Group())

        sub1.add_subsystem('src', SrcComp())
        sub2.add_subsystem('tgtF', TgtCompF())
        sub2.add_subsystem('tgtC', TgtCompC())
        sub2.add_subsystem('tgtK', TgtCompK())

        prob.model.connect('x1', 'sub1.src.x1')
        prob.model.connect('sub1.src.x2', 'sub2.tgtF.x2')
        prob.model.connect('sub1.src.x2', 'sub2.tgtC.x2')
        prob.model.connect('sub1.src.x2', 'sub2.tgtK.x2')

        sub2.approx_totals(method='fd')

        prob.setup()
        prob.run_model()

        assert_near_equal(prob['sub1.src.x2'], 100.0, 1e-6)
        assert_near_equal(prob['sub2.tgtF.x3'], 212.0, 1e-6)
        assert_near_equal(prob['sub2.tgtC.x3'], 100.0, 1e-6)
        assert_near_equal(prob['sub2.tgtK.x3'], 373.15, 1e-6)

        wrt = ['x1']
        of = ['sub2.tgtF.x3', 'sub2.tgtC.x3', 'sub2.tgtK.x3']
        J = prob.compute_totals(of=of, wrt=wrt, return_format='dict')

        assert_near_equal(J['sub2.tgtF.x3']['x1'][0][0], 1.8, 1e-6)
        assert_near_equal(J['sub2.tgtC.x3']['x1'][0][0], 1.0, 1e-6)
        assert_near_equal(J['sub2.tgtK.x3']['x1'][0][0], 1.0, 1e-6)

        # Check the total derivatives in reverse mode
        prob.setup(check=False, mode='rev')
        prob.run_model()
        J = prob.compute_totals(of=of, wrt=wrt, return_format='dict')

        assert_near_equal(J['sub2.tgtF.x3']['x1'][0][0], 1.8, 1e-6)
        assert_near_equal(J['sub2.tgtC.x3']['x1'][0][0], 1.0, 1e-6)
        assert_near_equal(J['sub2.tgtK.x3']['x1'][0][0], 1.0, 1e-6)

    def test_sellar(self):
        # Basic sellar test.

        prob = om.Problem()
        model = prob.model

        model.add_subsystem('px', om.IndepVarComp('x', 1.0), promotes=['x'])
        model.add_subsystem('pz', om.IndepVarComp('z', np.array([5.0, 2.0])), promotes=['z'])

        model.add_subsystem('d1', SellarDis1withDerivatives(), promotes=['x', 'z', 'y1', 'y2'])
        model.add_subsystem('d2', SellarDis2withDerivatives(), promotes=['z', 'y1', 'y2'])

        model.add_subsystem('obj_cmp', om.ExecComp('obj = x**2 + z[1] + y1 + exp(-y2)',
                                                z=np.array([0.0, 0.0]), x=0.0),
                            promotes=['obj', 'x', 'z', 'y1', 'y2'])

        model.add_subsystem('con_cmp1', om.ExecComp('con1 = 3.16 - y1'), promotes=['con1', 'y1'])
        model.add_subsystem('con_cmp2', om.ExecComp('con2 = y2 - 24.0'), promotes=['con2', 'y2'])

        prob.model.nonlinear_solver = om.NonlinearBlockGS()

        model.approx_totals(method='fd', step=1e-5)

        prob.setup()
        prob.set_solver_print(level=0)
        prob.run_model()

        assert_near_equal(prob['y1'], 25.58830273, .00001)
        assert_near_equal(prob['y2'], 12.05848819, .00001)

        wrt = ['z']
        of = ['obj']

        J = prob.compute_totals(of=of, wrt=wrt, return_format='flat_dict')
        assert_near_equal(J['obj', 'z'][0][0], 9.61001056, .00001)
        assert_near_equal(J['obj', 'z'][0][1], 1.78448534, .00001)

    def test_desvar_with_indices(self):
        # Just desvars on this one to cover code missed by desvar+response test.

        class ArrayComp2D(om.ExplicitComponent):
            """
            A fairly simple array component.
            """

            def setup(self):

                self.JJ = np.array([[1.0, 3.0, -2.0, 7.0],
                                    [6.0, 2.5, 2.0, 4.0],
                                    [-1.0, 0.0, 8.0, 1.0],
                                    [1.0, 4.0, -5.0, 6.0]])

                # Params
                self.add_input('x1', np.zeros([4]))

                # Unknowns
                self.add_output('y1', np.zeros([4]))

                # Derivatives
                self.declare_partials('*', '*')

            def compute(self, inputs, outputs):
                """
                Execution.
                """
                outputs['y1'] = self.JJ.dot(inputs['x1'])

            def compute_partials(self, inputs, partials):
                """
                Analytical derivatives.
                """
                partials[('y1', 'x1')] = self.JJ

        prob = om.Problem()
        model = prob.model
        model.add_subsystem('x_param1', om.IndepVarComp('x1', np.ones((4))),
                            promotes=['x1'])
        mycomp = model.add_subsystem('mycomp', ArrayComp2D(), promotes=['x1', 'y1'])

        model.add_design_var('x1', indices=[1, 3])
        model.add_constraint('y1')

        prob.set_solver_print(level=0)
        model.approx_totals(method='fd')

        prob.setup(check=False, mode='fwd')
        prob.run_model()

        Jbase = mycomp.JJ
        of = ['y1']
        wrt = ['x1']

        J = prob.compute_totals(of=of, wrt=wrt, return_format='flat_dict')
        assert_near_equal(J['y1', 'x1'][0][0], Jbase[0, 1], 1e-8)
        assert_near_equal(J['y1', 'x1'][0][1], Jbase[0, 3], 1e-8)
        assert_near_equal(J['y1', 'x1'][2][0], Jbase[2, 1], 1e-8)
        assert_near_equal(J['y1', 'x1'][2][1], Jbase[2, 3], 1e-8)

    def test_desvar_and_response_with_indices(self):

        class ArrayComp2D(om.ExplicitComponent):
            """
            A fairly simple array component.
            """

            def setup(self):

                self.JJ = np.array([[1.0, 3.0, -2.0, 7.0],
                                    [6.0, 2.5, 2.0, 4.0],
                                    [-1.0, 0.0, 8.0, 1.0],
                                    [1.0, 4.0, -5.0, 6.0]])

                # Params
                self.add_input('x1', np.zeros([4]))

                # Unknowns
                self.add_output('y1', np.zeros([4]))

                self.declare_partials(of='*', wrt='*')

            def compute(self, inputs, outputs):
                """
                Execution.
                """
                outputs['y1'] = self.JJ.dot(inputs['x1'])

            def compute_partials(self, inputs, partials):
                """
                Analytical derivatives.
                """
                partials[('y1', 'x1')] = self.JJ

        prob = om.Problem()
        model = prob.model
        model.add_subsystem('x_param1', om.IndepVarComp('x1', np.ones((4))),
                            promotes=['x1'])
        mycomp = model.add_subsystem('mycomp', ArrayComp2D(), promotes=['x1', 'y1'])

        model.add_design_var('x1', indices=[1, 3])
        model.add_constraint('y1', indices=[0, 2])

        prob.set_solver_print(level=0)
        model.approx_totals(method='fd')

        prob.setup(check=False, mode='fwd')
        prob.run_model()

        Jbase = mycomp.JJ
        of = ['y1']
        wrt = ['x1']

        J = prob.compute_totals(of=of, wrt=wrt, return_format='flat_dict')
        assert_near_equal(J['y1', 'x1'][0][0], Jbase[0, 1], 1e-8)
        assert_near_equal(J['y1', 'x1'][0][1], Jbase[0, 3], 1e-8)
        assert_near_equal(J['y1', 'x1'][1][0], Jbase[2, 1], 1e-8)
        assert_near_equal(J['y1', 'x1'][1][1], Jbase[2, 3], 1e-8)

    def test_full_model_fd(self):

        class DontCall(om.LinearRunOnce):
            def solve(self, mode, rel_systems=None):
                raise RuntimeError("This solver should be ignored!")

        class Simple(om.ExplicitComponent):
            def setup(self):
                self.add_input('x', val=0.0)
                self.add_output('y', val=0.0)

                self.declare_partials('y', 'x')

            def compute(self, inputs, outputs):
                x = inputs['x']
                outputs['y'] = 4.0*x

        prob = om.Problem()
        model = prob.model
        model.add_subsystem('p1', om.IndepVarComp('x', 0.0), promotes=['x'])
        model.add_subsystem('comp', Simple(), promotes=['x', 'y'])

        model.linear_solver = DontCall()
        model.approx_totals()

        model.add_design_var('x')
        model.add_objective('y')

        prob.setup(check=False, mode='fwd')
        prob.set_solver_print(level=0)
        prob.run_model()

        of = ['comp.y']
        wrt = ['p1.x']
        derivs = prob.driver._compute_totals(of=of, wrt=wrt, return_format='dict')

        assert_near_equal(derivs['comp.y']['p1.x'], [[4.0]], 1e-6)

    def test_newton_with_densejac_under_full_model_fd(self):
        # Basic sellar test.

        prob = om.Problem()
        model = prob.model = om.Group(assembled_jac_type='dense')

        model.add_subsystem('px', om.IndepVarComp('x', 1.0), promotes=['x'])
        model.add_subsystem('pz', om.IndepVarComp('z', np.array([5.0, 2.0])), promotes=['z'])

        sub = model.add_subsystem('sub', om.Group(), promotes=['*'])

        sub.add_subsystem('d1', SellarDis1withDerivatives(), promotes=['x', 'z', 'y1', 'y2'])
        sub.add_subsystem('d2', SellarDis2withDerivatives(), promotes=['z', 'y1', 'y2'])

        model.add_subsystem('obj_cmp', om.ExecComp('obj = x**2 + z[1] + y1 + exp(-y2)',
                                                   z=np.array([0.0, 0.0]), x=0.0),
                            promotes=['obj', 'x', 'z', 'y1', 'y2'])

        model.add_subsystem('con_cmp1', om.ExecComp('con1 = 3.16 - y1'), promotes=['con1', 'y1'])
        model.add_subsystem('con_cmp2', om.ExecComp('con2 = y2 - 24.0'), promotes=['con2', 'y2'])

        sub.nonlinear_solver = om.NewtonSolver(solve_subsystems=False)
        sub.linear_solver = om.ScipyKrylov(assemble_jac=True)

        model.approx_totals(method='fd', step=1e-5)

        prob.setup()
        prob.set_solver_print(level=0)
        prob.run_model()

        assert_near_equal(prob['y1'], 25.58830273, .00001)
        assert_near_equal(prob['y2'], 12.05848819, .00001)

        wrt = ['z']
        of = ['obj']

        J = prob.compute_totals(of=of, wrt=wrt, return_format='flat_dict')
        assert_near_equal(J['obj', 'z'][0][0], 9.61001056, .00001)
        assert_near_equal(J['obj', 'z'][0][1], 1.78448534, .00001)

    def test_newton_with_cscjac_under_full_model_fd(self):
        # Basic sellar test.

        prob = om.Problem()
        model = prob.model
        sub = model.add_subsystem('sub', om.Group(), promotes=['*'])

        model.add_subsystem('px', om.IndepVarComp('x', 1.0), promotes=['x'])
        model.add_subsystem('pz', om.IndepVarComp('z', np.array([5.0, 2.0])), promotes=['z'])

        sub.add_subsystem('d1', SellarDis1withDerivatives(), promotes=['x', 'z', 'y1', 'y2'])
        sub.add_subsystem('d2', SellarDis2withDerivatives(), promotes=['z', 'y1', 'y2'])

        model.add_subsystem('obj_cmp', om.ExecComp('obj = x**2 + z[1] + y1 + exp(-y2)',
                                                   z=np.array([0.0, 0.0]), x=0.0),
                            promotes=['obj', 'x', 'z', 'y1', 'y2'])

        model.add_subsystem('con_cmp1', om.ExecComp('con1 = 3.16 - y1'), promotes=['con1', 'y1'])
        model.add_subsystem('con_cmp2', om.ExecComp('con2 = y2 - 24.0'), promotes=['con2', 'y2'])

        sub.nonlinear_solver = om.NewtonSolver(solve_subsystems=False)
        sub.linear_solver = om.ScipyKrylov(assemble_jac=True)

        model.approx_totals(method='fd', step=1e-5)

        prob.setup()
        prob.set_solver_print(level=0)
        prob.run_model()

        assert_near_equal(prob['y1'], 25.58830273, .00001)
        assert_near_equal(prob['y2'], 12.05848819, .00001)

        wrt = ['z']
        of = ['obj']

        J = prob.compute_totals(of=of, wrt=wrt, return_format='flat_dict')
        assert_near_equal(J['obj', 'z'][0][0], 9.61001056, .00001)
        assert_near_equal(J['obj', 'z'][0][1], 1.78448534, .00001)

    def test_approx_totals_multi_input_constrained_desvar(self):
        p = om.Problem()

        indeps = p.model.add_subsystem('indeps', om.IndepVarComp(), promotes_outputs=['*'])

        indeps.add_output('x', np.array([ 0.55994437, -0.95923447,  0.21798656, -0.02158783,  0.62183717,
                                          0.04007379,  0.46044942, -0.10129622,  0.27720413, -0.37107886]))
        indeps.add_output('y', np.array([ 0.52577864,  0.30894559,  0.8420792 ,  0.35039912, -0.67290778,
                                         -0.86236787, -0.97500023,  0.47739414,  0.51174103,  0.10052582]))
        indeps.add_output('r', .7)

        arctan_yox = om.ExecComp('g=arctan(y/x)', has_diag_partials=True,
                                 g=np.ones(10), x=np.ones(10), y=np.ones(10))

        p.model.add_subsystem('arctan_yox', arctan_yox)

        p.model.add_subsystem('circle', om.ExecComp('area=pi*r**2'))

        p.model.add_subsystem('r_con', om.ExecComp('g=x**2 + y**2 - r', has_diag_partials=True,
                                                   g=np.ones(10), x=np.ones(10), y=np.ones(10)))

        p.model.connect('r', ('circle.r', 'r_con.r'))
        p.model.connect('x', ['r_con.x', 'arctan_yox.x'])
        p.model.connect('y', ['r_con.y', 'arctan_yox.y'])

        p.model.approx_totals(method='cs')

        p.model.add_design_var('x')
        p.model.add_design_var('y')
        p.model.add_design_var('r', lower=.5, upper=10)
        p.model.add_constraint('y', equals=0, indices=[0,])
        p.model.add_objective('circle.area', ref=-1)

        p.setup(derivatives=True, force_alloc_complex=True)

        p.run_model()
        # Formerly a KeyError
        derivs = p.check_totals(compact_print=True, out_stream=None)
        assert_check_totals(derivs)

        # Coverage
        derivs = p.driver._compute_totals(return_format='dict')
        assert_near_equal(np.zeros((1, 10)), derivs['y']['x'])

    def test_opt_with_linear_constraint(self):
        # Test for a bug where we weren't re-initializing things in-between computing totals on
        # linear constraints, and the nonlinear ones.
        if OPT is None:
            raise unittest.SkipTest("pyoptsparse is not installed")

        if OPTIMIZER is None:
            raise unittest.SkipTest("pyoptsparse is not providing SNOPT or SLSQP")

        p = om.Problem()

        indeps = p.model.add_subsystem('indeps', om.IndepVarComp(), promotes_outputs=['*'])

        indeps.add_output('x', np.array([ 0.55994437, -0.95923447,  0.21798656, -0.02158783,  0.62183717,
                                          0.04007379,  0.46044942, -0.10129622,  0.27720413, -0.37107886]))
        indeps.add_output('y', np.array([ 0.52577864,  0.30894559,  0.8420792 ,  0.35039912, -0.67290778,
                                         -0.86236787, -0.97500023,  0.47739414,  0.51174103,  0.10052582]))
        indeps.add_output('r', .7)

        arctan_yox = om.ExecComp('g=arctan(y/x)', has_diag_partials=True,
                                 g=np.ones(10), x=np.ones(10), y=np.ones(10))

        p.model.add_subsystem('arctan_yox', arctan_yox)

        p.model.add_subsystem('circle', om.ExecComp('area=pi*r**2'))

        p.model.add_subsystem('r_con', om.ExecComp('g=x**2 + y**2 - r', has_diag_partials=True,
                                                   g=np.ones(10), x=np.ones(10), y=np.ones(10)))

        thetas = np.linspace(0, np.pi/4, 10)
        p.model.add_subsystem('theta_con', om.ExecComp('g = x - theta', has_diag_partials=True,
                                                       g=np.ones(10), x=np.ones(10),
                                                       theta=thetas))
        p.model.add_subsystem('delta_theta_con', om.ExecComp('g = even - odd', has_diag_partials=True,
                                                             g=np.ones(10//2), even=np.ones(10//2),
                                                             odd=np.ones(10//2)))

        p.model.add_subsystem('l_conx', om.ExecComp('g=x-1', has_diag_partials=True, g=np.ones(10), x=np.ones(10)))

        IND = np.arange(10, dtype=int)
        ODD_IND = IND[1::2]  # all odd indices
        EVEN_IND = IND[0::2]  # all even indices

        p.model.connect('r', ('circle.r', 'r_con.r'))
        p.model.connect('x', ['r_con.x', 'arctan_yox.x', 'l_conx.x'])
        p.model.connect('y', ['r_con.y', 'arctan_yox.y'])
        p.model.connect('arctan_yox.g', 'theta_con.x')
        p.model.connect('arctan_yox.g', 'delta_theta_con.even', src_indices=EVEN_IND)
        p.model.connect('arctan_yox.g', 'delta_theta_con.odd', src_indices=ODD_IND)

        p.driver = pyOptSparseDriver()
        p.driver.options['print_results'] = False

        p.model.approx_totals(method='fd')

        p.model.add_design_var('x')
        p.model.add_design_var('y')
        p.model.add_design_var('r', lower=.5, upper=10)

        # nonlinear constraints
        p.model.add_constraint('r_con.g', equals=0)

        p.model.add_constraint('theta_con.g', lower=-1e-5, upper=1e-5, indices=EVEN_IND)
        p.model.add_constraint('delta_theta_con.g', lower=-1e-5, upper=1e-5)
        p.model.add_constraint('l_conx.g', equals=0, linear=False, indices=[0,])
        p.model.add_constraint('y', equals=0, indices=[0,], linear=True)

        p.model.add_objective('circle.area', ref=-1)

        p.setup(mode='fwd', derivatives=True)

        p.run_driver()

        assert_near_equal(p['circle.area'], np.pi, 1e-6)

    def test_bug_subsolve(self):
        # There was a bug where a group with an approximation was still performing a linear
        # solve on its subsystems, which led to partials declared with 'val' corrupting the
        # results.

        class DistParab(om.ExplicitComponent):

            def initialize(self):

                self.options.declare('arr_size', types=int, default=10,
                                     desc="Size of input and output vectors.")

            def setup(self):
                arr_size = self.options['arr_size']

                self.add_input('x', val=np.ones(arr_size))
                self.add_output('f_xy', val=np.ones(arr_size))

                self.declare_partials('f_xy', 'x')

            def compute(self, inputs, outputs):
                x = inputs['x']
                outputs['f_xy'] = x**2

        class NonDistComp(om.ExplicitComponent):

            def initialize(self):
                self.options.declare('arr_size', types=int, default=10,
                                     desc="Size of input and output vectors.")

            def setup(self):
                arr_size = self.options['arr_size']

                self.add_input('f_xy', val=np.ones(arr_size))

                self.add_output('g', val=np.ones(arr_size))

                # Make this wrong to see if it shows up in the answer.
                mat = np.array([7.0, 13, 27])

                row_col = np.arange(arr_size)
                self.declare_partials('g', ['f_xy'], rows=row_col, cols=row_col, val=mat)
                #self.declare_partials('g', ['f_xy'])

            def compute(self, inputs, outputs):
                x = inputs['f_xy']
                outputs['g'] = x * np.array([3.5, -1.0, 5.0])

        size = 3

        prob = om.Problem()
        model = prob.model

        ivc = om.IndepVarComp()
        ivc.add_output('x', np.ones((size, )))

        model.add_subsystem('p', ivc, promotes=['*'])
        sub = model.add_subsystem('sub', om.Group(), promotes=['*'])

        sub.add_subsystem("parab", DistParab(arr_size=size), promotes=['*'])
        sub.add_subsystem("ndp", NonDistComp(arr_size=size), promotes=['*'])

        model.add_design_var('x', lower=-50.0, upper=50.0)
        model.add_constraint('g', lower=0.0)

        sub.approx_totals(method='fd')

        prob.setup()

        prob.run_model()

        of = ['sub.ndp.g']
        totals = prob.driver._compute_totals(of=of, wrt=['p.x'], return_format='dict')
        assert_near_equal(totals['sub.ndp.g']['p.x'], np.diag([7.0, -2.0, 10.0]), 1e-6)

        assert_check_totals(prob.check_totals())


@unittest.skipUnless(MPI and PETScVector, "MPI and PETSc are required.")
class TestGroupFiniteDifferenceMPI(unittest.TestCase):

    N_PROCS = 2

    def test_indepvarcomp_under_par_sys(self):
        prob = om.Problem()
        prob.model = FanInSubbedIDVC()

        prob.model.approx_totals()
        prob.setup(local_vector_class=vector_class, check=False, mode='rev')
        prob.set_solver_print(level=0)
        prob.run_model()

        J = prob.compute_totals(wrt=['sub.sub1.p1.x', 'sub.sub2.p2.x'], of=['sum.y'])
        assert_near_equal(J['sum.y', 'sub.sub1.p1.x'], [[2.0]], 1.0e-6)
        assert_near_equal(J['sum.y', 'sub.sub2.p2.x'], [[4.0]], 1.0e-6)


@unittest.skipUnless(MPI and  PETScVector, "MPI and PETSc are required.")
class TestGroupCSMPI(unittest.TestCase):

    N_PROCS = 2

    def test_indepvarcomp_under_par_sys_par_cs(self):
        prob = om.Problem()
        prob.model = FanInSubbedIDVC(num_par_fd=2)
        prob.model.approx_totals(method='cs')

        prob.setup(local_vector_class=vector_class, check=False, mode='rev')
        prob.set_solver_print(level=0)
        prob.run_model()

        J = prob.compute_totals(wrt=['sub.sub1.p1.x', 'sub.sub2.p2.x'], of=['sum.y'])
        assert_near_equal(J['sum.y', 'sub.sub1.p1.x'], [[2.0]], 1.0e-6)
        assert_near_equal(J['sum.y', 'sub.sub2.p2.x'], [[4.0]], 1.0e-6)


@unittest.skipUnless(MPI and  PETScVector, "MPI and PETSc are required.")
class TestGroupFDMPI(unittest.TestCase):

    N_PROCS = 2

    def test_indepvarcomp_under_par_sys_par_fd(self):
        prob = om.Problem()
        prob.model = FanInSubbedIDVC(num_par_fd=2)

        prob.model.approx_totals(method='fd')
        prob.setup(local_vector_class=vector_class, check=False, mode='rev')
        prob.set_solver_print(level=0)
        prob.run_model()

        J = prob.compute_totals(wrt=['sub.sub1.p1.x', 'sub.sub2.p2.x'], of=['sum.y'])
        assert_near_equal(J['sum.y', 'sub.sub1.p1.x'], [[2.0]], 1.0e-6)
        assert_near_equal(J['sum.y', 'sub.sub2.p2.x'], [[4.0]], 1.0e-6)


def title(txt):
    """ Provide nice title for parameterized testing."""
    return str(txt).split('.')[-1].replace("'", '').replace('>', '')


class TestGroupComplexStep(unittest.TestCase):

    def setUp(self):

        self.prob = om.Problem()

    def tearDown(self):
        # Global stuff seems to not get cleaned up if test fails.
        try:
            self.prob.model._outputs._under_complex_step = False
        except Exception:
            pass

    def test_paraboloid(self):

        prob = self.prob
        model = prob.model
        model.add_subsystem('p1', om.IndepVarComp('x', 0.0), promotes=['x'])
        model.add_subsystem('p2', om.IndepVarComp('y', 0.0), promotes=['y'])
        model.add_subsystem('comp', Paraboloid(), promotes=['x', 'y', 'f_xy'])

        model.linear_solver = om.ScipyKrylov()
        model.approx_totals(method='cs')

        prob.setup(mode='fwd')
        prob.set_solver_print(level=0)
        prob.run_model()

        of = ['f_xy']
        wrt = ['x', 'y']
        derivs = prob.compute_totals(of=of, wrt=wrt)

        assert_near_equal(derivs['f_xy', 'x'], [[-6.0]], 1e-6)
        assert_near_equal(derivs['f_xy', 'y'], [[8.0]], 1e-6)

        # 1 output x 2 inputs
        self.assertEqual(np.sum(list(v.size for v in derivs.values())), 2)

    def test_paraboloid_subbed(self):

        prob = self.prob
        model = prob.model
        model.add_subsystem('p1', om.IndepVarComp('x', 0.0), promotes=['x'])
        model.add_subsystem('p2', om.IndepVarComp('y', 0.0), promotes=['y'])
        sub = model.add_subsystem('sub', om.Group(), promotes=['x', 'y', 'f_xy'])
        sub.add_subsystem('comp', Paraboloid(), promotes=['x', 'y', 'f_xy'])

        model.linear_solver = om.ScipyKrylov()
        sub.approx_totals(method='cs')

        prob.setup(mode='fwd')
        prob.set_solver_print(level=0)
        prob.run_model()

        of = ['f_xy']
        wrt = ['x', 'y']
        derivs = prob.compute_totals(of=of, wrt=wrt)

        assert_near_equal(derivs['f_xy', 'x'], [[-6.0]], 1e-6)
        assert_near_equal(derivs['f_xy', 'y'], [[8.0]], 1e-6)

        Jfd = sub._jacobian
        assert_near_equal(Jfd['sub.comp.f_xy', 'sub.comp.x'], [[-6.0]], 1e-6)
        assert_near_equal(Jfd['sub.comp.f_xy', 'sub.comp.y'], [[8.0]], 1e-6)

        # 1 output x 2 inputs
        self.assertEqual(len(sub._approx_schemes['cs']._wrt_meta), 2)

    def test_paraboloid_subbed_with_connections(self):

        prob = self.prob
        model = prob.model
        model.add_subsystem('p1', om.IndepVarComp('x', 0.0))
        model.add_subsystem('p2', om.IndepVarComp('y', 0.0))
        sub = model.add_subsystem('sub', om.Group())
        sub.add_subsystem('bx', om.ExecComp('xout = xin'))
        sub.add_subsystem('by', om.ExecComp('yout = yin'))
        sub.add_subsystem('comp', Paraboloid())

        model.connect('p1.x', 'sub.bx.xin')
        model.connect('sub.bx.xout', 'sub.comp.x')
        model.connect('p2.y', 'sub.by.yin')
        model.connect('sub.by.yout', 'sub.comp.y')

        model.linear_solver = om.ScipyKrylov()
        sub.approx_totals(method='cs')

        prob.setup(mode='fwd')
        prob.set_solver_print(level=0)
        prob.run_model()

        of = ['sub.comp.f_xy']
        wrt = ['p1.x', 'p2.y']
        derivs = prob.compute_totals(of=of, wrt=wrt)

        assert_near_equal(derivs['sub.comp.f_xy', 'p1.x'], [[-6.0]], 1e-6)
        assert_near_equal(derivs['sub.comp.f_xy', 'p2.y'], [[8.0]], 1e-6)

        Jfd = sub._jacobian
        assert_near_equal(Jfd['sub.comp.f_xy', 'sub.bx.xin'], [[-6.0]], 1e-6)
        assert_near_equal(Jfd['sub.comp.f_xy', 'sub.by.yin'], [[8.0]], 1e-6)

    def test_array_comp(self):

        class DoubleArrayFD(DoubleArrayComp):

            def compute_partials(self, inputs, partials):
                """
                Override deriv calculation.
                """
                pass

        prob = self.prob
        model = prob.model

        model.add_subsystem('p1', om.IndepVarComp('x1', val=np.ones(2)))
        model.add_subsystem('p2', om.IndepVarComp('x2', val=np.ones(2)))
        comp = model.add_subsystem('comp', DoubleArrayFD())
        model.connect('p1.x1', 'comp.x1')
        model.connect('p2.x2', 'comp.x2')

        model.linear_solver = om.ScipyKrylov()
        model.approx_totals(method='cs')

        model.add_design_var('p1.x1')
        model.add_design_var('p2.x2')
        model.add_constraint('comp.y1')
        model.add_constraint('comp.y2')

        prob.setup()
        prob.run_model()
        model.run_linearize(driver=prob.driver)

        Jfd = model._jacobian.J_dict
        assert_near_equal(Jfd['comp.y1', 'p1.x1'], comp.JJ[0:2, 0:2], 1e-6)
        assert_near_equal(Jfd['comp.y1', 'p2.x2'], comp.JJ[0:2, 2:4], 1e-6)
        assert_near_equal(Jfd['comp.y2', 'p1.x1'], comp.JJ[2:4, 0:2], 1e-6)
        assert_near_equal(Jfd['comp.y2', 'p2.x2'], comp.JJ[2:4, 2:4], 1e-6)

    def test_unit_conv_group(self):

        prob = self.prob
        prob.model.add_subsystem('px1', om.IndepVarComp('x1', 100.0), promotes=['x1'])
        sub1 = prob.model.add_subsystem('sub1', om.Group())
        sub2 = prob.model.add_subsystem('sub2', om.Group())

        sub1.add_subsystem('src', SrcComp())
        sub2.add_subsystem('tgtF', TgtCompF())
        sub2.add_subsystem('tgtC', TgtCompC())
        sub2.add_subsystem('tgtK', TgtCompK())

        prob.model.connect('x1', 'sub1.src.x1')
        prob.model.connect('sub1.src.x2', 'sub2.tgtF.x2')
        prob.model.connect('sub1.src.x2', 'sub2.tgtC.x2')
        prob.model.connect('sub1.src.x2', 'sub2.tgtK.x2')

        sub2.approx_totals(method='cs')

        prob.setup()
        prob.run_model()

        assert_near_equal(prob['sub1.src.x2'], 100.0, 1e-6)
        assert_near_equal(prob['sub2.tgtF.x3'], 212.0, 1e-6)
        assert_near_equal(prob['sub2.tgtC.x3'], 100.0, 1e-6)
        assert_near_equal(prob['sub2.tgtK.x3'], 373.15, 1e-6)

        wrt = ['x1']
        of = ['sub2.tgtF.x3', 'sub2.tgtC.x3', 'sub2.tgtK.x3']
        J = prob.compute_totals(of=of, wrt=wrt, return_format='dict')

        assert_near_equal(J['sub2.tgtF.x3']['x1'][0][0], 1.8, 1e-6)
        assert_near_equal(J['sub2.tgtC.x3']['x1'][0][0], 1.0, 1e-6)
        assert_near_equal(J['sub2.tgtK.x3']['x1'][0][0], 1.0, 1e-6)

        # Check the total derivatives in reverse mode
        prob.setup(mode='rev')
        prob.run_model()
        J = prob.compute_totals(of=of, wrt=wrt, return_format='dict')

        assert_near_equal(J['sub2.tgtF.x3']['x1'][0][0], 1.8, 1e-6)
        assert_near_equal(J['sub2.tgtC.x3']['x1'][0][0], 1.0, 1e-6)
        assert_near_equal(J['sub2.tgtK.x3']['x1'][0][0], 1.0, 1e-6)

    def test_sellar(self):
        # Basic sellar test.

        prob = self.prob
        model = prob.model

        model.add_subsystem('px', om.IndepVarComp('x', 1.0), promotes=['x'])
        model.add_subsystem('pz', om.IndepVarComp('z', np.array([5.0, 2.0])), promotes=['z'])

        model.add_subsystem('d1', SellarDis1withDerivatives(), promotes=['x', 'z', 'y1', 'y2'])
        model.add_subsystem('d2', SellarDis2withDerivatives(), promotes=['z', 'y1', 'y2'])

        model.add_subsystem('obj_cmp', om.ExecComp('obj = x**2 + z[1] + y1 + exp(-y2)',
                                                   z=np.array([0.0, 0.0]), x=0.0),
                            promotes=['obj', 'x', 'z', 'y1', 'y2'])

        model.add_subsystem('con_cmp1', om.ExecComp('con1 = 3.16 - y1'), promotes=['con1', 'y1'])
        model.add_subsystem('con_cmp2', om.ExecComp('con2 = y2 - 24.0'), promotes=['con2', 'y2'])

        prob.model.nonlinear_solver = om.NonlinearBlockGS()
        prob.model.nonlinear_solver.options['atol'] = 1e-50
        prob.model.nonlinear_solver.options['rtol'] = 1e-50

        model.approx_totals(method='cs')

        prob.setup()
        prob.set_solver_print(level=0)
        prob.run_model()

        assert_near_equal(prob['y1'], 25.58830273, .00001)
        assert_near_equal(prob['y2'], 12.05848819, .00001)

        wrt = ['z']
        of = ['obj']

        J = prob.compute_totals(of=of, wrt=wrt, return_format='flat_dict')
        assert_near_equal(J['obj', 'z'][0][0], 9.61001056, .00001)
        assert_near_equal(J['obj', 'z'][0][1], 1.78448534, .00001)

        self.assertFalse(model._vectors['output']['linear']._alloc_complex,
                         msg="Linear vector should not be allocated as complex.")

    def test_desvar_and_response_with_indices(self):

        class ArrayComp2D(om.ExplicitComponent):
            """
            A fairly simple array component.
            """

            def setup(self):

                self.JJ = np.array([[1.0, 3.0, -2.0, 7.0],
                                    [6.0, 2.5, 2.0, 4.0],
                                    [-1.0, 0.0, 8.0, 1.0],
                                    [1.0, 4.0, -5.0, 6.0]])

                # Params
                self.add_input('x1', np.zeros([4]))

                # Unknowns
                self.add_output('y1', np.zeros([4]))

                self.declare_partials(of='*', wrt='*')

            def compute(self, inputs, outputs):
                """
                Execution.
                """
                outputs['y1'] = self.JJ.dot(inputs['x1'])

            def compute_partials(self, inputs, partials):
                """
                Analytical derivatives.
                """
                partials[('y1', 'x1')] = self.JJ

        prob = om.Problem()
        model = prob.model
        mycomp = model.add_subsystem('mycomp', ArrayComp2D(), promotes=['x1', 'y1'])

        model.add_design_var('x1', indices=[1, 3])
        model.add_constraint('y1', indices=[0, 2])

        prob.set_solver_print(level=0)
        model.approx_totals(method='cs')

        prob.setup(check=False, mode='fwd')
        prob.run_model()

        Jbase = mycomp.JJ
        of = ['y1']
        wrt = ['x1']

        J = prob.compute_totals(of=of, wrt=wrt, return_format='flat_dict')
        assert_near_equal(J['y1', 'x1'][0][0], Jbase[0, 1], 1e-8)
        assert_near_equal(J['y1', 'x1'][0][1], Jbase[0, 3], 1e-8)
        assert_near_equal(J['y1', 'x1'][1][0], Jbase[2, 1], 1e-8)
        assert_near_equal(J['y1', 'x1'][1][1], Jbase[2, 3], 1e-8)

    def test_desvar_with_indices(self):
        # Just desvars on this one to cover code missed by desvar+response test.

        class ArrayComp2D(om.ExplicitComponent):
            """
            A fairly simple array component.
            """

            def setup(self):

                self.JJ = np.array([[1.0, 3.0, -2.0, 7.0],
                                    [6.0, 2.5, 2.0, 4.0],
                                    [-1.0, 0.0, 8.0, 1.0],
                                    [1.0, 4.0, -5.0, 6.0]])

                # Params
                self.add_input('x1', np.zeros([4]))

                # Unknowns
                self.add_output('y1', np.zeros([4]))

                self.declare_partials(of='*', wrt='*')

            def compute(self, inputs, outputs):
                """
                Execution.
                """
                outputs['y1'] = self.JJ.dot(inputs['x1'])

            def compute_partials(self, inputs, partials):
                """
                Analytical derivatives.
                """
                partials[('y1', 'x1')] = self.JJ

        prob = om.Problem()
        model = prob.model
        model.add_subsystem('x_param1', om.IndepVarComp('x1', np.ones((4))),
                            promotes=['x1'])
        mycomp = model.add_subsystem('mycomp', ArrayComp2D(), promotes=['x1', 'y1'])

        model.add_design_var('x1', indices=[1, 3])
        model.add_constraint('y1')

        prob.set_solver_print(level=0)
        model.approx_totals(method='cs')

        prob.setup(check=False, mode='fwd')
        prob.run_model()

        Jbase = mycomp.JJ
        of = ['y1']
        wrt = ['x1']

        J = prob.compute_totals(of=of, wrt=wrt, return_format='flat_dict')
        assert_near_equal(J['y1', 'x1'][0][0], Jbase[0, 1], 1e-8)
        assert_near_equal(J['y1', 'x1'][0][1], Jbase[0, 3], 1e-8)
        assert_near_equal(J['y1', 'x1'][2][0], Jbase[2, 1], 1e-8)
        assert_near_equal(J['y1', 'x1'][2][1], Jbase[2, 3], 1e-8)

    def test_newton_with_direct_solver(self):
        # Basic sellar test.

        prob = om.Problem()
        model = prob.model
        sub = model.add_subsystem('sub', om.Group(), promotes=['*'])

        model.add_subsystem('px', om.IndepVarComp('x', 1.0), promotes=['x'])
        model.add_subsystem('pz', om.IndepVarComp('z', np.array([5.0, 2.0])), promotes=['z'])

        sub.add_subsystem('d1', SellarDis1withDerivatives(), promotes=['x', 'z', 'y1', 'y2'])
        sub.add_subsystem('d2', SellarDis2withDerivatives(), promotes=['z', 'y1', 'y2'])

        model.add_subsystem('obj_cmp', om.ExecComp('obj = x**2 + z[1] + y1 + exp(-y2)',
                                                   z=np.array([0.0, 0.0]), x=0.0),
                            promotes=['obj', 'x', 'z', 'y1', 'y2'])

        model.add_subsystem('con_cmp1', om.ExecComp('con1 = 3.16 - y1'), promotes=['con1', 'y1'])
        model.add_subsystem('con_cmp2', om.ExecComp('con2 = y2 - 24.0'), promotes=['con2', 'y2'])

        sub.nonlinear_solver = om.NewtonSolver(solve_subsystems=False)
        sub.linear_solver = om.DirectSolver(assemble_jac=False)
        sub.nonlinear_solver.options['atol'] = 1e-10
        sub.nonlinear_solver.options['rtol'] = 1e-10

        model.approx_totals(method='cs')

        prob.setup()
        prob.set_solver_print(level=0)
        prob.run_model()

        assert_near_equal(prob['y1'], 25.58830273, .00001)
        assert_near_equal(prob['y2'], 12.05848819, .00001)

        wrt = ['z', 'x']
        of = ['obj', 'con1', 'con2']

        J = prob.compute_totals(of=of, wrt=wrt, return_format='flat_dict')
        assert_near_equal(J['obj', 'z'][0][0], 9.61001056, 1.0e-6)
        assert_near_equal(J['obj', 'z'][0][1], 1.78448534, 1.0e-6)
        assert_near_equal(J['obj', 'x'][0][0], 2.98061391, 1.0e-6)
        assert_near_equal(J['con1', 'z'][0][0], -9.61002186, 1.0e-6)
        assert_near_equal(J['con1', 'z'][0][1], -0.78449158, 1.0e-6)
        assert_near_equal(J['con1', 'x'][0][0], -0.98061448, 1.0e-6)

    def test_newton_with_direct_solver_dense(self):
        # Basic sellar test.

        prob = om.Problem()
        model = prob.model
        sub = model.add_subsystem('sub', om.Group(), promotes=['*'])

        model.add_subsystem('px', om.IndepVarComp('x', 1.0), promotes=['x'])
        model.add_subsystem('pz', om.IndepVarComp('z', np.array([5.0, 2.0])), promotes=['z'])

        sub.add_subsystem('d1', SellarDis1withDerivatives(), promotes=['x', 'z', 'y1', 'y2'])
        sub.add_subsystem('d2', SellarDis2withDerivatives(), promotes=['z', 'y1', 'y2'])

        model.add_subsystem('obj_cmp', om.ExecComp('obj = x**2 + z[1] + y1 + exp(-y2)',
                                                   z=np.array([0.0, 0.0]), x=0.0),
                            promotes=['obj', 'x', 'z', 'y1', 'y2'])

        model.add_subsystem('con_cmp1', om.ExecComp('con1 = 3.16 - y1'), promotes=['con1', 'y1'])
        model.add_subsystem('con_cmp2', om.ExecComp('con2 = y2 - 24.0'), promotes=['con2', 'y2'])

        sub.nonlinear_solver = om.NewtonSolver(solve_subsystems=False)
        sub.linear_solver = om.DirectSolver()
        sub.options['assembled_jac_type'] = 'dense'

        sub.nonlinear_solver.options['atol'] = 1e-10
        sub.nonlinear_solver.options['rtol'] = 1e-10

        model.approx_totals(method='cs')

        prob.setup()
        prob.set_solver_print(level=0)
        prob.run_model()

        assert_near_equal(prob['y1'], 25.58830273, .00001)
        assert_near_equal(prob['y2'], 12.05848819, .00001)

        wrt = ['z', 'x']
        of = ['obj', 'con1', 'con2']

        J = prob.compute_totals(of=of, wrt=wrt, return_format='flat_dict')
        assert_near_equal(J['obj', 'z'][0][0], 9.61001056, 1.0e-6)
        assert_near_equal(J['obj', 'z'][0][1], 1.78448534, 1.0e-6)
        assert_near_equal(J['obj', 'x'][0][0], 2.98061391, 1.0e-6)
        assert_near_equal(J['con1', 'z'][0][0], -9.61002186, 1.0e-6)
        assert_near_equal(J['con1', 'z'][0][1], -0.78449158, 1.0e-6)
        assert_near_equal(J['con1', 'x'][0][0], -0.98061448, 1.0e-6)

    def test_newton_with_direct_solver_csc(self):
        # Basic sellar test.

        prob = om.Problem()
        model = prob.model
        sub = model.add_subsystem('sub', om.Group(), promotes=['*'])

        model.add_subsystem('px', om.IndepVarComp('x', 1.0), promotes=['x'])
        model.add_subsystem('pz', om.IndepVarComp('z', np.array([5.0, 2.0])), promotes=['z'])

        sub.add_subsystem('d1', SellarDis1withDerivatives(), promotes=['x', 'z', 'y1', 'y2'])
        sub.add_subsystem('d2', SellarDis2withDerivatives(), promotes=['z', 'y1', 'y2'])

        model.add_subsystem('obj_cmp', om.ExecComp('obj = x**2 + z[1] + y1 + exp(-y2)',
                                                   z=np.array([0.0, 0.0]), x=0.0),
                            promotes=['obj', 'x', 'z', 'y1', 'y2'])

        model.add_subsystem('con_cmp1', om.ExecComp('con1 = 3.16 - y1'), promotes=['con1', 'y1'])
        model.add_subsystem('con_cmp2', om.ExecComp('con2 = y2 - 24.0'), promotes=['con2', 'y2'])

        sub.nonlinear_solver = om.NewtonSolver(solve_subsystems=False)
        sub.linear_solver = om.DirectSolver()
        sub.options['assembled_jac_type'] = 'csc'

        sub.nonlinear_solver.options['atol'] = 1e-10
        sub.nonlinear_solver.options['rtol'] = 1e-10

        model.approx_totals(method='cs')

        prob.setup()
        prob.set_solver_print(level=0)
        prob.run_model()

        assert_near_equal(prob['y1'], 25.58830273, .00001)
        assert_near_equal(prob['y2'], 12.05848819, .00001)

        wrt = ['z', 'x']
        of = ['obj', 'con1', 'con2']

        J = prob.compute_totals(of=of, wrt=wrt, return_format='flat_dict')
        assert_near_equal(J['obj', 'z'][0][0], 9.61001056, 1.0e-6)
        assert_near_equal(J['obj', 'z'][0][1], 1.78448534, 1.0e-6)
        assert_near_equal(J['obj', 'x'][0][0], 2.98061391, 1.0e-6)
        assert_near_equal(J['con1', 'z'][0][0], -9.61002186, 1.0e-6)
        assert_near_equal(J['con1', 'z'][0][1], -0.78449158, 1.0e-6)
        assert_near_equal(J['con1', 'x'][0][0], -0.98061448, 1.0e-6)

    def test_subbed_newton_assembled_csc(self):

        from openmdao.test_suite.components.sellar import SellarDis1withDerivatives, SellarDis2withDerivatives
        class SellarDerivatives(om.Group):

            def setup(self):
                self.add_subsystem('px', om.IndepVarComp('x', 1.0), promotes=['x'])
                self.add_subsystem('pz', om.IndepVarComp('z', np.array([5.0, 2.0])), promotes=['z'])

                self.add_subsystem('d1', SellarDis1withDerivatives(), promotes=['x', 'z', 'y1', 'y2'])
                self.add_subsystem('d2', SellarDis2withDerivatives(), promotes=['z', 'y1', 'y2'])
                sub = self.add_subsystem('sub', om.Group(), promotes=['*'])

                sub.linear_solver = om.DirectSolver(assemble_jac=True)
                sub.options['assembled_jac_type'] = 'csc'

                obj = sub.add_subsystem('obj_cmp', om.ExecComp('obj = x**2 + z[1] + y1 + exp(-y2)', obj=0.0,
                                                         x=0.0, z=np.array([0.0, 0.0]), y1=0.0, y2=0.0),
                                  promotes=['obj', 'x', 'z', 'y1', 'y2'])

                con1 = sub.add_subsystem('con_cmp1', om.ExecComp('con1 = 3.16 - y1', con1=0.0, y1=0.0),
                                  promotes=['con1', 'y1'])
                con2 = sub.add_subsystem('con_cmp2', om.ExecComp('con2 = y2 - 24.0', con2=0.0, y2=0.0),
                                  promotes=['con2', 'y2'])

                obj.declare_partials(of='*', wrt='*', method='cs')
                con1.declare_partials(of='*', wrt='*', method='cs')
                con2.declare_partials(of='*', wrt='*', method='cs')

                self.nonlinear_solver = om.NewtonSolver(solve_subsystems=False)
                self.linear_solver = om.DirectSolver(assemble_jac=False)

        prob = om.Problem()
        prob.model = SellarDerivatives()

        prob.setup(force_alloc_complex=True)

        prob.model.approx_totals(method='cs')

        prob.run_model()

        wrt = ['z', 'x']
        of = ['obj', 'con1', 'con2']

        J = prob.compute_totals(of=of, wrt=wrt, return_format='flat_dict')
        assert_near_equal(J['obj', 'z'][0][0], 9.61001056, 1.0e-8)
        assert_near_equal(J['obj', 'z'][0][1], 1.78448534, 1.0e-8)
        assert_near_equal(J['obj', 'x'][0][0], 2.98061391, 1.0e-8)
        assert_near_equal(J['con1', 'z'][0][0], -9.61002186, 1.0e-8)
        assert_near_equal(J['con1', 'z'][0][1], -0.78449158, 1.0e-8)
        assert_near_equal(J['con1', 'x'][0][0], -0.98061448, 1.0e-8)

    def test_subbed_newton_assembled_dense(self):

        from openmdao.test_suite.components.sellar import SellarDis1withDerivatives, SellarDis2withDerivatives
        class SellarDerivatives(om.Group):

            def setup(self):
                self.add_subsystem('px', om.IndepVarComp('x', 1.0), promotes=['x'])
                self.add_subsystem('pz', om.IndepVarComp('z', np.array([5.0, 2.0])), promotes=['z'])

                self.add_subsystem('d1', SellarDis1withDerivatives(), promotes=['x', 'z', 'y1', 'y2'])
                self.add_subsystem('d2', SellarDis2withDerivatives(), promotes=['z', 'y1', 'y2'])
                sub = self.add_subsystem('sub', om.Group(), promotes=['*'])

                sub.linear_solver = om.DirectSolver(assemble_jac=True)
                sub.options['assembled_jac_type'] = 'dense'

                obj = sub.add_subsystem('obj_cmp', om.ExecComp('obj = x**2 + z[1] + y1 + exp(-y2)', obj=0.0,
                                                         x=0.0, z=np.array([0.0, 0.0]), y1=0.0, y2=0.0),
                                  promotes=['obj', 'x', 'z', 'y1', 'y2'])

                con1 = sub.add_subsystem('con_cmp1', om.ExecComp('con1 = 3.16 - y1', con1=0.0, y1=0.0),
                                  promotes=['con1', 'y1'])
                con2 = sub.add_subsystem('con_cmp2', om.ExecComp('con2 = y2 - 24.0', con2=0.0, y2=0.0),
                                  promotes=['con2', 'y2'])

                obj.declare_partials(of='*', wrt='*', method='cs')
                con1.declare_partials(of='*', wrt='*', method='cs')
                con2.declare_partials(of='*', wrt='*', method='cs')

                self.nonlinear_solver = om.NewtonSolver(solve_subsystems=False)
                self.linear_solver = om.DirectSolver(assemble_jac=False)

        prob = om.Problem()
        prob.model = SellarDerivatives()

        prob.setup(force_alloc_complex=True)

        prob.model.approx_totals(method='cs')

        prob.run_model()

        wrt = ['z', 'x']
        of = ['obj', 'con1', 'con2']

        J = prob.compute_totals(of=of, wrt=wrt, return_format='flat_dict')
        assert_near_equal(J['obj', 'z'][0][0], 9.61001056, 1.0e-8)
        assert_near_equal(J['obj', 'z'][0][1], 1.78448534, 1.0e-8)
        assert_near_equal(J['obj', 'x'][0][0], 2.98061391, 1.0e-8)
        assert_near_equal(J['con1', 'z'][0][0], -9.61002186, 1.0e-8)
        assert_near_equal(J['con1', 'z'][0][1], -0.78449158, 1.0e-8)
        assert_near_equal(J['con1', 'x'][0][0], -0.98061448, 1.0e-8)

    def test_newton_with_krylov_solver(self):
        # Basic sellar test.

        prob = om.Problem()
        model = prob.model
        sub = model.add_subsystem('sub', om.Group(), promotes=['*'])

        model.add_subsystem('px', om.IndepVarComp('x', 1.0), promotes=['x'])
        model.add_subsystem('pz', om.IndepVarComp('z', np.array([5.0, 2.0])), promotes=['z'])

        sub.add_subsystem('d1', SellarDis1withDerivatives(), promotes=['x', 'z', 'y1', 'y2'])
        sub.add_subsystem('d2', SellarDis2withDerivatives(), promotes=['z', 'y1', 'y2'])

        model.add_subsystem('obj_cmp', om.ExecComp('obj = x**2 + z[1] + y1 + exp(-y2)',
                                                   z=np.array([0.0, 0.0]), x=0.0),
                            promotes=['obj', 'x', 'z', 'y1', 'y2'])

        model.add_subsystem('con_cmp1', om.ExecComp('con1 = 3.16 - y1'), promotes=['con1', 'y1'])
        model.add_subsystem('con_cmp2', om.ExecComp('con2 = y2 - 24.0'), promotes=['con2', 'y2'])

        sub.nonlinear_solver = om.NewtonSolver(solve_subsystems=False)
        sub.linear_solver = om.ScipyKrylov()
        sub.nonlinear_solver.options['atol'] = 1e-10
        sub.nonlinear_solver.options['rtol'] = 1e-10
        sub.linear_solver.options['atol'] = 1e-15

        model.approx_totals(method='cs', step=1e-14)

        prob.setup()
        prob.set_solver_print(level=0)
        prob.run_model()

        assert_near_equal(prob['y1'], 25.58830273, .00001)
        assert_near_equal(prob['y2'], 12.05848819, .00001)

        wrt = ['z', 'x']
        of = ['obj', 'con1', 'con2']

        J = prob.compute_totals(of=of, wrt=wrt, return_format='flat_dict')
        assert_near_equal(J['obj', 'z'][0][0], 9.61001056, 1.0e-6)
        assert_near_equal(J['obj', 'z'][0][1], 1.78448534, 1.0e-6)
        assert_near_equal(J['obj', 'x'][0][0], 2.98061391, 1.0e-6)
        assert_near_equal(J['con1', 'z'][0][0], -9.61002186, 1.0e-6)
        assert_near_equal(J['con1', 'z'][0][1], -0.78449158, 1.0e-6)
        assert_near_equal(J['con1', 'x'][0][0], -0.98061448, 1.0e-6)

    def test_newton_with_cscjac_under_cs(self):
        # Basic sellar test.

        prob = om.Problem()
        model = prob.model
        sub = model.add_subsystem('sub', om.Group(), promotes=['*'])

        model.add_subsystem('px', om.IndepVarComp('x', 1.0), promotes=['x'])
        model.add_subsystem('pz', om.IndepVarComp('z', np.array([5.0, 2.0])), promotes=['z'])

        sub.add_subsystem('d1', SellarDis1withDerivatives(), promotes=['x', 'z', 'y1', 'y2'])
        sub.add_subsystem('d2', SellarDis2withDerivatives(), promotes=['z', 'y1', 'y2'])

        model.add_subsystem('obj_cmp', om.ExecComp('obj = x**2 + z[1] + y1 + exp(-y2)',
                                                   z=np.array([0.0, 0.0]), x=0.0),
                            promotes=['obj', 'x', 'z', 'y1', 'y2'])

        model.add_subsystem('con_cmp1', om.ExecComp('con1 = 3.16 - y1'), promotes=['con1', 'y1'])
        model.add_subsystem('con_cmp2', om.ExecComp('con2 = y2 - 24.0'), promotes=['con2', 'y2'])

        sub.nonlinear_solver = om.NewtonSolver(solve_subsystems=False)
        sub.linear_solver = om.ScipyKrylov(assemble_jac=True)
        sub.nonlinear_solver.options['atol'] = 1e-20
        sub.nonlinear_solver.options['rtol'] = 1e-20

        model.approx_totals(method='cs', step=1e-11)

        prob.setup()
        prob.set_solver_print(level=0)
        prob.run_model()

        assert_near_equal(prob['y1'], 25.58830273, .00001)
        assert_near_equal(prob['y2'], 12.05848819, .00001)

        wrt = ['z', 'x']
        of = ['obj', 'con1']

        J = prob.compute_totals(of=of, wrt=wrt, return_format='flat_dict')
        assert_near_equal(J['obj', 'z'][0][0], 9.61001056, .00001)
        assert_near_equal(J['obj', 'z'][0][1], 1.78448534, .00001)
        assert_near_equal(J['obj', 'x'][0][0], 2.98061391, 1.0e-6)
        assert_near_equal(J['con1', 'z'][0][0], -9.61002186, 1.0e-6)
        assert_near_equal(J['con1', 'z'][0][1], -0.78449158, 1.0e-6)
        assert_near_equal(J['con1', 'x'][0][0], -0.98061448, 1.0e-6)

    def test_newton_with_fd_group(self):
        # Basic sellar test.

        prob = om.Problem()
        model = prob.model
        sub = model.add_subsystem('sub', om.Group(), promotes=['*'])
        subfd = sub.add_subsystem('subfd', om.Group(), promotes=['*'])

        model.add_subsystem('px', om.IndepVarComp('x', 1.0), promotes=['x'])
        model.add_subsystem('pz', om.IndepVarComp('z', np.array([5.0, 2.0])), promotes=['z'])

        subfd.add_subsystem('d1', SellarDis1withDerivatives(), promotes=['x', 'z', 'y1', 'y2'])
        subfd.add_subsystem('d2', SellarDis2withDerivatives(), promotes=['z', 'y1', 'y2'])

        model.add_subsystem('obj_cmp', om.ExecComp('obj = x**2 + z[1] + y1 + exp(-y2)',
                                                   z=np.array([0.0, 0.0]), x=0.0),
                            promotes=['obj', 'x', 'z', 'y1', 'y2'])

        model.add_subsystem('con_cmp1', om.ExecComp('con1 = 3.16 - y1'), promotes=['con1', 'y1'])
        model.add_subsystem('con_cmp2', om.ExecComp('con2 = y2 - 24.0'), promotes=['con2', 'y2'])

        # Finite difference for the Newton linear solve only
        subfd.approx_totals(method='fd')

        sub.nonlinear_solver = om.NewtonSolver(solve_subsystems=False)
        sub.nonlinear_solver.options['maxiter'] = 12
        sub.linear_solver = om.DirectSolver(assemble_jac=False)
        sub.nonlinear_solver.options['atol'] = 1e-20
        sub.nonlinear_solver.options['rtol'] = 1e-20

        # Complex Step for top derivatives
        model.approx_totals(method='cs', step=1e-14)

        prob.setup()
        prob.set_solver_print(level=0)
        prob.run_model()

        assert_near_equal(prob['y1'], 25.58830273, .00001)
        assert_near_equal(prob['y2'], 12.05848819, .00001)

        wrt = ['z', 'x']
        of = ['obj', 'con1', 'con2']

        J = prob.compute_totals(of=of, wrt=wrt, return_format='flat_dict')
        assert_near_equal(J['obj', 'z'][0][0], 9.61001056, 1.0e-6)
        assert_near_equal(J['obj', 'z'][0][1], 1.78448534, 1.0e-6)
        assert_near_equal(J['obj', 'x'][0][0], 2.98061391, 1.0e-6)
        assert_near_equal(J['con1', 'z'][0][0], -9.61002186, 1.0e-6)
        assert_near_equal(J['con1', 'z'][0][1], -0.78449158, 1.0e-6)
        assert_near_equal(J['con1', 'x'][0][0], -0.98061448, 1.0e-6)

    def test_nested_complex_step_unsupported(self):
        # Basic sellar test.

        prob = self.prob = om.Problem()
        model = prob.model

        model.add_subsystem('px', om.IndepVarComp('x', 1.0), promotes=['x'])
        model.add_subsystem('pz', om.IndepVarComp('z', np.array([5.0, 2.0])), promotes=['z'])

        model.add_subsystem('d1', SellarDis1CS(), promotes=['x', 'z', 'y1', 'y2'])
        model.add_subsystem('d2', SellarDis2CS(), promotes=['z', 'y1', 'y2'])

        obj = model.add_subsystem('obj_cmp', om.ExecComp('obj = x**2 + z[1] + y1 + exp(-y2)',
                                                   z=np.array([0.0, 0.0]), x=0.0),
                            promotes=['obj', 'x', 'z', 'y1', 'y2'])
        obj.declare_partials(of='*', wrt='*', method='cs')

        con1 = model.add_subsystem('con_cmp1', om.ExecComp('con1 = 3.16 - y1'), promotes=['con1', 'y1'])
        con2 = model.add_subsystem('con_cmp2', om.ExecComp('con2 = y2 - 24.0'), promotes=['con2', 'y2'])

        con1.declare_partials(of='*', wrt='*', method='cs')
        con2.declare_partials(of='*', wrt='*', method='cs')

        prob.model.nonlinear_solver = om.NewtonSolver(solve_subsystems=False)
        prob.model.linear_solver = om.DirectSolver(assemble_jac=False)

        prob.model.approx_totals(method='cs')
        prob.setup()
        prob.set_solver_print(level=0)
        prob.run_model()

        assert_near_equal(prob['y1'], 25.58830273, .00001)
        assert_near_equal(prob['y2'], 12.05848819, .00001)

        wrt = ['z']
        of = ['obj']

        expected_warnings = [(om.DerivativesWarning,
                              'd1: Nested complex step detected. '
                              'Finite difference will be used.'),
                             (om.DerivativesWarning,
                              'd2: Nested complex step detected. '
                              'Finite difference will be used.')]

        with assert_warnings(expected_warnings):
            J = prob.compute_totals(of=of, wrt=wrt, return_format='flat_dict')

        assert_near_equal(J['obj', 'z'][0][0], 9.61001056, .00001)
        assert_near_equal(J['obj', 'z'][0][1], 1.78448534, .00001)

        outs = prob.model.list_outputs(residuals=True, out_stream=None)
        for name, meta in outs:
            val = np.linalg.norm(meta['resids'])
            self.assertLess(val, 1e-8, msg="Check if CS cleans up after itself.")


class TestComponentComplexStep(unittest.TestCase):

    def test_implicit_component(self):

        class TestImplCompArrayDense(TestImplCompArray):

            def setup_partials(self):
                self.declare_partials('*', '*', method='cs')

        prob = self.prob = om.Problem()
        model = prob.model

        model.add_subsystem('p_rhs', om.IndepVarComp('rhs', val=np.ones(2)))
        sub = model.add_subsystem('sub', om.Group())
        comp = sub.add_subsystem('comp', TestImplCompArrayDense())
        model.connect('p_rhs.rhs', 'sub.comp.rhs')

        model.linear_solver = om.ScipyKrylov()

        prob.setup()
        prob.run_model()

        model.run_linearize()

        Jfd = comp._jacobian
        assert_near_equal(Jfd['sub.comp.x', 'sub.comp.rhs'], -np.eye(2), 1e-6)
        assert_near_equal(Jfd['sub.comp.x', 'sub.comp.x'], comp.mtx, 1e-6)

    def test_vector_methods(self):

        class KenComp(om.ExplicitComponent):

            def setup(self):

                self.add_input('x1', np.array([[7.0, 3.0], [2.4, 3.33]]))
                self.add_output('y1', np.zeros((2, 2)))

                self.declare_partials('*', '*', method='cs')

            def compute(self, inputs, outputs):

                x1 = inputs['x1']

                outputs['y1'] = x1

                outputs['y1'][0][0] += 14.0
                outputs['y1'][0][1] *= 3.0
                outputs['y1'][1][0] -= 6.67
                outputs['y1'][1][1] /= 2.34

                outputs['y1'] *= 1.0

        prob = self.prob = om.Problem()
        model = prob.model

        model.add_subsystem('px', om.IndepVarComp('x', val=np.array([[7.0, 3.0], [2.4, 3.33]])))
        model.add_subsystem('comp', KenComp())
        model.connect('px.x', 'comp.x1')

        prob.setup()
        prob.run_model()

        of = ['comp.y1']
        wrt = ['px.x']
        derivs = prob.compute_totals(of=of, wrt=wrt)

        assert_near_equal(derivs['comp.y1', 'px.x'][0][0], 1.0, 1e-6)
        assert_near_equal(derivs['comp.y1', 'px.x'][1][1], 3.0, 1e-6)
        assert_near_equal(derivs['comp.y1', 'px.x'][2][2], 1.0, 1e-6)
        assert_near_equal(derivs['comp.y1', 'px.x'][3][3], 1.0/2.34, 1e-6)

    def test_sellar_comp_cs(self):
        # Basic sellar test.

        prob = self.prob = om.Problem()
        model = prob.model

        model.add_subsystem('px', om.IndepVarComp('x', 1.0), promotes=['x'])
        model.add_subsystem('pz', om.IndepVarComp('z', np.array([5.0, 2.0])), promotes=['z'])

        model.add_subsystem('d1', SellarDis1CS(), promotes=['x', 'z', 'y1', 'y2'])
        model.add_subsystem('d2', SellarDis2CS(), promotes=['z', 'y1', 'y2'])

        model.add_subsystem('obj_cmp', om.ExecComp('obj = x**2 + z[1] + y1 + exp(-y2)',
                                                   z=np.array([0.0, 0.0]), x=0.0),
                            promotes=['obj', 'x', 'z', 'y1', 'y2'])

        model.add_subsystem('con_cmp1', om.ExecComp('con1 = 3.16 - y1'), promotes=['con1', 'y1'])
        model.add_subsystem('con_cmp2', om.ExecComp('con2 = y2 - 24.0'), promotes=['con2', 'y2'])

        prob.model.nonlinear_solver = om.NonlinearBlockGS()
        prob.model.linear_solver = om.DirectSolver(assemble_jac=False)

        prob.setup()
        prob.set_solver_print(level=0)
        prob.run_model()

        assert_near_equal(prob['y1'], 25.58830273, .00001)
        assert_near_equal(prob['y2'], 12.05848819, .00001)

        wrt = ['z']
        of = ['obj']

        J = prob.compute_totals(of=of, wrt=wrt, return_format='flat_dict')
        assert_near_equal(J['obj', 'z'][0][0], 9.61001056, .00001)
        assert_near_equal(J['obj', 'z'][0][1], 1.78448534, .00001)

        outs = prob.model.list_outputs(residuals=True, out_stream=None)
        for name, meta in outs:
            val = np.linalg.norm(meta['resids'])
            self.assertLess(val, 1e-8, msg="Check if CS cleans up after itself.")

    def test_stepsizes_under_complex_step(self):

        class SimpleComp(om.ExplicitComponent):

            def setup(self):
                self.add_input('x', val=1.0)
                self.add_output('y', val=1.0)

                # Need to set step to something other than default because
                #   OpenMDAO now checks to make sure step is different for compute and check
                self.declare_partials(of='y', wrt='x', method='cs', step=1e-20)
                self.count = 0

            def compute(self, inputs, outputs):
                outputs['y'] = 3.0*inputs['x']

                if self.under_complex_step:

                    # Local cs
                    if self.count == 0 and inputs['x'].imag != 1.0e-20:
                        msg = "Wrong stepsize for local CS"
                        raise RuntimeError(msg)

                    # Global cs with default setting.
                    if self.count == 1 and inputs['x'].imag != 1.0e-40:
                        msg = "Wrong stepsize for default global CS"
                        raise RuntimeError(msg)

                    # Global cs with user setting.
                    if self.count == 3 and inputs['x'].imag != 1.0e-12:
                        msg = "Wrong stepsize for user global CS"
                        raise RuntimeError(msg)

                    # Check partials cs with default setting forward.
                    if self.count == 4 and inputs['x'].imag != 1.0e-20:
                        msg = "Wrong stepsize for check partial default CS forward"
                        raise RuntimeError(msg)

                    # Check partials cs with default setting.
                    if self.count == 5 and inputs['x'].imag != 1.0e-40:
                        msg = "Wrong stepsize for check partial default CS"
                        raise RuntimeError(msg)

                    # Check partials cs with user setting forward.
                    if self.count == 6 and inputs['x'].imag != 1.0e-20:
                        msg = "Wrong stepsize for check partial user CS forward"
                        raise RuntimeError(msg)

                    # Check partials cs with user setting.
                    if self.count == 7 and inputs['x'].imag != 1.0e-14:
                        msg = "Wrong stepsize for check partial user CS"
                        raise RuntimeError(msg)

                    self.count += 1

            def compute_partials(self, inputs, partials):
                partials['y', 'x'] = 3.

        prob = om.Problem()
        prob.model.add_subsystem('px', om.IndepVarComp('x', val=1.0))
        prob.model.add_subsystem('comp', SimpleComp())
        prob.model.connect('px.x', 'comp.x')

        prob.model.add_design_var('px.x', lower=-100, upper=100)
        prob.model.add_objective('comp.y')

        prob.setup(force_alloc_complex=True)

        prob.run_model()

        assert_check_totals(prob.check_totals(method='cs', out_stream=None))

        assert_check_totals(prob.check_totals(method='cs', step=1e-12, out_stream=None))

        assert_check_partials(prob.check_partials(method='cs', out_stream=None))

        assert_check_partials(prob.check_partials(method='cs', step=1e-14, out_stream=None))

    def test_partials_bad_sparse_explicit(self):
        class BadSparsityComp(om.ExplicitComponent):

            def setup(self):
                self.sparsity = np.array(
                    [[1, 0, 1, 0, 0, 1, 1],
                     [0, 1, 0, 1, 0, 1, 0],
                     [0, 0, 1, 0, 0, 0, 1],
                     [1, 0, 0, 1, 0, 1, 0],
                     [0, 1, 0, 1, 1, 0, 1]], dtype=int)

                self.add_input('x0', val=np.ones(4))

                self.add_input('x1', val=np.ones(2))
                self.add_input('x2', val=np.ones(1))

                self.add_output('y0', val=np.zeros(2))
                self.add_output('y1', val=np.zeros(3))

                rows, cols = np.nonzero(self.sparsity[0:2, 0:4])
                self.declare_partials(of='y0', wrt='x0', rows=rows, cols=cols, method='cs')
                rows, cols = np.nonzero(self.sparsity[0:2, 4:6])
                self.declare_partials(of='y0', wrt='x1', rows=rows, cols=cols, method='cs')
                rows, cols = np.nonzero(self.sparsity[0:2, 6:7])
                self.declare_partials(of='y0', wrt='x2', rows=rows, cols=cols, method='cs')
                # corrupt the rows/cols
                bad_sparsity = self.sparsity[2:5, 0:4].copy()
                bad_sparsity[1, 3] = 0  # missing nz at col x0[3] row y1[1]
                rows, cols = np.nonzero(bad_sparsity)
                self.declare_partials(of='y1', wrt='x0', rows=rows, cols=cols, method='cs')
                rows, cols = np.nonzero(self.sparsity[2:5, 4:6])
                self.declare_partials(of='y1', wrt='x1', rows=rows, cols=cols, method='cs')
                rows, cols = np.nonzero(self.sparsity[2:5, 6:7])
                self.declare_partials(of='y1', wrt='x2', rows=rows, cols=cols, method='cs')

            def compute(self, inputs, outputs):
                prod = self.sparsity.dot(inputs.asarray())
                start = end = 0
                for i in range(2):
                    outname = 'y%d' % i
                    end += outputs[outname].size
                    outputs[outname] = prod[start:end]
                    start = end

        for compact_print in [True, False]:
            with self.subTest(f'{compact_print=}'):
                prob = om.Problem()
                model = prob.model
                model.add_subsystem('comp', BadSparsityComp())

                prob.setup(check=False, mode='fwd')
                prob.set_solver_print(level=0)
                prob.run_model()

                ss = StringIO()
                prob.check_partials(includes=['comp'], compact_print=compact_print, out_stream=ss)
                lines = ss.getvalue().splitlines()
                for i in range(len(lines)):
                    lines[i] = strip_formatting(lines[i])

                if compact_print:
                    self.assertIn('<BAD SPARSITY>', lines[13])
                    self.assertIn("| y1            | x0             |", lines[13])
                else:
                    self.assertIn("comp: 'y1' wrt 'x0'", lines[58])
                    self.assertIn("Sparsity excludes 1 entries which appear to be non-zero. (Magnitudes exceed 1e-16) *", lines[66])
                    self.assertIn("Rows: [1]", lines[67])
                    self.assertIn("Cols: [3]", lines[68])


class TestComponentRelevance(unittest.TestCase):

    def get_fd_relevance_prob(self, star_partials=False):
        class CompOne(om.ExplicitComponent):

            def setup(self):
                self.n_execs = 0
                self.add_input('a', val=0.0)
                self.add_input('b', val=0.0)
                self.add_output('x', val=0.0)
                self.add_output('y', val=0.0)
                if star_partials:
                    self.declare_partials('*', '*', method='fd')
                else:
                    self.declare_partials('x', 'a', method='fd')
                    self.declare_partials('y', 'b', method='fd')

            def compute(self, inputs, outputs):
                outputs['x'] = inputs['a'] ** 2
                outputs['y'] = inputs['b'] ** 2
                self.n_execs += 1

        prob = om.Problem()
        model = prob.model

        indep = model.add_subsystem('indep', om.IndepVarComp('a', val=0.0))
        indep.add_output('b', val=0.0)
        model.add_subsystem('comp1', CompOne())
        model.add_subsystem('sink1', om.ExecComp('x=a'))
        model.add_subsystem('sink2', om.ExecComp('y=a'))

        model.connect('indep.a', 'comp1.a')
        model.connect('indep.b', 'comp1.b')
        model.connect('comp1.x', 'sink1.a')
        model.connect('comp1.y', 'sink2.a')

        model.add_design_var('indep.a', lower=-100, upper=100)
        model.add_design_var('indep.b', lower=-100, upper=100)
        model.add_constraint('sink1.x', lower=0.0)
        model.add_objective('sink2.y')

        prob.setup()
        prob.run_model()

        return prob

    def test_fd_relevance_star_partials(self):

        prob = self.get_fd_relevance_prob(star_partials=True)
        comp = prob.model.comp1

        count = comp.n_execs
        J = prob.compute_totals(of=['sink1.x', 'sink2.y'], wrt=['indep.a', 'indep.b'])

        assert_near_equal(J['sink1.x', 'indep.a'], [2.0 * prob['indep.a']], 1e-6)
        assert_near_equal(J['sink1.x', 'indep.b'], [[0.]], 1e-6)
        assert_near_equal(J['sink2.y', 'indep.a'], [[0.]], 1e-6)
        assert_near_equal(J['sink2.y', 'indep.b'], [2.0 * prob['indep.b']], 1e-6)

        self.assertEqual(comp.n_execs - count, 2)
        count = comp.n_execs
        J = prob.compute_totals(of=['sink1.x', 'sink2.y'], wrt=['indep.a'])
        self.assertEqual(comp.n_execs - count, 1)

        count = comp.n_execs
        # Because comp1 has declared *, * partials, the relevance graph thinks that all comp1
        # inputs are relevant to all comp1 outputs, so it executes the component once even though
        # internally there is no connection between comp1.b and comp1.x.
        J = prob.compute_totals(of=['sink1.x'], wrt=['indep.b'])
        self.assertEqual(comp.n_execs - count, 1)

    def test_fd_relevance(self):

        prob = self.get_fd_relevance_prob(star_partials=False)
        comp = prob.model.comp1

        count = comp.n_execs
        J = prob.compute_totals(of=['sink1.x', 'sink2.y'], wrt=['indep.a', 'indep.b'])

        assert_near_equal(J['sink1.x', 'indep.a'], [2.0 * prob['indep.a']], 1e-6)
        assert_near_equal(J['sink1.x', 'indep.b'], [[0.]], 1e-6)
        assert_near_equal(J['sink2.y', 'indep.a'], [[0.]], 1e-6)
        assert_near_equal(J['sink2.y', 'indep.b'], [2.0 * prob['indep.b']], 1e-6)

        self.assertEqual(comp.n_execs - count, 2)
        count = comp.n_execs
        J = prob.compute_totals(of=['sink1.x', 'sink2.y'], wrt=['indep.a'])
        self.assertEqual(comp.n_execs - count, 1)

        count = comp.n_execs
        J = prob.compute_totals(of=['sink1.x'], wrt=['indep.b'])
        self.assertEqual(comp.n_execs - count, 0)


class ApproxTotalsFeature(unittest.TestCase):

    def test_basic(self):

        class CompOne(om.ExplicitComponent):

            def setup(self):
                self.add_input('x', val=0.0)
                self.add_output('y', val=np.zeros(25))
                self._exec_count = 0

            def compute(self, inputs, outputs):
                x = inputs['x']
                outputs['y'] = np.arange(25) * x
                self._exec_count += 1

        class CompTwo(om.ExplicitComponent):

            def setup(self):
                self.add_input('y', val=np.zeros(25))
                self.add_output('z', val=0.0)
                self._exec_count = 0

            def compute(self, inputs, outputs):
                y = inputs['y']
                outputs['z'] = np.sum(y)
                self._exec_count += 1

        prob = om.Problem()
        model = prob.model

        model.set_input_defaults('x', 0.0)

        model.add_subsystem('comp1', CompOne(), promotes=['x', 'y'])
        comp2 = model.add_subsystem('comp2', CompTwo(), promotes=['y', 'z'])

        model.linear_solver = om.ScipyKrylov()
        model.approx_totals()

        prob.setup()
        prob.run_model()

        of = ['z']
        wrt = ['x']
        derivs = prob.compute_totals(of=of, wrt=wrt)

        assert_near_equal(derivs['z', 'x'], [[300.0]], 1e-6)
        self.assertEqual(comp2._exec_count, 2)

    def test_basic_cs(self):

        class CompOne(om.ExplicitComponent):

            def setup(self):
                self.add_input('x', val=0.0)
                self.add_output('y', val=np.zeros(25))
                self._exec_count = 0

            def compute(self, inputs, outputs):
                x = inputs['x']
                outputs['y'] = np.arange(25) * x
                self._exec_count += 1

        class CompTwo(om.ExplicitComponent):

            def setup(self):
                self.add_input('y', val=np.zeros(25))
                self.add_output('z', val=0.0)
                self._exec_count = 0

            def compute(self, inputs, outputs):
                y = inputs['y']
                outputs['z'] = np.sum(y)
                self._exec_count += 1

        prob = om.Problem()
        model = prob.model
        model.set_input_defaults('x', 0.0)

        model.add_subsystem('comp1', CompOne(), promotes=['x', 'y'])
        model.add_subsystem('comp2', CompTwo(), promotes=['y', 'z'])

        model.linear_solver = om.ScipyKrylov()
        model.approx_totals(method='cs')

        prob.setup()
        prob.run_model()

        of = ['z']
        wrt = ['x']
        derivs = prob.compute_totals(of=of, wrt=wrt)

        assert_near_equal(derivs['z', 'x'], [[300.0]], 1e-6)

    def test_arguments(self):

        class CompOne(om.ExplicitComponent):

            def setup(self):
                self.add_input('x', val=1.0)
                self.add_output('y', val=np.zeros(25))
                self._exec_count = 0

            def compute(self, inputs, outputs):
                x = inputs['x']
                outputs['y'] = np.arange(25) * x
                self._exec_count += 1

        class CompTwo(om.ExplicitComponent):

            def setup(self):
                self.add_input('y', val=np.zeros(25))
                self.add_output('z', val=0.0)
                self._exec_count = 0

            def compute(self, inputs, outputs):
                y = inputs['y']
                outputs['z'] = np.sum(y)
                self._exec_count += 1

        prob = om.Problem()
        model = prob.model
        model.add_subsystem('comp1', CompOne(), promotes=['x', 'y'])
        model.add_subsystem('comp2', CompTwo(), promotes=['y', 'z'])

        model.linear_solver = om.ScipyKrylov()
        model.approx_totals(method='fd', step=1e-7, form='central', step_calc='rel')

        prob.setup()
        prob.run_model()

        of = ['z']
        wrt = ['x']
        derivs = prob.compute_totals(of=of, wrt=wrt)

        assert_near_equal(derivs['z', 'x'], [[300.0]], 1e-6)

    def test_sellarCS(self):
        # Just tests Newton on Sellar with FD derivs.

        prob = om.Problem()
        prob.model = SellarNoDerivativesCS()

        prob.setup()
        prob.run_model()

        assert_near_equal(prob['y1'], 25.58830273, .00001)
        assert_near_equal(prob['y2'], 12.05848819, .00001)

        # Make sure we aren't iterating like crazy
        self.assertLess(prob.model.nonlinear_solver._iter_count, 9)


class TestFDRelative(unittest.TestCase):

    def test_rel_element(self):
        # Due to the 20 spread in orders of magnitude, accurate derivatives are only possible with
        # rel_element calculation.

        class FDComp(om.ExplicitComponent):

            def initialize(self):
                self.options.declare('vec_size', types=int, default=1)

            def setup(self):
                nn = self.options['vec_size']

                self.add_input('x', np.ones((nn, )))
                self.add_output('y', np.ones((nn, )))

            def setup_partials(self):
                self.declare_partials('y', 'x', method='fd', step=1e-6, step_calc='rel_element')

            def compute(self, inputs, outputs):
                x = inputs['x']
                outputs['y'] = 0.5 * x ** 2

        prob = om.Problem()
        model = prob.model

        model.add_subsystem('comp', FDComp(vec_size=3))

        prob.setup()

        x = np.array([1e10, 1.0, 1e-10])
        prob.set_val('comp.x', x)

        prob.run_model()

        totals = prob.compute_totals(of='comp.y', wrt='comp.x')
        deriv = np.diag(totals['comp.y', 'comp.x'])

        # Testing absolute error due to wide range.
        self.assertTrue(np.abs(deriv[0] - x[0]) < 1e4)
        self.assertTrue(np.abs(deriv[1] - x[1]) < 1e-5)
        self.assertTrue(np.abs(deriv[2] - x[2]) < 1e-10)

    def test_rel_element_central(self):

        class FDComp(om.ExplicitComponent):

            def initialize(self):
                self.options.declare('vec_size', types=int, default=1)

            def setup(self):
                nn = self.options['vec_size']

                self.add_input('x', np.ones((nn, )))
                self.add_output('y', np.ones((nn, )))

            def setup_partials(self):
                self.declare_partials('y', 'x', method='fd', step=1e-6, form='central',
                                      step_calc='rel_element')

            def compute(self, inputs, outputs):
                x = inputs['x']
                outputs['y'] = 0.5 * x ** 2

        prob = om.Problem()
        model = prob.model

        model.add_subsystem('comp', FDComp(vec_size=3))

        prob.setup()

        x = np.array([1e10, 1.0, 1e-10])
        prob.set_val('comp.x', x)

        prob.run_model()

        totals = prob.compute_totals(of='comp.y', wrt='comp.x')
        deriv = np.diag(totals['comp.y', 'comp.x'])

        # Central diff is super accurate on this.
        assert_near_equal(deriv, x, 1e-6)

    def test_minimum_step(self):
        # Test that minimum_step prevents us from taking a zero step.

        class FDComp(om.ExplicitComponent):

            def initialize(self):
                self.options.declare('vec_size', types=int, default=1)

            def setup(self):
                nn = self.options['vec_size']

                self.add_input('x_norm', np.ones((nn, )))
                self.add_input('x_avg', np.ones((nn, )))
                self.add_input('x_element', np.ones((nn, )))
                self.add_output('y', np.ones((nn, )))

            def setup_partials(self):
                self.declare_partials('y', 'x_norm', method='fd', step_calc='rel_legacy')
                self.declare_partials('y', 'x_avg', method='fd', step_calc='rel_avg')
                self.declare_partials('y', 'x_element', method='fd', step_calc='rel_element')

            def compute(self, inputs, outputs):
                x1 = inputs['x_norm']
                x2 = inputs['x_avg']
                x3 = inputs['x_element']
                outputs['y'] = 0.5 * x1 ** 2 + 0.5 * x2 ** 2 + 0.5 * x3 ** 2

        prob = om.Problem()
        model = prob.model

        model.add_subsystem('comp', FDComp(vec_size=3))

        prob.setup()

        prob.set_val('comp.x_norm', np.zeros((3, )))
        prob.set_val('comp.x_avg', np.zeros((3, )))
        prob.set_val('comp.x_element', np.zeros((3, )))

        prob.run_model()

        totals = prob.compute_totals(of='comp.y', wrt=['comp.x_norm', 'comp.x_avg', 'comp.x_element'])

        assert_near_equal(totals['comp.y', 'comp.x_norm'][0][0], 0.0, 1e-6)
        assert_near_equal(totals['comp.y', 'comp.x_avg'][1][1], 0.0, 1e-6)
        assert_near_equal(totals['comp.y', 'comp.x_element'][2][2], 0.0, 1e-6)

    def test_set_minimum_step(self):
        # Test that we can set a new minimum step size.

        class FDComp(om.ExplicitComponent):

            def initialize(self):
                self.options.declare('vec_size', types=int, default=1)

            def setup(self):
                nn = self.options['vec_size']

                self.add_input('x_norm', np.ones((nn, )))
                self.add_input('x_avg', np.ones((nn, )))
                self.add_input('x_element', np.ones((nn, )))
                self.add_output('y', np.ones((nn, )))

            def setup_partials(self):
                self.declare_partials('y', 'x_norm', method='fd', step_calc='rel_legacy', minimum_step=1e-3)
                self.declare_partials('y', 'x_avg', method='fd', step_calc='rel_avg', minimum_step=1e-4)
                self.declare_partials('y', 'x_element', method='fd', step_calc='rel_element', minimum_step=1e-5)

            def compute(self, inputs, outputs):
                x1 = inputs['x_norm']
                x2 = inputs['x_avg']
                x3 = inputs['x_element']
                outputs['y'] = 0.5 * x1 ** 2 + 0.5 * x2 ** 2 + 0.5 * x3 ** 2

        prob = om.Problem()
        model = prob.model

        model.add_subsystem('comp', FDComp(vec_size=3))

        prob.setup()

        prob.set_val('comp.x_norm', np.zeros((3, )))
        prob.set_val('comp.x_avg', np.zeros((3, )))
        prob.set_val('comp.x_element', np.zeros((3, )))

        prob.run_model()

        totals = prob.compute_totals(of='comp.y', wrt=['comp.x_norm', 'comp.x_avg', 'comp.x_element'])

        assert_near_equal(totals['comp.y', 'comp.x_norm'][0][0], 0.0, 5e-4)
        assert_near_equal(totals['comp.y', 'comp.x_avg'][1][1], 0.0, 5e-5)
        assert_near_equal(totals['comp.y', 'comp.x_element'][2][2], 0.0, 5e-6)

    def test_bad_step_calc(self):

        class FDComp(om.ExplicitComponent):

            def initialize(self):
                self.options.declare('vec_size', types=int, default=1)

            def setup(self):
                nn = self.options['vec_size']

                self.add_input('x', np.ones((nn, )))
                self.add_output('y', np.ones((nn, )))

            def setup_partials(self):
                self.declare_partials('y', 'x', method='fd', step_calc='junk')

        prob = om.Problem()
        model = prob.model

        model.add_subsystem('comp', FDComp(vec_size=3))

        prob.setup()
        prob.run_model()

        with self.assertRaises(ValueError) as cm:
            prob.model.run_linearize()

        msg = "'comp' <class FDComp>: 'junk' is not a valid setting for step_calc; must be one of ['abs', 'rel', 'rel_legacy', 'rel_avg', 'rel_element']."
        self.assertEqual(cm.exception.args[0], msg)

    def test_directional_and_rel_element(self):
        # Directional currently not supported with rel_element.
        class FDComp(om.ExplicitComponent):

            def initialize(self):
                self.options.declare('vec_size', types=int, default=1)

            def setup(self):
                nn = self.options['vec_size']

                self.add_input('x_element', np.ones((nn, )))
                self.add_output('y', np.ones((nn, )))

            def setup_partials(self):
                self.set_check_partial_options('x_element', method='fd', step_calc='rel_element', directional=True)

            def compute(self, inputs, outputs):
                x3 = inputs['x_element']
                outputs['y'] = 0.5 * x3 ** 2

            def compute_jacvec_product(self, inputs, d_inputs, d_outputs, mode):
                x3 = inputs['x_element']

                if mode == 'fwd':
                    if 'y' in d_outputs:
                        if 'x_element' in d_inputs:
                            d_outputs['y'] += x3 * d_inputs['x_element']
                elif mode == 'rev':
                    if 'y' in d_outputs:
                        if 'x_element' in d_inputs:
                            d_inputs['x_element'] += x3 * d_outputs['y']

        prob = om.Problem()
        model = prob.model

        model.add_subsystem('comp', FDComp(vec_size=3))

        prob.setup(force_alloc_complex=True)

        x = np.array([1e10, 1.0, 1e-10])
        prob.set_val('comp.x_element', x)

        with self.assertRaises(ValueError) as cm:
            prob.check_partials()

        msg = "'comp' <class FDComp>: Option 'directional' is not supported when 'step_calc' is set to 'rel_element.'"
        self.assertEqual(cm.exception.args[0], msg)

    def test_check_settings_on_comp(self):

        class FDComp(om.ExplicitComponent):

            def initialize(self):
                self.options.declare('vec_size', types=int, default=1)

            def setup(self):
                nn = self.options['vec_size']

                self.add_input('x_element', np.ones((nn, )))
                self.add_output('y', np.ones((nn, )))

            def setup_partials(self):
                nn = self.options['vec_size']
                self.declare_partials('*', '*', rows=np.arange(nn), cols=np.arange(nn))

                self.set_check_partial_options('x_element', method='fd', step_calc='rel_element')

            def compute(self, inputs, outputs):
                x3 = inputs['x_element']
                outputs['y'] = 0.5 * x3 ** 2

            def compute_partials(self, inputs, partials):
                x3 = inputs['x_element']
                partials['y', 'x_element'] = x3

        prob = om.Problem()
        model = prob.model

        model.add_subsystem('comp', FDComp(vec_size=3))

        prob.setup()

        x = np.array([1e10, 1.0, 1e-10])
        prob.set_val('comp.x_element', x)

        prob.run_model()

        partials = prob.check_partials(out_stream=None)

        # Only will pass with rel_element as step_calc.
        # Absolute error on largest element is high, just check relative.
        assert_check_partials(partials, atol=1e10, rtol=1e-6)

    def test_check_partials(self):

        class FDComp(om.ExplicitComponent):

            def initialize(self):
                self.options.declare('vec_size', types=int, default=1)

            def setup(self):
                nn = self.options['vec_size']

                self.add_input('x_element', np.ones((nn, )))
                self.add_output('y', np.ones((nn, )))

            def setup_partials(self):
                nn = self.options['vec_size']
                self.declare_partials('*', '*', rows=np.arange(nn), cols=np.arange(nn))

            def compute(self, inputs, outputs):
                x3 = inputs['x_element']
                outputs['y'] = 0.5 * x3 ** 2

            def compute_partials(self, inputs, partials):
                x3 = inputs['x_element']
                partials['y', 'x_element'] = x3

        prob = om.Problem()
        model = prob.model

        model.add_subsystem('comp', FDComp(vec_size=3))

        prob.setup()

        x = np.array([1e10, 1.0, 1e-10])
        prob.set_val('comp.x_element', x)

        prob.run_model()

        partials = prob.check_partials(out_stream=None, step_calc='rel_element')

        # Only will pass with rel_element as step_calc.
        # Absolute error on largest element is high, just check relative.
        assert_check_partials(partials, atol=1e10, rtol=1e-6)

        # This derivative requires rel_element to be accurate.
        self.assertTrue(np.abs(partials['comp']['y', 'x_element']['J_fd'][2, 2]) < 1e-9)

        totals = prob.check_totals(of='comp.y', wrt=['comp.x_element'], step_calc='rel_element',
                                   out_stream=None)
        assert_check_totals(totals)

        # This derivative requires rel_element to be accurate.
        self.assertTrue(np.abs(totals['comp.y', 'comp.x_element']['J_fd'][2, 2]) < 1e-9)


class ParallelFDParametricTestCase(unittest.TestCase):

    @parametric_suite(
        assembled_jac=[False],
        jacobian_type=['dense'],
        partial_type=['array'],
        partial_method=['fd', 'cs'],
        num_var=[3],
        var_shape=[(2, 3), (2,)],
        connection_type=['explicit'],
        run_by_default=True,
    )
    def test_subset(self, param_instance):
        param_instance.linear_solver_class = om.DirectSolver
        param_instance.linear_solver_options = {}  # defaults not valid for DirectSolver

        param_instance.setup()
        problem = param_instance.problem
        model = problem.model

        expected_values = model.expected_values
        if expected_values:
            actual = {key: problem[key] for key in expected_values}
            assert_near_equal(actual, expected_values, 1e-4)

        expected_totals = model.expected_totals
        if expected_totals:
            # Forward Derivatives Check
            totals = param_instance.compute_totals('fwd')
            assert_near_equal(totals, expected_totals, 1e-4)

            # Reverse Derivatives Check
            totals = param_instance.compute_totals('rev')
            assert_near_equal(totals, expected_totals, 1e-4)


class CheckTotalsParallelGroup(unittest.TestCase):

    N_PROCS = 3

    def test_vois_in_parallelgroup(self):
        class PassThruComp(om.ExplicitComponent):
            def initialize(self):
                self.options.declare('time', default=3.0)
                self.options.declare('size', default=1)

            def setup(self):
                size = self.options['size']
                self.add_input('x', shape=size)
                self.add_output('y', shape=size)
                self.declare_partials('y', 'x')

            def compute(self, inputs, outputs):
                waittime = self.options['time']
                if not inputs._under_complex_step:
                    print('sleeping: ')
                    time.sleep(waittime)
                outputs['y'] = inputs['x']

            def compute_partials(self, inputs, J):
                size = self.options['size']
                J['y', 'x'] = np.eye(size)

        model = om.Group()
        iv = om.IndepVarComp()
        size = 1
        iv.add_output('x', val=3.0 * np.ones((size, )))
        model.add_subsystem('iv', iv)
        pg = model.add_subsystem('pg', om.ParallelGroup(), promotes=['*'])
        pg.add_subsystem('dc1', PassThruComp(size=size, time=0.0))
        pg.add_subsystem('dc2', PassThruComp(size=size, time=0.0))
        pg.add_subsystem('dc3', PassThruComp(size=size, time=0.0))
        model.connect('iv.x', ['dc1.x', 'dc2.x', 'dc3.x'])
        model.add_subsystem('adder', om.ExecComp('z = sum(y1)+sum(y2)+sum(y3)', y1={'val': np.zeros((size, ))},
                                                                                y2={'val': np.zeros((size, ))},
                                                                                y3={'val': np.zeros((size, ))}))
        model.connect('dc1.y', 'adder.y1')
        model.connect('dc2.y', 'adder.y2')
        model.connect('dc3.y', 'adder.y3')

        model.add_design_var('iv.x', lower=-1.0, upper=1.0)
        # this objective works fine
        # model.add_objective('adder.z')

        # this objective raises a concatenation error whether under fd or cs
        # issue 1403
        model.add_objective('dc1.y')

        # for some reason this constraint is fine even though only lives on proc 3
        model.add_constraint('dc3.y', lower=-1.0, upper=1.0)

        prob = om.Problem(model=model)
        prob.setup(force_alloc_complex=True)
        prob.run_model()
        assert_check_totals(prob.check_totals(method='cs', out_stream=None))

class CheckTotalsIndices(unittest.TestCase):

    def test_w_indices(self):
        # just checks for indexing error.  Doesn't raise exception if derivs are wrong.
        class TopComp(om.ExplicitComponent):

            def setup(self):

                size = 10

                self.add_input('c_ae_C', np.zeros(size))
                self.add_input('theta_c2_C', np.zeros(size))
                self.add_output('c_ae', np.zeros(size))

            def compute(self, inputs, outputs):
                pass

            def compute_partials(self, inputs, partials):
                pass

        prob = om.Problem()
        model = prob.model

        model.add_subsystem('tcomp', TopComp())

        # setting indices here caused an indexing error later on
        model.add_design_var('tcomp.theta_c2_C', lower=-20., upper=20., indices=range(2, 9))
        model.add_constraint('tcomp.c_ae', lower=0.e0,)

        prob.setup()

        prob.run_model()
        assert_check_totals(prob.check_totals(compact_print=True, out_stream=None))


def _setup_1ivc_fdgroupwithpar_1sink(size=7):

    prob = om.Problem()
    model = prob.model

    model.add_subsystem('p', om.IndepVarComp('x', np.ones((size, ))))

    sub = model.add_subsystem('sub', om.Group())
    par = sub.add_subsystem('par', om.ParallelGroup())
    par.add_subsystem('C1', om.ExecComp('y = 2.*x', shape=size))
    par.add_subsystem('C2', om.ExecComp('y = 3.*x', shape=size))

    sub.add_subsystem('C3', om.ExecComp('y = x1 + x2', shape=size))

    model.connect('p.x', 'sub.par.C1.x')
    model.connect('p.x', 'sub.par.C2.x')
    model.connect('sub.par.C1.y', 'sub.C3.x1')
    model.connect('sub.par.C2.y', 'sub.C3.x2')

    model.add_design_var('p.x', lower=-50.0, upper=50.0)
    model.add_constraint('sub.par.C1.y', lower=0.0)
    model.add_constraint('sub.par.C2.y', lower=0.0)
    model.add_objective('sub.C3.y', index=-1)

    sub.approx_totals(method='fd')

    return prob

def _setup_2ivcs_fdgroupwithpar_1sink(size=7):
    prob = om.Problem()
    model = prob.model

    model.add_subsystem('p', om.IndepVarComp('x', np.ones((size, ))))
    model.add_subsystem('p2', om.IndepVarComp('x', np.ones((size, ))))

    sub = model.add_subsystem('sub', om.Group())
    par = sub.add_subsystem('par', om.ParallelGroup())
    par.add_subsystem('C1', om.ExecComp('y = 2.*x', shape=size))
    par.add_subsystem('C2', om.ExecComp('y = 3.*x', shape=size))
    sub.add_subsystem('C3', om.ExecComp('y = x1 + x2', shape=size))

    model.connect('p.x', 'sub.par.C1.x')
    model.connect('p2.x', 'sub.par.C2.x')
    model.connect('sub.par.C1.y', 'sub.C3.x1')
    model.connect('sub.par.C2.y', 'sub.C3.x2')

    model.add_design_var('p.x', lower=-50.0, upper=50.0)
    model.add_design_var('p2.x', lower=-50.0, upper=50.0)
    model.add_constraint('sub.par.C1.y', lower=0.0)
    model.add_constraint('sub.par.C2.y', lower=0.0)
    model.add_objective('sub.C3.y', index=-1)

    sub.approx_totals(method='fd')

    return prob


def _setup_1ivc_dum_fdgroupwithpar_1sink(size=7):
    prob = om.Problem()
    model = prob.model

    model.add_subsystem('p', om.IndepVarComp('x', np.ones((size, ))))

    model.add_subsystem('dum', om.ExecComp('y = x', shape=size))

    sub = model.add_subsystem('sub', om.Group())
    par = sub.add_subsystem('par', om.ParallelGroup())
    par.add_subsystem('C1', om.ExecComp('y = 2.*x', shape=size))
    par.add_subsystem('C2', om.ExecComp('y = 3.*x', shape=size))

    sub.add_subsystem('C3', om.ExecComp('y = x1 + x2', shape=size))

    model.connect('p.x', 'dum.x')
    model.connect('dum.y', 'sub.par.C1.x')
    model.connect('dum.y', 'sub.par.C2.x')
    model.connect('sub.par.C1.y', 'sub.C3.x1')
    model.connect('sub.par.C2.y', 'sub.C3.x2')

    model.add_design_var('p.x', lower=-50.0, upper=50.0)
    model.add_constraint('sub.par.C1.y', lower=0.0)
    model.add_constraint('sub.par.C2.y', lower=0.0)
    model.add_objective('sub.C3.y', index=-1)

    sub.approx_totals(method='fd')

    return prob

def _setup_1ivc_dum_fdgroupwithpar_1sink_c4(size=7):

    prob = om.Problem()
    model = prob.model

    ivc = om.IndepVarComp()
    ivc.add_output('x', np.ones((size, )))

    model.add_subsystem('p', ivc)
    model.add_subsystem('dum', om.ExecComp('y = x', shape=size))
    sub = model.add_subsystem('sub', om.Group())
    par = sub.add_subsystem('par', om.ParallelGroup())
    par.add_subsystem('C1', om.ExecComp('y = 2.*x', shape=size))
    par.add_subsystem('C2', om.ExecComp('y = 3.*x', shape=size))
    sub.add_subsystem('C3', om.ExecComp('y = x1 + x2', shape=size))
    model.add_subsystem('C4', om.ExecComp('y = x', shape=size))

    model.connect('p.x', 'dum.x')
    model.connect('dum.y', 'sub.par.C1.x')
    model.connect('dum.y', 'sub.par.C2.x')
    model.connect('sub.par.C1.y', 'sub.C3.x1')
    model.connect('sub.par.C2.y', 'sub.C3.x2')
    model.connect('sub.C3.y', 'C4.x')

    model.add_design_var('p.x', lower=-50.0, upper=50.0)
    model.add_constraint('sub.par.C1.y', lower=0.0)
    model.add_constraint('sub.par.C2.y', lower=0.0)
    model.add_objective('C4.y', index=-1)

    sub.approx_totals(method='fd')

    return prob


def _setup_2ivcs_fdgroupwithpar_2sinks(size=7):
    prob = om.Problem()
    model = prob.model

    model.add_subsystem('p', om.IndepVarComp('x', np.ones((size, ))))
    model.add_subsystem('p2', om.IndepVarComp('x', np.ones((size, ))))

    sub = model.add_subsystem('sub', om.Group())
    par = sub.add_subsystem('par', om.ParallelGroup())
    par.add_subsystem('C1', om.ExecComp('y = 2.*x', shape=size))
    par.add_subsystem('C2', om.ExecComp('y = 3.*x', shape=size))

    sub.add_subsystem('C3', om.ExecComp('y = x*.5', shape=size))
    sub.add_subsystem('C4', om.ExecComp('y = x*3.', shape=size))

    model.connect('p.x', 'sub.par.C1.x')
    model.connect('p2.x', 'sub.par.C2.x')
    model.connect('sub.par.C1.y', 'sub.C3.x')
    model.connect('sub.par.C2.y', 'sub.C4.x')

    model.add_design_var('p.x', lower=-50.0, upper=50.0)
    model.add_design_var('p2.x', lower=-50.0, upper=50.0)
    model.add_constraint('sub.par.C1.y', lower=0.0)
    model.add_constraint('sub.par.C2.y', lower=0.0)
    model.add_constraint('sub.C4.y', lower=0.0)
    model.add_objective('sub.C3.y', index=-1)

    sub.approx_totals(method='fd')

    return prob


def _setup_2ivcs_fdgroupwith2pars_2sinks(size=7):
    prob = om.Problem()
    model = prob.model

    model.add_subsystem('p', om.IndepVarComp('x', np.ones((size, ))))
    model.add_subsystem('p2', om.IndepVarComp('x', np.ones((size, ))))

    sub = model.add_subsystem('sub', om.Group())
    par = sub.add_subsystem('par', om.ParallelGroup())
    par.add_subsystem('C1', om.ExecComp('y = 2.*x', shape=size))
    par.add_subsystem('C2', om.ExecComp('y = 3.*x', shape=size))

    par2 = sub.add_subsystem('par2', om.ParallelGroup())
    par2.add_subsystem('C1a', om.ExecComp('y = 2.*x', shape=size))
    par2.add_subsystem('C2a', om.ExecComp('y = 3.*x', shape=size))

    sub.add_subsystem('C3', om.ExecComp('y = x*.5', shape=size))
    sub.add_subsystem('C4', om.ExecComp('y = x*3.', shape=size))

    model.connect('p.x', 'sub.par.C1.x')
    model.connect('p2.x', 'sub.par.C2.x')
    model.connect('sub.par.C1.y', 'sub.par2.C1a.x')
    model.connect('sub.par.C2.y', 'sub.par2.C2a.x')
    model.connect('sub.par2.C1a.y', 'sub.C3.x')
    model.connect('sub.par2.C2a.y', 'sub.C4.x')

    model.add_design_var('p.x', lower=-50.0, upper=50.0)
    model.add_design_var('p2.x', lower=-50.0, upper=50.0)
    model.add_constraint('sub.par.C1.y', lower=0.0)
    model.add_constraint('sub.par.C2.y', lower=0.0)
    model.add_constraint('sub.par2.C1a.y', lower=0.0)
    model.add_constraint('sub.par2.C2a.y', lower=0.0)
    model.add_constraint('sub.C4.y', lower=0.0)
    model.add_objective('sub.C3.y', index=-1)

    sub.approx_totals(method='fd')

    return prob


def _setup_2ivcs_fdgroupwith2pars_1sink(size=7):
    prob = om.Problem()
    model = prob.model

    model.add_subsystem('p', om.IndepVarComp('x', np.ones((size, ))))
    model.add_subsystem('p2', om.IndepVarComp('x', np.ones((size, ))))

    sub = model.add_subsystem('sub', om.Group())
    par = sub.add_subsystem('par', om.ParallelGroup())
    par.add_subsystem('C1', om.ExecComp('y = 2.*x', shape=size))
    par.add_subsystem('C2', om.ExecComp('y = 3.*x', shape=size))

    par2 = sub.add_subsystem('par2', om.ParallelGroup())
    par2.add_subsystem('C1a', om.ExecComp('y = 2.*x', shape=size))
    par2.add_subsystem('C2a', om.ExecComp('y = 3.*x', shape=size))

    sub.add_subsystem('C3', om.ExecComp('y = x1 + x2', shape=size))

    model.connect('p.x', 'sub.par.C1.x')
    model.connect('p2.x', 'sub.par.C2.x')
    model.connect('sub.par.C1.y', 'sub.par2.C1a.x')
    model.connect('sub.par.C2.y', 'sub.par2.C2a.x')
    model.connect('sub.par2.C1a.y', 'sub.C3.x1')
    model.connect('sub.par2.C2a.y', 'sub.C3.x2')

    model.add_design_var('p.x', lower=-50.0, upper=50.0)
    model.add_design_var('p2.x', lower=-50.0, upper=50.0)
    model.add_constraint('sub.par.C1.y', lower=0.0)
    model.add_constraint('sub.par.C2.y', lower=0.0)
    model.add_constraint('sub.par2.C1a.y', lower=0.0)
    model.add_constraint('sub.par2.C2a.y', lower=0.0)
    model.add_objective('sub.C3.y', index=-1)

    sub.approx_totals(method='fd')

    return prob


def _setup_2ivcs_fdgroupwithcrisscrosspars_2sinks(size=7):
    prob = om.Problem()
    model = prob.model

    model.add_subsystem('p', om.IndepVarComp('x', np.ones((size, ))))
    model.add_subsystem('p2', om.IndepVarComp('x', np.ones((size, ))))

    sub = model.add_subsystem('sub', om.Group())
    par = sub.add_subsystem('par', om.ParallelGroup())
    par.add_subsystem('C1', om.ExecComp('y = 2.*x', shape=size))
    par.add_subsystem('C2', om.ExecComp('y = 3.*x', shape=size))

    par2 = sub.add_subsystem('par2', om.ParallelGroup())
    par2.add_subsystem('C1a', om.ExecComp('y = 2.*x', shape=size))
    par2.add_subsystem('C2a', om.ExecComp('y = 3.*x', shape=size))

    sub.add_subsystem('C3', om.ExecComp('y = x*.5', shape=size))
    sub.add_subsystem('C4', om.ExecComp('y = x*3.', shape=size))

    model.connect('p.x', 'sub.par.C1.x')
    model.connect('p2.x', 'sub.par.C2.x')
    model.connect('sub.par.C1.y', 'sub.par2.C2a.x')
    model.connect('sub.par.C2.y', 'sub.par2.C1a.x')
    model.connect('sub.par2.C1a.y', 'sub.C3.x')
    model.connect('sub.par2.C2a.y', 'sub.C4.x')

    model.add_design_var('p.x', lower=-50.0, upper=50.0)
    model.add_design_var('p2.x', lower=-50.0, upper=50.0)
    model.add_constraint('sub.par.C1.y', lower=0.0)
    model.add_constraint('sub.par.C2.y', lower=0.0)
    model.add_constraint('sub.par2.C1a.y', lower=0.0)
    model.add_constraint('sub.par2.C2a.y', lower=0.0)
    model.add_constraint('sub.C4.y', lower=0.0)
    model.add_objective('sub.C3.y', index=-1)

    sub.approx_totals(method='fd')

    return prob

def _setup_2ivcs_fdgroupwithcrisscrosspars_1sink(size=7):
    prob = om.Problem()
    model = prob.model

    model.add_subsystem('p', om.IndepVarComp('x', np.ones((size, ))))
    model.add_subsystem('p2', om.IndepVarComp('x', np.ones((size, ))))

    sub = model.add_subsystem('sub', om.Group())
    par = sub.add_subsystem('par', om.ParallelGroup())
    par.add_subsystem('C1', om.ExecComp('y = 2.*x', shape=size))
    par.add_subsystem('C2', om.ExecComp('y = 3.*x', shape=size))

    par2 = sub.add_subsystem('par2', om.ParallelGroup())
    par2.add_subsystem('C1a', om.ExecComp('y = 2.*x', shape=size))
    par2.add_subsystem('C2a', om.ExecComp('y = 3.*x', shape=size))

    sub.add_subsystem('C3', om.ExecComp('y = x1 + x2', shape=size))

    model.connect('p.x', 'sub.par.C1.x')
    model.connect('p2.x', 'sub.par.C2.x')
    model.connect('sub.par.C1.y', 'sub.par2.C2a.x')
    model.connect('sub.par.C2.y', 'sub.par2.C1a.x')
    model.connect('sub.par2.C1a.y', 'sub.C3.x1')
    model.connect('sub.par2.C2a.y', 'sub.C3.x2')

    model.add_design_var('p.x', lower=-50.0, upper=50.0)
    model.add_design_var('p2.x', lower=-50.0, upper=50.0)
    model.add_constraint('sub.par.C1.y', lower=0.0)
    model.add_constraint('sub.par.C2.y', lower=0.0)
    model.add_constraint('sub.par2.C1a.y', lower=0.0)
    model.add_constraint('sub.par2.C2a.y', lower=0.0)
    model.add_objective('sub.C3.y', index=-1)

    sub.approx_totals(method='fd')

    return prob


@unittest.skipUnless(MPI and PETScVector, "MPI and PETSc are required.")
class TestFDWithParallelSubGroups(unittest.TestCase):

    N_PROCS = 2

    def test_group_fd_inner_par_fwd(self):
        prob = _setup_1ivc_fdgroupwithpar_1sink(size=7)
        prob.setup(mode='fwd', force_alloc_complex=True)
        prob.run_model()
        assert_check_totals(prob.check_totals(method='fd', out_stream=None), atol=3e-6)

    def test_group_fd_inner_par_rev(self):
        prob = _setup_1ivc_fdgroupwithpar_1sink(size=7)
        prob.setup(mode='rev', force_alloc_complex=True)
        prob.run_model()
        assert_check_totals(prob.check_totals(method='fd', out_stream=None), atol=3e-6)

    def test_group_fd_inner_par2_fwd(self):
        prob = _setup_2ivcs_fdgroupwithpar_2sinks(size=7)
        prob.setup(mode='fwd', force_alloc_complex=True)
        prob.run_model()
        assert_check_totals(prob.check_totals(method='fd', out_stream=None), atol=3e-6)

    def test_group_fd_inner_par2_rev(self):
        prob = _setup_2ivcs_fdgroupwithpar_2sinks(size=7)
        prob.setup(mode='rev', force_alloc_complex=True)
        prob.run_model()
        assert_check_totals(prob.check_totals(method='fd', out_stream=None), atol=3e-6)

    def test_2ivcs_fdgroupwithcrisscrosspars_2sinks_fwd(self):
        prob = _setup_2ivcs_fdgroupwithcrisscrosspars_2sinks(size=7)
        prob.setup(mode='fwd', force_alloc_complex=True)
        prob.run_model()
        assert_check_totals(prob.check_totals(method='fd', out_stream=None), atol=3e-6)

    def test_2ivcs_fdgroupwithcrisscrosspars_2sinks_rev(self):
        prob = _setup_2ivcs_fdgroupwithcrisscrosspars_2sinks(size=7)
        prob.setup(mode='rev', force_alloc_complex=True)
        prob.run_model()
        assert_check_totals(prob.check_totals(method='fd', out_stream=None), atol=3e-6)

    def test_2ivcs_fdgroupwith2pars_2sinks_fwd(self):
        prob = _setup_2ivcs_fdgroupwith2pars_2sinks(size=7)
        prob.setup(mode='fwd', force_alloc_complex=True)
        prob.run_model()
        assert_check_totals(prob.check_totals(method='fd', out_stream=None), atol=3e-6)

    def test_2ivcs_fdgroupwith2pars_2sinks_rev(self):
        prob = _setup_2ivcs_fdgroupwith2pars_2sinks(size=7)
        prob.setup(mode='rev', force_alloc_complex=True)
        prob.run_model()
        assert_check_totals(prob.check_totals(method='fd', out_stream=None), atol=3e-6)

    def test_2ivcs_fdgroupwithcrisscrosspars_1sink_fwd(self):
        prob = _setup_2ivcs_fdgroupwithcrisscrosspars_1sink(size=7)
        prob.setup(mode='fwd', force_alloc_complex=True)
        prob.run_model()
        prob.compute_totals()
        assert_check_totals(prob.check_totals(method='fd', out_stream=None), atol=3e-6)

    def test_2ivcs_fdgroupwithcrisscrosspars_1sink_rev(self):
        prob = _setup_2ivcs_fdgroupwithcrisscrosspars_1sink(size=7)
        prob.setup(mode='rev', force_alloc_complex=True)
        prob.run_model()
        assert_check_totals(prob.check_totals(method='fd', out_stream=None), atol=3e-6)

    def test_2ivcs_fdgroupwith2pars_1sink_fwd(self):
        prob = _setup_2ivcs_fdgroupwith2pars_1sink(size=7)
        prob.setup(mode='fwd', force_alloc_complex=True)
        prob.run_model()
        assert_check_totals(prob.check_totals(method='fd', out_stream=None), atol=3e-6)

    def test_2ivcs_fdgroupwith2pars_1sink_rev(self):
        prob = _setup_2ivcs_fdgroupwith2pars_1sink(size=7)
        prob.setup(mode='rev', force_alloc_complex=True)
        prob.run_model()
        assert_check_totals(prob.check_totals(method='fd', out_stream=None), atol=3e-6)

    def test_group_fd_inner_par2ivcs_fwd(self):
        prob = _setup_2ivcs_fdgroupwithpar_1sink(size=7)
        prob.setup(mode='fwd', force_alloc_complex=True)
        prob.run_model()
        assert_check_totals(prob.check_totals(method='fd', out_stream=None), atol=3e-6)

    def test_group_fd_inner_par2ivcs_rev(self):
        prob = _setup_2ivcs_fdgroupwithpar_1sink(size=7)
        prob.setup(mode='rev', force_alloc_complex=True)
        prob.run_model()
        assert_check_totals(prob.check_totals(method='fd', out_stream=None), atol=3e-6)

    def test_group_fd_inner_par_indirect_fwd(self):
        prob = _setup_1ivc_dum_fdgroupwithpar_1sink(size=7)
        prob.setup(mode='fwd', force_alloc_complex=True)
        prob.run_model()
        assert_check_totals(prob.check_totals(method='fd', out_stream=None), atol=3e-6)

    def test_group_fd_inner_par_indirect_rev(self):
        prob = _setup_1ivc_dum_fdgroupwithpar_1sink(size=7)
        prob.setup(mode='rev', force_alloc_complex=True)
        prob.run_model()
        assert_check_totals(prob.check_totals(method='fd', out_stream=None), atol=3e-6)

    def test_group_fd_inner_par_indirect2_fwd(self):
        prob = _setup_1ivc_dum_fdgroupwithpar_1sink_c4(size=7)
        prob.setup(mode='fwd', force_alloc_complex=True)
        prob.run_model()
        assert_check_totals(prob.check_totals(method='fd', out_stream=None), atol=3e-6)

    def test_group_fd_inner_par_indirect2_rev(self):
        prob = _setup_1ivc_dum_fdgroupwithpar_1sink_c4(size=7)
        prob.setup(mode='rev', force_alloc_complex=True)
        prob.run_model()
        assert_check_totals(prob.check_totals(method='fd', out_stream=None), atol=3e-6)


@unittest.skipUnless(MPI and PETScVector, "MPI and PETSc are required.")
class TestFDWithParallelSubGroups4(TestFDWithParallelSubGroups):

    N_PROCS = 4


if __name__ == "__main__":
    unittest.main()
