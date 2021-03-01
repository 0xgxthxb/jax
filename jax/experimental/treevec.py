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

import itertools as it
from functools import partial, reduce, wraps
import itertools
from jax._src.numpy.lax_numpy import isin
import operator
from typing import (
    Any, Callable, Dict, Iterable, Iterator, List, Sequence, Tuple, TypeVar,
)

import numpy as np

import jax.numpy as jnp
from .. import core
from .. import api
from .. import dtypes
from .. import lax
from .. import linear_util as lu
from ..api_util import flatten_fun_nokwargs
from ..interpreters import batching
from ..interpreters import partial_eval
from ..interpreters import xla
from .._src.util import prod, safe_map as map, split_list, unzip2, unzip3
from ..tree_util import (
    tree_structure, tree_flatten, tree_unflatten,
)
from jax import api_util


TRIVIAL_TREEDEF = tree_structure(1)

TreeDef = Any
ArrayLike = Any
PyTree = Any
LeafShapes = Sequence[Sequence[Tuple[int, ...]]]
Leaves = Dict[Tuple[int, ...], ArrayLike]


def tree_vectorize(fun):
  """Apply the tree-vectorization transfrom to a function."""
  api._check_callable(fun)
  @wraps(fun)
  def transformed(*args):
    f = lu.wrap_init(fun)
    f, args = _apply_callables(f, tree_callable, args)
    f, out_tree = _flatten_fun_output(f)
    outputs = tree_fun(tree_trace(f)).call_wrapped(args)
    return tree_unflatten(out_tree(), outputs)
  return transformed

@lu.transformation
def tree_fun(trees):
  with core.new_main(TreeTrace) as main:
    out_trees = yield (main, trees), {}
    del main
  yield out_trees

@lu.transformation
def tree_trace(main, trees):
  trace = TreeTrace(main, core.cur_sublevel())
  in_tracers = [TreeTracer(trace, *convert_vectorized_tree(t)) for t in trees]
  ans = yield in_tracers, {}
  out_tracers = map(trace.full_raise, ans)
  out = tuple(restore_tree(t.treedefs, t.leaves) for t in out_tracers)
  yield out

@lu.transformation_with_aux
def _flatten_fun_output(*args):
  ans = yield args, {}
  yield tree_flatten(ans)

def _apply_callables(f, callable_transform, args):
  callables = []
  out_args = []
  for i, arg in enumerate(args):
    if callable(arg):
      callables.append((i, callable_transform(arg)))
    else:
      out_args.append(arg)
  return _partial_args(f, callables), tuple(out_args)

@lu.transformation
def _partial_args(fixed_args, *dyn_args):
  args = list(dyn_args)
  for i, arg in fixed_args:
    args.insert(i, arg)
  ans = yield args, {}
  yield ans


def is_trivial_axis(
    treedef: TreeDef, leafshapes: Sequence[Tuple[int, ...]],
) -> bool:
  return treedef is TRIVIAL_TREEDEF and len(leafshapes) == 1 and len(leafshapes[0]) == 1


def _iter_leaf_coords(treedefs: Sequence[TreeDef]) -> Iterator[Tuple[int, ...]]:
  return itertools.product(*[range(treedef.num_leaves) for treedef in treedefs])

def _iter_leaf_coords2(leafshapes: LeafShapes) -> Iterator[Tuple[int, ...]]:
  return itertools.product(*[range(len(shapes)) for shapes in leafshapes])

def _axis_length(shapes: Iterable[Tuple[int, ...]]) -> int:
  return sum(map(prod, shapes))


T = TypeVar("T")

def _concat_tuple(tuples: Iterable[Sequence[T]]) -> Tuple[T, ...]:
  return tuple(itertools.chain.from_iterable(tuples))


def _leafshape(
    leafshapes: LeafShapes,
    coords: Tuple[int, ...],
) -> Tuple[int, ...]:
  return _concat_tuple([leafshapes[i][j] for i, j in enumerate(coords)])


class TreeTracer(core.Tracer):
  __slots__ = ["treedefs", "leafshapes", "leaves"]

  treedefs: Tuple[TreeDef, ...]
  leafshapes: Tuple[Tuple[Tuple[int, ...], ...], ...]
  leaves: Leaves

  def __init__(self, trace, treedefs, leafshapes, leaves):
    assert len(treedefs) == len(leafshapes)
    for treedef, shapes in zip(treedefs, leafshapes):
      assert treedef.num_leaves == len(shapes)
    assert leaves
    for coords in _iter_leaf_coords(treedefs):
      expected_shape = _leafshape(leafshapes, coords)
      actual_shape = np.shape(leaves[coords])
      assert actual_shape == expected_shape
    self._trace = trace
    self.treedefs = tuple(treedefs)
    self.leafshapes = tuple(map(tuple, leafshapes))
    self.leaves = leaves

  @property
  def aval(self):
    shape = tuple(map(_axis_length, self.leafshapes))
    dtype = dtypes.dtype(next(iter(self.leaves.values())))
    return core.ShapedArray(shape, dtype)

  def full_lower(self):
    if all(map(is_trivial_axis, self.treedefs, self.leafshapes)):
      value, = self.leaves.values()
      return core.full_lower(value)
    else:
      return self


def _flatten_tracer(tracer):
  xs = tuple(tracer.leaves.values())
  tracer_treedef = (
      tracer._trace, tracer.treedefs, tracer.leafshapes, tracer.leaves)
  return xs, tracer_treedef

def _unflatten_tracer(tracer_treedef, xs):
  trace, treedefs, leafshapes, leaf_keys = tracer_treedef
  leaves = dict(zip(leaf_keys, xs))
  return TreeTracer(trace, treedefs, leafshapes, leaves)

# TODO(shoyer): consider making TreeTracer a pytree instead? This could be a
# very convenient simplification, but currently causes everything to break. We
# need something like a trace level for tree_flatten/unflatten, so we don't
# unflatten at the wrong level of abstraction.

# register_pytree_node(TreeTracer, _flatten_tracer, _unflatten_tracer)

def _rebuild_leaves(keys_list, flat_values):
  ns = map(len, keys_list)
  values_list = split_list(flat_values, ns)
  leaves = []
  for keys, values in zip(keys_list, values_list):
    leaves.append(dict(zip(keys, values)))
  return leaves

def _unflatten_tree_tracers(trace, tree_tracer_def, flat_in):
  treedefs_in, leafshapes_in, leaf_keys_in = unzip3(tree_tracer_def)
  leaves_in = _rebuild_leaves(leaf_keys_in, flat_in)
  tracers = map(partial(TreeTracer, trace), treedefs_in, leafshapes_in, leaves_in)
  return tracers

def _flatten_tree_tracers(tracers):
  tree_tracer_def, leaf_values = unzip2(
      ((t.treedefs, t.leafshapes, tuple(t.leaves.keys())), t.leaves.values())
      for t in tracers)
  flat = _concat_tuple(leaf_values)
  return flat, tree_tracer_def


def tree_subtrace_gen(main, tree_tracer_def_in, *flat_in):
  trace = TreeTrace(main, core.cur_sublevel())
  in_tracers = _unflatten_tree_tracers(trace, tree_tracer_def_in, flat_in)
  ans = yield in_tracers, {}
  out_tracers = map(trace.full_raise, ans)
  flat_out, tree_tracer_def_out = _flatten_tree_tracers(out_tracers)
  yield flat_out, tree_tracer_def_out


tree_subtrace = lu.transformation_with_aux(tree_subtrace_gen)


class TreeTrace(core.Trace):

  def pure(self, val):
    # constant array/scalar, no tracers
    return TreeTracer(self, *convert_leaf_array(val))

  def lift(self, tracer):
    # called for tracers of a lower priority
    return TreeTracer(self, *convert_leaf_array(tracer))

  def sublift(self, tracer):
    # specifically called for transformations of functions that involve
    # jit/pmap via lexical closure -- called for tracers of your trace type
    return TreeTracer(self, tracer.treedefs, tracer.leafshapes, tracer.leaves)

  def process_primitive(self, primitive, tracers, params):
    rule = tree_rules[primitive]
    treedefs_in, leafshapes_in, leaves_in = unzip3(
        (t.treedefs, t.leafshapes, t.leaves) for t in tracers)
    result = rule(treedefs_in, leafshapes_in, leaves_in, **params)
    if primitive.multiple_results:
      return map(partial(TreeTracer, self), *result)
    else:
      return TreeTracer(self, *result)

  def process_call(self, call_primitive, f, tracers, params):
    flat_in, tree_tracer_def_in = _flatten_tree_tracers(tracers)
    f_tree, out_structure = tree_subtrace(f, self.main, tree_tracer_def_in)
    update_params = call_param_updaters.get(call_primitive)
    new_params = (update_params(params, len(flat_in))
                  if update_params else params)
    flat_out = call_primitive.bind(f_tree, *flat_in, **new_params)
    out_tracers = _unflatten_tree_tracers(self, out_structure(), flat_out)
    return out_tracers

  def post_process_call(self, call_primitive, out_tracers, params):
    flat, tree_tracer_def = _flatten_tree_tracers(out_tracers)
    main = self.main
    def todo(flat):
      trace = TreeTrace(main, core.cur_sublevel())
      return _unflatten_tree_tracers(trace, tree_tracer_def, flat)
    return flat, todo

  def process_tree_call(self, call_primitive, f, tracers, params):
    args = _tree_tracers_to_trees(tracers)
    result = call_primitive.bind(f, *args, **params)
    return _trees_to_tree_tracers(self, result)


class TreeCall(core.Primitive):
  multiple_results = True

  def bind(self, f, *args, **params):
    top_trace = core.find_top_trace(args)
    tracers = map(top_trace.full_raise, args)
    out = top_trace.process_tree_call(self, f, tracers, params)
    return map(core.full_lower, out)


from jax.interpreters import ad

@lu.transformation
def _tree_jvp_subtrace(main, primal_in_trees, tangent_in_trees):
  # trees -> lists for primals and tangents
  primal_in_lists, primal_in_tdefs = unzip2(tree_flatten(pt) for pt in primal_in_trees)
  tangent_in_lists, tangent_in_tdefs = unzip2(tree_flatten(tt) for tt in tangent_in_trees)
  assert primal_in_tdefs == tangent_in_tdefs
  in_tdefs = primal_in_tdefs
  # lists -> flat list for primals and tangents
  primal_in_list = list(it.chain(*primal_in_lists))
  tangent_in_list = list(it.chain(*tangent_in_lists))
  # flat list -> jvp tracer list
  gen = ad.jvp_subtrace_gen(main, primal_in_list, tangent_in_list)
  jvp_in_list, _ = next(gen)
  # jvp tracer list -> jvp tracer tree
  jvp_in_lists = split_list(jvp_in_list, [td.num_leaves for td in in_tdefs[:-1]])
  jvp_out_trees = yield map(tree_unflatten, in_tdefs, jvp_in_lists), {}
  # jvp tracer tree -> jvp tracer list
  jvp_out_lists, out_tdefs = unzip2(tree_flatten(jt) for jt in jvp_out_trees)
  jvp_out_list = list(it.chain(*jvp_out_lists))
  # jvp tracer list -> lists for primals and tangents
  primal_out_list, tangent_out_list = gen.send(jvp_out_list)
  tree_sizes = [td.num_leaves for td in out_tdefs[:-1]]
  # lists for primals and tangents -> trees for primals and tangents
  primal_out_trees = map(tree_unflatten, out_tdefs, split_list(primal_out_list, tree_sizes))
  tangent_out_trees = map(tree_unflatten, out_tdefs, split_list(tangent_out_list, tree_sizes))
  yield primal_out_trees, tangent_out_trees

@lu.transformation_with_aux
def _zero_tangent_adapter(num_primals, in_tree_def, *primals_and_tangents):
  new_primals  = primals_and_tangents[:num_primals]
  new_tangents = primals_and_tangents[num_primals:]
  new_tangents = tree_unflatten(in_tree_def, new_tangents)
  primal_out_trees, tangent_out_trees = yield (new_primals, new_tangents), {}
  # TODO: Deal with zero tangents!
  yield primal_out_trees + tangent_out_trees, len(primal_out_trees)

def _jvp_process_tree_call(self, call_primitive, f: lu.WrappedFun, tracers, params):
  assert call_primitive.multiple_results
  primals, tangents = unzip2((t.primal, t.tangent) for t in tracers)
  nonzero_tangents, tangent_tree_def = tree_flatten(tangents)
  f_jvp = _tree_jvp_subtrace(f, self.main)
  f_jvp, num_out_primals = _zero_tangent_adapter(f_jvp, len(primals), tangent_tree_def)
  result = call_primitive.bind(f_jvp, *primals, *nonzero_tangents, **params)
  primal_out, tangent_out = split_list(result, [num_out_primals()])
  return [ad.JVPTracer(self, p, t) for p, t in zip(primal_out, tangent_out)]
ad.JVPTrace.process_tree_call = _jvp_process_tree_call

core.EvalTrace.process_tree_call = core.EvalTrace.process_call

partial_eval.JaxprTrace.process_tree_call = partial_eval.JaxprTrace.process_call

@lu.transformation_with_aux
def _tree_batch_subtrace(main, in_dims_trees, *in_vals_trees, **params):
  # trees -> lists
  in_vals_lists, in_vals_tdefs = unzip2(tree_flatten(x) for x in in_vals_trees)
  in_dims_lists = [[d] * len(v) for d, v in zip(in_dims_trees, in_vals_lists)]
  # lists -> flat lists for values and dims
  in_vals_list = list(it.chain(*in_vals_lists))
  in_dims_list = list(it.chain(*in_dims_lists))
  # flat list -> batch tracer list
  gen = batching.batch_subtrace_gen(main, in_dims_list, *in_vals_list, **params)
  batch_in_list, _ = next(gen)
  # batch tracer list -> batch tracer tree
  batch_in_lists = split_list(batch_in_list, [td.num_leaves for td in in_vals_tdefs[:-1]])
  batch_out_trees = yield map(tree_unflatten, in_vals_tdefs, batch_in_lists), params
  # brace tracer tree -> batch tracer list
  batch_out_lists, out_tdefs = unzip2(tree_flatten(x) for x in batch_out_trees)
  batch_out_list = list(it.chain(*batch_out_lists))
  # batch tracer list -> lists
  out_vals_list, out_dims_list = gen.send(batch_out_list)
  tree_sizes = [td.num_leaves for td in out_tdefs[:-1]]
  # lists -> trees
  out_vals_trees = map(tree_unflatten, out_tdefs, split_list(out_vals_list, tree_sizes))
  out_dims_lists = split_list(out_dims_list, tree_sizes)
  out_dims_trees = []
  for dims in out_dims_lists:
    dim, = set(dims)
    out_dims_trees.append(dim)
  yield out_vals_trees, out_dims_trees


def _batch_process_tree_call(self, call_primitive, f: lu.WrappedFun, tracers, params):
  assert call_primitive.multiple_results
  vals, dims = unzip2((t.val, t.batch_dim) for t in tracers)
  f, dims_out = _tree_batch_subtrace(f, self.main, dims)
  # TODO: deal with unbatched values?
  vals_out = call_primitive.bind(f, *vals, **params)
  return [batching.BatchTracer(self, v, d) for v, d in zip(vals_out, dims_out())]
batching.BatchTrace.process_tree_call = _batch_process_tree_call


def _tree_call_impl(fun: lu.WrappedFun, *args, **params):
  return fun.call_wrapped(*args)

tree_call_p = TreeCall('tree_call')
tree_call_p.def_impl(_tree_call_impl)


@lu.transformation
def _fun_one_output(*args):
  ans = yield args, {}
  yield [ans]


def tree_call(f, *args, **params):
  # TODO(shoyer): handle trees of TreeTracer args
  return tree_call_p.bind(f, *args, **params)


def tree_callable(fun):
  def wrapper(*args):
    f = _fun_one_output(lu.wrap_init(fun))
    y, = tree_call(f, *args, name=fun.__name__)
    return y
  return wrapper


TreeState = Tuple[Sequence[TreeDef], LeafShapes, Leaves]


def convert_vectorized_tree(tree: PyTree) -> TreeState:
  xs, treedef = tree_flatten(tree)
  leafshape = tuple(np.shape(x) for x in xs)
  dtype = jnp.result_type(*xs)
  leaves: Leaves = {(i,): jnp.asarray(leaf, dtype) for i, leaf in enumerate(xs)}
  return (treedef,), (leafshape,), leaves


def convert_leaf_array(leaf: ArrayLike) -> TreeState:
  treedef = tree_structure(leaf)
  if treedef != TRIVIAL_TREEDEF:
    raise ValueError(
        f"argument to from_array must be a leaf already, got {treedef}")
  ndim = np.ndim(leaf)
  treedefs = (TRIVIAL_TREEDEF,) * ndim
  leafshapes: LeafShapes = tuple(((s,),) for s in np.shape(leaf))
  leaves: Leaves = {(0,) * ndim: leaf}
  return treedefs, leafshapes, leaves


def restore_tree(treedefs: Tuple[TreeDef, ...], leaves: Leaves) -> PyTree:
  while treedefs:
    flattened_leaves = {}
    for coords in _iter_leaf_coords(treedefs[:-1]):
      leaf_list = [leaves[coords + (i,)] for i in range(treedefs[-1].num_leaves)]
      flattened_leaves[coords] = tree_unflatten(treedefs[-1], leaf_list)
    treedefs = treedefs[:-1]
    leaves = flattened_leaves
  return leaves[()]


def _tree_tracers_to_trees(tracers: Sequence[TreeTracer]) -> Tuple[PyTree, ...]:
  treedefs_in, leaves_in = unzip2((t.treedefs, t.leaves) for t in tracers)
  return tuple(map(restore_tree, treedefs_in, leaves_in))


def _trees_to_tree_tracers(
    tree_trace: TreeTrace, trees: Sequence[PyTree],
) -> Tuple[TreeTracer, ...]:
  parts = unzip3(map(convert_vectorized_tree, trees))
  return tuple(map(partial(TreeTracer, tree_trace), *parts))


call_param_updaters = {}

def _xla_call_param_updater(params, num_inputs):
  donated_invars = params['donated_invars']
  if any(donated_invars):
    raise NotImplementedError("donated_invars not supported with tree_vectorize")
  return dict(params, donated_invars=(False,) * num_inputs)
call_param_updaters[xla.xla_call_p] = _xla_call_param_updater


### rule definitions

tree_rules: Dict[core.Primitive, Callable] = {}


def tie_in_tree_rule(treedefs_in, leafshapes_in, leaves_in) -> TreeState:
  x_treedefs, y_treedefs = treedefs_in
  x_leafshapes, y_leafshapes = leafshapes_in
  x_leaves, y_leaves = leaves_in
  # TODO(shoyer): should we try somehow to add a data depedency on everything,
  # not just the first value?
  x_example = next(iter(x_leaves.values()))
  out_leaves = {}
  for coords in _iter_leaf_coords(y_treedefs):
    out_leaves[coords] = lax.tie_in_p.bind(x_example, y_leaves[coords])
  return y_treedefs, y_leafshapes, out_leaves

tree_rules[lax.tie_in_p] = tie_in_tree_rule


def vectorized_tree_rule(prim, treedefs_in, leafshapes_in, leaves_in, **params):
  treedefs, = treedefs_in
  leafshapes, = leafshapes_in
  leaves, = leaves_in
  out_leaves = {coords: prim.bind(leaves[coords], **params)
                for coords in _iter_leaf_coords(treedefs)}
  return treedefs, leafshapes, out_leaves

def defvectorized(prim):
  tree_rules[prim] = partial(vectorized_tree_rule, prim)

defvectorized(lax.neg_p)
defvectorized(lax.sign_p)
defvectorized(lax.floor_p)
defvectorized(lax.ceil_p)
defvectorized(lax.round_p)
defvectorized(lax.is_finite_p)
defvectorized(lax.exp_p)
defvectorized(lax.log_p)
defvectorized(lax.expm1_p)
defvectorized(lax.log1p_p)
defvectorized(lax.tanh_p)
defvectorized(lax.sin_p)
defvectorized(lax.cos_p)
defvectorized(lax.sinh_p)
defvectorized(lax.cosh_p)
defvectorized(lax.asinh_p)
defvectorized(lax.acosh_p)
defvectorized(lax.atanh_p)
defvectorized(lax.lgamma_p)
defvectorized(lax.digamma_p)
defvectorized(lax.bessel_i0e_p)
defvectorized(lax.bessel_i1e_p)
defvectorized(lax.erf_p)
defvectorized(lax.erfc_p)
defvectorized(lax.erf_inv_p)
defvectorized(lax.real_p)
defvectorized(lax.imag_p)
defvectorized(lax.conj_p)
defvectorized(lax.abs_p)
defvectorized(lax.sqrt_p)
defvectorized(lax.rsqrt_p)
defvectorized(lax.integer_pow_p)
defvectorized(lax.not_p)
defvectorized(lax.population_count_p)
defvectorized(lax.convert_element_type_p)
defvectorized(lax.bitcast_convert_type_p)

defvectorized(xla.device_put_p)


def _filter_scalar_leaves(treedefs_in, leafshapes_in, leaves_in):
  treedefs_out = []
  leafshapes_out = []
  leaves_out = []
  scalars = []
  for i, (treedefs, leafshapes, leaves) in enumerate(
      zip(treedefs_in, leafshapes_in, leaves_in)):
    if treedefs:
      treedefs_out.append(treedefs)
      leafshapes_out.append(leafshapes)
      leaves_out.append(leaves)
    else:
      scalars.append((i, leaves[()]))
  return treedefs_out, leafshapes_out, leaves_out, scalars

def _split_leaf(
    array: ArrayLike,
    axis: int,
    shapes: Sequence[Tuple[int, ...]],
) -> List[ArrayLike]:
  if _axis_length(shapes) != array.shape[axis]:
    raise ValueError("mismatched axis shape")
  indices = np.cumsum([prod(shape) for shape in shapes[:-1]])
  pieces = jnp.split(array, indices, axis)
  outputs = []
  for piece, axis_shape in zip(pieces, shapes):
    shape = array.shape[:axis] + axis_shape + array.shape[axis+1:]
    outputs.append(piece.reshape(shape))
  return outputs

def _split_leaves(
    leafshapes: LeafShapes,
    leaves: Leaves,
    axis: int,
    shapes: Sequence[Tuple[int, ...]],
) -> Leaves:
  if len(leafshapes[axis]) != 1 or len(leafshapes[axis][0]) != 1:
    raise ValueError(f"invalid leafshapes {leafshapes[axis]} along axis={axis}")
  leaves_out = {}
  for in_coords in _iter_leaf_coords2(leafshapes):
    leaf = leaves[in_coords]
    leaf_axis, = _axes_for_leaf(leafshapes, in_coords, (axis,))
    new_leaves = _split_leaf(leaf, leaf_axis, shapes)
    for i, new_leaf in enumerate(new_leaves):
      out_coords = in_coords[:axis] + (i,) + in_coords[axis+1:]
      leaves_out[out_coords] = new_leaf
  return leaves_out

def _axes_for_leaf(
    leafshapes: LeafShapes, coords: Tuple[int, ...], axes: Tuple[int, ...],
) -> Tuple[int, ...]:
  out_axes: List[int] = []
  leaf_axis = 0
  for axis, coord in enumerate(coords):
    leaf_ndim = len(leafshapes[axis][coord])
    if axis in axes:
      out_axes.extend(range(leaf_axis, leaf_axis + leaf_ndim))
    leaf_axis += leaf_ndim
  return tuple(out_axes)


def naryop_tree_rule(
    prim: core.Primitive,
    treedefs_in: Tuple[Tuple[TreeDef, ...], ...],
    leafshapes_in: Tuple[LeafShapes, ...],
    leaves_in: Tuple[Leaves, ...],
    **params,
) -> TreeState:

  treedefs_in, leafshapes_in, leaves_in, scalars = _filter_scalar_leaves(
      treedefs_in, leafshapes_in, leaves_in)

  if not treedefs_in:
    args = [scalar for _, scalar in scalars]
    return (), (), {(): prim.bind(*args, **params)}

  ndim, = {len(treedefs) for treedefs in treedefs_in}

  out_treedefs = []
  out_leafshapes = []

  for axis in range(ndim):
    # check treedefs
    non_trivial_treedefs = {treedefs[axis] for treedefs in treedefs_in
                            if treedefs[axis] != TRIVIAL_TREEDEF}
    if len(non_trivial_treedefs) > 1:
      raise ValueError(
          f"conflicting treedefs along axis={axis}: {non_trivial_treedefs}"
      )
    elif len(non_trivial_treedefs) == 1:
      treedef, = non_trivial_treedefs
      out_treedefs.append(treedef)
    else:
      out_treedefs.append(TRIVIAL_TREEDEF)

    # check shapes
    non_trivial_shapes = {leafshapes[axis] for leafshapes in leafshapes_in
                          if len(leafshapes[axis]) != 1}
    if len(non_trivial_shapes) > 1:
      raise ValueError(
          f"conflicting shapes along axis={axis}: {non_trivial_shapes}"
      )
    elif len(non_trivial_shapes) == 1:
      shapes, = non_trivial_shapes
      out_leafshapes.append(shapes)
    else:
      size = max(_axis_length(leafshapes[axis]) for leafshapes in leafshapes_in)
      out_leafshapes.append(((size,),))

  # split "trivial" axes to match the output leafshape
  # This lets us support arithmetic with arrays created by functions like
  # np.zeros().
  leafshapes_fixed = []
  leaves_fixed: List[Leaves] = []
  for leafshapes, leaves in zip(leafshapes_in, leaves_in):
    leafshapes_ = list(leafshapes)
    for axis in range(ndim):
      if leafshapes[axis] != out_leafshapes[axis] and _axis_length(leafshapes[axis]) != 1:
        leaves = _split_leaves(leafshapes, leaves, axis, out_leafshapes[axis])
        leafshapes_[axis] = out_leafshapes[axis]
    leafshapes_fixed.append(leafshapes_)
    leaves_fixed.append(leaves)

  # compute leaves
  out_leaves = {}
  for out_coords in _iter_leaf_coords(out_treedefs):

    args = []
    for leafshapes, leaves in zip(leafshapes_fixed, leaves_fixed):
      in_coords = tuple(coord if len(leafshapes[axis]) != 1 else 0
                        for axis, coord in enumerate(out_coords))
      leaf = leaves[in_coords]
      broadcasting_dims = tuple(axis for axis, shapes in enumerate(leafshapes)
                                if shapes == ((1,),))
      remove_dims = _axes_for_leaf(leafshapes, in_coords, broadcasting_dims)
      insert_dims = _axes_for_leaf(out_leafshapes, out_coords, broadcasting_dims)
      leaf = lax.expand_dims(lax.squeeze(leaf, remove_dims), insert_dims)

      args.append(leaf)

    for i, scalar in scalars:
      args.insert(i, scalar)

    out_leaves[out_coords] = prim.bind(*args, **params)

  return out_treedefs, out_leafshapes, out_leaves

def defnaryop(prim: core.Primitive) -> None:
  tree_rules[prim] = partial(naryop_tree_rule, prim)

defnaryop(lax.nextafter_p)
defnaryop(lax.atan2_p)
defnaryop(lax.regularized_incomplete_beta_p)
defnaryop(lax.igamma_p)
defnaryop(lax.igamma_grad_a_p)
defnaryop(lax.igammac_p)
defnaryop(lax.complex_p)
defnaryop(lax.pow_p)
defnaryop(lax.and_p)
defnaryop(lax.or_p)
defnaryop(lax.xor_p)
defnaryop(lax.add_p)
defnaryop(lax.sub_p)
defnaryop(lax.mul_p)
defnaryop(lax.div_p)
defnaryop(lax.rem_p)
defnaryop(lax.max_p)
defnaryop(lax.min_p)
defnaryop(lax.shift_left_p)
defnaryop(lax.shift_right_arithmetic_p)
defnaryop(lax.shift_right_logical_p)
defnaryop(lax.eq_p)
defnaryop(lax.ne_p)
defnaryop(lax.ge_p)
defnaryop(lax.gt_p)
defnaryop(lax.le_p)
defnaryop(lax.lt_p)


def concatenate_tree_rule(
    treedefs_in: Tuple[Tuple[TreeDef, ...], ...],
    leafshapes_in: Tuple[LeafShapes, ...],
    leaves_in: Tuple[Leaves, ...],
    *,
    dimension: int,
) -> TreeState:

  ndim, = {len(treedefs) for treedefs in treedefs_in}

  out_treedefs = []
  out_leafshapes = []

  for axis in range(ndim):
    # check treedefs
    non_trivial_treedefs = {treedefs[axis] for treedefs in treedefs_in
                            if treedefs[axis] != TRIVIAL_TREEDEF}
    if axis == dimension and non_trivial_treedefs:
      raise ValueError(
          f"non-trivial treedefs along concatenated axis={axis}: "
          f"{non_trivial_treedefs}"
      )
    if len(non_trivial_treedefs) > 1:
      raise ValueError(
          f"conflicting treedefs along axis={axis}: {non_trivial_treedefs}"
      )

    if len(non_trivial_treedefs) == 1:
      treedef, = non_trivial_treedefs
      out_treedefs.append(treedef)
    else:
      out_treedefs.append(TRIVIAL_TREEDEF)

    # check shapes
    non_trivial_shapes = {leafshapes[axis] for leafshapes in leafshapes_in
                          if len(leafshapes[axis]) != 1}
    if axis == dimension and non_trivial_shapes:
      raise ValueError(
          f"shapes along concatenated axis are not all 1D: "
          f"{non_trivial_shapes}"
      )
    if len(non_trivial_shapes) > 1:
      raise ValueError(
          f"conflicting shapes along axis={axis}: {non_trivial_shapes}"
      )
    if len(non_trivial_shapes) == 1:
      shapes, = non_trivial_shapes
      out_leafshapes.append(shapes)
    else:
      lengths = [_axis_length(leafshapes[axis]) for leafshapes in leafshapes_in]
      if axis == dimension:
        size = sum(lengths)
      else:
        size, = lengths
      out_leafshapes.append(((size,),))

  # split "trivial" axes to match the output leafshape
  leaves_fixed: List[Leaves] = []
  for leafshapes, leaves in zip(leafshapes_in, leaves_in):
    for axis in range(ndim):
      if axis != dimension and leafshapes[axis] != out_leafshapes[axis]:
        leaves = _split_leaves(leafshapes, leaves, axis, out_leafshapes[axis])
    leaves_fixed.append(leaves)

  # compute leaves
  out_leaves = {}
  for coords in _iter_leaf_coords(out_treedefs):
    args = [leaves[coords] for leaves in leaves_fixed]
    leaf_dim, = _axes_for_leaf(out_leafshapes, coords, (dimension,))
    out_leaves[coords] = lax.concatenate_p.bind(*args, dimension=leaf_dim)

  return out_treedefs, out_leafshapes, out_leaves

tree_rules[lax.concatenate_p] = concatenate_tree_rule


def broadcast_in_dim_tree_rule(
    treedefs_in: Tuple[Tuple[TreeDef, ...]],
    leafshapes_in: Tuple[LeafShapes],
    leaves_in: Tuple[Leaves],
    *,
    shape: Tuple[int, ...],
    broadcast_dimensions: Tuple[int, ...],
) -> TreeState:
  treedefs, = treedefs_in
  leafshapes, = leafshapes_in
  leaves, = leaves_in

  out_treedefs = [TRIVIAL_TREEDEF] * len(shape)
  out_leafshapes: List[Sequence[Tuple[int, ...]]] = [((s,),) for s in shape]

  for input_dim, output_dim in enumerate(broadcast_dimensions):
    treedef = out_treedefs[output_dim] = treedefs[input_dim]
    leafshape = leafshapes[input_dim]
    if not is_trivial_axis(treedef, leafshape):
      if _axis_length(leafshape) != shape[output_dim]:
        raise ValueError(f"cannot resize dimension {input_dim} because it "
                         f"corresponds to a non-trivial pytree: {treedef}")
      out_leafshapes[output_dim] = leafshape

  out_leaves = {}
  for in_coords, out_coords in zip(
      _iter_leaf_coords(treedefs), _iter_leaf_coords(out_treedefs)):
    leaf = leaves[in_coords]
    leaf_shape = _leafshape(out_leafshapes, out_coords)
    leaf_bdims = _axes_for_leaf(
        out_leafshapes, out_coords, broadcast_dimensions)
    out_leaves[out_coords] = lax.broadcast_in_dim_p.bind(
        leaf, shape=leaf_shape, broadcast_dimensions=leaf_bdims)

  return out_treedefs, out_leafshapes, out_leaves

tree_rules[lax.broadcast_in_dim_p] = broadcast_in_dim_tree_rule


def squeeze_tree_rule(
    treedefs_in: Tuple[Tuple[TreeDef, ...]],
    leafshapes_in: Tuple[LeafShapes],
    leaves_in: Tuple[Leaves],
    *,
    dimensions: Tuple[int, ...],
) -> TreeState:
  treedefs, = treedefs_in
  leafshapes, = leafshapes_in
  leaves, = leaves_in

  for axis, treedef in enumerate(treedefs):
    if axis in dimensions:
      if treedef != TRIVIAL_TREEDEF:
        raise ValueError(f"cannot squeeze dimension {axis} because it "
                         f"corresponds to a non-trivial pytree: {treedef}")

  out_treedefs = tuple(t for i, t in enumerate(treedefs) if i not in dimensions)
  out_leafshapes = tuple(s for i, s in enumerate(leafshapes) if i not in dimensions)

  out_leaves = {}
  for in_coords, out_coords in zip(
      _iter_leaf_coords(treedefs), _iter_leaf_coords(out_treedefs)):
    leaf = leaves[in_coords]
    leaf_dims = _axes_for_leaf(leafshapes, in_coords, dimensions)
    out_leaves[out_coords] = lax.squeeze_p.bind(leaf, dimensions=leaf_dims)

  return out_treedefs, out_leafshapes, out_leaves

tree_rules[lax.squeeze_p] = squeeze_tree_rule


def transpose_tree_rule(
    treedefs_in: Tuple[Tuple[TreeDef, ...]],
    leafshapes_in: Tuple[LeafShapes],
    leaves_in: Tuple[Leaves],
    *,
    permutation: Tuple[int, ...],
) -> TreeState:
  treedefs, = treedefs_in
  leafshapes, = leafshapes_in
  leaves, = leaves_in

  out_treedefs = tuple(treedefs[p] for p in permutation)
  out_leafshapes = tuple(leafshapes[p] for p in permutation)

  out_leaves = {}
  for in_coords in _iter_leaf_coords(treedefs):
    out_coords = tuple(in_coords[p] for p in permutation)
    leaf = leaves[in_coords]
    leaf_perm = _axes_for_leaf(leafshapes, in_coords, permutation)
    out_leaves[out_coords] = lax.transpose_p.bind(leaf, permutation=leaf_perm)

  return out_treedefs, out_leafshapes, out_leaves

tree_rules[lax.transpose_p] = transpose_tree_rule


def reducer_tree_rule(
    prim: core.Primitive,
    binop: Callable[[ArrayLike, ArrayLike], ArrayLike],
    treedefs_in: Tuple[Tuple[TreeDef, ...]],
    leafshapes_in: Tuple[LeafShapes],
    leaves_in: Tuple[Leaves],
    *,
    axes: Tuple[int, ...],
    **params,
) -> TreeState:
  treedefs, = treedefs_in
  leafshapes, = leafshapes_in
  leaves, = leaves_in

  out_treedefs = tuple(t for i, t in enumerate(treedefs) if i not in axes)
  out_leafshapes = tuple(s for i, s in enumerate(leafshapes) if i not in axes)

  out_nodes: Dict[Tuple[int, ...], List[ArrayLike]] = {
      coords: [] for coords in _iter_leaf_coords(out_treedefs)}

  for in_coords in _iter_leaf_coords(treedefs):
    out_coords = tuple(c for i, c in enumerate(in_coords) if i not in axes)
    leaf_axes = _axes_for_leaf(leafshapes, in_coords, axes)
    reduced_leaf = prim.bind(leaves[in_coords], axes=tuple(leaf_axes), **params)
    out_nodes[out_coords].append(reduced_leaf)

  out_leaves = {k: reduce(binop, v) for k, v in out_nodes.items()}
  return out_treedefs, out_leafshapes, out_leaves

def defreducer(prim: core.Primitive, binop_prim: core.Primitive) -> None:
  tree_rules[prim] = partial(reducer_tree_rule, prim, binop_prim.bind)

defreducer(lax.reduce_sum_p, lax.add_p)
defreducer(lax.reduce_prod_p, lax.mul_p)
defreducer(lax.reduce_max_p, lax.max_p)
defreducer(lax.reduce_min_p, lax.min_p)
defreducer(lax.reduce_or_p, lax.or_p)
defreducer(lax.reduce_and_p, lax.and_p)


def dot_general_tree_rule(
    treedefs_in: Tuple[Tuple[TreeDef, ...], ...],
    leafshapes_in: Tuple[LeafShapes, ...],
    leaves_in: Tuple[Leaves, ...],
    *,
    dimension_numbers: Tuple[Tuple[Tuple[int, ...], Tuple[int, ...]],
                             Tuple[Tuple[int, ...], Tuple[int, ...]]],
    **params,
) -> TreeState:
  (lhs_contracting, rhs_contracting), (lhs_batch, rhs_batch) = dimension_numbers
  batch, = {lhs_batch, rhs_batch}
  lhs_treedefs, rhs_treedefs = treedefs_in
  lhs_leafshapes, rhs_leafshapes = leafshapes_in
  lhs_leaves, rhs_leaves = leaves_in

  for lhs_axis, rhs_axis in itertools.chain(
        zip(batch, batch), zip(lhs_contracting, rhs_contracting)):
    if lhs_treedefs[lhs_axis] != rhs_treedefs[rhs_axis]:
      raise ValueError(
          f"conflicting treedefs: {lhs_treedefs[lhs_axis]} != {rhs_treedefs[rhs_axis]}"
      )
    if lhs_leafshapes[lhs_axis] != rhs_leafshapes[rhs_axis]:
      raise ValueError(
          f"conflicting shapes: {lhs_leafshapes[lhs_axis]} != {rhs_leafshapes[rhs_axis]}"
      )

  lhs_contract_or_batch = set(tuple(lhs_contracting) + tuple(batch))
  lhs_remaining = tuple(i for i in range(len(lhs_treedefs)) if i not in lhs_contract_or_batch)

  rhs_contract_or_batch = set(tuple(rhs_contracting) + tuple(batch))
  rhs_remaining = tuple(i for i in range(len(rhs_treedefs)) if i not in rhs_contract_or_batch)

  out_treedefs = ([lhs_treedefs[i] for i in batch + lhs_remaining]
                  + [rhs_treedefs[i] for i in rhs_remaining])
  out_leafshapes = ([lhs_leafshapes[i] for i in batch + lhs_remaining]
                    + [rhs_leafshapes[i] for i in rhs_remaining])

  out_nodes: Dict[Tuple[int, ...], List[ArrayLike]] = {
      coords: [] for coords in _iter_leaf_coords(out_treedefs)}

  rhs_nonbatch_treedefs = [rhs_treedefs[i] for i in rhs_remaining]

  for lhs_coords in _iter_leaf_coords(lhs_treedefs):
    for rhs_nonbatch_coords in _iter_leaf_coords(rhs_nonbatch_treedefs):

      rhs_only_coords = list(rhs_nonbatch_coords)
      for lhs_axis, rhs_axis in zip(lhs_contracting, rhs_contracting):
        rhs_only_coords.insert(rhs_axis, lhs_coords[lhs_axis])
      rhs_coords = lhs_coords[:len(batch)] + tuple(rhs_only_coords)

      out_coords = tuple([lhs_coords[i] for i in batch + lhs_remaining]
                         + [rhs_coords[i] for i in rhs_remaining])

      leaf_lhs_contracting = _axes_for_leaf(
          lhs_leafshapes, lhs_coords, lhs_contracting)
      leaf_rhs_contracting = _axes_for_leaf(
          rhs_leafshapes, rhs_coords, rhs_contracting)
      leaf_batch =_axes_for_leaf(lhs_leafshapes, lhs_coords, batch)
      assert leaf_batch == _axes_for_leaf(rhs_leafshapes, rhs_coords, batch)
      leaf_dim_numbers = ((leaf_lhs_contracting, leaf_rhs_contracting),
                          (leaf_batch, leaf_batch))

      reduced_leaf = lax.dot_general_p.bind(
          lhs_leaves[lhs_coords], rhs_leaves[rhs_coords],
          dimension_numbers=leaf_dim_numbers, **params)
      out_nodes[out_coords].append(reduced_leaf)

  out_leaves = {k: reduce(operator.add, v) for k, v in out_nodes.items()}
  return out_treedefs, out_leafshapes, out_leaves

tree_rules[lax.dot_general_p] = dot_general_tree_rule
