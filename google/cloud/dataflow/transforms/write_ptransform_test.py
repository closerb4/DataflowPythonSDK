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
"""Unit tests for the write transform."""

import logging
import unittest

import google.cloud.dataflow as df

from google.cloud.dataflow.io import iobase
from google.cloud.dataflow.pipeline import Pipeline
from google.cloud.dataflow.transforms.ptransform import PTransform
from google.cloud.dataflow.transforms.util import assert_that, is_empty
from google.cloud.dataflow.utils.options import PipelineOptions


class _TestSink(iobase.Sink):
  TEST_INIT_RESULT = 'test_init_result'

  def __init__(self, return_init_result=True, return_write_results=True):
    self.return_init_result = return_init_result
    self.return_write_results = return_write_results

  def initialize_write(self):
    if self.return_init_result:
      return _TestSink.TEST_INIT_RESULT

  def finalize_write(self, init_result, writer_results):
    self.init_result_at_finalize = init_result
    self.write_results_at_finalize = writer_results

  def open_writer(self, init_result, uid):
    writer = _TestWriter(init_result, uid, self.return_write_results)
    return writer


class _TestWriter(iobase.Writer):
  STATE_UNSTARTED, STATE_WRITTEN, STATE_CLOSED = 0, 1, 2
  TEST_WRITE_RESULT = 'test_write_result'

  def __init__(self, init_result, uid, return_write_results=True):
    self.state = _TestWriter.STATE_UNSTARTED
    self.init_result = init_result
    self.uid = uid
    self.write_output = []
    self.return_write_results = return_write_results

  def close(self):
    assert self.state in (
        _TestWriter.STATE_WRITTEN, _TestWriter.STATE_UNSTARTED)
    self.state = _TestWriter.STATE_CLOSED
    if self.return_write_results:
      return _TestWriter.TEST_WRITE_RESULT

  def write(self, value):
    if self.write_output:
      assert self.state == _TestWriter.STATE_WRITTEN
    else:
      assert self.state == _TestWriter.STATE_UNSTARTED

    self.state = _TestWriter.STATE_WRITTEN
    self.write_output.append(value)


class WriteToTestSink(PTransform):

  def __init__(self, return_init_result=True, return_write_results=True):
    self.return_init_result = return_init_result
    self.return_write_results = return_write_results
    self.last_sink = None
    self.label = 'write_to_test_sink'

  def apply(self, pcoll):
    self.last_sink = _TestSink(return_init_result=self.return_init_result,
                               return_write_results=self.return_write_results)
    return pcoll | df.io.Write(self.last_sink)


class WriteTest(unittest.TestCase):
  DATA = ['some data', 'more data', 'another data', 'yet another data']

  def _run_write_test(self,
                      data,
                      return_init_result=True,
                      return_write_results=True):
    write_to_test_sink = WriteToTestSink(return_init_result,
                                         return_write_results)
    p = Pipeline(options=PipelineOptions([]))
    result = p | df.Create('start', data) | write_to_test_sink

    assert_that(result, is_empty())
    p.run()

    sink = write_to_test_sink.last_sink
    self.assertIsNotNone(sink)

  def test_write(self):
    self._run_write_test(WriteTest.DATA)

  def test_write_with_empty_pcollection(self):
    data = []
    self._run_write_test(data)

  def test_write_no_init_result(self):
    self._run_write_test(WriteTest.DATA, return_init_result=False)

  def test_write_no_write_results(self):
    self._run_write_test(WriteTest.DATA, return_write_results=False)


if __name__ == '__main__':
  logging.getLogger().setLevel(logging.INFO)
  unittest.main()
