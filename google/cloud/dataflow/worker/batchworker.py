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

"""Python Dataflow batch worker.

A Worker implements a lease/execute work loop. Multiple processes may execute
the same worker loop to get more throughput. In each worker process there are
two threads running: the main worker thread and the progress updating thread.
The main worker thread will lease a work item, execute it and then report
completion (either success or failure).  The progress updating thread will pick
up the current work item executed by main thread (see the synchronized
current_work_item property) and then will send periodic progress updates to the
service. These progress updates are essential for ensuring the worker does not
lose the lease on the worker item. This can happen if the service does not get
timely updates, declares the work item lost, and reassigns to another worker.

The two threads can be in contention only when work item attributes related to
the progress updating protocol are accessed (reporting index, lease expiration
time, duration till next report, etc.). The threads will not be in contention
while the work item is getting executed. This is essential in order to make sure
that long work items have progress updates sent in a timely manner and leases
are not lost often.
"""

import BaseHTTPServer
from collections import namedtuple
import datetime
import logging
import os
import random
import re
import resource
import sys
import threading
import time
import traceback

from google.cloud.dataflow.internal import apiclient
from google.cloud.dataflow.internal import auth
from google.cloud.dataflow.internal import pickler
from google.cloud.dataflow.utils import names
from google.cloud.dataflow.utils import options
from google.cloud.dataflow.utils import profiler
from google.cloud.dataflow.utils import retry
from google.cloud.dataflow.worker import environment
from google.cloud.dataflow.worker import executor
from google.cloud.dataflow.worker import logger
from google.cloud.dataflow.worker import maptask
from google.cloud.dataflow.worker import workitem

from apitools.base.py.exceptions import HttpError


class ProgressReporter(object):
  """A utility class that can be used to send progress of work items to service.


  An instance of this should be used to send progress reports for a given work
  item.
  """

  DEFAULT_MIN_REPORTING_INTERVAL_SECS = 5.0
  DEFAULT_MAX_REPORTING_INTERVAL_SECS = 10 * 60.0
  DEFAULT_LEASE_RENEWAL_LATENCY_SECS = 5.0

  def __init__(self, work_item, work_executor, batch_worker, client):
    assert work_item is not None
    assert work_executor is not None
    assert batch_worker is not None
    assert client is not None

    self._work_item = work_item
    self._work_executor = work_executor
    self._batch_worker = batch_worker
    self._client = client
    self._stopped = False
    self._stop_reporting_progress = False
    self._desired_lease_duration = None

    # Public for testing
    self.dynamic_split_result_to_report = None

  def start_reporting_progress(self):
    """Starts sending progress reports."""
    thread = threading.Thread(target=self.progress_reporting_thread)
    thread.daemon = True
    thread.start()

  def stop_reporting_progress(self):
    """Stops sending progress updates and shuts down the progress reporter.

    May fail with an exception if unable to send the last split request to the
    service in which case the work item should be marked as failed.
    """
    self._stop_reporting_progress = True

    # Shutting down cleanly
    while not self._stopped:
      time.sleep(1)

    # If there is an unreported dynamic work rebalancing response, we must send
    # it now to guarantee correctness. This may raise an error which will
    # result in current WorkItem being re-tried by the service.
    if self.dynamic_split_result_to_report is not None:
      self.report_status(progress=self._work_executor.get_progress())

  def progress_reporting_thread(self):
    """Sends progress reports for the work item till stopped."""

    try:
      while not self._stop_reporting_progress:
        try:
          BatchWorker.log_memory_usage_if_needed(self._batch_worker.worker_id,
                                                 force=False)
          with self._work_item.lock:
            # If WorkItem was marked 'done' in the main worker thread we stop
            # reporting progress of it.
            if self._work_item.done:
              break
          self.report_status(progress=self._work_executor.get_progress())
          sleep_time = self.next_progress_report_interval(
              self._work_item.report_status_interval,
              self._work_item.lease_expire_time)
          logging.debug(
              'Progress reporting thread will sleep %f secs between updates.',
              sleep_time)
          time.sleep(sleep_time)
        except Exception:  # pylint: disable=broad-except
          logging.info('Progress reporting thread got error: %s',
                       traceback.format_exc())
    finally:
      self._stopped = True

  # Public for testing
  def next_progress_report_interval(self, suggested_interval,
                                    lease_renewal_deadline):
    """Returns the duration till next progress report is needed (in secs).

    Args:
      suggested_interval: Duration (as a string) until a status update for the
        work item should be send back to the service (e.g., '5.000s' or '5s' if
        zero milliseconds).
      lease_renewal_deadline: UTC time (a string) when the lease will expire
        (e.g., '2015-06-17T17:22:49.999Z' or '2015-06-17T17:22:49Z' if zero
        milliseconds).

    Returns:
      Seconds with fractional msecs when next report is expected.
    """
    # Note that the calculation below will clear out a zero returned from the
    # cloud_time_to_timestamp() function which can happen if the service sends
    # cloud time strings in an unexpected format.
    suggested_interval = min(
        float(suggested_interval.rstrip('s')),
        self.cloud_time_to_timestamp(lease_renewal_deadline) - time.time() -
        self.DEFAULT_LEASE_RENEWAL_LATENCY_SECS)
    return min(
        max(self.DEFAULT_MIN_REPORTING_INTERVAL_SECS, suggested_interval),
        self.DEFAULT_MAX_REPORTING_INTERVAL_SECS)

  def cloud_time_to_timestamp(self, cloud_time_string):
    """Converts a cloud time string into a timestamp (seconds since EPOCH).

    Args:
      cloud_time_string: UTC time (a string) when the lease will expire
        (e.g., '2015-06-17T17:22:49.999Z' or '2015-06-17T17:22:49Z' if zero
        milliseconds).

    Returns:
      Seconds since EPOCH as a float with fractional part representing msecs.
      Will return 0 if the string is not in expected format.
    """
    rgx_cloud_time = (r'^(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})T'
                      r'(?P<hour>\d{2}):(?P<minute>\d{2}):(?P<second>\d{2})'
                      r'(\.(?P<msec>\d{3}))?Z$')

    match = re.match(rgx_cloud_time, cloud_time_string)
    if match:
      dt = datetime.datetime(
          int(match.group('year')), int(match.group('month')),
          int(match.group('day')), int(match.group('hour')),
          int(match.group('minute')), int(match.group('second')), 0 if
          match.group('msec') is None else int(match.group('msec')) * 1000)
      return (dt - datetime.datetime.fromtimestamp(0)).total_seconds()
    else:
      # Callers will handle this unexpected error.
      logging.warning('Unexpected cloud time string: %s', cloud_time_string)
      return 0

  def report_status(self,
                    completed=False,
                    progress=None,
                    source_operation_response=None,
                    exception_details=None):
    """Reports to the service status of a work item (completion or progress).

    Args:
      completed: True if there is no further work to be done on this work item
        either because it succeeded or because it failed. False if this is a
        progress report.
      progress: Progress of processing the work_item.
      source_operation_response: Response to a custom source operation
      exception_details: A string representation of the stack trace for an
        exception raised while executing the work item. The string is the
        output of the standard traceback.format_exc() function.


    Note. Callers of this function should acquire the work_item.lock because
    the function will change fields in the work item based on the response
    received (e.g., next_report_index, lease_expire_time, etc.).
    """

    response = self._client.report_status(
        self._batch_worker.worker_info_for_client(),
        self.desired_lease_duration(),
        self._work_item,
        completed,
        progress if not completed else None,
        self.dynamic_split_result_to_report if not completed else None,
        source_operation_response=source_operation_response,
        exception_details=exception_details)

    # Resetting dynamic_split_result_to_report after reporting status
    # successfully.
    self.dynamic_split_result_to_report = None

    # If this a progress report (not completion report) then pick up the
    # new reporting parameters for the work item from the response.
    if not completed:
      self.process_report_status_response(response)

  # Public for testing
  def process_report_status_response(self, response):
    """Processes a response to a progress report received from the service."""
    work_item_state = response.workItemServiceStates[0]
    self._work_item.next_report_index = work_item_state.nextReportIndex
    self._work_item.lease_expire_time = work_item_state.leaseExpireTime
    self._work_item.report_status_interval = (
        work_item_state.reportStatusInterval)

    suggested_split_point = work_item_state.suggestedStopPoint
    # Along with the response to the status report, Dataflow service may
    # send a suggested_split_point, which basically is a request for
    # performing dynamic work rebalancing if possible.
    #
    # Here we pass the received suggested_split_point to current
    # 'SourceReader' and try to perform a dynamic split.
    #
    # If splitting is successful, the corresponding 'DynamicSplitResult'
    # will be sent to the Dataflow service along with the next progress
    # report.
    if suggested_split_point is not None:
      self.dynamic_split_result_to_report = (
          self._work_executor.request_dynamic_split(
              apiclient.approximate_progress_to_dynamic_split_request(
                  suggested_split_point)))

  def desired_lease_duration(self):
    """Returns the desired duration for a work item lease.

    This duration is send to the service in progress updates. The service may
    or may not honor the request. The worker has to use the progress updating
    timings sent by the service in the response in order to not lose the lease
    on the work item.

    Returns:
      The duration to request, as a string representing number of seconds.
    """
    return (self. _desired_lease_duration or
            self._batch_worker.default_desired_lease_duration())


# Encapsulates information about a BatchWorker object needed when sending
# requests to Dataflow service.
BatchWorkerInfo = namedtuple(
    'WorkerInfo',
    'worker_id project_id job_id work_types capabilities '
    'formatted_current_time')


class BatchWorker(object):
  """A worker class with all the knowledge to lease and execute work items."""

  # TODO(vladum): Make this configurable via a flag.
  STATUS_HTTP_PORT = 0  # A value of 0 will pick a random unused port.
  MEMORY_USAGE_REPORTING_INTERVAL_SECS = 5 * 60
  DEFAULT_LEASE_DURATION_SECS = 3 * 60.0

  last_memory_usage_report_time = None

  def __init__(self, properties, sdk_pipeline_options):
    """Initializes a worker object from command line arguments."""
    self.project_id = properties['project_id']
    self.job_id = properties['job_id']
    self.worker_id = properties['worker_id']
    self.service_path = properties['service_path']
    # TODO(silviuc): Make sure environment_info_path is always specified.
    self.environment_info_path = properties.get('environment_info_path', None)
    self.pipeline_options = options.PipelineOptions.from_dictionary(
        sdk_pipeline_options)
    self.capabilities = [self.worker_id, 'remote_source', 'custom_source']
    self.work_types = ['map_task', 'seq_map_task', 'remote_source_task']
    # The following properties are passed to the worker when its container
    # gets started and are not used right now.
    self.root_url = properties['root_url']
    self.reporting_enabled = properties['reporting_enabled']
    self.temp_gcs_directory = properties['temp_gcs_directory']
    # Detect if the worker is running in a GCE VM.
    self.running_in_gce = self.temp_gcs_directory.startswith('gs://')
    # When running in a GCE VM the local_staging_property is always set.
    # For non-VM scenarios (integration tests) the local_staging_directory will
    # default to the temp directory.
    self.local_staging_directory = (properties['local_staging_directory']
                                    if self.running_in_gce else
                                    self.temp_gcs_directory)

    self.client = apiclient.DataflowWorkerClient(
        worker=self,
        skip_get_credentials=(not self.running_in_gce))

    self.environment = maptask.WorkerEnvironment()

    # If 'True' each work item will be profiled with cProfile. Results will
    # be logged and also saved to profile_location if set.
    self.work_item_profiling = sdk_pipeline_options.get('profile', False)
    self.profile_location = sdk_pipeline_options.get('profile_location', None)

    self._shutdown = False

  def worker_info_for_client(self):
    return BatchWorkerInfo(self.worker_id, self.project_id, self.job_id,
                           self.work_types, self.capabilities,
                           self.formatted_current_time)

  @property
  def formatted_current_time(self):
    # TODO(silviuc): Do we need to support milliseconds too?
    # The format supports also '...:5.123' (5 secs and 123 msecs).
    # TODO(silviuc): Consider using utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')
    now = datetime.datetime.utcnow()
    return '%04d-%02d-%02dT%02d:%02d:%02d.%03dZ' % (
        now.year, now.month, now.day, now.hour, now.minute, now.second,
        now.microsecond / 1000)

  def default_desired_lease_duration(self):
    return '%.3fs' % self.DEFAULT_LEASE_DURATION_SECS

  def _load_main_session(self, session_path):
    """Loads a pickled main session from the path specified."""
    session_file = os.path.join(session_path, names.PICKLED_MAIN_SESSION_FILE)
    if os.path.isfile(session_file):
      pickler.load_session(session_file)
    else:
      logging.warning(
          'No session file found: %s. Functions defined in __main__ '
          '(interactive session) may fail.', session_file)

  @retry.with_exponential_backoff()  # Using retry defaults from utils/retry.py
  def report_completion_status(
      self,
      current_work_item,
      progress_reporter,
      source_operation_response=None,
      exception_details=None):
    """Reports to the service a work item completion (successful or failed).

    Reporting completion status will do retry with exponential backoff in order
    to maximize the chances of getting the result to the service. An interim
    progress report on the other hand will not be retried since it can be
    sent on the next reporting cycle.

    The exponential backoff is done by doubling at each retry the initial delay
    and also introducing some fuzzing in the exact delay.

    Args:
      current_work_item: A WorkItem instance describing the work.
      progress_reporter: A ProgressReporter configured to process work item
        current_work_item.
      source_operation_response: Response to a custom source operation.
      exception_details: A string representation of the stack trace for an
        exception raised while executing the work item. The string is the
        output of the standard traceback.format_exc() function.

    Note. Callers of this function should acquire the work_item.lock.
    """
    # The log message string 'Finished processing' is looked for by
    # internal tests. Please do not modify the prefix without checking.
    logging.info('Finished processing %s %s', current_work_item,
                 'successfully' if exception_details is None
                 else 'with exception')

    progress_reporter.report_status(
        completed=True,
        source_operation_response=source_operation_response,
        exception_details=exception_details)

  @staticmethod
  def log_memory_usage_if_needed(worker_id, force=False):
    """Periodically logs memory usage of the current worker.

    Args:
      worker_id: Id of the worker.
      force: if True forces logging.
    """
    if (force or BatchWorker.last_memory_usage_report_time is None or
        int(time.time()) - BatchWorker.last_memory_usage_report_time >
        BatchWorker.MEMORY_USAGE_REPORTING_INTERVAL_SECS):
      logging.info('Memory usage of worker %s is %d MB', worker_id,
                   resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1000)
      BatchWorker.last_memory_usage_report_time = int(time.time())

  def shutdown(self):
    self._shutdown = True

  def get_executor_for_work_item(self, work_item):
    if work_item.map_task is not None:
      return executor.MapTaskExecutor(work_item.map_task)
    elif work_item.source_operation_split_task is not None:
      return executor.CustomSourceSplitExecutor(
          work_item.source_operation_split_task)
    else:
      raise ValueError('Unknown type of work item : %s', work_item)

  def do_work(self, work_item, deferred_exception_details=None):
    """Executes worker operations and adds any failures to the report status."""
    logging.info('Executing %s', work_item)
    BatchWorker.log_memory_usage_if_needed(self.worker_id, force=True)

    work_executor = self.get_executor_for_work_item(work_item)
    progress_reporter = ProgressReporter(
        work_item, work_executor, self, self.client)

    if deferred_exception_details:
      # Report (fatal) deferred exceptions that happened earlier. This
      # workflow will fail with the deferred exception.
      with work_item.lock:
        self.report_completion_status(
            work_item,
            progress_reporter,
            exception_details=deferred_exception_details)
        work_item.done = True
        logging.error('Not processing WorkItem %s since a deferred exception '
                      'was found: %s', work_item, deferred_exception_details)
        return

    exception_details = None
    try:
      progress_reporter.start_reporting_progress()
      work_executor.execute()
    except Exception:  # pylint: disable=broad-except
      exception_details = traceback.format_exc()
      logging.error('An exception was raised when trying to execute the '
                    'work item %s : %s',
                    work_item,
                    exception_details, exc_info=True)
    finally:
      try:
        progress_reporter.stop_reporting_progress()
      except Exception:  # pylint: disable=broad-except
        logging.error('An exception was raised when trying to stop the '
                      'progress reporter : %s',
                      traceback.format_exc(), exc_info=True)
        # If 'exception_details' was already set, we were already going to
        # mark this work item as failed. Hence only logging this error and
        # reporting the original error.
        if exception_details is None:
          # This will be reported to the service and work item will be marked as
          # failed.
          exception_details = traceback.format_exc()

      with work_item.lock:
        source_split_response = None
        if isinstance(work_executor, executor.CustomSourceSplitExecutor):
          source_split_response = work_executor.response

        self.report_completion_status(
            work_item, progress_reporter,
            source_operation_response=source_split_response,
            exception_details=exception_details)
        work_item.done = True

  def status_server(self):
    """Executes the serving loop for the status server."""

    class StatusHttpHandler(BaseHTTPServer.BaseHTTPRequestHandler):
      """HTTP handler for serving stacktraces of all worker threads."""

      def do_GET(self):  # pylint: disable=invalid-name
        """Return /threadz information for any GET request."""
        self.send_response(200)
        self.send_header('Content-Type', 'text/plain')
        self.end_headers()
        frames = sys._current_frames()  # pylint: disable=protected-access
        for t in threading.enumerate():
          self.wfile.write('--- Thread #%s name: %s ---\n' % (t.ident, t.name))
          self.wfile.write(''.join(traceback.format_stack(frames[t.ident])))

      def log_message(self, f, *args):
        """Do not log any messages."""
        pass

    httpd = BaseHTTPServer.HTTPServer(
        ('localhost', self.STATUS_HTTP_PORT), StatusHttpHandler)
    logging.info('Status HTTP server running at %s:%s', httpd.server_name,
                 httpd.server_port)
    httpd.serve_forever()

  def run(self):
    """Runs the worker loop for leasing and executing work items."""
    if self.running_in_gce:
      auth.set_running_in_gce(worker_executing_project=self.project_id)

    # Deferred exceptions are used as a way to report unrecoverable errors that
    # happen before they could be reported to the service. If it is not None,
    # worker will use the first work item to report deferred exceptions and
    # fail eventually.
    # TODO(silviuc): Add the deferred exception mechanism to streaming worker
    deferred_exception_details = None

    if self.environment_info_path is not None:
      try:
        environment.check_sdk_compatibility(self.environment_info_path)
      except Exception:  # pylint: disable=broad-except
        deferred_exception_details = traceback.format_exc()
        logging.error('SDK compatibility check failed: %s',
                      deferred_exception_details, exc_info=True)

    if deferred_exception_details is None:
      logging.info('Loading main session from the staging area...')
      try:
        self._load_main_session(self.local_staging_directory)
      except Exception:  # pylint: disable=broad-except
        deferred_exception_details = traceback.format_exc()
        logging.error('Could not load main session: %s',
                      deferred_exception_details, exc_info=True)

    # Start status HTTP server thread.
    thread = threading.Thread(target=self.status_server)
    thread.daemon = True
    thread.start()

    # The batch execution context is currently a placeholder, so we don't yet
    # need to have it change between work items.
    execution_context = maptask.BatchExecutionContext()
    work_item = None
    # Loop forever leasing work items, executing them, and reporting status.
    while not self._shutdown:
      try:
        # Lease a work item. The lease_work call will retry for server errors
        # (e.g., 500s) however it will not retry for a 404 (no item to lease).
        # In such cases we introduce random sleep delays with the code below.
        should_sleep = False
        try:
          work = self.client.lease_work(self.worker_info_for_client(),
                                        self.default_desired_lease_duration())
          work_item = workitem.get_work_items(work, self.environment,
                                              execution_context)
          if work_item is None:
            should_sleep = True
        except HttpError as exn:
          # Not found errors (404) are benign. The rest are not and must be
          # re-raised.
          if exn.status_code != 404:
            raise
          should_sleep = True
        if should_sleep:
          logging.debug('No work items. Sleeping a bit ...')
          # The sleeping is done with a bit of jitter to avoid having workers
          # requesting leases in lock step.
          time.sleep(1.0 * (1 - 0.5 * random.random()))
          continue

        stage_name = None
        if work_item.map_task:
          stage_name = work_item.map_task.stage_name

        with logger.PerThreadLoggingContext(
            work_item_id=work_item.proto.id,
            stage_name=stage_name):
          # TODO(silviuc): Add more detailed timing and profiling support.
          start_time = time.time()

          # Do the work. The do_work() call will mark the work completed or
          # failed.  The progress reporting_thread will take care of sending
          # updates and updating in the workitem object the reporting indexes
          # and duration for the lease.
          if self.work_item_profiling:
            with profiler.Profile(
                profile_id=work_item.proto.id,
                profile_location=self.profile_location, log_results=True):
              self.do_work(
                  work_item,
                  deferred_exception_details=deferred_exception_details)
          else:
            self.do_work(work_item,
                         deferred_exception_details=deferred_exception_details)

          logging.info('Completed work item: %s in %.9f seconds',
                       work_item.proto.id, time.time() - start_time)

      except Exception:  # pylint: disable=broad-except
        # This is an exception raised outside of executing a work item most
        # likely while leasing a work item. We log an error and march on.
        logging.error('Exception in worker loop: %s',
                      traceback.format_exc(),
                      exc_info=True)
        # sleeping a bit after Exception to prevent a busy loop.
        time.sleep(1)
