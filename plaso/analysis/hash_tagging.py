# -*- coding: utf-8 -*-
"""This file contains the interface for analysis plugins."""

import abc
import collections
import queue
import threading
import time

import requests

from plaso.analysis import interface
from plaso.analysis import logger
from plaso.containers import events
from plaso.lib import errors


class HashTaggingAnalysisPlugin(interface.AnalysisPlugin):
  """An interface for plugins that tag events based on the source file hash.

  An implementation of this class should be paired with an implementation of
  the HashAnalyzer interface.

  Attributes:
    hash_analysis_queue (queue.Queue): queue that contains the results of
        analysis of file hashes.
    hash_queue (queue.Queue): queue that contains file hashes.
  """
  # The event data types the plugin will collect hashes from. Subclasses
  # must override this attribute.
  DATA_TYPES = []

  # The default number of seconds for the plugin to wait for analysis results
  # to be added to the hash_analysis_queue by the analyzer thread.
  DEFAULT_QUEUE_TIMEOUT = 4
  SECONDS_BETWEEN_STATUS_LOG_MESSAGES = 30

  def __init__(self, analyzer_class):
    """Initializes a hash tagging analysis plugin.

    Args:
      analyzer_class (type): a subclass of HashAnalyzer that will be
          instantiated by the plugin.
    """
    super(HashTaggingAnalysisPlugin, self).__init__()
    self._analysis_queue_timeout = self.DEFAULT_QUEUE_TIMEOUT
    self._analyzer_started = False
    self._event_identifiers_by_path_spec = collections.defaultdict(list)
    self._hash_path_specs = collections.defaultdict(list)
    self._requester_class = None
    self._time_of_last_status_log = time.time()

    self.hash_analysis_queue = queue.Queue()
    self.hash_queue = queue.Queue()

    self._analyzer = analyzer_class(self.hash_queue, self.hash_analysis_queue)

  def _HandleHashAnalysis(self, hash_analysis):
    """Deals with the results of the analysis of a hash.

    This method ensures that labels are generated for the hash,
    then tags all events derived from files with that hash.

    Args:
      hash_analysis (HashAnalysis): hash analysis plugin's results for a given
          hash.

    Returns:
      tuple: containing:

        list[dfvfs.PathSpec]: path specifications that had the hash value
            looked up.
        list[str]: labels that corresponds to the hash value that was looked up.
        list[EventTag]: event tags for all events that were extracted from the
            path specifications.
    """
    tags = []
    labels = self.GenerateLabels(hash_analysis.hash_information)
    path_specifications = self._hash_path_specs.pop(hash_analysis.subject_hash)
    for path_specification in path_specifications:
      event_identifiers = self._event_identifiers_by_path_spec.pop(
          path_specification, [])

      if not labels:
        continue

      for event_identifier in event_identifiers:
        try:
          event_tag = events.EventTag()
          event_tag.SetEventIdentifier(event_identifier)
          event_tag.AddLabels(labels)
          tags.append(event_tag)
        except (TypeError, ValueError) as exception:
          logger.error('unable to add labels to event with error: {0!s}'.format(
              exception))

    return path_specifications, labels, tags

  def _EnsureRequesterStarted(self):
    """Checks if the analyzer is running and starts it if not."""
    if not self._analyzer_started:
      self._analyzer.start()
      self._analyzer_started = True

  def ExamineEvent(self, mediator, event, event_data, event_data_stream):
    """Evaluates whether an event contains the right data for a hash lookup.

    Args:
      mediator (AnalysisMediator): mediates interactions between
          analysis plugins and other components, such as storage and dfvfs.
      event (EventObject): event.
      event_data (EventData): event data.
      event_data_stream (EventDataStream): event data stream.
    """
    if (event_data.data_type not in self.DATA_TYPES or
        not self._analyzer.lookup_hash):
      return

    self._EnsureRequesterStarted()

    path_specification = getattr(event_data_stream, 'path_spec', None)
    if not path_specification:
      # Note that support for event_data.pathspec is kept for backwards
      # compatibility.
      path_specification = getattr(event_data, 'pathspec', None)

    # Not all events have a path specification, such as "fs:stat".
    if not path_specification:
      return

    event_identifiers = self._event_identifiers_by_path_spec[path_specification]

    event_identifier = event.GetIdentifier()
    event_identifiers.append(event_identifier)

    hash_attributes_container = event_data_stream
    if not hash_attributes_container:
      hash_attributes_container = event_data

    lookup_hash = '{0:s}_hash'.format(self._analyzer.lookup_hash)
    lookup_hash = getattr(hash_attributes_container, lookup_hash, None)
    if not lookup_hash:
      display_name = mediator.GetDisplayNameForPathSpec(path_specification)
      logger.warning((
          'Lookup hash attribute: {0:s}_hash missing from event that '
          'originated from: {1:s}.').format(
              self._analyzer.lookup_hash, display_name))
      return

    path_specs = self._hash_path_specs[lookup_hash]
    path_specs.append(path_specification)
    # There may be multiple path specifications that have the same hash. We only
    # want to look them up once.
    if len(path_specs) == 1:
      self.hash_queue.put(lookup_hash)

  def _ContinueReportCompilation(self):
    """Determines if the plugin should continue trying to compile the report.

    Returns:
      bool: True if the plugin should continue, False otherwise.
    """
    analyzer_alive = self._analyzer.is_alive()
    hash_queue_has_tasks = self.hash_queue.unfinished_tasks > 0
    analysis_queue = not self.hash_analysis_queue.empty()

    # pylint: disable=consider-using-ternary
    return (analyzer_alive and hash_queue_has_tasks) or analysis_queue

  # TODO: Refactor to do this more elegantly, perhaps via callback.
  def _LogProgressUpdateIfReasonable(self):
    """Prints a progress update if enough time has passed."""
    next_log_time = (
        self._time_of_last_status_log +
        self.SECONDS_BETWEEN_STATUS_LOG_MESSAGES)
    current_time = time.time()
    if current_time < next_log_time:
      return
    completion_time = time.ctime(current_time + self.EstimateTimeRemaining())
    log_message = (
        '{0:s} hash analysis plugin running. {1:d} hashes in queue, '
        'estimated completion time {2:s}.'.format(
            self.NAME, self.hash_queue.qsize(), completion_time))
    logger.info(log_message)
    self._time_of_last_status_log = current_time

  def CompileReport(self, mediator):
    """Compiles an analysis report.

    Args:
      mediator (AnalysisMediator): mediates interactions between
          analysis plugins and other components, such as storage and dfvfs.

    Returns:
      AnalysisReport: report.
    """
    # TODO: refactor to update the counter on demand instead of
    # during reporting.
    path_specs_per_labels_counter = collections.Counter()
    tags = []
    while self._ContinueReportCompilation():
      try:
        self._LogProgressUpdateIfReasonable()
        hash_analysis = self.hash_analysis_queue.get(
            timeout=self._analysis_queue_timeout)
      except queue.Empty:
        # The result queue is empty, but there could still be items that need
        # to be processed by the analyzer.
        continue
      path_specs, labels, new_tags = self._HandleHashAnalysis(hash_analysis)

      tags.extend(new_tags)
      for label in labels:
        path_specs_per_labels_counter[label] += len(path_specs)

    self._analyzer.SignalAbort()

    lines_of_text = ['{0:s} hash tagging results'.format(self.NAME)]
    for label, count in sorted(path_specs_per_labels_counter.items()):
      line_of_text = (
          '{0:d} path specifications tagged with label: {1:s}'.format(
              count, label))
      lines_of_text.append(line_of_text)
    lines_of_text.append('')
    report_text = '\n'.join(lines_of_text)

    for event_tag in tags:
      mediator.ProduceEventTag(event_tag)

    analysis_report = super(HashTaggingAnalysisPlugin, self).CompileReport(
        mediator)
    analysis_report.text = report_text
    return analysis_report

  def EstimateTimeRemaining(self):
    """Estimates how long until all hashes have been analyzed.

    Returns:
      int: estimated number of seconds until all hashes have been analyzed.
    """
    number_of_hashes = self.hash_queue.qsize()
    hashes_per_batch = self._analyzer.hashes_per_batch
    wait_time_per_batch = self._analyzer.wait_after_analysis
    analyses_performed = self._analyzer.analyses_performed

    if analyses_performed == 0:
      average_analysis_time = self._analyzer.seconds_spent_analyzing
    else:
      average_analysis_time, _ = divmod(
          self._analyzer.seconds_spent_analyzing, analyses_performed)

    batches_remaining, _ = divmod(number_of_hashes, hashes_per_batch)
    estimated_seconds_per_batch = average_analysis_time + wait_time_per_batch
    return batches_remaining * estimated_seconds_per_batch

  # pylint: disable=redundant-returns-doc
  @abc.abstractmethod
  def GenerateLabels(self, hash_information):
    """Generates a list of strings to tag events with.

    Args:
      hash_information (object): object that mediates the result of the
          analysis of a hash, as returned by the Analyze() method of the
          analyzer class associated with this plugin.

    Returns:
      list[str]: list of labels to apply to events.
    """

  def SetLookupHash(self, lookup_hash):
    """Sets the hash to query.

    Args:
      lookup_hash (str): name of the hash attribute to look up.
    """
    self._analyzer.SetLookupHash(lookup_hash)


class HashAnalyzer(threading.Thread):
  """Class that defines the interfaces for hash analyzer threads.

  This interface should be implemented once for each hash analysis plugin.

  Attributes:
    analyses_performed (int): number of analysis batches completed by this
        analyzer.
    hashes_per_batch (int): maximum number of hashes to analyze at once.
    lookup_hash (str): name of the hash attribute to look up.
    seconds_spent_analyzing (int): number of seconds this analyzer has spent
        performing analysis (as opposed to waiting on queues, etc.)
    wait_after_analysis (int): number of seconds the analyzer will sleep for
        after analyzing a batch of hashes.
  """
  # How long to wait for new items to be added to the the input queue.
  EMPTY_QUEUE_WAIT_TIME = 4

  # List of lookup hashes supported by the analyzer.
  SUPPORTED_HASHES = []

  def __init__(
      self, hash_queue, hash_analysis_queue, hashes_per_batch=1,
      lookup_hash='sha256', wait_after_analysis=0):
    """Initializes a hash analyzer.

    Args:
      hash_queue (queue.Queue): contains hashes to be analyzed.
      hash_analysis_queue (queue.Queue): queue that the analyzer will append
          HashAnalysis objects to.
      hashes_per_batch (Optional[int]): number of hashes to analyze at once.
      lookup_hash (Optional[str]): name of the hash attribute to look up.
      wait_after_analysis (Optional[int]): number of seconds to wait after each
          batch is analyzed.
    """
    super(HashAnalyzer, self).__init__()
    self._abort = False
    self._hash_queue = hash_queue
    self._hash_analysis_queue = hash_analysis_queue
    self.analyses_performed = 0
    self.hashes_per_batch = hashes_per_batch
    self.lookup_hash = lookup_hash
    self.seconds_spent_analyzing = 0
    self.wait_after_analysis = wait_after_analysis

  def _GetHashes(self, target_queue, max_hashes):
    """Retrieves a list of items from a queue.

    Args:
      target_queue (queue.Queue): queue to retrieve hashes from.
      max_hashes (int): maximum number of items to retrieve from the
          target_queue.

    Returns:
      list[object]: list of at most max_hashes elements from the target_queue.
          The list may have no elements if the target_queue is empty.
    """
    hashes = []
    for _ in range(0, max_hashes):
      try:
        item = target_queue.get_nowait()
      except queue.Empty:
        continue
      hashes.append(item)
    return hashes

  # pylint: disable=redundant-returns-doc
  @abc.abstractmethod
  def Analyze(self, hashes):
    """Analyzes a list of hashes.

    Args:
      hashes (list[str]): list of hashes to look up.

    Returns:
      list[HashAnalysis]: list of results of analyzing the hashes.
    """

  # This method is part of the threading.Thread interface, hence its name does
  # not follow the style guide.
  def run(self):
    """The method called by the threading library to start the thread."""
    while not self._abort:
      hashes = self._GetHashes(self._hash_queue, self.hashes_per_batch)
      if hashes:
        time_before_analysis = time.time()
        hash_analyses = self.Analyze(hashes)
        current_time = time.time()
        self.seconds_spent_analyzing += current_time - time_before_analysis
        self.analyses_performed += 1
        for hash_analysis in hash_analyses:
          self._hash_analysis_queue.put(hash_analysis)
          self._hash_queue.task_done()
        time.sleep(self.wait_after_analysis)
      else:
        # Wait for some more hashes to be added to the queue.
        time.sleep(self.EMPTY_QUEUE_WAIT_TIME)

  def SetLookupHash(self, lookup_hash):
    """Sets the hash to query.

    Args:
      lookup_hash (str): name of the hash attribute to look up.

    Raises:
      ValueError: if the lookup hash is not supported.
    """
    if lookup_hash not in self.SUPPORTED_HASHES:
      raise ValueError('Unsupported lookup hash: {0!s}'.format(lookup_hash))

    self.lookup_hash = lookup_hash

  def SignalAbort(self):
    """Instructs this analyzer to stop running."""
    self._abort = True


class HTTPHashAnalyzer(HashAnalyzer):
  """Interface for hash analysis plugins that use HTTP(S)"""

  @abc.abstractmethod
  def Analyze(self, hashes):
    """Analyzes a list of hashes.

    Args:
      hashes (list[str]): hashes to look up.

    Returns:
      list[HashAnalysis]: analysis results.
    """

  def MakeRequestAndDecodeJSON(self, url, method, **kwargs):
    """Make a HTTP request and decode the results as JSON.

    Args:
      url (str): URL to make a request to.
      method (str): HTTP method to used to make the request. GET and POST are
          supported.
      kwargs: parameters to the requests .get() or post() methods, depending
          on the value of the method parameter.

    Returns:
      dict[str, object]: body of the HTTP response, decoded from JSON.

    Raises:
      ConnectionError: If it is not possible to connect to the given URL, or it
          the request returns a HTTP error.
      ValueError: If an invalid HTTP method is specified.
    """
    method_upper = method.upper()
    if method_upper not in ('GET', 'POST'):
      raise ValueError('Method {0:s} is not supported')

    try:
      if method_upper == 'GET':
        response = requests.get(url, **kwargs)

      elif method_upper == 'POST':
        response = requests.post(url, **kwargs)

      response.raise_for_status()

    except requests.ConnectionError as exception:
      error_string = 'Unable to connect to {0:s} with error: {1!s}'.format(
          url, exception)
      raise errors.ConnectionError(error_string)

    except requests.HTTPError as exception:
      error_string = '{0:s} returned a HTTP error: {1!s}'.format(
          url, exception)
      raise errors.ConnectionError(error_string)

    return response.json()


class HashAnalysis(object):
  """Analysis information about a hash.

  Attributes:
    hash_information (object): object containing information about the hash.
    subject_hash (str): hash that was analyzed.
  """

  def __init__(self, subject_hash, hash_information):
    """Initializes analysis information about a hash.

    Args:
      subject_hash (str): hash that the hash_information relates to.
      hash_information (object): information about the hash. This object will be
          used by the GenerateLabels method in the HashTaggingAnalysisPlugin
          to tag events that relate to the hash.
    """
    self.hash_information = hash_information
    self.subject_hash = subject_hash
