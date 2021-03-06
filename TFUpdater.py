
from __future__ import print_function

import tensorflow as tf
from tensorflow.python.training.optimizer import Optimizer
from tensorflow.python.ops import resource_variable_ops

from Log import log
from TFNetwork import TFNetwork
from TFUtil import tf_version_tuple, assert_min_tf_version, CustomUpdate

_OptimizerClassesDict = {}  # type: dict[str,()->Optimizer]


def get_optimizer_class(class_name):
  """
  :param str class_name: e.g. "adam"
  :return: the class
  :rtype: type[Optimizer]|()->Optimizer
  """
  if not _OptimizerClassesDict:
    potential_list = list(vars(tf.train).items())
    if tf_version_tuple() >= (1, 2, 0):
      from tensorflow.contrib import opt
      potential_list += list(vars(opt).items())
    potential_list += list(globals().items())
    for name, v in potential_list:
      assert isinstance(name, str)
      if v is Optimizer:
        continue
      if not isinstance(v, type) or not issubclass(v, Optimizer):
        continue
      assert name.lower() not in _OptimizerClassesDict
      _OptimizerClassesDict[name.lower()] = v
      if name.endswith("Optimizer"):
        name = name[:-len("Optimizer")]
        assert name.lower() not in _OptimizerClassesDict
        _OptimizerClassesDict[name.lower()] = v
  return _OptimizerClassesDict[class_name.lower()]


class Updater(object):
  """
  This will create the :class:`tf.train.Optimizer` instance given the config
  and the update-op for all trainable vars.
  See the code of :func:`Updater.create_optimizer` for valid config options.

  Note: `Vincent Vanhoucke says <https://github.com/tensorflow/tensorflow/issues/323#issuecomment-159116515>`_,
  in case you get nans, consider increasing the epsilon (for Adam, Nadam and similar).
  This is the config option ``optimizer_epsilon``.
  In some places in our Theano code, 1e-16 is our default epsilon, in some other parts, 1e-8 is.
  1e-8 might be more stable. Or even 1e-6.
  Note that when the gradient is suddenly zero in one step, the update can be proportional to lr / eps.

  From the :class:`tf.train.AdamOptimizer` documentation:

      The default value of 1e-8 for epsilon might not be a good default in
      general. For example, when training an Inception network on ImageNet a
      current good choice is 1.0 or 0.1. Note that since AdamOptimizer uses the
      formulation just before Section 2.1 of the Kingma and Ba paper rather than
      the formulation in Algorithm 1, the "epsilon" referred to here is "epsilon
      hat" in the paper.

  More from Vincent Vanhoucke:

      One thing you can do is run with a tiny learning rate, or even zero learning rate.
      If you still have divergence then, you have a bug in your setup.
      If not, increase your rate slowly and see if there is a regime in which things train without diverging.
      It's completely possible to have weights that are in a good range,
      but activations or gradients going to infinity because of the shape of the loss, or too high a learning rate.
      It's obviously always a possibility that there is a bug in the optimizers, but in my experience,
      every single instance of this kind of problem could be traced back to a weirdly wired model,
      learning rate issues, bad randomization of the input examples,
      or - in the case of Adam or RMSProp - issues with the epsilon value.

  In addition, you might also want to try ``gradient_nan_inf_filter`` or maybe set beta1=0.5.

  For further debugging, see :func:`tf.add_check_numerics_ops` or :func:`add_check_numerics_ops_and_debug_print`,
  which is config option ``debug_add_check_numerics_ops``.

  """

  def __init__(self, config, tf_session, network):
    """
    :param Config.Config config:
    :param tf.Session tf_session:
    :param TFNetwork network:
    """
    self.config = config
    self.tf_session = tf_session
    self.learning_rate_var = tf.Variable(name="learning_rate", initial_value=0.0, trainable=False, dtype="float32")
    self.trainable_vars = []  # type: list[tf.Variable]
    self.network = network
    self.loss = network.get_objective()
    self.optimizer = None  # type: Optimizer
    self.optim_op = None  # type: tf.Operation
    self.optim_meta_losses = None  # type: dict[str,tf.Tensor]
    self.optimizer_vars = []  # type: list[tf.Variable]
    self.optimizer_init_vars_op = None  # type: tf.Operation

  def reset_optim_op(self):
    """
    Call this if sth is changed which the optim_op depends on.
    See self.create_optim_op().
    """
    self.optim_op = None  # type: tf.Operation

  def set_trainable_vars(self, trainable_vars):
    """
    :param list[tf.Variable] trainable_vars:
    """
    if trainable_vars == self.trainable_vars:
      return
    self.trainable_vars = trainable_vars
    self.reset_optim_op()

  def set_learning_rate(self, value):
    """
    :param float value:
    """
    self.network.get_var_assigner(self.learning_rate_var).assign(value, session=self.tf_session)

  def create_optimizer(self):
    lr = self.learning_rate_var
    epsilon = self.config.float("optimizer_epsilon", 1e-16)
    use_locking = self.config.bool("optimizer_use_locking", False)
    momentum = self.config.float("momentum", 0.0)
    optim_config = self.config.typed_value("optimizer")
    if optim_config:
      if isinstance(optim_config, str):
        optim_config = {"class": optim_config}
      assert isinstance(optim_config, dict)
      optim_config = optim_config.copy()
      optim_class_name = optim_config.pop("class")
      optim_class = get_optimizer_class(optim_class_name)
      from Util import collect_class_init_kwargs
      optim_class_kwargs = collect_class_init_kwargs(optim_class)
      if "epsilon" in optim_class_kwargs:
        optim_config.setdefault("epsilon", epsilon)
      if "momentum" in optim_class_kwargs and momentum:
        optim_config.setdefault("momentum", momentum)
      if "use_locking" in optim_class_kwargs and use_locking:
        optim_config.setdefault("use_locking", use_locking)
      assert "learning_rate" not in optim_config, "learning_rate will be set implicitly"
      optim_config["learning_rate"] = lr
      print("Create optimizer %s with options %r." % (optim_class, optim_config), file=log.v2)
      optimizer = optim_class(**optim_config)
      assert isinstance(optimizer, tf.train.Optimizer)
    elif self.config.bool("adam", False):
      assert not momentum
      print("Create Adam optimizer.", file=log.v2)
      # Default TF values: learning_rate=0.001, beta1=0.9, beta2=0.999, epsilon=1e-8.
      # Default Keras values: lr=0.001, beta_1=0.9, beta_2=0.999, epsilon=1e-8.
      # Our Theano default values: beta1=0.9, beta2=0.999, epsilon=1e-16
      # https://github.com/openai/improved-gan/blob/master/imagenet/train_imagenet.py: beta1=0.5
      optimizer = tf.train.AdamOptimizer(learning_rate=lr, epsilon=epsilon, use_locking=use_locking)
    elif self.config.bool("nadam", False):
      assert_min_tf_version((1, 2, 0), "NadamOptimizer introduced in TF 1.2.0")
      assert not momentum
      print("Create NAdam optimizer.", file=log.v2)
      # TF default values: like Adam: beta1=0.9, beta2=0.999, epsilon=1e-8
      # Our Theano default values: decay=0.004, beta1=0.9, beta2=0.999, epsilon=1e-8
      from tensorflow.contrib.opt import NadamOptimizer
      optimizer = NadamOptimizer(learning_rate=lr, epsilon=epsilon, use_locking=use_locking)
    elif self.config.bool("adadelta", False):
      assert not momentum
      print("Create Adadelta optimizer.", file=log.v2)
      optimizer = tf.train.AdadeltaOptimizer(learning_rate=lr, epsilon=epsilon, use_locking=use_locking)
    elif self.config.bool("adagrad", False):
      assert not momentum
      print("Create Adagrad optimizer.", file=log.v2)
      optimizer = tf.train.AdagradOptimizer(learning_rate=lr, use_locking=use_locking)
    elif self.config.is_of_type("rmsprop", float):
      print("Create RMSProp optimizer. With Decay %f" % (self.config.float("rmsprop", 0.9)), file=log.v2)
      optimizer = tf.train.RMSPropOptimizer(decay=self.config.float("rmsprop", 0.9), learning_rate=lr, momentum=momentum, epsilon=epsilon, use_locking=use_locking)
    elif self.config.bool("rmsprop", False):
      print("Create RMSProp optimizer.", file=log.v2)
      optimizer = tf.train.RMSPropOptimizer(learning_rate=lr, momentum=momentum, epsilon=epsilon, use_locking=use_locking)
    elif momentum:
      print("Create Momentum optimizer.", file=log.v2)
      optimizer = tf.train.MomentumOptimizer(learning_rate=lr, momentum=momentum, use_locking=use_locking)
    else:
      print("Create SGD optimizer.", file=log.v2)
      optimizer = tf.train.GradientDescentOptimizer(learning_rate=lr, use_locking=use_locking)
    self.optimizer = optimizer
    self.reset_optim_op()

  def _get_apply_grads_op(self, loss, trainable_vars_for_gradients):
    """
    :param tf.Tensor loss:
    :param list[tf.Variable] trainable_vars_for_gradients:
    :return: op with all variable updates combined, using the optimizer
    :rtype: tf.Operation
    """
    if not trainable_vars_for_gradients:
      return tf.no_op(name="no_grad_vars_no_op")
    # AccumulateN might not be deterministic but should be faster and should require less memory.
    # We might want to make this configurable.
    aggregation_method = tf.AggregationMethod.EXPERIMENTAL_ACCUMULATE_N
    grad_noise = self.config.float("gradient_noise", 0.0)
    grad_clip = self.config.float("gradient_clip", 0.0)
    grad_clip_global_norm = self.config.float("gradient_clip_global_norm", 0.0)
    # E.g. https://github.com/openai/baselines/blob/master/baselines/deepq/simple.py: grad_norm_clipping=10 -> tf.clip_by_norm

    # Extended self.optimizer.minimize() to optionally modify gradients.
    grads_and_vars = self.optimizer.compute_gradients(
      loss, var_list=trainable_vars_for_gradients,
      aggregation_method=aggregation_method)
    if not [v for g, v in grads_and_vars if g is not None]:
      raise Exception("no single variable to train")
    if self.config.bool("debug_grad_summaries", False):
      from TFUtil import variable_summaries, get_base_name, reuse_name_scope_of_tensor
      for grad, var in grads_and_vars:
        with reuse_name_scope_of_tensor(grad, prefix="grads/"):
          variable_summaries(grad, name="grad_of_%s" % get_base_name(var))
        with reuse_name_scope_of_tensor(var, prefix="vars/"):
          variable_summaries(var, name=get_base_name(var))
    # Also see tf.contrib.layers.optimizers.optimize_loss() for reference.
    if self.config.bool("gradient_nan_inf_filter", False):
      from TFUtil import nan_to_num
      grads_and_vars = [(nan_to_num(grad, nan_num=0.0, inf_num=0.0), var) for (grad, var) in grads_and_vars]
    if grad_noise:
      assert grad_noise > 0
      from TFUtil import add_scaled_noise_to_gradients
      grads_and_vars = add_scaled_noise_to_gradients(grads_and_vars, grad_noise)
    if grad_clip:
      assert grad_clip > 0
      grads_and_vars = [(tf.clip_by_value(grad, -grad_clip, grad_clip), var) for grad, var in grads_and_vars]
    if grad_clip_global_norm:
      assert grad_clip_global_norm > 0
      grads_clipped, _ = tf.clip_by_global_norm([grad for (grad, _) in grads_and_vars], grad_clip_global_norm)
      grads_and_vars = zip(grads_clipped, [var for (_, var) in grads_and_vars])
    apply_grads = self.optimizer.apply_gradients(grads_and_vars)
    return apply_grads

  def create_optim_op(self):
    assert self.loss is not None
    assert self.trainable_vars, "no variables to update/optimize"
    from TFUtil import SyntheticGradient

    # Keep track of all current available vars.
    # The optimizer could add some, even some which are not so-called "slot-vars",
    # and we want to keep track about them.
    all_vars = tf.global_variables()  # type: list[tf.Variable]

    if not self.optimizer:
      self.create_optimizer()

    trainable_vars_for_gradients = list(self.trainable_vars)
    trainable_vars_custom_update = []  # type: list[tf.Variable]
    for v in self.trainable_vars:
      if hasattr(v, "custom_update"):
        trainable_vars_custom_update.append(v)
        trainable_vars_for_gradients.remove(v)

    with tf.variable_scope("optimize"):
      synthetic_gradient_scope = SyntheticGradient.enter_gradient_scope()
      apply_grads = self._get_apply_grads_op(self.loss, trainable_vars_for_gradients)
      synthetic_gradient_scope.exit()
      self.optim_meta_losses = synthetic_gradient_scope.as_fetch_dict()
      if synthetic_gradient_scope.losses:
        with tf.name_scope("meta_loss"):
          meta_loss = tf.add_n(synthetic_gradient_scope.losses)
          meta_apply_grads = self._get_apply_grads_op(meta_loss, trainable_vars_for_gradients)
        apply_grads = tf.group(apply_grads, meta_apply_grads)
      incr_step_op = tf.assign_add(self.network.global_train_step, 1, name="global_train_step_increment")
      self.optim_op = tf.group(apply_grads, incr_step_op, name="optim_and_step_incr")

    if trainable_vars_custom_update:
      with tf.variable_scope("custom_update"):
        updates = [self.optim_op]
        for param in trainable_vars_custom_update:
          custom_update = getattr(param, "custom_update")
          assert isinstance(custom_update, CustomUpdate)
          updates.append(custom_update.update_var(param))
        self.optim_op = tf.group(*updates)

    print("Initialize optimizer with slots %s." % self.optimizer.get_slot_names(), file=log.v3)
    slot_vars = []
    for slot_name in self.optimizer.get_slot_names():
      for v in trainable_vars_for_gradients:
        slot_var = self.optimizer.get_slot(var=v, name=slot_name)
        assert slot_var is not None
        assert isinstance(slot_var, tf.Variable)
        slot_vars.append(slot_var)
    self.optimizer_vars = slot_vars

    # Check if there were any other variables added.
    # E.g. currently (TF 1.0) the `AdamOptimizer` creates these additional vars
    # `[<tf.Variable 'optimize/beta1_power:0' shape=() dtype=float32_ref>,
    #   <tf.Variable 'optimize/beta2_power:0' shape=() dtype=float32_ref>]`
    # which do not correspond to trainable vars, thus we did not get them as slot vars above.
    other_new_vars = []
    for v in tf.global_variables():
      if v in all_vars:
        continue
      if v in self.optimizer_vars:
        continue
      other_new_vars.append(v)
    if other_new_vars:
      print("These additional variable were created by the optimizer: %s." % other_new_vars, file=log.v3)
      self.optimizer_vars += other_new_vars
    with tf.name_scope("optimizer_init_vars"):
      self.optimizer_init_vars_op = tf.variables_initializer(self.optimizer_vars, name="init_optim_slot_vars")
    self.init_optimizer_vars()

    if self.config.bool("debug_grad_summaries", False):
      from TFUtil import variable_summaries, get_base_name, reuse_name_scope_of_tensor
      for key in self.network.used_data_keys:
        data = self.network.extern_data.data[key]
        if data.sparse:
          continue
        with reuse_name_scope_of_tensor(data.placeholder):
          variable_summaries(data.placeholder)

    if self.config.bool("debug_add_check_numerics_ops", False):
      print("Adding checks for inf/nan.", file=log.v3)
      self.optim_op = tf.group(self.optim_op, add_check_numerics_ops([self.optim_op]))

    if self.config.bool("debug_save_updater_vars", False):
      print("Save updater/optimizer vars:", file=log.v3)
      print(self.optimizer_vars)
      for v in self.optimizer_vars:
        if v not in self.network.extra_vars_to_save:
          self.network.extra_vars_to_save.append(v)
      self.network.reset_saver()

  def get_optim_op(self, callback_on_new=None):
    """
    :param None|()->None callback_on_new:
    :rtype: tf.Operation
    """
    if self.optim_op is None:
      self.create_optim_op()
      if callback_on_new:
        callback_on_new()
    return self.optim_op

  def init_optimizer_vars(self):
    self.tf_session.run(self.optimizer_init_vars_op)


def add_check_numerics_ops(
  fetches=None, ignore_ops=None, use_check_numerics=True, debug_print_added_checks=True,
  name="add_check_numerics_ops"):
  """
  This is similar to :func:`tf.add_check_numerics_ops` and based on similar code.
  It adds some more logic and options.

  :param list[tf.Operation|tf.Tensor]|None fetches: in case this is given, will only look at these and dependent ops
  :param list[str] ignore_ops: e.g. ""
  :param bool use_check_numerics: if False, instead of :func:`tf.check_numerics`,
    it does the check manually (via :func:`tf.is_finite`) and in case there is inf/nan,
    it will also print the tensor (while `tf.check_numerics` does not print the tensor).
    Note that this can be about 50 times slower.
  :param bool debug_print_added_checks: prints info about each added check
  :param str name: op-name for the final tf.group
  :return: operation which performs all the checks
  :rtype: tf.Operation
  """
  if fetches is None:
    ops = tf.get_default_graph().get_operations()
  else:
    fetch_ops = [v.op if isinstance(v, tf.Tensor) else v for v in fetches]
    assert all([isinstance(op, tf.Operation) for op in fetch_ops])
    from tensorflow.contrib import graph_editor
    ops = graph_editor.get_backward_walk_ops(fetch_ops, inclusive=True, control_inputs=True)
  if ignore_ops is None:
    # The checks could increase the memory usage a lot.
    # Ignore some common ops which should not be able to introduce inf/nan.
    ignore_ops = {
      "Add", "AddN", "Sum", "Mul", "MatMul", "Sub", "L2Loss", "Floor", "Neg", "UnsortedSegmentSum",
      "Switch", "Merge", "PreventGradient",
      "Const", "Identity", "Fill", "ZerosLike",
      "Reshape", "Tile", "ExpandDims", "ConcatV2", "Transpose",
      "Slice", "StridedSlice", "StridedSliceGrad", "Gather",
      "TruncatedNormal", "RandomUniform"}
  with tf.name_scope(name):
    check_op = []
    # This code relies on the ordering of ops in get_operations().
    # The producer of a tensor always comes before that tensor's consumer in
    # this list. This is true because get_operations() returns ops in the order
    # added, and an op can only be added after its inputs are added.
    for op in ops:
      assert isinstance(op, tf.Operation)
      if op.type in ignore_ops:
        continue
      for output in op.outputs:
        if output.dtype in [tf.float16, tf.float32, tf.float64]:
          message = op.name + ":" + str(output.value_index)
          with tf.control_dependencies(check_op):
            if debug_print_added_checks:
              print("add check for:", output, op.type)
            if use_check_numerics:
              check_op = [tf.check_numerics(output, message=message, name=op.name + "_check_numerics")]
            else:
              is_finite = tf.reduce_all(tf.is_finite(output))
              check_op = [tf.Assert(is_finite, [message, "Tensor had inf or nan values:", output])]
    return tf.group(*check_op)


class _BaseCustomOptimizer(Optimizer):
  """
  Base class for our own optimizer implementations.
  This simplifies the interface to be implemented a bit from :class:`Optimizer`.
  """

  def __init__(self, learning_rate, use_locking=False, name=None):
    """Construct a new optimizer.

    Args:
      learning_rate: A Tensor or a floating point value.  The learning
        rate to use.
      use_locking: If True use locks for update operations.
      name: Optional name prefix for the operations created when applying
        gradients. Defaults to `self.__class__.__name__`.
    """
    if name is None:
      name = self.__class__.__name__
    super(_BaseCustomOptimizer, self).__init__(use_locking, name)
    self._learning_rate = learning_rate

  def _prepare(self):
    self._learning_rate_tensor = tf.convert_to_tensor(self._learning_rate, name="learning_rate")

  def _apply(self, grad, var, indices=None):
    """
    :param tf.Tensor grad:
    :param tf.Variable|resource_variable_ops.ResourceVariable var:
    :param tf.Tensor|None indices: if this is a sparse update, the indices of the grad values
    :return: update
    :rtype: tf.Tensor|tf.Operation
    """
    raise NotImplementedError

  def _apply_dense(self, grad, var):
    return self._apply(grad=grad, var=var)

  def _resource_apply_dense(self, grad, handle):
    return self._apply_dense(grad=grad, var=handle)

  def _resource_apply_sparse_duplicate_indices(self, grad, handle, indices):
    return self._apply(grad=grad, var=handle, indices=indices)

  def _resource_apply_sparse(self, grad, handle, indices):
    return self._resource_apply_sparse_duplicate_indices(grad=grad, handle=handle, indices=indices)

  def _apply_sparse_duplicate_indices(self, grad, var):
    return self._apply(grad=grad.values, var=var, indices=grad.indices)

  def _apply_sparse(self, grad, var):
    return self._apply_sparse_duplicate_indices(grad=grad, var=var)

  def _assign_add(self, ref, updates, indices=None):
    if indices is not None:
      if isinstance(ref, tf.Variable):
        return tf.scatter_add(ref, indices, updates, use_locking=self._use_locking)
      elif isinstance(ref, resource_variable_ops.ResourceVariable):
        with tf.control_dependencies([resource_variable_ops.resource_scatter_add(ref.handle, indices, updates)]):
          return ref.value()
      else:
        raise TypeError("did not expect type %r" % type(ref))
    else:
      return tf.assign_add(ref, updates, use_locking=self._use_locking)

  def _assign_sub(self, ref, updates, indices=None):
    if indices is not None:
      if isinstance(ref, tf.Variable):
        return tf.scatter_sub(ref, indices, updates, use_locking=self._use_locking)
      elif isinstance(ref, resource_variable_ops.ResourceVariable):
        with tf.control_dependencies([resource_variable_ops.resource_scatter_add(ref.handle, indices, -updates)]):
          return ref.value()
      else:
        raise TypeError("did not expect type %r" % type(ref))
    else:
      return tf.assign_sub(ref, updates, use_locking=self._use_locking)

  def _gather(self, dense, indices=None):
    if indices is not None:
      return tf.gather(dense, indices=indices)
    return dense


class CustomGradientDescentOptimizer(_BaseCustomOptimizer):
  """
  Just an example implementation for simple gradient descent.
  """

  def _apply(self, grad, var, indices=None):
    lr = tf.cast(self._learning_rate_tensor, grad.dtype.base_dtype)
    return self._assign_sub(ref=var, updates=lr * grad, indices=indices).op


class NeuralOptimizer1(_BaseCustomOptimizer):
  """
  Via Neural Optimizer Search with Reinforcement Learning (http://proceedings.mlr.press/v70/bello17a/bello17a.pdf).

  Equivalent to the optimizer g * exp(sign(g) * sign(m)), we use:

    g * where(sign(g) == sign(m), 1.0, decrease_factor)

  where m is the running average of g.

  Calculation of m: m_t <- beta1 * m_{t-1} + (1 - beta1) * g
  Same beta1 default as in Adam and in the paper: beta1=0.9
  """

  def __init__(self, beta1=0.9, decrease_factor=0.1, **kwargs):
    """
    :param float beta1: used for the running average of m
    :param float decrease_factor: in the original paper, it is e^-2 ~= 0.135
    """
    super(NeuralOptimizer1, self).__init__(**kwargs)
    self._beta1 = beta1
    self._decrease_factor = decrease_factor

  def _prepare(self):
    super(NeuralOptimizer1, self)._prepare()
    self._beta1_t = tf.convert_to_tensor(self._beta1, name="beta1")

  def _create_slots(self, var_list):
    for v in var_list:
      self._zeros_slot(v, "m", self._name)

  def _apply(self, grad, var, indices=None):
    lr = tf.cast(self._learning_rate_tensor, var.dtype.base_dtype)
    m = self.get_slot(var, "m")
    # m_t = beta1 * m + (1 - beta1) * g_t
    beta1_t = tf.cast(self._beta1_t, var.dtype.base_dtype)
    m_scaled_g_values = grad * (1 - beta1_t)
    m_t = tf.assign(m, m * beta1_t, use_locking=self._use_locking)
    with tf.control_dependencies([m_t]):
      m_t = self._assign_add(m, updates=m_scaled_g_values, indices=indices)
    # update = lr * grad * where(...)
    m_gathered = self._gather(m_t, indices=indices)
    ones = tf.ones_like(grad)
    update = lr * grad * tf.where(tf.equal(tf.sign(m_gathered), tf.sign(grad)), ones, ones * self._decrease_factor)
    var_update = self._assign_sub(ref=var, updates=update, indices=indices)
    return tf.group(*[var_update, m_t])


class GradVarianceScaledOptimizer(_BaseCustomOptimizer):
  """
  Let m be the running average of g.
  Calculation of m: m_t <- beta1 * m_{t-1} + (1 - beta1) * g
  Same beta1 default as in Adam and in the paper: beta1=0.9

  Let v be the running average of the variance of g, i.e. of (g - m)^2.
  """

  def __init__(self, beta1=0.9, beta2=0.999, epsilon=1e-8, **kwargs):
    """
    :param float beta1: used for the running average of g (m)
    :param float beta2: used for the running average of variance of g (v)
    :param float epsilon:
    """
    super(GradVarianceScaledOptimizer, self).__init__(**kwargs)
    self._beta1 = beta1
    self._beta2 = beta2
    self._epsilon = epsilon

  def _prepare(self):
    super(GradVarianceScaledOptimizer, self)._prepare()
    self._beta1_t = tf.convert_to_tensor(self._beta1, name="beta1")
    self._beta2_t = tf.convert_to_tensor(self._beta2, name="beta2")
    self._epsilon_t = tf.convert_to_tensor(self._epsilon, name="epsilon")

  def _create_slots(self, var_list):
    for v in var_list:
      self._zeros_slot(v, "m", self._name)
      self._zeros_slot(v, "v", self._name)

  def _apply(self, grad, var, indices=None):
    lr = tf.cast(self._learning_rate_tensor, var.dtype.base_dtype)
    m = self.get_slot(var, "m")
    v = self.get_slot(var, "v")
    beta1_t = tf.cast(self._beta1_t, var.dtype.base_dtype)
    beta2_t = tf.cast(self._beta2_t, var.dtype.base_dtype)
    epsilon_t = tf.cast(self._epsilon_t, var.dtype.base_dtype)

    # m_t = beta1 * m + (1 - beta1) * g_t
    m_scaled_g_values = grad * (1 - beta1_t)
    m_t = tf.assign(m, m * beta1_t, use_locking=self._use_locking)
    with tf.control_dependencies([m_t]):
      m_t = self._assign_add(m, updates=m_scaled_g_values, indices=indices)
    m_gathered = self._gather(m_t, indices=indices)

    # Also see tf.nn.moments.
    variance = tf.squared_difference(grad, m_gathered)

    # v_t = beta2 * v + (1 - beta2) * variance
    v_scaled_new_values = variance * (1 - beta2_t)
    v_t = tf.assign(v, v * beta2_t, use_locking=self._use_locking)
    with tf.control_dependencies([v_t]):
      v_t = self._assign_add(v, updates=v_scaled_new_values, indices=indices)
    v_gathered = self._gather(v_t, indices=indices)

    # update = lr * grad * v / (variance + eps)
    factor = v_gathered / (variance + epsilon_t)
    # with tf.get_default_graph().colocate_with(None, True):
    #   with tf.control_dependencies([tf.Print(factor, [tf.reduce_min(factor), tf.reduce_max(factor), tf.reduce_mean(factor)])]):
    #     factor = tf.identity(factor)
    update = lr * grad * tf.minimum(factor, 1.0)
    var_update = self._assign_sub(ref=var, updates=update, indices=indices)
    return tf.group(*[var_update, m_t])
