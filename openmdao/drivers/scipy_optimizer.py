"""
OpenMDAO Wrapper for the scipy.optimize.minimize family of local optimizers.
"""

import sys
from packaging.version import Version

import numpy as np
from scipy import __version__ as scipy_version
from scipy.optimize import minimize

from openmdao.core.constants import INF_BOUND
from openmdao.core.driver import Driver, RecordingDebugging
from openmdao.core.group import Group
from openmdao.utils.class_util import WeakMethodWrapper
from openmdao.utils.mpi import MPI


# Optimizers in scipy.minimize
_optimizers = {'Nelder-Mead', 'Powell', 'CG', 'BFGS', 'Newton-CG', 'L-BFGS-B',
               'TNC', 'COBYLA', 'SLSQP'}
if Version(scipy_version) >= Version("1.1"):  # Only available in newer versions
    _optimizers.add('trust-constr')

# For 'basinhopping' and 'shgo' gradients are used only in the local minimization
_gradient_optimizers = {'CG', 'BFGS', 'Newton-CG', 'L-BFGS-B', 'TNC', 'SLSQP', 'dogleg',
                        'trust-ncg', 'trust-constr', 'basinhopping', 'shgo'}
_hessian_optimizers = {'trust-constr', 'trust-ncg'}
_bounds_optimizers = {'L-BFGS-B', 'TNC', 'SLSQP', 'trust-constr', 'dual_annealing', 'shgo',
                      'differential_evolution', 'basinhopping', 'Nelder-Mead'}
if Version(scipy_version) >= Version("1.11"):
    # COBYLA supports bounds starting with SciPy Version 1.11
    _bounds_optimizers |= {'COBYLA'}

_constraint_optimizers = {'COBYLA', 'SLSQP', 'trust-constr', 'shgo'}
_constraint_grad_optimizers = _gradient_optimizers & _constraint_optimizers
if Version(scipy_version) >= Version("1.4"):
    _constraint_optimizers.add('differential_evolution')
    _constraint_grad_optimizers.add('differential_evolution')

if Version(scipy_version) >= Version("1.14"):
    # COBYLA supports bounds starting with SciPy Version 1.14
    _optimizers.add('COBYQA')
    _bounds_optimizers |= {'COBYQA'}
    _constraint_optimizers |= {'COBYQA'}

_eq_constraint_optimizers = {'SLSQP', 'trust-constr'}
_global_optimizers = {'differential_evolution', 'basinhopping'}
if Version(scipy_version) >= Version("1.2"):  # Only available in newer versions
    _global_optimizers |= {'shgo', 'dual_annealing'}

# Global optimizers and optimizers in minimize
_all_optimizers = _optimizers | _global_optimizers

# These require Hessian or Hessian-vector product, so they are not supported
# right now.
# dual-annealing and basinhopping not supported yet
_unsupported_optimizers = {'dogleg', 'trust-ncg'}

# With "old-style" a constraint is a dictionary, with "new-style" an object
# With "old-style" a bound is a tuple, with "new-style" a Bounds instance
# In principle now everything can work with "old-style"
# These settings have no effect to the optimizers implemented before SciPy 1.1
_supports_new_style = {'trust-constr'}
if Version(scipy_version) >= Version("1.4"):
    _supports_new_style.add('differential_evolution')
if Version(scipy_version) >= Version("1.14"):
    _supports_new_style.add('COBYQA')
_use_new_style = True  # Recommended to set to True

CITATIONS = """
@article{Hwang_maud_2018
 author = {Hwang, John T. and Martins, Joaquim R.R.A.},
 title = "{A Computational Architecture for Coupling Heterogeneous
          Numerical Models and Computing Coupled Derivatives}",
 journal = "{ACM Trans. Math. Softw.}",
 volume = {44},
 number = {4},
 month = jun,
 year = {2018},
 pages = {37:1--37:39},
 articleno = {37},
 numpages = {39},
 doi = {10.1145/3182393},
 publisher = {ACM},
"""


class ScipyOptimizeDriver(Driver):
    """
    Driver wrapper for the scipy.optimize.minimize family of local optimizers.

    Inequality constraints are supported by COBYLA and SLSQP,
    but equality constraints are only supported by SLSQP. None of the other
    optimizers support constraints.

    ScipyOptimizeDriver supports the following:
        equality_constraints
        inequality_constraints

    Parameters
    ----------
    **kwargs : dict of keyword arguments
        Keyword arguments that will be mapped into the Driver options.

    Attributes
    ----------
    fail : bool
        Flag that indicates failure of most recent optimization.
    iter_count : int
        Counter for function evaluations.
    _scipy_optimize_result : OptimizeResult
        Result returned from scipy.optimize call.
    opt_settings : dict
        Dictionary of solver-specific options. See the scipy.optimize.minimize documentation.
    _check_jac : bool
        Used internally to control when to perform singular checks on computed total derivs.
    _con_cache : dict
        Cached result of constraint evaluations because scipy asks for them in a separate function.
    _con_idx : dict
        Used for constraint bookkeeping in the presence of 2-sided constraints.
    _grad_cache : {}
        Cached result of nonlinear constraint derivatives because scipy asks for them in a separate
        function.
    _exc_info : 3 item tuple
        Storage for exception and traceback information.
    _obj_and_nlcons : list
        List of objective + nonlinear constraints. Used to compute total derivatives
        for all except linear constraints.
    _dvlist : list
        Copy of _designvars.
    _lincongrad_cache : np.ndarray
        Pre-calculated gradients of linear constraints.
    _desvar_array_cache : np.ndarray
        Cached array for setting design variables.
    """

    def __init__(self, **kwargs):
        """
        Initialize the ScipyOptimizeDriver.
        """
        super().__init__(**kwargs)

        # What we support
        self.supports['optimization'] = True
        self.supports['inequality_constraints'] = True
        self.supports['equality_constraints'] = True
        self.supports['two_sided_constraints'] = True
        self.supports['linear_constraints'] = True
        self.supports['simultaneous_derivatives'] = True

        # What we don't support
        self.supports['multiple_objectives'] = False
        self.supports['active_set'] = False
        self.supports['integer_design_vars'] = False
        self.supports['distributed_design_vars'] = False
        self.supports._read_only = True

        # The user places optimizer-specific settings in here.
        self.opt_settings = {}

        self._scipy_optimize_result = None
        self._grad_cache = None
        self._con_cache = None
        self._con_idx = {}
        self._obj_and_nlcons = None
        self._dvlist = None
        self._lincongrad_cache = None
        self._desvar_array_cache = None
        self.fail = False
        self.iter_count = 0
        self._check_jac = False
        self._exc_info = None
        self._total_jac_format = 'array'

        self.cite = CITATIONS

    def _declare_options(self):
        """
        Declare options before kwargs are processed in the init method.
        """
        self.options.declare('optimizer', 'SLSQP', values=_all_optimizers,
                             desc='Name of optimizer to use')
        self.options.declare('tol', 1.0e-6, lower=0.0,
                             desc='Tolerance for termination. For detailed '
                             'control, use solver-specific options.')
        self.options.declare('maxiter', 200, lower=0,
                             desc='Maximum number of iterations.')
        self.options.declare('disp', True, types=bool,
                             desc='Set to False to prevent printing of Scipy convergence messages')
        self.options.declare('singular_jac_behavior', default='warn',
                             values=['error', 'warn', 'ignore'],
                             desc='Defines behavior of a zero row/col check after first call to'
                             'compute_totals:'
                             'error - raise an error.'
                             'warn - raise a warning.'
                             "ignore - don't perform check.")
        self.options.declare('singular_jac_tol', default=1e-16,
                             desc='Tolerance for zero row/column check.')

    def _get_name(self):
        """
        Get name of current optimizer.

        Returns
        -------
        str
            The name of the current optimizer.
        """
        return "ScipyOptimize_" + self.options['optimizer']

    def _setup_driver(self, problem):
        """
        Prepare the driver for execution.

        This is the final thing to run during setup.

        Parameters
        ----------
        problem : <Problem>
            Pointer
        """
        super()._setup_driver(problem)
        opt = self.options['optimizer']

        self.supports._read_only = False
        self.supports['gradients'] = opt in _gradient_optimizers
        self.supports['inequality_constraints'] = opt in _constraint_optimizers
        self.supports['two_sided_constraints'] = opt in _constraint_optimizers
        self.supports['equality_constraints'] = opt in _eq_constraint_optimizers
        self.supports._read_only = True
        self._check_jac = self.options['singular_jac_behavior'] in ['error', 'warn']

        # Raises error if multiple objectives are not supported, but more objectives were defined.
        if not self.supports['multiple_objectives'] and len(self._objs) > 1:
            msg = '{} currently does not support multiple objectives.'
            raise RuntimeError(msg.format(self.msginfo))

        # Since COBYLA did not support bounds in versions of SciPy prior to 1.11, we need to
        # add to the _cons metadata for any bounds that need to be translated into a constraint
        if opt == 'COBYLA' and Version(scipy_version) < Version("1.11"):
            for name, meta in self._designvars.items():
                lower = meta['lower']
                upper = meta['upper']
                if isinstance(lower, np.ndarray) or lower > -INF_BOUND \
                        or isinstance(upper, np.ndarray) or upper < INF_BOUND:
                    self._cons[name] = meta.copy()
                    self._cons[name]['equals'] = None
                    self._cons[name]['linear'] = True
                    self._cons[name]['alias'] = None

    def run(self):
        """
        Optimize the problem using selected Scipy optimizer.

        Returns
        -------
        bool
            Failure flag; True if failed to converge, False is successful.
        """
        self.result.reset()
        prob = self._problem()
        opt = self.options['optimizer']
        model = prob.model
        self.iter_count = 0
        self._total_jac = None
        self._total_jac_linear = None
        self._desvar_array_cache = None

        self._check_for_missing_objective()
        self._check_for_invalid_desvar_values()

        # Initial Run
        with RecordingDebugging(self._get_name(), self.iter_count, self):
            with model._relevance.nonlinear_active('iter'):
                self._run_solve_nonlinear()
            self.iter_count += 1

        self._con_cache = self.get_constraint_values()
        desvar_vals = self.get_design_var_values()
        self._dvlist = list(self._designvars)

        # maxiter and disp get passed into scipy with all the other options.
        if 'maxiter' not in self.opt_settings:  # lets you override the value in options
            self.opt_settings['maxiter'] = self.options['maxiter']
        self.opt_settings['disp'] = self.options['disp']

        # Size Problem
        ndesvar = 0
        for desvar in self._designvars.values():
            size = desvar['global_size'] if desvar['distributed'] else desvar['size']
            ndesvar += size
        x_init = np.empty(ndesvar)

        # Initial Design Vars
        i = 0
        use_bounds = (opt in _bounds_optimizers)
        if use_bounds:
            bounds = []
        else:
            bounds = None

        for name, meta in self._designvars.items():
            size = meta['global_size'] if meta['distributed'] else meta['size']
            x_init[i:i + size] = desvar_vals[name]
            i += size

            # Bounds if our optimizer supports them
            if use_bounds:
                meta_low = meta['lower']
                meta_high = meta['upper']
                for j in range(size):

                    if isinstance(meta_low, np.ndarray):
                        p_low = meta_low[j]
                    else:
                        p_low = meta_low

                    if isinstance(meta_high, np.ndarray):
                        p_high = meta_high[j]
                    else:
                        p_high = meta_high

                    bounds.append((p_low, p_high))

        if use_bounds and (opt in _supports_new_style) and _use_new_style:
            # For 'trust-constr' it is better to use the new type bounds, because it seems to work
            # better (for the current examples in the tests) with the "keep_feasible" option
            try:
                from scipy.optimize import Bounds
                from scipy.optimize._constraints import old_bound_to_new
            except ImportError:
                msg = ('The "trust-constr" optimizer is supported for SciPy 1.1.0 and above. '
                       'The installed version is {}')
                raise ImportError(msg.format(scipy_version))

            # Convert "old-style" bounds to "new_style" bounds
            lower, upper = old_bound_to_new(bounds)  # tuple, tuple
            keep_feasible = self.opt_settings.get('keep_feasible_bounds', True)
            bounds = Bounds(lb=lower, ub=upper, keep_feasible=keep_feasible)

        # Constraints
        constraints = []
        nl_i = 1  # start at 1 since row 0 is the objective.  Constraints start at row 1.
        lin_i = 0  # counter for linear constraint jacobian
        lincons = []  # list of linear constraints
        self._obj_and_nlcons = list(self._objs)

        if opt in _constraint_optimizers:
            # get list of linear constraints and precalculate gradients for them (if any)
            if opt in _constraint_grad_optimizers:
                lincons = [name for name, meta in self._cons.items() if meta.get('linear')]
            else:
                lincons = []

            if lincons:
                lincongrad = self._lincongrad_cache = \
                    self._compute_totals(of=lincons, wrt=self._dvlist, return_format='array')
            else:
                self._lincongrad_cache = None

            # map constraints to index and instantiate constraints for scipy
            for name, meta in self._cons.items():
                if meta['indices'] is not None:
                    meta['size'] = size = meta['indices'].indexed_src_size
                else:
                    size = meta['global_size'] if meta['distributed'] else meta['size']
                upper = meta['upper']
                lower = meta['lower']
                equals = meta['equals']
                linear = name in lincons

                if linear:
                    self._con_idx[name] = lin_i
                    lin_i += size
                else:
                    self._obj_and_nlcons.append(name)
                    self._con_idx[name] = nl_i
                    nl_i += size

                # In scipy constraint optimizers take constraints in two separate formats

                if opt in _supports_new_style and _use_new_style:
                    # Type of constraints is list of NonlinearConstraint and/or LinearConstraint
                    try:
                        from scipy.optimize import NonlinearConstraint, LinearConstraint
                    except ImportError:
                        msg = ('The "trust-constr" optimizer is supported for SciPy 1.1.0 and'
                               'above. The installed version is {}')
                        raise ImportError(msg.format(scipy_version))

                    if equals is not None:
                        lb = ub = equals
                    else:
                        lb = lower
                        ub = upper

                    if linear:
                        # LinearConstraint
                        con = LinearConstraint(A=lincongrad[self._con_idx[name]],
                                               lb=lower, ub=upper, keep_feasible=True)
                    else:
                        # NonlinearConstraint
                        # Loop over every index separately,
                        # because scipy calls each constraint by index.
                        for j in range(size):
                            # TODO add option for Hessian
                            # Double-sided constraints are accepted by the algorithm
                            args = [name, False, j]
                            con = NonlinearConstraint(
                                fun=signature_extender(
                                    WeakMethodWrapper(self, '_con_val_func'), args),
                                lb=lb, ub=ub,
                                jac=signature_extender(
                                    WeakMethodWrapper(self, '_congradfunc'), args)
                            )

                    constraints.append(con)
                else:
                    # Type of constraints is list of dict

                    # Loop over every index separately,
                    # because scipy calls each constraint by index.
                    for j in range(size):
                        con_dict = {}
                        if meta['equals'] is not None:
                            con_dict['type'] = 'eq'
                        else:
                            con_dict['type'] = 'ineq'
                        con_dict['fun'] = WeakMethodWrapper(self, '_confunc')
                        if opt in _constraint_grad_optimizers:
                            con_dict['jac'] = WeakMethodWrapper(self, '_congradfunc')
                        con_dict['args'] = [name, False, j]
                        constraints.append(con_dict)

                        if isinstance(upper, np.ndarray):
                            upper = upper[j]

                        if isinstance(lower, np.ndarray):
                            lower = lower[j]

                        dblcon = (upper < INF_BOUND) and (lower > -INF_BOUND)

                        # Add extra constraint if double-sided
                        if dblcon:
                            dcon_dict = {}
                            dcon_dict['type'] = 'ineq'
                            dcon_dict['fun'] = WeakMethodWrapper(self, '_confunc')
                            if opt in _constraint_grad_optimizers:
                                dcon_dict['jac'] = WeakMethodWrapper(self, '_congradfunc')
                            dcon_dict['args'] = [name, True, j]
                            constraints.append(dcon_dict)

        # Provide gradients for optimizers that support it
        if opt in _gradient_optimizers:
            jac = self._gradfunc
        else:
            jac = None

        # Hessian calculation method for optimizers, which require it
        if opt in _hessian_optimizers:
            if 'hess' in self.opt_settings:
                hess = self.opt_settings.pop('hess')
            else:
                # Defaults to BFGS, if not in opt_settings
                from scipy.optimize import BFGS
                hess = BFGS()
        else:
            hess = None

        # compute dynamic simul deriv coloring if option is set
        prob.get_total_coloring(self._coloring_info, run_model=False)

        # optimize
        try:
            if opt in _optimizers:
                if prob.comm.rank != 0:
                    self.opt_settings['disp'] = False

                result = minimize(self._objfunc, x_init,
                                  # args=(),
                                  method=opt,
                                  jac=jac,
                                  hess=hess,
                                  # hessp=None,
                                  bounds=bounds,
                                  constraints=constraints,
                                  tol=self.options['tol'],
                                  # callback=None,
                                  options=self.opt_settings)
            elif opt == 'basinhopping':
                from scipy.optimize import basinhopping

                def fun(x):
                    return self._objfunc(x), jac(x)

                if 'minimizer_kwargs' not in self.opt_settings:
                    self.opt_settings['minimizer_kwargs'] = {"method": "L-BFGS-B", "jac": True}
                self.opt_settings.pop('maxiter')  # It does not have this argument

                def accept_test(f_new, x_new, f_old, x_old):
                    # Used to implement bounds besides the original functionality
                    if bounds is not None:
                        bound_check = all([b[0] <= xi <= b[1] for xi, b in zip(x_new, bounds)])
                        user_test = self.opt_settings.pop('accept_test', None)  # callable
                        # has to satisfy both the bounds and the acceptance test defined by the
                        # user
                        if user_test is not None:
                            test_res = user_test(f_new, x_new, f_old, x_old)
                            if test_res == 'force accept':
                                return test_res
                            else:  # result is boolean
                                return bound_check and test_res
                        else:  # no user acceptance test, check only the bounds
                            return bound_check
                    else:
                        return True

                result = basinhopping(fun, x_init,
                                      accept_test=accept_test,
                                      **self.opt_settings)
            elif opt == 'dual_annealing':
                from scipy.optimize import dual_annealing
                self.opt_settings.pop('disp')  # It does not have this argument
                # There is no "options" param, so "opt_settings" can be used to set the (many)
                # keyword arguments
                result = dual_annealing(self._objfunc,
                                        bounds=bounds,
                                        **self.opt_settings)
            elif opt == 'differential_evolution':
                from scipy.optimize import differential_evolution
                # There is no "options" param, so "opt_settings" can be used to set the (many)
                # keyword arguments
                result = differential_evolution(self._objfunc, bounds=bounds,
                                                constraints=constraints,
                                                **self.opt_settings)
            elif opt == 'shgo':
                from scipy.optimize import shgo
                kwargs = dict()
                for option in ('minimizer_kwargs', 'sampling_method ', 'n', 'iters'):
                    if option in self.opt_settings:
                        kwargs[option] = self.opt_settings[option]
                # Set the Jacobian and the Hessian to the value calculated in OpenMDAO
                if 'minimizer_kwargs' not in kwargs or kwargs['minimizer_kwargs'] is None:
                    kwargs['minimizer_kwargs'] = {}
                kwargs['minimizer_kwargs'].setdefault('jac', jac)
                kwargs['minimizer_kwargs'].setdefault('hess', hess)
                # Objective function tolerance
                self.opt_settings['f_tol'] = self.options['tol']
                result = shgo(self._objfunc,
                              bounds=bounds,
                              constraints=constraints,
                              options=self.opt_settings,
                              **kwargs)
            else:
                msg = 'Optimizer "{}" is not implemented yet. Choose from: {}'
                raise NotImplementedError(msg.format(opt, _all_optimizers))

        # If an exception was swallowed in one of our callbacks, we want to raise it
        # rather than the cryptic message from scipy.
        except Exception as msg:
            if self._exc_info is None:
                raise

        if self._exc_info is not None:
            self._reraise()

        self._scipy_optimize_result = result

        if hasattr(result, 'success'):
            self.fail = not result.success
            if self.fail:
                if prob.comm.rank == 0:
                    print('Optimization FAILED.')
                    print(result.message)
                    print('-' * 35)

            elif self.options['disp']:
                if prob.comm.rank == 0:
                    print('Optimization Complete')
                    print('-' * 35)
        else:
            self.fail = True  # It is not known, so the worst option is assumed
            if prob.comm.rank == 0:
                print('Optimization Complete (success not known)')
                print(result.message)
                print('-' * 35)

        return self.fail

    def _update_design_vars(self, x_new):
        """
        Update the design variables in the model.

        Parameters
        ----------
        x_new : ndarray
            Array containing input values at new design point.
        """
        i = 0
        for name, meta in self._designvars.items():
            size = meta['size']
            self.set_design_var(name, x_new[i:i + size])
            i += size

    def _objfunc(self, x_new):
        """
        Evaluate and return the objective function.

        Model is executed here.

        Parameters
        ----------
        x_new : ndarray
            Array containing input values at new design point.

        Returns
        -------
        float
            Value of the objective function evaluated at the new design point.
        """
        model = self._problem().model

        try:

            # Pass in new inputs
            if MPI:
                model.comm.Bcast(x_new, root=0)

            if self._desvar_array_cache is None:
                self._desvar_array_cache = np.empty(x_new.shape, dtype=x_new.dtype)

            self._desvar_array_cache[:] = x_new

            self._update_design_vars(x_new)

            with RecordingDebugging(self._get_name(), self.iter_count, self):
                self.iter_count += 1
                with model._relevance.nonlinear_active('iter'):
                    self._run_solve_nonlinear()

            # Get the objective function evaluations
            for obj in self.get_objective_values().values():
                f_new = obj
                break

            self._con_cache = self.get_constraint_values()

        except Exception:
            if self._exc_info is None:  # only record the first one
                self._exc_info = sys.exc_info()
            return 0

        # print("Functions calculated")
        # rank = MPI.COMM_WORLD.rank if MPI else 0
        # print(rank, '   xnew', x_new)
        # print(rank, '   fnew', f_new)

        return f_new

    def _con_val_func(self, x_new, name, dbl, idx):
        """
        Return the value of the constraint function requested in args.

        The lower or upper bound is **not** subtracted from the value. Used for optimizers,
        which take the bounds of the constraints (e.g. trust-constr)

        Parameters
        ----------
        x_new : ndarray
            Array containing input values at new design point.
        name : str
            Name of the constraint to be evaluated.
        dbl : bool
            True if double sided constraint.
        idx : float
            Contains index into the constraint array.

        Returns
        -------
        float
            Value of the constraint function.
        """
        if self.options['optimizer'] in ['differential_evolution', 'COBYQA']:
            # the DE opt will not have called this, so we do it here to update DV/resp values
            self._objfunc(x_new)

        return self._con_cache[name][idx]

    def _confunc(self, x_new, name, dbl, idx):
        """
        Return the value of the constraint function requested in args.

        Note that this function is called for each constraint, so the model is only run when the
        objective is evaluated.

        Parameters
        ----------
        x_new : ndarray
            Array containing input values at new design point.
        name : str
            Name of the constraint to be evaluated.
        dbl : bool
            True if double sided constraint.
        idx : float
            Contains index into the constraint array.

        Returns
        -------
        float
            Value of the constraint function.
        """
        if self._exc_info is not None:
            self._reraise()

        cons = self._con_cache
        meta = self._cons[name]

        # Equality constraints
        equals = meta['equals']
        if equals is not None:
            if isinstance(equals, np.ndarray):
                equals = equals[idx]
            return cons[name][idx] - equals

        # Note, scipy defines constraints to be satisfied when positive,
        # which is the opposite of OpenMDAO.
        upper = meta['upper']
        if isinstance(upper, np.ndarray):
            upper = upper[idx]

        lower = meta['lower']
        if isinstance(lower, np.ndarray):
            lower = lower[idx]

        if dbl or (lower <= -INF_BOUND):
            return upper - cons[name][idx]
        else:
            return cons[name][idx] - lower

    def _gradfunc(self, x_new):
        """
        Evaluate and return the gradient for the objective.

        Gradients for the constraints are also calculated and cached here.

        Parameters
        ----------
        x_new : ndarray
            Array containing input values at new design point.

        Returns
        -------
        ndarray
            Gradient of objective with respect to input array.
        """
        prob = self._problem()
        model = prob.model

        try:
            grad = self._compute_totals(of=self._obj_and_nlcons, wrt=self._dvlist,
                                        return_format=self._total_jac_format)
            self._grad_cache = grad

            # First time through, check for zero row/col.
            if self._check_jac and self._total_jac is not None:
                for subsys in model.system_iter(include_self=True, recurse=True, typ=Group):
                    if subsys._has_approx:
                        break
                else:
                    raise_error = self.options['singular_jac_behavior'] == 'error'
                    self._total_jac.check_total_jac(raise_error=raise_error,
                                                    tol=self.options['singular_jac_tol'])
                self._check_jac = False

        except Exception:
            if self._exc_info is None:  # only record the first one
                self._exc_info = sys.exc_info()
            return np.array([[]])

        # print("Gradients calculated for objective")
        # print('   xnew', x_new)
        # print('   grad', grad[0, :])

        return grad[0, :]

    def _congradfunc(self, x_new, name, dbl, idx):
        """
        Return the cached gradient of the constraint function.

        Note, scipy calls the constraints one at a time, so the gradient is cached when the
        objective gradient is called.

        Parameters
        ----------
        x_new : ndarray
            Array containing input values at new design point.
        name : str
            Name of the constraint to be evaluated.
        dbl : bool
            Denotes if a constraint is double-sided or not.
        idx : float
            Contains index into the constraint array.

        Returns
        -------
        float
            Gradient of the constraint function wrt all inputs.
        """
        if self._exc_info is not None:
            self._reraise()

        meta = self._cons[name]

        if meta['linear']:
            grad = self._lincongrad_cache
        else:
            if self._grad_cache is None:
                # _gradfunc has not been called, meaning gradients are not
                # used for the objective but are needed for the constraints
                self._gradfunc(x_new)
            grad = self._grad_cache

        grad_idx = self._con_idx[name] + idx

        # print("Constraint Gradient returned")
        # print('   xnew', x_new)
        # print('   grad', name, 'idx', idx, grad[grad_idx, :])

        # Equality constraints
        if meta['equals'] is not None:
            return grad[grad_idx, :]

        # Note, scipy defines constraints to be satisfied when positive,
        # which is the opposite of OpenMDAO.
        lower = meta['lower']
        if isinstance(lower, np.ndarray):
            lower = lower[idx]

        if dbl or (lower <= -INF_BOUND):
            return -grad[grad_idx, :]
        else:
            return grad[grad_idx, :]

    def _reraise(self):
        """
        Reraise any exception encountered when scipy calls back into our method.
        """
        exc_info = self._exc_info
        self._exc_info = None  # clear since we're done with it
        raise exc_info[1].with_traceback(exc_info[2])


def signature_extender(fcn, extra_args):
    """
    Closure function, which appends extra arguments to the original function call.

    The first argument is the design vector. The possible extra arguments from the callback
    of :func:`scipy.optimize.minimize` are not passed to the function.

    Some algorithms take a sequence of :class:`~scipy.optimize.NonlinearConstraint` as input
    for the constraints. For this class it is not possible to pass additional arguments.
    With this function the signature will be correct for both scipy and the driver.

    Parameters
    ----------
    fcn : callable
        Function, which takes the design vector as the first argument.
    extra_args : tuple or list
        Extra arguments for the function.

    Returns
    -------
    callable
        The function with the signature expected by the driver.
    """
    def closure(x, *args):
        return fcn(x, *extra_args)

    return closure
