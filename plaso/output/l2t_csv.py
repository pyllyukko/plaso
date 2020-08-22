# -*- coding: utf-8 -*-
"""Output module for the log2timeline (L2T) CSV format.

For documentation on the L2T CSV format see:
https://forensicswiki.xyz/wiki/index.php?title=L2T_CSV
"""

from __future__ import unicode_literals

from plaso.formatters import manager as formatters_manager
from plaso.lib import errors
from plaso.lib import timelib
from plaso.output import formatting_helper
from plaso.output import interface
from plaso.output import logger
from plaso.output import manager
from plaso.output import shared_dsv


class L2TCSVEventFormattingHelper(shared_dsv.DSVEventFormattingHelper):
  """L2T CSV output module event formatting helper."""

  def GetFormattedEventMACBGroup(self, event_macb_group):
    """Retrieves a string representation of the event.

    Args:
      event_macb_group (list[tuple[EventObject, EventData, EventDataStream,
          EventTag]]): group of events with identical timestamps, attributes
          and values.

    Returns:
      str: string representation of the event MACB group.
    """
    timestamp_descriptions = [
        event.timestamp_desc for event, _, _, _ in event_macb_group]

    field_values = []
    for field_name in self._field_names:
      if field_name == 'MACB':
        field_value = (
            self._output_mediator.GetMACBRepresentationFromDescriptions(
                timestamp_descriptions))
      elif field_name == 'type':
        # TODO: fix timestamp description in source.
        field_value = '; '.join(timestamp_descriptions)
      else:
        event, event_data, event_data_stream, event_tag = event_macb_group[0]
        field_value = self._field_formatting_helper.GetFormattedField(
            field_name, event, event_data, event_data_stream, event_tag)

      field_value = self._SanitizeField(field_value)
      field_values.append(field_value)

    return self._field_delimiter.join(field_values)


class L2TCSVFieldFormattingHelper(formatting_helper.FieldFormattingHelper):
  """L2T CSV output module field formatting helper."""

  # Maps the name of a fields to a a callback function that formats
  # the field value.
  _FIELD_FORMAT_CALLBACKS = {
      'date': '_FormatDate',
      'desc': '_FormatMessage',
      'extra': '_FormatExtraAttributes',
      'filename': '_FormatDisplayName',
      'format': '_FormatParser',
      'host': '_FormatHostname',
      'inode': '_FormatInode',
      'MACB': '_FormatMACB',
      'notes': '_FormatTag',
      'short': '_FormatMessageShort',
      'source': '_FormatSourceShort',
      'sourcetype': '_FormatSource',
      'time': '_FormatTime',
      'timezone': '_FormatTimeZone',
      'type': '_FormatType',
      'user': '_FormatUsername',
      'version': '_FormatVersion',
  }

  # The field format callback methods require specific arguments hence
  # the check for unused arguments is disabled here.
  # pylint: disable=unused-argument

  def _FormatDate(self, event, event_data, event_data_stream):
    """Formats a date field.

    Args:
      event (EventObject): event.
      event_data (EventData): event data.
      event_data_stream (EventDataStream): event data stream.

    Returns:
      str: date field.
    """
    try:
      iso_date_time = timelib.Timestamp.CopyToIsoFormat(
          event.timestamp, timezone=self._output_mediator.timezone,
          raise_error=True)

      return '{0:s}/{1:s}/{2:s}'.format(
          iso_date_time[5:7], iso_date_time[8:10], iso_date_time[:4])

    except (OverflowError, ValueError):
      self._ReportEventError(event, event_data, (
          'unable to copy timestamp: {0!s} to a human readable date. '
          'Defaulting to: "00/00/0000"').format(event.timestamp))

      return '00/00/0000'

  def _FormatExtraAttributes(self, event, event_data, event_data_stream):
    """Formats an extra attributes field.

    Args:
      event (EventObject): event.
      event_data (EventData): event data.
      event_data_stream (EventDataStream): event data stream.

    Returns:
      str: extra attributes field.

    Raises:
      NoFormatterFound: if no event formatter can be found to match the data
          type in the event data.
    """
    # TODO: reverse logic and get formatted attributes instead.
    unformatted_attributes = (
        formatters_manager.FormattersManager.GetUnformattedAttributes(
            event_data))

    if unformatted_attributes is None:
      raise errors.NoFormatterFound(
          'Unable to find event formatter for: {0:s}.'.format(
              event_data.data_type))

    extra_attributes = []
    for attribute_name, attribute_value in event_data.GetAttributes():
      if attribute_name in unformatted_attributes:
        # Some parsers have written bytes values to storage.
        if isinstance(attribute_value, bytes):
          attribute_value = attribute_value.decode('utf-8', 'replace')
          logger.warning(
              'Found bytes value for attribute "{0:s}" for data type: '
              '{1!s}. Value was converted to UTF-8: "{2:s}"'.format(
                  attribute_name, event_data.data_type, attribute_value))

        # With ! in {1!s} we force a string conversion since some of
        # the extra attributes values can be integer, float point or
        # boolean values.
        extra_attributes.append('{0:s}: {1!s}'.format(
            attribute_name, attribute_value))

    if event_data_stream:
      for attribute_name, attribute_value in event_data_stream.GetAttributes():
        if attribute_name != 'path_spec':
          extra_attributes.append('{0:s}: {1!s}'.format(
              attribute_name, attribute_value))

    extra_attributes = '; '.join(sorted(extra_attributes))

    return extra_attributes.replace('\n', '-').replace('\r', '')

  def _FormatParser(self, event, event_data, event_data_stream):
    """Formats a parser field.

    Args:
      event (EventObject): event.
      event_data (EventData): event data.
      event_data_stream (EventDataStream): event data stream.

    Returns:
      str: parser field.
    """
    return getattr(event_data, 'parser', '-')

  def _FormatType(self, event, event_data, event_data_stream):
    """Formats a type field.

    Args:
      event (EventObject): event.
      event_data (EventData): event data.
      event_data_stream (EventDataStream): event data stream.

    Returns:
      str: type field.
    """
    return getattr(event, 'timestamp_desc', '-')

  def _FormatVersion(self, event, event_data, event_data_stream):
    """Formats a version field.

    Args:
      event (EventObject): event.
      event_data (EventData): event data.
      event_data_stream (EventDataStream): event data stream.

    Returns:
      str: version field.
    """
    return '2'

  # pylint: enable=unused-argument


class L2TCSVOutputModule(interface.LinearOutputModule):
  """CSV format used by log2timeline, with 17 fixed fields."""

  NAME = 'l2tcsv'
  DESCRIPTION = 'CSV format used by legacy log2timeline, with 17 fixed fields.'

  _FIELD_NAMES = [
      'date', 'time', 'timezone', 'MACB', 'source', 'sourcetype', 'type',
      'user', 'host', 'short', 'desc', 'version', 'filename', 'inode', 'notes',
      'format', 'extra']

  def __init__(self, output_mediator):
    """Initializes a L2T CSV output module object.

    Args:
      output_mediator (OutputMediator): an output mediator.
    """
    field_formatting_helper = L2TCSVFieldFormattingHelper(output_mediator)
    event_formatting_helper = L2TCSVEventFormattingHelper(
        output_mediator, field_formatting_helper, self._FIELD_NAMES)
    super(L2TCSVOutputModule, self).__init__(
        output_mediator, event_formatting_helper)

  def WriteEventMACBGroup(self, event_macb_group):
    """Writes an event MACB group to the output.

    Args:
      event_macb_group (list[tuple[EventObject, EventData, EventDataStream,
          EventTag]]): group of events with identical timestamps, attributes
          and values.
    """
    output_text = self._event_formatting_helper.GetFormattedEventMACBGroup(
        event_macb_group)

    output_text = '{0:s}\n'.format(output_text)
    self._output_writer.Write(output_text)

  def WriteHeader(self):
    """Writes the header to the output."""
    output_text = self._event_formatting_helper.GetFormattedFieldNames()
    output_text = '{0:s}\n'.format(output_text)
    self._output_writer.Write(output_text)


manager.OutputManager.RegisterOutput(L2TCSVOutputModule)
