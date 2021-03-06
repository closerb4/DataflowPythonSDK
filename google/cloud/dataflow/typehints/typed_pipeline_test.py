# Copyright 2016 Google Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Unit tests for the type-hint objects and decorators."""
import inspect
import unittest


import google.cloud.dataflow as df
from google.cloud.dataflow import pvalue
from google.cloud.dataflow import typehints
from google.cloud.dataflow.transforms.util import assert_that, equal_to
from google.cloud.dataflow.typehints import WithTypeHints
from google.cloud.dataflow.utils.options import OptionsContext
from google.cloud.dataflow.utils.options import PipelineOptions

# These test often construct a pipeline as value | PTransform to test side
# effects (e.g. errors).
# pylint: disable=expression-not-assigned


class MainInputTest(unittest.TestCase):

  def test_bad_main_input(self):
    @typehints.with_input_types(str, int)
    def repeat(s, times):
      return s * times
    with self.assertRaises(typehints.TypeCheckError):
      [1, 2, 3] | df.Map(repeat, 3)

  def test_non_function(self):
    result = ['a', 'bb', 'c'] | df.Map(str.upper)
    self.assertEqual(['A', 'BB', 'C'], sorted(result))

    result = ['xa', 'bbx', 'xcx'] | df.Map(str.strip, 'x')
    self.assertEqual(['a', 'bb', 'c'], sorted(result))

    result = ['1', '10', '100'] | df.Map(int)
    self.assertEqual([1, 10, 100], sorted(result))

    result = ['1', '10', '100'] | df.Map(int, 16)
    self.assertEqual([1, 16, 256], sorted(result))

    with self.assertRaises(typehints.TypeCheckError):
      [1, 2, 3] | df.Map(str.upper)

  def test_loose_bounds(self):
    @typehints.with_input_types(typehints.Union[int, float, long])
    @typehints.with_output_types(basestring)
    def format_number(x):
      return '%g' % x
    result = [1, 2, 3] | df.Map(format_number)
    self.assertEqual(['1', '2', '3'], sorted(result))

  def test_typed_dofn_class(self):
    @typehints.with_input_types(int)
    @typehints.with_output_types(str)
    class MyDoFn(df.DoFn):
      def process(self, context):
        return [str(context.element)]

    result = [1, 2, 3] | df.ParDo(MyDoFn())
    self.assertEqual(['1', '2', '3'], sorted(result))

    with self.assertRaises(typehints.TypeCheckError):
      ['a', 'b', 'c'] | df.ParDo(MyDoFn())

    with self.assertRaises(typehints.TypeCheckError):
      [1, 2, 3] | (df.ParDo(MyDoFn()) | df.ParDo('again', MyDoFn()))

  def test_typed_dofn_instance(self):
    class MyDoFn(df.DoFn):
      def process(self, context):
        return [str(context.element)]
    my_do_fn = MyDoFn().with_input_types(int).with_output_types(str)

    result = [1, 2, 3] | df.ParDo(my_do_fn)
    self.assertEqual(['1', '2', '3'], sorted(result))

    with self.assertRaises(typehints.TypeCheckError):
      ['a', 'b', 'c'] | df.ParDo(my_do_fn)

    with self.assertRaises(typehints.TypeCheckError):
      [1, 2, 3] | (df.ParDo(my_do_fn) | df.ParDo('again', my_do_fn))


class SideInputTest(unittest.TestCase):

  def _run_repeat_test(self, repeat):
    self._run_repeat_test_good(repeat)
    self._run_repeat_test_bad(repeat)

  @OptionsContext(pipeline_type_check=True)
  def _run_repeat_test_good(self, repeat):
    # As a positional argument.
    result = ['a', 'bb', 'c'] | df.Map(repeat, 3)
    self.assertEqual(['aaa', 'bbbbbb', 'ccc'], sorted(result))

    # As a keyword argument.
    result = ['a', 'bb', 'c'] | df.Map(repeat, times=3)
    self.assertEqual(['aaa', 'bbbbbb', 'ccc'], sorted(result))

  def _run_repeat_test_bad(self, repeat):
    # Various mismatches.
    with self.assertRaises(typehints.TypeCheckError):
      ['a', 'bb', 'c'] | df.Map(repeat, 'z')
    with self.assertRaises(typehints.TypeCheckError):
      ['a', 'bb', 'c'] | df.Map(repeat, times='z')
    with self.assertRaises(typehints.TypeCheckError):
      ['a', 'bb', 'c'] | df.Map(repeat, 3, 4)
    if not inspect.getargspec(repeat).defaults:
      with self.assertRaises(typehints.TypeCheckError):
        ['a', 'bb', 'c'] | df.Map(repeat)

  def test_basic_side_input_hint(self):
    @typehints.with_input_types(str, int)
    def repeat(s, times):
      return s * times
    self._run_repeat_test(repeat)

  def test_keyword_side_input_hint(self):
    @typehints.with_input_types(str, times=int)
    def repeat(s, times):
      return s * times
    self._run_repeat_test(repeat)

  def test_default_typed_hint(self):
    @typehints.with_input_types(str, int)
    def repeat(s, times=3):
      return s * times
    self._run_repeat_test(repeat)

  def test_default_untyped_hint(self):
    @typehints.with_input_types(str)
    def repeat(s, times=3):
      return s * times
    # No type checking on dfault arg.
    self._run_repeat_test_good(repeat)

  @OptionsContext(pipeline_type_check=True)
  def test_varargs_side_input_hint(self):
    @typehints.with_input_types(str, int)
    def repeat(s, *times):
      return s * times[0]

    result = ['a', 'bb', 'c'] | df.Map(repeat, 3)
    self.assertEqual(['aaa', 'bbbbbb', 'ccc'], sorted(result))

  # TODO(robertwb): Support partially defined varargs.
  # with self.assertRaises(typehints.TypeCheckError):
  #   ['a', 'bb', 'c'] | df.Map(repeat, 'z')

  def test_deferred_side_inputs(self):
    @typehints.with_input_types(str, int)
    def repeat(s, times):
      return s * times
    p = df.Pipeline(options=PipelineOptions([]))
    main_input = p | df.Create(['a', 'bb', 'c'])
    side_input = p | df.Create('side', [3])
    result = main_input | df.Map(repeat, pvalue.AsSingleton(side_input))
    assert_that(result, equal_to(['aaa', 'bbbbbb', 'ccc']))
    p.run()

    bad_side_input = p | df.Create('bad_side', ['z'])
    with self.assertRaises(typehints.TypeCheckError):
      main_input | df.Map('again', repeat, pvalue.AsSingleton(bad_side_input))

  def test_deferred_side_input_iterable(self):
    @typehints.with_input_types(str, typehints.Iterable[str])
    def concat(glue, items):
      return glue.join(sorted(items))
    p = df.Pipeline(options=PipelineOptions([]))
    main_input = p | df.Create(['a', 'bb', 'c'])
    side_input = p | df.Create('side', ['x', 'y', 'z'])
    result = main_input | df.Map(concat, pvalue.AsIter(side_input))
    assert_that(result, equal_to(['xayaz', 'xbbybbz', 'xcycz']))
    p.run()

    bad_side_input = p | df.Create('bad_side', [1, 2, 3])
    with self.assertRaises(typehints.TypeCheckError):
      main_input | df.Map('fail', concat, pvalue.AsIter(bad_side_input))


class CustomTransformTest(unittest.TestCase):

  class CustomTransform(df.PTransform):

    def _extract_input_pvalues(self, pvalueish):
      return pvalueish, (pvalueish['in0'], pvalueish['in1'])

    def apply(self, pvalueish):
      return {'out0': pvalueish['in0'], 'out1': pvalueish['in1']}

    # TODO(robertwb): (typecheck) Make these the default?
    def with_input_types(self, *args, **kwargs):
      return WithTypeHints.with_input_types(self, *args, **kwargs)

    def with_output_types(self, *args, **kwargs):
      return WithTypeHints.with_output_types(self, *args, **kwargs)

  test_input = {'in0': ['a', 'b', 'c'], 'in1': [1, 2, 3]}

  def check_output(self, result):
    self.assertEqual(['a', 'b', 'c'], sorted(result['out0']))
    self.assertEqual([1, 2, 3], sorted(result['out1']))

  def test_custom_transform(self):
    self.check_output(self.test_input | self.CustomTransform())

  def test_keyword_type_hints(self):
    self.check_output(
        self.test_input | self.CustomTransform().with_input_types(
            in0=str, in1=int))
    self.check_output(
        self.test_input | self.CustomTransform().with_input_types(in0=str))
    self.check_output(
        self.test_input | self.CustomTransform().with_output_types(
            out0=str, out1=int))
    with self.assertRaises(typehints.TypeCheckError):
      self.test_input | self.CustomTransform().with_input_types(in0=int)
    with self.assertRaises(typehints.TypeCheckError):
      self.test_input | self.CustomTransform().with_output_types(out0=int)

  def test_flat_type_hint(self):
    # Type hint is applied to both.
    ({'in0': ['a', 'b', 'c'], 'in1': ['x', 'y', 'z']}
     | self.CustomTransform().with_input_types(str))
    with self.assertRaises(typehints.TypeCheckError):
      self.test_input | self.CustomTransform().with_input_types(str)
    with self.assertRaises(typehints.TypeCheckError):
      self.test_input | self.CustomTransform().with_input_types(int)
    with self.assertRaises(typehints.TypeCheckError):
      self.test_input | self.CustomTransform().with_output_types(int)


if __name__ == '__main__':
  unittest.main()
