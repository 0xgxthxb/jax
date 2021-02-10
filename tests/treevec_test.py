# Copyright 2020 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from functools import partial
import unittest

import numpy as np
from absl.testing import absltest, parameterized
import pytest

import jax
from jax import linear_util as lu
from jax import api
from jax import disable_jit, jit, make_jaxpr
from jax import lax
from jax.config import config
from jax.experimental.treevec import (
    TRIVIAL_TREEDEF, convert_vectorized_tree, convert_leaf_array,
    restore_tree, tree_vectorize,
)
import jax.numpy as jnp
from jax.tree_util import tree_flatten, tree_structure
import jax.test_util as jtu

config.parse_flags_with_absl()


class FlatTreeTest(jtu.JaxTestCase):

  def assertTreeStateEqual(self, expected, actual, check_dtypes):
    actual_treedefs, actual_leafshapes, actual_leaves = actual
    expected_treedefs, expected_leafshapes, expected_leaves = expected
    self.assertEqual(actual_treedefs, expected_treedefs)
    self.assertEqual(actual_leafshapes, expected_leafshapes)
    self.assertEqual(actual_leaves.keys(), expected_leaves.keys())
    for key in actual_leaves:
      self.assertArraysEqual(actual_leaves[key], expected_leaves[key],
                             check_dtypes=check_dtypes)

  def assertTreeEqual(self, expected, actual, check_dtypes):
    expected_leaves, expected_treedef = tree_flatten(expected)
    actual_leaves, actual_treedef = tree_flatten(actual)
    self.assertEqual(actual_treedef, expected_treedef)
    for actual_leaf, expected_leaf in zip(actual_leaves, expected_leaves):
      self.assertArraysEqual(actual_leaf, expected_leaf, check_dtypes=check_dtypes)

  @parameterized.parameters([
      (1.0, ((), (), {(): 1.0})),
      (np.arange(3.0), ((TRIVIAL_TREEDEF,), (((3,),),), {(0,): np.arange(3.0)})),
      (np.array([[1, 2, 3], [4, 5, 6]]),
       ((TRIVIAL_TREEDEF, TRIVIAL_TREEDEF), (((2,),), ((3,),)),
        {(0, 0): np.array([[1, 2, 3], [4, 5, 6]])})),
  ])
  def test_convert_leaf_array(self, leaf, expected):
    actual = convert_leaf_array(leaf)
    self.assertTreeStateEqual(actual, expected, check_dtypes=True)
    treedefs, _, leaves = actual
    roundtripped = restore_tree(treedefs, leaves)
    self.assertArraysEqual(roundtripped, leaf, check_dtypes=True)

  @parameterized.parameters([
      (1.0, ((TRIVIAL_TREEDEF,), (((),),), {(0,): np.array(1.0)})),
      ({'a': 0, 'b': np.array([1.0]), 'c': np.array([2, 3])},
        ((tree_structure({'a': 0, 'b': 0, 'c': 0}),),
          (((), (1,), (2,)),),
          {(0,): np.array(0.0),
           (1,): np.array([1.0]),
           (2,): np.array([2.0, 3.0])})),
  ])
  def test_convert_vectorized_tree(self, tree, expected):
    actual = convert_vectorized_tree(tree)
    self.assertTreeStateEqual(actual, expected, check_dtypes=True)
    treedefs, _, leaves = actual
    roundtripped = restore_tree(treedefs, leaves)
    self.assertTreeEqual(roundtripped, tree, check_dtypes=False)

  @parameterized.parameters([
      ((TRIVIAL_TREEDEF,), {(0,): 1.0}, 1.0),
      ((TRIVIAL_TREEDEF, TRIVIAL_TREEDEF), {(0, 0): 2.0}, 2.0),
      ([tree_structure({'a': 0, 'b': 0})], {(0,): 1.0, (1,): 2.0},
        {'a': 1.0, 'b': 2.0}),
      ([tree_structure({'a': 0, 'b': 0}), tree_structure({'c': 0, 'd': 0})],
        {(0, 0): 1, (0, 1): 2, (1, 0): 3, (1, 1): 4},
        {'a': {'c': 1, 'd': 2}, 'b': {'c': 3, 'd': 4}}),
  ])
  def test_restore_tree(self, treedefs, leaves, expected):
    actual = restore_tree(treedefs, leaves)
    self.assertTreeEqual(actual, expected, check_dtypes=True)

  def test_identity(self):
    tree = {'x': jnp.array(0.0),
            'y': jnp.array([1.0]),
            'z': jnp.array([[2.0, 3.0]])}
    actual = tree_vectorize(lambda x: x)(tree)
    self.assertTreeEqual(actual, tree, check_dtypes=True)

  def test_tree_output(self):
    tree = (1, 2)
    actual = tree_vectorize(lambda x: {'a': x, 'b': x})(tree)
    expected = {'a': (1, 2), 'b': (1, 2)}
    self.assertTreeEqual(actual, expected, check_dtypes=True)

  def test_broadcast(self):
    tree = {'x': jnp.array(0.0),
            'y': jnp.array([1.0]),
            'z': jnp.array([[2.0, 3.0]])}
    expected = {'x': jnp.array([0.0]),
                'y': jnp.array([[1.0]]),
                'z': jnp.array([[[2.0, 3.0]]])}
    actual = tree_vectorize(lambda x: jnp.broadcast_to(x, (1, 4)))(tree)
    self.assertTreeEqual(actual, expected, check_dtypes=True)

  def test_expand_dims(self):
    tree = {'x': jnp.array(0.0),
            'y': jnp.array([1.0]),
            'z': jnp.array([[2.0, 3.0]])}
    expected = {'x': jnp.array([0.0]),
                'y': jnp.array([[1.0]]),
                'z': jnp.array([[[2.0], [3.0]]])}
    actual = tree_vectorize(lambda x: jnp.expand_dims(x, 1))(tree)
    self.assertTreeEqual(actual, expected, check_dtypes=True)

  def test_squeeze(self):
    tree = {'x': jnp.array(0.0),
            'y': jnp.array([1.0]),
            'z': jnp.array([[2.0, 3.0]])}
    actual = tree_vectorize(lambda x: jnp.expand_dims(x, 1).squeeze())(tree)
    self.assertTreeEqual(actual, tree, check_dtypes=True)

  def test_transpose(self):
    tree = {'x': jnp.array(0.0),
            'y': jnp.array([1.0]),
            'z': jnp.array([[2.0, 3.0]])}
    actual = tree_vectorize(jnp.transpose)(tree)
    self.assertTreeEqual(actual, tree, check_dtypes=True)

  def test_unary_arithmetic(self):
    tree = {'a': 0, 'b': jnp.array([1, 2])}
    expected = {'a': 1, 'b': jnp.array([2, 3])}
    actual = tree_vectorize(lambda x: x + 1)(tree)
    self.assertTreeEqual(actual, expected, check_dtypes=True)

  def test_binary_arithmetic(self):
    tree1 = {'a': 0, 'b': jnp.array([1, 2])}
    tree2 = {'a': 10, 'b': jnp.array([20, 30])}
    expected = {'a': 10, 'b': jnp.array([21, 32])}
    actual = tree_vectorize(lambda x, y: x + y)(tree1, tree2)
    self.assertTreeEqual(actual, expected, check_dtypes=True)

  def test_arithmetic_with_scalar(self):
    tree = {'x': 0.0, 'y': jnp.array([1.0]), 'z': jnp.array([[2.0, 3.0]])}
    expected = {'x': 1.0, 'y': jnp.array([2.0]), 'z': jnp.array([[3.0, 4.0]])}
    actual = tree_vectorize(jnp.add)(tree, 1)
    self.assertTreeEqual(actual, expected, check_dtypes=True)

  def test_arithmetic_with_vector(self):
    tree = {'x': 0.0, 'y': jnp.array([1.0]), 'z': jnp.array([[2.0, 3.0]])}
    expected = {'x': 1.0, 'y': jnp.array([2.0]), 'z': jnp.array([[3.0, 4.0]])}
    actual = tree_vectorize(lambda x: x + jnp.ones_like(x))(tree)
    self.assertTreeEqual(actual, expected, check_dtypes=True)

  def test_arithmetic_broadcasting(self):

    @tree_vectorize
    # @shapecheck(['n', 'm'], '(n, m)')
    def add_outer(x, y):
      return jnp.expand_dims(x, 1) + jnp.expand_dims(y, 0)

    tree1 = {'a': 1, 'b': jnp.array([2, 3])}
    tree2 = {'c': 10, 'd': jnp.array([20, 30])}
    expected = {'a': {'c': jnp.array(11),
                      'd': jnp.array([21, 31])},
                'b': {'c': jnp.array([12, 13]),
                      'd': jnp.array([[22, 32], [23, 33]])}}
    actual = add_outer(tree1, tree2)
    self.assertTreeEqual(actual, expected, check_dtypes=True)

    add_outer2 = lambda x, y: jnp.expand_dims(x, 1) + y
    actual = tree_vectorize(add_outer2)(tree1, tree2)
    self.assertTreeEqual(actual, expected, check_dtypes=True)

    tree1 = {'a': 1, 'b': jnp.array([[2, 3]])}
    tree2 = {'c': 10, 'd': jnp.array([[20], [30]])}
    expected = {'a': {'c': jnp.array(11),
                      'd': jnp.array([[21], [31]])},
                'b': {'c': jnp.array([[12, 13]]),
                      'd': jnp.array([[[[22], [32]], [[23], [33]]]])}}
    actual = add_outer(tree1, tree2)
    self.assertTreeEqual(actual, expected, check_dtypes=True)

    tree1 = {'a': 1, 'b': 2 * jnp.ones((2, 3), int)}
    tree2 = {'c': 10 * jnp.ones((1,), int), 'd': 20 * jnp.ones((4, 5), int)}
    expected = {'a': {'c': 11 * jnp.ones((1,), int),
                      'd': 21 * jnp.ones(((4, 5)), int)},
                'b': {'c': 12 * jnp.ones((2, 3, 1), int),
                      'd': 22 * jnp.ones(((2, 3, 4, 5)), int)}}
    actual = add_outer(tree1, tree2)
    self.assertTreeEqual(actual, expected, check_dtypes=True)

    def add_outer3(x, y):
      x, y = jnp.broadcast_arrays(jnp.expand_dims(x, 1), jnp.expand_dims(y, 0))
      return x + y
    actual = tree_vectorize(add_outer3)(tree1, tree2)
    self.assertTreeEqual(actual, expected, check_dtypes=True)

  def test_concatenate_trivial(self):
    @tree_vectorize
    def concat(x, y):
      return jnp.concatenate([x, y], axis=0)

    actual = concat(jnp.arange(3), jnp.arange(3, 5))
    expected = jnp.arange(5)
    self.assertArraysEqual(actual, expected)

  def test_concatenate_stack(self):
    @tree_vectorize
    def stack(*args):
      return jnp.stack(args)

    tree1 = {'x': 0, 'y': jnp.array([1, 2])}
    tree2 = {'x': 3, 'y': jnp.array([4, 5])}
    actual = stack(tree1, tree2)
    expected = {'x': jnp.array([0, 3]), 'y': jnp.array([[1, 2], [4, 5]])}
    self.assertTreeEqual(actual, expected, check_dtypes=True)

    actual = stack(tree1, jnp.array([3, 4, 5]))
    self.assertTreeEqual(actual, expected, check_dtypes=True)

  def test_reduce(self):
    tree = {'x': jnp.array(1.0),
            'y': jnp.array([2.0]),
            'z': jnp.array([[3.0, 4.0]])}
    self.assertEqual(tree_vectorize(jnp.sum)(tree), 10.0)
    self.assertEqual(tree_vectorize(jnp.prod)(tree), 24.0)
    self.assertEqual(tree_vectorize(jnp.min)(tree), 1.0)
    self.assertEqual(tree_vectorize(jnp.max)(tree), 4.0)
    self.assertEqual(tree_vectorize(lambda x: jnp.all(x > 2))(tree), False)
    self.assertEqual(tree_vectorize(lambda x: jnp.any(x > 2))(tree), True)

  def test_dot(self):
    tree = {'x': jnp.array(1.0),
            'y': jnp.array([2.0]),
            'z': jnp.array([[3.0, 4.0]])}
    self.assertEqual(tree_vectorize(jnp.dot)(tree, tree), 1.0 + 4 + 9 + 16)

    tree2 = {'a': 1.0, 'b': -1.0}
    expected = {'x': {'a': tree['x'], 'b': -tree['x']},
                'y': {'a': tree['y'], 'b': -tree['y']},
                'z': {'a': tree['z'], 'b': -tree['z']}}
    f = lambda x, y: x[:, None] @ y[None, :]
    actual = tree_vectorize(f)(tree, tree2)
    self.assertTreeEqual(actual, expected, check_dtypes=True)

  def test_jit_identity(self):
    tree = {'x': 0, 'y': 1}
    result = tree_vectorize(jit(lambda x: x))(tree)
    self.assertTreeEqual(result, tree, check_dtypes=True)

  def test_jit_plus1(self):
    tree = {'x': 0, 'y': 1}
    expected = {'x': 1, 'y': 2}
    result = tree_vectorize(jit(lambda x: x + 1))(tree)
    self.assertTreeEqual(result, expected, check_dtypes=True)

  def test_norm(self):
    tree = [3.0, jnp.array([[4.0]])]
    self.assertEqual(tree_vectorize(jnp.linalg.norm)(tree), 5.0)

  def test_tree_call(self):
    tree = {'x': 1, 'y': 2}

    def f(x):
      return {'a': x['x'], 'b': x['y']}

    @tree_vectorize
    def g(f, x):
      # return tree_call(f, x, signature)
      # jvp(tree_call(f.original, signature=signature), (x,), (dx,))
      return f(x)

    actual = g(f, tree)
    expected = {'a': 1, 'b': 2}
    self.assertTreeEqual(actual, expected, check_dtypes=True)

  def test_tree_call_on_scalar(self):
    tree = {'x': 1, 'y': 2}
    def f(x):
      self.assertEqual(x, 3)
      return x
    actual = tree_vectorize(lambda f, x: f(x.sum()))(f, tree)
    self.assertTreeEqual(actual, 3, check_dtypes=True)

  def test_tie_in(self):
    if config.omnistaging_enabled:
      raise unittest.SkipTest("test only works without omnistaging")
    x = jnp.array(1)
    tree = {'x': 1, 'y': 2}
    actual = tree_vectorize(lax.tie_in)(x, tree)
    self.assertTreeEqual(actual, tree, check_dtypes=True)
    self.assertIn('tie_in', str(make_jaxpr(tree_vectorize(lax.tie_in))(x, tree)))

  # integration tests

  # TODO(shoyer): not clear how we could make this work -- need to somehow pass
  # on the tree structure as part of the shape.
  # def test_zeros(self):
  #   tree = {'x': 1, 'y': 2}
  #   actual = tree_vectorize(jnp.zeros_like)(tree)
  #   expected = {'x': 0, 'y': 0}
  #   self.assertTreeEqual(actual, expected, check_dtypes=True)

  def test_transposing_arithmetic(self):
    tree = {'x': 1, 'y': 2}

    @tree_vectorize
    # @shapecheck(['n'], '(n, 1, n)')
    def f(x):
      y = x[:, None, None]
      return y + 10 * y.T

    expected = {'x': {'x': jnp.array([11]), 'y': jnp.array([21])},
                'y': {'x': jnp.array([12]), 'y': jnp.array([22])}}
    actual = f(tree)
    self.assertTreeEqual(actual, expected, check_dtypes=True)

  def test_cg(self):

    def cg(A, b, x0, M, maxiter=5, tol=1e-5, atol=0.0):

      # tolerance handling uses the "non-legacy" behavior of scipy.sparse.linalg.cg
      bs = b @ b
      atol2 = jnp.maximum(jnp.square(tol) * bs, jnp.square(atol))

      # https://en.wikipedia.org/wiki/Conjugate_gradient_method#The_preconditioned_conjugate_gradient_method

      def cond_fun(value):
        x, r, gamma, p, k = value
        rs = r @ r
        return (rs > atol2) & (k < maxiter)

      def body_fun(value):
        x, r, gamma, p, k = value
        Ap = A(p)
        alpha = gamma / (p.conj() @ Ap)
        x_ = x + alpha * p
        r_ = r - alpha * Ap
        z_ = M(r_)
        gamma_ = r_.conj() @ z_
        beta_ = gamma_ / gamma
        p_ = z_ + beta_ * p
        return x_, r_, gamma_, p_, k + 1

      r0 = b - A(x0)
      p0 = z0 = M(r0)
      gamma0 = r0 @ z0
      initial_value = (x0, r0, gamma0, p0, 0)

      x_final, *_ = lax.while_loop(cond_fun, body_fun, initial_value)

      return x_final

    A = lambda x: {'a': x['a'] + 0.5 * x['b'], 'b': 0.5 * x['a'] + x['b']}
    b = {'a': 1.0, 'b': -1.0}
    x0 = {'a': 0.0, 'b': 0.0}
    M = lambda x: x

    # TODO(shoyer): remove disable_jit, once while_loop and jit work
    with disable_jit():
      actual = tree_vectorize(cg)(A, b, x0, M)

    expected = {'a': 2.0, 'b': -2.0}
    self.assertAllClose(actual, expected, check_dtypes=True)

  def test_vmap(self):

    def square_scalar(x):
      assert x.shape == ()
      return jnp.square(x)

    @tree_vectorize
    def f(x):
      return jax.vmap(square_scalar)(x)

    tree = {'x': 2.0, 'y': np.array([3.0, 4.0])}
    expected = {'x': 4.0, 'y': np.array([9.0, 16.0])}
    actual = f(tree)
    self.assertAllClose(actual, expected, check_dtypes=True)

  @pytest.mark.xfail
  def test_vmap_tree_call(self):

    def g(x):
      assert x['a'].shape == ()
      return {'b': x['a']}

    @tree_vectorize
    def f(g, x):
      return jax.vmap(g)(x)

    tree = {'a': jnp.arange(3.0)}
    expected = {'b': jnp.arange(3.0)}
    actual = f(g, tree)
    self.assertAllClose(actual, expected, check_dtypes=True)

  def test_jvp(self):
    @tree_vectorize
    def f(x, y):
      return jax.jvp(lambda x: 0.5 * x ** 2, (x,), (y,))

    primal = {'x': 1.0, 'y': 2.0}
    tangent = {'x': 3.0, 'y': 4.0}
    actual = f(primal, tangent)
    expected = ({'x': 0.5, 'y': 2.0}, {'x': 3.0, 'y': 8.0})
    self.assertAllClose(actual, expected, check_dtypes=True)

  # @pytest.mark.xfail
  def test_jvp_tree_call(self):

    def g(x):
      print("g(x) called with", x)
      return {'b': 0.5 * x['a'] ** 2}

    @tree_vectorize
    def f(g, x, y):
      z, dz = jax.jvp(g, (x,), (y,))
      return z, dz

    actual = f(g, {'a': 2.0}, {'a': 3.0})
    expected = ({'b': 2.0}, {'b': 6.0})
    self.assertTreeEqual(actual, expected, check_dtypes=True)

  @pytest.mark.xfail
  def test_jacobian(self):

    @tree_vectorize
    def _jacfwd(f, x):
      pushfwd = partial(jax.jvp, f, (x,))
      basis = jnp.eye(x.size, dtype=x.dtype)
      y, jac = jax.vmap(pushfwd, out_axes=(None, 1))((basis,))
      return jac

    @tree_vectorize
    def _jacrev(f, x):
      y, pullback = jax.vjp(f, x)
      basis = jnp.eye(y.size, dtype=y.dtype)
      jac = jax.vmap(pullback)(basis)
      return jac

    def _apply_argnums(transform, fun, argnums=0):
      def jacfun(*args):
        f_partial, dyn_args = api.argnums_partial(
            lu.wrap_init(fun), argnums, args)
        result = transform(f_partial.call_wrapped, dyn_args)
        return result[0] if isinstance(argnums, int) else result
      return jacfun

    jacfwd = partial(_apply_argnums, _jacfwd)
    jacrev = partial(_apply_argnums, _jacrev)

    f = lambda x: {'c': x['a'] * (1 + x['b'] ** 2), 'd': x['a'] - x['b']}
    tree = {'a': 1.0, 'b': 2.0}
    expected = jax.jacfwd(f)(tree)

    actual = jacfwd(f)(tree)
    self.assertTreeEqual(expected, actual, check_dtypes=True)

    actual = jacrev(f)(tree)
    self.assertTreeEqual(expected, actual, check_dtypes=True)



if __name__ == "__main__":
  absltest.main()
