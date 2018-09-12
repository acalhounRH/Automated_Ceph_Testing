#!/usr/bin/env python3
# -*- mode: python -*-

# prereqs:
# config file: specified in ES_CONFIG_PATH
# mapping file:

from __future__ import print_function

import sys, os, time, re, stat, copy
import hashlib, json, glob, csv, tarfile, logging

from urllib3 import Timeout
from datetime import datetime, timedelta
from collections import Counter
from optparse import OptionParser, make_option
try:
    from configparser import SafeConfigParser, Error as ConfigParserError, NoSectionError, NoOptionError
except ImportError:
    from ConfigParser import SafeConfigParser, Error as ConfigParserError, NoSectionError, NoOptionError
try:
    from elasticsearch1 import VERSION as es_VERSION, Elasticsearch
except ImportError:
    from elasticsearch import VERSION as es_VERSION, Elasticsearch

from pbench import tstos, es_index, es_put_template


_VERSION_ = "0.2.0.0"
_NAME_    = "index-pbench"
_DEBUG    = 0

# global - defaults to normal dict, ordered dict for unittests
_dict_const = dict

# 100,000 minute timeout talking to Elasticsearch; basically we just don't
# want to timeout waiting for Elasticsearch and then have to retry, as that
# can add undue burden to the Elasticsearch cluster.
_read_timeout = 100000*60

# All indexing uses "create" (instead of "index") to avoid updating
# existing records, allowing us to detect duplicates.
_op_type = "create"


class MappingFileError(Exception):
    pass


class BadDate(Exception):
    pass


class ConfigFileNotSpecified(Exception):
    pass


class ConfigFileError(Exception):
    pass


class BadMDLogFormat(Exception):
    pass


class TemplateError(Exception):
    pass


class UnsupportedTarballFormat(Exception):
    pass


class SosreportHostname(Exception):
    pass


_ts_start = None
def ts(msg, newline=False):
    """Debugging helper for emitting a timestamp aiding timing.
    """
    global _ts_start

    now = datetime.now()
    if _ts_start:
        print(now - _ts_start, file=sys.stderr)
    _ts_start = now
    if newline:
        print(msg, file=sys.stderr)
        _ts_start = None
    else:
        print(msg, end=' ', file=sys.stderr)
    sys.stderr.flush()

def load_json_mapping(mapping_fn):
    with open(mapping_fn, "r") as mappingfp:
        try:
            mapping = json.load(mappingfp)
        except ValueError as err:
            raise MappingFileError("%s: %s" % (mapping_fn, err))
    return mapping

def fetch_mapping(mapping_fn):
    """Fetch the mapping JSON data from the given file.

    Returns a tuple consisting of the mapping name pulled from the file, and
    the python dictionary loaded from the JSON file.

    Raises MappingFileError if it encounters any problem loading the file.
    """
    mapping = load_json_mapping(mapping_fn)
    keys = list(mapping.keys())
    if len(keys) != 1:
        raise MappingFileError("Invalid mapping file: %s" % mapping_fn)
    return keys[0], mapping

def es_template(es, options, INDEX_PREFIX, INDEX_VERSION, config, dbg=0):
    """Load the various Elasticsearch index templates required by pbench.

    We first load the pbench-run index templates, and then we construct and
    load the templates for each tool data index.
    """
    index_settings = _dict_const(
        gateway=_dict_const(
            local=_dict_const(
                sync='1m')),
        merge=_dict_const(
            scheduler=_dict_const(
                max_thread_count=1)),
        translog=_dict_const(
            flush_threshold_size='1g'),
    )

    try:
        NUMBER_OF_SHARDS = config.get('Settings', 'number_of_shards')
    except Exception:
        pass
    else:
        index_settings['number_of_shards'] = NUMBER_OF_SHARDS

    try:
        NUMBER_OF_REPLICAS = config.get('Settings', 'number_of_replicas')
    except Exception:
        pass
    else:
        index_settings['number_of_replicas'] = NUMBER_OF_REPLICAS

    # where to find the mappings
    MAPPING_DIR = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(sys.argv[0]))),
        'lib', 'mappings')

    run_mappings = _dict_const()
    for mapping_fn in glob.iglob(os.path.join(MAPPING_DIR, "run*.json")):
        key, mapping = fetch_mapping(mapping_fn)
        run_mappings[key] = mapping

    # The API body for the create() contains a dictionary with the settings and
    # the mappings.
    run_template_name = '%s.run' % (INDEX_PREFIX,)
    run_template_body = _dict_const(
        template='%s.run.*' % (INDEX_PREFIX,),
        settings=index_settings,
        mappings=run_mappings)

    # Now for the tool data mappings. First we fetch the base skeleton they
    # all share.
    skel = load_json_mapping(os.path.join(MAPPING_DIR, "tool-data-skel.json"))

    # Next we load all the tool fragments
    fpat = re.compile(r'tool-data-frag-(?P<toolname>.+)\.json')
    tool_mapping_frags = _dict_const()
    for mapping_fn in glob.iglob(os.path.join(MAPPING_DIR, "tool-data-frag-*.json")):
        m = fpat.match(os.path.basename(mapping_fn))
        toolname = m.group('toolname')
        tool_mapping_frags[toolname] = load_json_mapping(mapping_fn)

    tool_templates = []
    for toolname,frag in tool_mapping_frags.items():
        tool_skel = copy.deepcopy(skel)
        tool_skel['properties'][toolname] = frag
        tool_mapping = _dict_const([("pbench-tool-data-%s" % toolname, tool_skel)])
        tool_template_body = _dict_const(
            template='%s.tool-data-%s.*' % (INDEX_PREFIX, toolname),
            settings=index_settings,
            mappings=tool_mapping)
        tool_templates.append((toolname, tool_template_body))

    # Next we load all the results
    results_mappings = _dict_const()
    for mapping_fn in glob.iglob(os.path.join(MAPPING_DIR, "result-*.json")):
        key, mapping = fetch_mapping(mapping_fn)
        if dbg > 0:
            print("fetch_mapping: {}".format(key))
            from pprint import pprint;
            pprint(mapping)
        results_mappings[key] = mapping

    results_template_name = '%s.result-data' % (INDEX_PREFIX,)
    results_template_body = _dict_const(
        template='%s.result-data.*' % (INDEX_PREFIX,),
        settings=index_settings,
        mappings=results_mappings)

    # FIXME: We should query to see if a template exists, and only
    # create it if it does not, or if the version of it is older than
    # expected.
    try:
        # Pbench Run template
        beg, end, retries = es_put_template(es, name=run_template_name, body=run_template_body)
        successes = 1
        # Pbench Tool Data templates
        for name, body in sorted(tool_templates):
            tool_template_name = '%s.tool-data-%s' % (INDEX_PREFIX,name)
            _, end, retry_count = es_put_template(es, name=tool_template_name, body=body)
            retries += retry_count
            successes += 1
    except Exception as e:
        raise TemplateError(e)
    else:
        print("\tdone templates (end ts: %s, duration: %.2fs,"
                " successes: %d, retries: %d)" % (
            tstos(end), end - beg, successes, retries))
        sys.stdout.flush()


###########################################################################
# Benchmark result data: json files only
#

# convert a 0 value in a timeseries array to a float, else ES barfs.
def convert_to_float(d):
    if type(d) == type({}) and len(d) != 0:
        for k1 in d:
            convert_to_float(d[k1])
    elif type(d) == type([]) and len(d) != 0:
        # for {date, value} timeseries, convert the value but leave the date alone
        if type(d[0]) == type({}) and set(d[0].keys()).issuperset(set(['date', 'value'])):
            for i in range(0, len(d)):
                d[i]['value'] = float(d[i]['value'])
        else:
            for i in range(0, len(d)):
                convert_to_float(d[i])
    else:
        pass


class ResultData(object):
    def __init__(self, ptb, experiment, opctx):
        self.run_metadata = _dict_const([
            ("runtstamp", ptb.start_run),
            ("runid", ptb.md5sum),
            ("experiment", experiment)
        ])
        self.ptb = ptb
        self.counters = Counter()
        opctx.append(_dict_const(object="ResultData", counters=self.counters))
        self.json_files = None
        try:
            self.json_files = ResultData.get_json_files(ptb)
        except KeyError:
            self.json_files = None

    def _make_source_json(self):
        for df in self.json_files:
            try:
                # Read it once to generate the source_id
                with open(os.path.join(self.ptb.extracted_root, df['path']), "rb") as fp:
                    hash_md5 = hashlib.md5()
                    for chunk in iter(lambda: fp.read(4096), b""):
                        hash_md5.update(chunk)
                    source_id = hash_md5.hexdigest()
            except Exception as e:
                print("result-data-indexing: encountered unreadable JSON file,"
                        " %s: %r" % (df['path'], e))
                self.counters['unreadable_json_file'] += 1
                continue

            try:
                # Read it a second time to interpret it as a JSON document.
                with open(os.path.join(self.ptb.extracted_root, df['path'])) as fp:
                    results = json.load(fp)
            except Exception as e:
                print("result-data-indexing: encountered invalid JSON file,"
                        " %s: %r" % (df['path'], e))
                self.counters['not_valid_json_file'] += 1
                continue

            convert_to_float(results)

            source = _dict_const()
            source['results'] = results
            source['@metadata'] = self.run_metadata
            ts_str = self.run_metadata['runtstamp']
            source['@timestamp'] = ts_str

            yield source, source_id
        return

    def make_source(self):
        """Simple jump method to pick the write source generator based on the
        handler's prospectus."""
        if not self.json_files:
            # If we do not have any json files for this experiment, ignore it.
            return None
        gen = self._make_source_json()
        return gen

    @staticmethod
    def get_json_files(ptb):
        """
        Fetch the list of result.json files for this experiment;
        return a list of dicts containing their metadata.
        """
        paths = [x for x in ptb.tb.getnames() if x.find("result.json") >= 0 and ptb.tb.getmember(x).isfile()]
        datafiles = []
        for p in paths:
            fname = os.path.basename(p)
            datafile = _dict_const(path=p, basename=fname)
            datafiles.append(datafile)
        return datafiles


###########################################################################
# Tool data routines

def _make_source_id(source):
    return hashlib.md5(json.dumps(source, sort_keys=True).encode('utf-8')).hexdigest()

# Handlers data table / dictionary describing how to process a given tool's
# data files to be indexed.  The outer dictionary holds one dictionary for
# each tool. Each tool entry has one '@prospectus' entry, which records
# behavior and handling controls for that tool, and a "patterns" list, which
# contains a handler record for each file that match a given pattern emitted
# by the tool (can be per file or a pattern to match multiple files).
#
# For each data file's handler record, we record the metric "class" (e.g.
# "disk"), and its "metric" name (e.g. "iops), which are used to contruct the
# fully qualified namespace for the index field values.  E.g., the metadata
# path hierarchy for iostat disk_IOPS.csv data would be, 'iostat.disk.iops',
# for disk_Queue_Size.csv it would be 'iostat.disk.qsize', etc.
#
# We also record the metrics "display" name (human readable, or really what is
# currently used by pbench today in generated html charts), and the "units"
# for the metric, but neither field is currently indexed (FIXME!).
#
# The "colpat" field, or column pattern, describes as a regular expression the
# constituent components of the .csv file column header.  Currently, the "id"
# and "subfield" match groups are the only two expected groups, with
# "subfield" considered optional.
#
# The "subfields" field contents in the handler data structure itself is
# meant to be a programmatic way to ensure any subfield derived from a column
# header can be checked, but that is not currently used (FIXME!).
#
# The "metadata" field and "metadata_pat" are option entries which control how
# to extract metadata from column headers.
_known_handlers = {
    'iostat': {
        '@prospectus': {
            # For iostat .csv files, we want to merge all columns into one
            # JSON document
            'handling': 'csv',
            'method': 'unify'
        },
        'patterns': [
            {
                'pattern': re.compile(r'disk_IOPS\.csv'),
                'class': 'disk',
                'metric': 'iops',
                'display': 'IOPS',
                'units': 'count_per_sec',
                'subfields': [ 'read', 'write' ],
                'colpat': re.compile(r'(?P<id>.+)-(?P<subfield>read|write)')
            }, {
                'pattern': re.compile(r'disk_Queue_Size\.csv'),
                'class': 'disk',
                'metric': 'qsize',
                'display': 'Queue_Size',
                'units': 'count',
                'subfields': [],
                'colpat': re.compile(r'(?P<id>.+)')
            }, {
                'pattern': re.compile(r'disk_Request_Merges_per_sec\.csv'),
                'class': 'disk',
                'metric': 'reqmerges',
                'display': 'Request_Merges',
                'units': 'count_per_sec',
                'subfields': [ 'read', 'write' ],
                'colpat': re.compile(r'(?P<id>.+)-(?P<subfield>read|write)')
            }, {
                'pattern': re.compile(r'disk_Request_Size_in_512_byte_sectors\.csv'),
                'class': 'disk',
                'metric': 'reqsize',
                'display': 'Request_Size',
                'units': 'count_512b_sectors',
                'subfields': [],
                'colpat': re.compile(r'(?P<id>.+)')
            }, {
                'pattern': re.compile(r'disk_Throughput_MB_per_sec\.csv'),
                'class': 'disk',
                'metric': 'tput',
                'display': 'Throughput',
                'units': 'MB_per_sec',
                'subfields': [ 'read', 'write' ],
                'colpat': re.compile(r'(?P<id>.+)-(?P<subfield>read|write)')
            }, {
                'pattern': re.compile(r'disk_Utilization_percent\.csv'),
                'class': 'disk',
                'metric': 'util',
                'display': 'Utilization',
                'units': 'percent',
                'subfields': [],
                'colpat': re.compile(r'(?P<id>.+)')
            }, {
                'pattern': re.compile(r'disk_Wait_Time_msec\.csv'),
                'class': 'disk',
                'metric': 'wtime',
                'display': 'Wait_Time',
                'units': 'msec',
                'subfields': [ 'read', 'write' ],
                'colpat': re.compile(r'(?P<id>.+)-(?P<subfield>read|write)')
            }
        ]
    },
    'pidstat': {
        '@prospectus': {
            # For pidstat .csv files, we want to individually index
            # each column entry as its own JSON document
            'handling': 'csv',
            'method': 'unify'
        },
        'patterns': [
            {
                'pattern': re.compile(r'context_switches_nonvoluntary_switches_sec\.csv'),
                'class': 'pidstat',
                'metric': 'context_switches_nonvoluntary_switches',
                'display': 'Context_Switches_Nonvoluntary',
                'units': 'count_per_sec',
                'subfields': [],
                'colpat': re.compile(r'(?P<id>.+)'),
                'metadata': [ 'pid', 'command' ],
                'metadata_pat': re.compile(r'(?P<pid>.+?)-(?P<command>.+)')
            }, {
                'pattern': re.compile(r'context_switches_voluntary_switches_sec\.csv'),
                'class': 'pidstat',
                'metric': 'context_switches_voluntary_switches',
                'display': 'Context_Switches_Voluntary',
                'units': 'count_per_sec',
                'subfields': [],
                'colpat': re.compile(r'(?P<id>.+)'),
                'metadata': [ 'pid', 'command' ],
                'metadata_pat': re.compile(r'(?P<pid>.+?)-(?P<command>.+)')
            }, {
                'pattern': re.compile(r'cpu_usage_percent_cpu\.csv'),
                'class': 'pidstat',
                'metric': 'cpu_usage',
                'display': 'CPU_Usage',
                'units': 'percent_cpu',
                'subfields': [],
                'colpat': re.compile(r'(?P<id>.+)'),
                'metadata': [ 'pid', 'command' ],
                'metadata_pat': re.compile(r'(?P<pid>.+?)-(?P<command>.+)')
            }, {
                'pattern': re.compile(r'file_io_io_reads_KB_sec\.csv'),
                'class': 'pidstat',
                'metric': 'io_reads',
                'display': 'IO_Reads',
                'units': 'KB_per_sec',
                'subfields': [],
                'colpat': re.compile(r'(?P<id>.+)'),
                'metadata': [ 'pid', 'command' ],
                'metadata_pat': re.compile(r'(?P<pid>.+?)-(?P<command>.+)')
            }, {
                'pattern': re.compile(r'file_io_io_writes_KB_sec\.csv'),
                'class': 'pidstat',
                'metric': 'io_writes',
                'display': 'IO_Writes',
                'units': 'KB_per_sec',
                'subfields': [],
                'colpat': re.compile(r'(?P<id>.+)'),
                'metadata': [ 'pid', 'command' ],
                'metadata_pat': re.compile(r'(?P<pid>.+?)-(?P<command>.+)')
            }, {
                'pattern': re.compile(r'memory_faults_major_faults_sec\.csv'),
                'class': 'pidstat',
                'metric': 'memory_faults_major',
                'display': 'Memory_Faults_Major',
                'units': 'count_per_sec',
                'subfields': [],
                'colpat': re.compile(r'(?P<id>.+)'),
                'metadata': [ 'pid', 'command' ],
                'metadata_pat': re.compile(r'(?P<pid>.+?)-(?P<command>.+)')
            }, {
                'pattern': re.compile(r'memory_faults_minor_faults_sec\.csv'),
                'class': 'pidstat',
                'metric': 'memory_faults_minor',
                'display': 'Memory_Faults_Minor',
                'units': 'count_per_sec',
                'subfields': [],
                'colpat': re.compile(r'(?P<id>.+)'),
                'metadata': [ 'pid', 'command' ],
                'metadata_pat': re.compile(r'(?P<pid>.+?)-(?P<command>.+)')
            }, {
                'pattern': re.compile(r'memory_usage_resident_set_size\.csv'),
                'class': 'pidstat',
                'metric': 'rss',
                'display': 'RSS',
                'units': 'KB',
                'subfields': [],
                'colpat': re.compile(r'(?P<id>.+)'),
                'metadata': [ 'pid', 'command' ],
                'metadata_pat': re.compile(r'(?P<pid>.+?)-(?P<command>.+)')
            }, {
                'pattern': re.compile(r'memory_usage_virtual_size\.csv'),
                'class': 'pidstat',
                'metric': 'vsz',
                'display': 'VSZ',
                'units': 'KB',
                'subfields': [],
                'colpat': re.compile(r'(?P<id>.+)'),
                'metadata': [ 'pid', 'command' ],
                'metadata_pat': re.compile(r'(?P<pid>.+?)-(?P<command>.+)')
            }
        ]
    },
    'proc-interrupts': {
        '@prospectus': {
            # For proc-interrupts .csv files, we want to individually index
            # each column entry as its own JSON document
            'handling': 'csv',
            'method': 'individual'
        },
        'patterns': []
    },
    'prometheus-metrics': {
        '@prospectus': {
            'handling': 'json',
            'method': 'json'
         }
     },
    'sar': None,
    'turbostat': None,
    'mpstat': {
        '@prospectus': {
            # For mpstat .csv files, we want to individually index each row of
            # a file as its own JSON document.
            'handling': 'csv',
            'method': 'individual'
        },
        'patterns': [
            {
                # The mpstat tool produces csv files with names based on cpu
                # cores. The number of cpu cores can be different on each
                # computer, so map a regular expression for each file type.
                'pattern': re.compile(r'(?P<id>cpu.+)_cpu\w+\.csv'),
                'class': 'mpstat',
                'metric': 'cpu',
                'display': 'CPU',
                'units': 'percent_cpu'
            }
        ]
    },
    'perf': None,
    'proc-vmstat': {
        # The proc-vmstat tool writes the timestamp and key value pairs to
        # the stdout text file which is not json or csv.
        '@prospectus': {
            'handling': 'stdout',
            'method': 'periodic_timestamp_key_value'
        },
        'patterns': [
            {
                'pattern': re.compile(r'proc-vmstat-stdout\.txt'),
                'class': 'vmstat',
                'metric': 'metric'
            }
        ]
    }
}

# If we need to deal with old .csv file names, place an alias here to map to
# an existing handler above.
_aliases = {
    'disk_Request_Merges.csv': 'disk_Request_Merges_per_sec.csv',
    'disk_Request_Size.csv': 'disk_Request_Size_in_512_byte_sectors.csv',
    'disk_Throughput.csv': 'disk_Throughput_MB_per_sec.csv',
    'disk_Utilization.csv': 'disk_Utilization_percent.csv',
    'disk_Wait_Time.csv': 'disk_Wait_Time_msec.csv',
}


class ToolData(object):
    def __init__(self, ptb, experiment, iteration, sample, host, tool,
                toolgroup, opctx):
        self.ptb = ptb
        self.toolname = tool
        self.counters = Counter()
        opctx.append(_dict_const(object="ToolData-%s" % (tool), counters=self.counters))
        try:
            (iterseqno, itername) = iteration.split('-', 1)
        except ValueError:
            iterseqno = itername = iteration
        try:
            self.start_run_ts = datetime.strptime(ptb.start_run, "%Y-%m-%dT%H:%M:%S.%f")
        except ValueError:
            try:
                self.start_run_ts = datetime.strptime(ptb.start_run, "%Y-%m-%dT%H:%M:%S")
            except ValueError:
                self.start_run_ts = None
        try:
            self.end_run_ts = datetime.strptime(ptb.end_run, "%Y-%m-%dT%H:%M:%S.%f")
        except ValueError:
            try:
                self.end_run_ts = datetime.strptime(ptb.end_run, "%Y-%m-%dT%H:%M:%S")
            except ValueError:
                self.end_run_ts = None
        self.run_metadata = _dict_const(
            runtstamp=ptb.start_run,
            runid=ptb.md5sum,
            experiment=experiment,
            # FIXME=What are the constituent parts of an iteration name?
            iteration=itername,
            iterseqno=iterseqno,
            sample=sample,
            host=host,
            toolgroup=toolgroup,
        )

        # Impedance match between host names used when
        # registering tools and <label>:<hostname_s>
        # convention used when collecting the results.

        # import pdb; pdb.set_trace()
        label = get_tool_label(host, ptb.mdconf)
        hostname_s = get_tool_hostname_s(host, ptb.mdconf)
        if label and hostname_s:
            hostpath = "{}:{}".format(label, hostname_s)
        elif hostname_s:
            hostpath = "{}".format(hostname_s)
        else:
            hostpath = host

        try:
            self.handler = _known_handlers[tool]
        except KeyError:
            self.handler = None
            self.files = None
        else:
            # Fetch all the data files as a dictionary containing metadata
            # about them.
            self.files = ToolData.get_files(
                    self.handler, iteration, sample, hostpath, tool, toolgroup, ptb)

    def convert_timestamp(self, orig_ts):
        # Convert the given string timestamp, assumed to be a float in
        # milliseconds since the epoch, to the expected string timestamp format.
        ts_float = float(orig_ts)/1000
        ts = datetime.utcfromtimestamp(ts_float)
        if self.start_run_ts is not None and ts < self.start_run_ts:
            self.counters['tool_ts_before_start_run_ts'] += 1
        if self.end_run_ts is not None and ts > self.end_run_ts:
            self.counters['tool_ts_after_end_run_ts'] += 1
        return ts.strftime("%Y-%m-%dT%H:%M:%S.%f%z")

    def _make_source_unified(self):
        """Create one JSON document per identifier, per timestamp from
        the data found in multiple csv files.

        This algorithm is only applicable to 2 or more csv files which
        contain data about 1 or more identifiers.

        The approach is to process each .csv file at the same time,
        reading one row from each in lock step. The field data found
        in each column across all files for a given identifier at the
        same timestamp are unified into one JSON document.

        For example, given 2 csv files:

          * file0.csv
            * timestamp_ms,id0_foo,id1_foo,id2_foo
              * 00000, 1.0, 2.0, 3.0
              * 00001, 1.1, 2.1, 3.1
          * file1.csv
            * timestamp_ms,id0_bar,id1_bar,id2_bar
              * 00000, 4.0, 5.0, 6.0
              * 00001, 4.1, 5.1, 6.1

        The output would be 6 JSON records, one for each 3 identifiers
        at each of two timestamps, with the fields "foo" and "bar" in
        each:

          [ { "@timestamp": 00000, "id": "id0", "foo": 1.0, "bar": 4.0 },
            { "@timestamp": 00000, "id": "id1", "foo": 2.0, "bar": 5.0 },
            { "@timestamp": 00000, "id": "id2", "foo": 3.0, "bar": 6.0 },
            { "@timestamp": 00001, "id": "id0", "foo": 1.1, "bar": 4.1 },
            { "@timestamp": 00001, "id": "id1", "foo": 2.1, "bar": 5.1 },
            { "@timestamp": 00001, "id": "id2", "foo": 3.1, "bar": 6.1 } ]
        """
        # Class list is generated from the handler data
        class_list = _dict_const()
        # The metric mapping provides (klass, metric) tuples for a given
        # .csv file.
        metric_mapping = _dict_const()
        # The list of identifiers is generated from the combined headers,
        # parsing out the IDs
        identifiers = _dict_const()
        # Records the association between the column number of a given
        # .csv file and the identifier / subfield tuple.
        field_mapping = _dict_const()
        # Metadata extracted from column header
        metadata = _dict_const()

        # To begin the unification process, we have to generate a data
        # structure to drive processing of the rows from all csv files
        # by deriving data from the header rows of all the csv files,
        # first. This is driven by the data provided in the handler.
        for csv in self.files:
            # Each csv file dictionary provides its header row.
            header = csv['header']
            if header[0] != 'timestamp_ms':
                print("tool-data-indexing: expected first column of .csv file"
                        " (%s) to be 'timestamp_ms', found %s",
                        (csv['basename'], header[0]))
                self.counters['first_column_not_timestamp_ms'] += 1
                continue
            handler_rec = csv['handler_rec']
            class_list[handler_rec['class']] = True
            metric_mapping[csv['basename']] = (handler_rec['class'], handler_rec['metric'])
            colpat = handler_rec['colpat']
            if csv['basename'] not in field_mapping:
                field_mapping[csv['basename']] = _dict_const()
            for idx,col in enumerate(header):
                if idx == 0:
                    # No field mapping necessary for the timestamp
                    field_mapping[csv['basename']][idx] = None
                    continue
                # First pull out the identifier of the target from the column
                # header and record it in the list of identifiers.
                m = colpat.match(col)
                identifier = m.group('id')
                identifiers[identifier] = True
                try:
                    # Pull out any sub-field names found in the column
                    # header.
                    subfield = m.group('subfield')
                except IndexError:
                    # Columns do not have to have sub-fields, so we can
                    # safely use None.
                    subfield = None
                else:
                    # Ensure the name of the subfield found in the
                    # column is in the list of expected subfields from
                    # "known handlers" table.
                    if subfield not in handler_rec['subfields']:
                        print("tool-data-indexing: column header, %r, has an"
                                " unexpected subfield, %r, expected %r"
                                " subfields, for .csv %s" % (
                                    col, subfield, handler_rec['subfields'],
                                    csv['basename']))
                        self.counters['column_subfields_do_not_match_handler'] += 1
                        subfield = None
                # Record the association between the column number
                # ('idx') of a given .csv file ('basename') and the
                # identifier / subfield tuple.
                field_mapping[csv['basename']][idx] = (identifier, subfield)
                try:
                    # Some identifiers are constructed by combining
                    # pieces of metadata to make a unique ID.  Pull
                    # out of the handler the pattern that can extract
                    # that metadata.
                    metadata_pat = handler_rec['metadata_pat']
                except KeyError:
                    # We can safely ignore handlers which do not
                    # provide metadata handlers.
                    pass
                else:
                    # Parse out the metadata name(s) from the column
                    # header.
                    m = metadata_pat.match(col)
                    if m:
                        colmd = _dict_const()
                        # We matched one or more names, loop through
                        # the list of expected metadata regex group
                        # names to build up the mapping of regex
                        # group names to actual metadata field names.
                        for md in handler_rec['metadata']:
                            try:
                                val = m.group(md)
                            except IndexError:
                                print("tool-data-indexing: handler metadata,"
                                        " %r, not found in column %r using"
                                        " pattern %r, for .csv %s" % (
                                            handler_rec['metadata'], col,
                                            handler_rec['metadata_pat'],
                                            csv['basename']))
                                self.counters['expected_column_metadata_not_found'] += 1
                            else:
                                colmd[md] = val
                        # Store the association between the identifier
                        # and the metadata mapping for field names.
                        metadata[identifier] = colmd
        # At this point, we have processed all the data about csv files
        # and are ready to start reading the contents of all the csv
        # files and building the unified records.
        def rows_generator():
            # We use this generator to highlight the process of reading from
            # all the csv files, reading one row from each of the csv files,
            # returning that as a dictionary of csv file to row read, which
            # in turn is yielded by the generator.
            while True:
                # Read a row from each .csv file
                rows = _dict_const()
                for csv in self.files:
                    try:
                        rows[csv['basename']] = next(csv['reader'])
                    except StopIteration:
                        # This should handle the case of mismatched number of
                        # rows across all .csv files. All readers which have
                        # finished will emit a StopIteration.
                        pass
                if not rows:
                    # None of the csv file readers returned any rows to
                    # process, so we're done.
                    break
                # Yield the one dictionary that contains each newly read row
                # from all the csv files.
                yield rows
        for rows in rows_generator():
            # Verify timestamps are all the same
            tstamp = None
            first = None
            for fname in rows.keys():
                tstamp = rows[fname][0]
                if first is None:
                    first = tstamp
                elif first != tstamp:
                    print("tool-data-indexing: %s csv files have inconsistent"
                            " timestamps per row" % self.toolname)
                    self.counters['inconsistent_timestamps_across_csv_files'] += 1
                    break
            # We are now ready to create a base document per identifier to
            # hold all the fields from the various columns. Given the two
            # input dictionaries, "identifiers" and "metadata", we create
            # an output dictionary, "datum", which has keys for all the
            # identifiers and a base dictionary for forming the JSON docs.

            # For example, given these inputs:
            #   * identifiers = { "id0": True, "id1": True }
            #   * metadata = { "id0": { "f1": "foo", "f2": "bar" },
            #                  "id1": { "f1": "faz", "f2": "baz" } }
            # The for loop below would generate the following dictionary:
            #   * datum = { "id0": { "@timestamp": ts_str,
            #                        "@metadata": self.run_metadata,
            #                        self.toolname: { "id": "id0",
            #                                         "f1": "foo",
            #                                         "f2": "bar" } },
            #               "id1": { "@timestamp": ts_str,
            #                        "@metadata": self.run_metadata,
            #                        self.toolname: { "id": "id1",
            #                                         "f1": "faz",
            #                                         "f2": "baz" } },

            # The timestamp is taken from the "first" timestamp, converted
            # to a floating point value in seconds, and then formatted as a
            # string.
            ts_str = self.convert_timestamp(first)
            datum = _dict_const()
            for identifier in identifiers.keys():
                datum[identifier] = _dict_const([
                    # Since they are all the same, we use the first to
                    # generate the real timestamp.
                    ('@timestamp', ts_str),
                    ('@metadata', self.run_metadata)
                ])
                datum[identifier][self.toolname] = _dict_const()
                try:
                    md = metadata[identifier]
                except KeyError:
                    md = None
                for klass in class_list.keys():
                    d = _dict_const([('id',identifier)])
                    if md:
                        d.update(md)
                    datum[identifier][self.toolname][klass] = d
            # Now we can perform the mapping from multiple .csv files to JSON
            # documents using a known field hierarchy (no identifiers in field
            # names) with the identifiers as additional metadata. Note that we
            # are constructing this document just from the current row of data
            # taken from all .csv files (assumes timestamps are the same).
            for fname,row in rows.items():
                klass, metric = metric_mapping[fname]
                for idx,val in enumerate(row):
                    if idx == 0:
                        continue
                    # Given an fname and a column offset, return the
                    # identifier from the header
                    identifier, subfield = field_mapping[fname][idx]
                    if subfield:
                        if metric not in datum[identifier][self.toolname][klass]:
                            datum[identifier][self.toolname][klass][metric] = _dict_const()
                        datum[identifier][self.toolname][klass][metric][subfield] = val
                    else:
                        datum[identifier][self.toolname][klass][metric] = val
            # At this point we have fully mapped all data from all .csv files
            # to their proper fields for each identifier. Now we can yield
            # records for each of the identifiers.
            for _id,source in datum.items():
                source_id = _make_source_id(source)
                yield source, source_id
        return

    def _make_source_individual(self):
        """Read .csv files individually, emitting records for each row and
        column coordinate."""
        for csv in self.files:
            assert csv['header'][0] == 'timestamp_ms'
            header = csv['header']
            handler_rec = csv['handler_rec']
            klass = handler_rec['class']
            metric = handler_rec['metric']
            reader = csv['reader']
            ts = None

            # Some individual handlers have the id in the filename pattern.
            if 'pattern' in handler_rec:
                # Does the pattern match the basename?
                match = handler_rec['pattern'].match(csv['basename'])
                if match:
                    try:
                        datum = _dict_const()
                        datum[klass] = _dict_const()
                        # This handler has the id in the filename pattern.
                        datum[klass][metric] = _dict_const([('id', match.group('id'))])
                        for row in reader:
                            for idx,val in enumerate(row):
                                # The timestamp column is index zero.
                                if idx == 0:
                                    time_val = float(val)/1000
                                    utc = datetime.utcfromtimestamp(time_val)
                                    # Convert the current timestamp to a formatted string.
                                    ts = utc.strftime("%Y-%m-%dT%H:%M:%S.%f%z")
                                    datum['@timestamp'] = ts
                                    datum['@metadata'] = self.run_metadata
                                else:
                                    column = header[idx]
                                    datum[klass][metric][column] = val

                            source_id = _make_source_id(datum)
                            yield datum, source_id
                    except IndexError:
                        # This handler does not have an id, skip this file.
                        break
        return

    def _make_source_stdout(self):
        """
        Read the given set of files one at a time, emitting a record
        for each set of key/value pairs associated with a timestamp.

        The format for files considered by this method are as follows:

         * each line of the file contains an ascii-numeric key and a
           numeric (integer or floating point) value separated by a ':'
         * the keyword, `timestamp`, is recognized to be the
           timestamp value to be associated with all following
           key/value pairs until the next `timestamp` keyword
           is encountered
         * all key/value pairs are treated as fields of one JSON
           document with the associated timestamp to be indexed

        An example file format:

        timestamp: 12345.00
        key0: 100.0
        key1: 100.1
        ...
        keyN: 100.N
        timestamp: 12345.01
        key0: 100.0
        key1: 100.1
        ...
        keyN: 100.N
        timestamp: ...
        """
        for output_file in self.files:
            basename = output_file['basename']
            handler = output_file['handler']
            klass = handler['class']
            path = os.path.join(self.ptb.extracted_root, output_file['path'])
            with open(path, 'r') as file_object:
                record = None
                for line in file_object:
                    if line.startswith('timestamp:'):
                        # timestamp delimits records, yield last record.
                        if record:
                            source_id = _make_source_id(record)
                            yield record, source_id
                        # Get the second column, the timestamp value.
                        ts = float(line.split(':')[1])
                        utc = datetime.utcfromtimestamp(ts)
                        tstamp = utc.strftime("%Y-%m-%dT%H:%M:%S.%f%z")
                        record = _dict_const()
                        record['@timestamp'] = tstamp
                        record['@metadata'] = self.run_metadata
                        record[self.toolname] = _dict_const()
                        record[self.toolname][klass] = _dict_const()
                    else:
                        key, value = line.strip().split(' ')
                        record[self.toolname][klass][key] = value
                if record:
                    source_id = _make_source_id(record)
                    yield record, source_id

    def _make_source_json(self):
        for df in self.files:
            try:
                with open(os.path.join(self.ptb.extracted_root, df['path'])) as fp:
                    payload = json.load(fp)
            except Exception as e:
                print("tool-data-indexing: encountered bad JSON file,"
                        " %s: %r" % (df['path'], e))
                self.counters["bad_json_file"] += 1
                continue

            missing_ts = False
            for source in payload:
                try:
                    ts_val = source['@timestamp']
                except KeyError:
                    # missing timestamps
                    if not missing_ts:
                        # Log the first record with missing timestamps we
                        # encounter for this file, and then count the rest and
                        # report the count with the summary of how the
                        # indexing went.
                        missing_ts = True
                        print("tool-data-indexing: encountered JSON file, %s,"
                                " with missing @timestamp fields" % (
                                    df['path']))
                    self.counters['json_doc_missing_timestamp'] += 1
                    continue

                # further timestamp handling
                try:
                    # unix seconds since epoch timestamp
                    ts = datetime.utcfromtimestamp(ts_val)
                except TypeError:
                    # the timestamp value is not in seconds since the epoch,
                    # so assume that source[@timestamp] is already in the
                    # expected format.
                    #
                    # FIXME - Should we validate the expected string format?
                    self.counters['json_doc_timestamp_not_validated'] += 1
                else:
                    ts_str = ts.strftime("%Y-%m-%dT%H:%M:%S.%f%z")
                    source['@timestamp'] = ts_str

                source['@metadata'] = self.run_metadata

                # any transformations needed should be done here

                source_id = _make_source_id(source)
                yield source, source_id
        return

    def make_source(self):
        """Simple jump method to pick the write source generator based on the
        handler's prospectus."""
        if not self.files:
            # If we do not have any data files for this tool, ignore it.
            return
        if self.handler['@prospectus']['method'] == 'unify':
            gen = self._make_source_unified()
        elif self.handler['@prospectus']['method'] == 'individual':
            gen = self._make_source_individual()
        elif self.handler['@prospectus']['method'] == 'json':
            gen = self._make_source_json()
        elif self.handler['@prospectus']['method'] == 'periodic_timestamp_key_value':
            gen = self._make_source_stdout()
        else:
            raise Exception("Logic bomb!")
        return gen

    @staticmethod
    def get_csv_files(handler, iteration, sample, host, tool, toolgroup, ptb):
        """
        Fetch the list of .csv files for this tool, fetch their headers, and
        return a dictionary mapping their column headers to their field names.
        """
        path = os.path.join(iteration, sample, "tools-%s" % (toolgroup,), host, tool, "csv")
        paths = [x for x in ptb.tb.getnames() if x.find(path) >= 0 and ptb.tb.getmember(x).isfile()]
        datafiles = []
        for p in paths:
            fname = os.path.basename(p)
            for rec in handler['patterns']:
                if rec['pattern'].match(fname):
                    handler_rec = rec
                    break
            else:
                # Try an alias
                try:
                    alias_name = _aliases[fname]
                except KeyError:
                    # Ignore .csv files for which we don't have a handler,
                    # after checking to see if they might have an alias
                    # name.
                    #print("WARNING: no .csv handler for %s (%s)\n" % (fname, p), file=sys.stdout)
                    continue
                else:
                    for rec in handler['patterns']:
                        if rec['pattern'].match(alias_name):
                            handler_rec = rec
                            break
                    else:
                        # Ignore .csv files for which we don't have a handler
                        #print("WARNING: no .csv handler for %s (%s, %s)\n" % (alias_name, fname, p), file=sys.stdout)
                        continue
            assert handler_rec is not None
            datafile = _dict_const(path=p, basename=fname, handler_rec=handler_rec)
            # FIXME: we might have to deal with UTF-8 decoding issues
            # reader = csv.reader(codecs.iterdecode(tb.extractfile(dfilepath), 'utf-8'))
            datafile['reader'] = reader = csv.reader(open(os.path.join(ptb.extracted_root, p)))
            # FIXME: how should we handle .csv files that are empty?
            datafile['header'] = next(reader)
            datafiles.append(datafile)
        return datafiles

    @staticmethod
    def get_json_files(handler, iteration, sample, host, tool, toolgroup, ptb):
        """
        Fetch the list of json files for this tool, and
        return a list of dicts containing their metadata.
        """
        # import pdb; pdb.set_trace()
        path = os.path.join(iteration, sample, "tools-%s" % (toolgroup,), host, tool, "json")
        paths = [x for x in ptb.tb.getnames() if x.find(path) >= 0 and ptb.tb.getmember(x).isfile()]
        datafiles = []
        for p in paths:
            fname = os.path.basename(p)
            datafile = _dict_const(path=p, basename=fname, handler_rec=None)
            datafiles.append(datafile)
        return datafiles

    @staticmethod
    def get_stdout_files(handler, iteration, sample, host, tool, toolgroup, ptb):
        """
        Fetch the stdout file for this tool returning a list of dicts
        containing metadata.
        """
        stdout_file = "{0}-stdout.txt".format(tool)
        path = os.path.join(iteration, sample, "tools-{0}".format(toolgroup), host, tool, stdout_file)
        paths = [x for x in ptb.tb.getnames() if x.find(path) >= 0 and ptb.tb.getmember(x).isfile()]
        datafiles = []
        for p in paths:
            fname = os.path.basename(p)
            handler_rec = None
            for rec in handler['patterns']:
                if rec['pattern'].match(fname):
                    handler_rec = rec
                    break

            if handler_rec is not None:
                datafile = _dict_const(path=p, basename=stdout_file, handler=handler_rec)
                datafiles.append(datafile)

        return datafiles

    @staticmethod
    def get_files(handler, iteration, sample, host, tool, toolgroup, ptb):
        if handler is None:
            datafiles = []
        elif handler['@prospectus']['handling'] == 'csv':
            datafiles = ToolData.get_csv_files(handler, iteration, sample, host, tool, toolgroup, ptb)
        elif handler['@prospectus']['handling'] == 'json':
            datafiles = ToolData.get_json_files(handler, iteration, sample, host, tool, toolgroup, ptb)
        elif handler['@prospectus']['handling'] == 'stdout':
            datafiles = ToolData.get_stdout_files(handler, iteration, sample, host, tool, toolgroup, ptb)
        else:
            raise Exception("Logic bomb! %s" % (handler['@prospectus']['handling']))
        return datafiles

# tool data are stored in csv files in the tarball.
# the structure is
#      <iterN> -> sampleN -> tools-$group -> <host> -> <tool> -> csv -> files
# we have access routines for getting the iterations, samples, hosts, tools and files
# because although we prefer to get as many of these things out of the metadata log,
# that may not be possible; in the latter case, we fall back to trawling through the
# tarball and using heuristics.

def get_iterations(mdconf, tb):
    try:
        # N.B. Comma-separated list
        iterations_str = mdconf.get("pbench", "iterations")
    except Exception:
        # TBD - trawl through tb with some heuristics
        iterations = []
        for x in tb.getnames():
            l = x.split('/')
            if len(l) != 2:
                continue
            iter = l[1]
            if re.search('^[1-9][0-9]*-', iter):
                iterations.append(iter)
    else:
        iterations = iterations_str.split(', ')
    iterations_set = set(iterations)
    iterations = list(iterations_set)
    iterations.sort()
    return iterations

def get_samples(iteration, mdconf, tb):
    samples = []
    for x in tb.getnames():
        if x.find("{}/".format(iteration)) < 0:
            continue
        l = x.split('/')
        if len(l) !=  3:
            continue
        sample = l[2]
        if sample.startswith('sample'):
            samples.append(sample)
    if len(samples) == 0:
        samples.append('reference-result')
    samples_set = set(samples)
    samples = list(samples_set)
    samples.sort()
    return samples

def get_hosts(iteration, sample, mdconf, tb):
    try:
        # N.B. Space-separated list
        hosts = mdconf.get("tools", "hosts")
    except ConfigParserError:
        print("ConfigParser error in get_hosts: tool data will *not* be indexed.")
        print("This is most probably a bug: please open an issue.")
        return []
    except NoSectionError:
        print("No [tools] section in metadata.log: tool data will *not* be indexed.")
        return []
    except NoOptionError:
        print("No hosts in [tools] section in metadata log: tool data will *NOT* be indexed.")
        return []
    hosts_set = set(hosts.split())
    hosts = list(hosts_set)
    hosts.sort()
    return hosts

def get_tools(iteration, sample, host, mdconf, tb):
    try:
        tools = mdconf.options("tools/{}".format(host))
    except ConfigParserError:
        print("ConfigParser error in get_tools: tool data will *not* be indexed.")
        print("This is most probably a bug: please open an issue.")
        return []
    except NoSectionError:
        print("No [tools/{}] section in metadata.log: tool data will *not* be indexed.".format(host))
        return []
    except NoOptionError:
        print("No tools in [tools/{}] section in metadata log: tool data will *NOT* be indexed.".format(host))
        return []
    tools_set = set(tools)
    tools = list(tools_set)
    tools.sort()
    return tools

def get_tool_label(host, mdconf):
    try:
        return mdconf.get("tools/" + host, "label")
    except:
        return ""

def get_tool_hostname_s(host, mdconf):
    try:
        return mdconf.get("tools/" + host, "hostname-s")
    except:
        return ""

def mk_tool_data(ptb, opctx):
    experiment = ptb.mdconf.get("pbench", "name")
    try:
        toolsgroup = ptb.mdconf.get("tools", "group")
    except Exception:
        toolsgroup = "default"
    iterations = get_iterations(ptb.mdconf, ptb.tb)
    for iteration in iterations:
        samples = get_samples(iteration, ptb.mdconf, ptb.tb)
        for sample in samples:
            hosts = get_hosts(iteration, sample, ptb.mdconf, ptb.tb)
            for host in hosts:
                tools = get_tools(iteration, sample, host, ptb.mdconf, ptb.tb)
                for tool in tools:
                    yield ToolData(
                            ptb, experiment, iteration, sample, host, tool,
                            toolsgroup, opctx)
    return

def mk_tool_data_actions(ptb, options, INDEX_PREFIX, opctx):
    index_name_template = "%s.tool-data-%%s" % INDEX_PREFIX
    for td in mk_tool_data(ptb, opctx):
        # Each ToolData object, td, that is returned here represents how
        # data collected for that tool across all hosts is to be returned.
        # The make_source method returns a generator that will emit each
        # source document for the appropriate unit of tool data.  Each has
        # the option of constructing that data as best fits its tool data.
        # The tool data for each tool is kept in its own index to allow
        # for different curation policies for each tool.
        index_name_template_for_tool = (index_name_template % td.toolname) + '.'
        asource = td.make_source()
        if not asource:
            continue
        for source, source_id in asource:
            action = _dict_const(
                _op_type=_op_type,
                _index=index_name_template_for_tool + source['@timestamp'].split('T', 1)[0],
                _type="pbench-tool-data-%s" % td.toolname,
                _id=source_id,
                _source=source
            )
            yield action
    return

def mk_result_data_actions(ptb, options, INDEX_PREFIX, opctx):
    index_name_template = "%s.result-data." % INDEX_PREFIX
    experiment = ptb.mdconf.get("pbench", "name")
    rd = ResultData(ptb, experiment, opctx)
    if not rd:
        return
    sources = rd.make_source()
    if not sources:
        return
    for source, source_id in sources:
        action = _dict_const(
            _op_type=_op_type,
            _index=index_name_template + source['@timestamp'].split('T', 1)[0],
            _type="pbench-result-data",
            _id=source_id,
            _source=source
        )
        yield action
    return

###########################################################################
# Build tar ball table-of-contents (toc) source documents.

def mk_toc(tb, md5sum, options):
    members = tb.getmembers()
    prefix = members[0].name.split(os.path.sep)[0]
    # create a list of dicts where each directory that contains files is represented
    # by a dictionary with a 'name' key, whose value is the pathname of the dictionary
    # and a 'files' key, which is a list of the files contained in the directory.
    d = _dict_const()
    for m in members:
        if m.isdir():
            dname = os.path.dirname(m.name)[len(prefix):] + os.path.sep
            if dname not in d:
                d[dname] = _dict_const(directory=dname)
        elif m.isfile() or m.issym():
            dname = os.path.dirname(m.name)[len(prefix):] + os.path.sep
            fentry = _dict_const(
                    name=os.path.basename(m.name),
                    size=m.size,
                    mode=oct(m.mode)
            )
            if m.issym():
                fentry['linkpath'] = m.linkpath
            if dname in d:
                # we may have created an empty dir entry already
                # without a 'files' entry, so we now make sure that
                # there is an empty 'files' entry that we can populate.
                if 'files' not in d[dname]:
                    d[dname]['files'] = []
                d[dname]['files'].append(fentry)
            else:
                # presumably we'll never fall through here any more:
                # the directory entry always preceded the file entry
                # in the tarball.
                d[dname] = _dict_const(directory=dname, files=[fentry])
        else:
            # if debug: print(repr(m))
            continue
    return (prefix, d)

def get_md5sum_of_dir(dir, parentid):
    """Calculate the md5 sum of all the names in the toc"""
    h = hashlib.md5()
    h.update(parentid.encode('utf-8'))
    h.update(dir['directory'].encode('utf-8'))
    if 'files' in dir:
        for f in dir['files']:
            for k in sorted(f.keys()):
                h.update(repr(f[k]).encode('utf-8'))
    return h.hexdigest()

def mk_toc_actions(ptb, options, INDEX_PREFIX, opctx):
    """Construct Table-of-Contents actions.

    These are indexed into the run index along side the runs."""
    index_name_template = "%s.run.%%d-%%02d" % INDEX_PREFIX
    tstamp = ptb.start_run.replace('_', 'T')
    prefix, dirs = mk_toc(ptb.tb, ptb.md5sum, options)
    assert(prefix == ptb.dirname)
    for dname,d in dirs.items():
        d["@timestamp"] = tstamp
        action = _dict_const(
            _id=get_md5sum_of_dir(d, ptb.md5sum),
            _op_type=_op_type,
            _index=index_name_template % (int(ptb.date[0]), int(ptb.date[1])),
            _type="pbench-run-toc-entry",
            _source=d,
            _parent=ptb.md5sum
        )
        yield action
    return

###########################################################################
# Build run source document

# routines for handling sosreports, hostnames, and tools
def valid_ip(address):
    import socket
    try:
        socket.inet_aton(address)
        return True
    except Exception:
        return False

def search_by_host(sos_d_list, host):
    for sos_d in sos_d_list:
        if host in sos_d.values():
            return sos_d['hostname-f']
    return None

def search_by_ip(sos_d_list, ip):
    # import pdb; pdb.set_trace()
    for sos_d in sos_d_list:
        for l in sos_d.values():
            if type(l) != type([]):
                continue
            for d in l:
                if type(d) != type({}):
                    continue
                if ip in d.values():
                    return sos_d['hostname-f']
    return None

def get_hostname_f_from_sos_d(sos_d, host=None, ip=None):
    if not host and not ip:
        return None

    if host:
        return search_by_host(sos_d, host)
    else:
        return search_by_ip(sos_d, ip)

def mk_tool_info(sos_d, mdconf):
    """Return a dict containing tool info (local and remote)"""
    try:
        tools_array = []
        # from pprint import pprint; pprint(mdconf.get("tools", "hosts"))

        labels = _dict_const()
        for host in mdconf.get("tools", "hosts").split():
            # from pprint import pprint; pprint(host)
            tools_info = _dict_const()
            # XXX - we should have an FQDN for the host but
            # sometimes we have only an IP address.
            tools_info['hostname'] = host
            # import pdb; pdb.set_trace()
            if valid_ip(host):
                tools_info['hostname-f'] = get_hostname_f_from_sos_d(sos_d, ip=host)
            else:
                tools_info['hostname-f'] = get_hostname_f_from_sos_d(sos_d, host=host)
            section = "tools/%s" % (host)
            items = mdconf.items(section)
            options = mdconf.options(section)
            if 'label' in options:
                tools_info['label'] = mdconf.get(section, 'label')

            # process remote entries for a label and remember them in the labels dict
            remoteitems = [x for x in items if x[0].startswith('remote@') and x[1]]
            for (k,v) in remoteitems:
                host = k.replace('remote@', '', 1)
                labels[host] = v

            # now, forget about any label or remote entries - they have been dealt with.
            items = [x for x in items if x[0] != 'label' and not x[0].startswith('remote')]

            tools = _dict_const()
            tools.update(items)
            tools_info['tools'] = tools
            tools_array.append(tools_info)

        # now process remote labels
        for item in tools_array:
            host = item['hostname']
            if host in labels:
                item['label'] = labels[host]

        return tools_array

    except Exception as err:
        print("mk_tool_info" , err)
        return []

def ip_address_to_ip_o_addr(s):
    # This routine deals with the contents of either the ip_-o_addr
    # (preferred) or the ip_address file in the sosreport.
    # If each line starts with a number followed by a colon,
    # leave it alone and return it as is - that's the preferred case.
    # If not, grovel through the ip_address file, collect the juicy pieces
    # and fake a string that is similar in format to the preferred case -
    # at least similar enough to satisfy the caller of this function.
    as_is = True
    pat = re.compile(r'^[0-9]+:')

    # reduce is not available in python3 :-(
    # as_is = reduce(lambda x, y: x and y,
    #               map(lambda x: re.match(pat, x), s.split('\n')[:-1]))
    for l in s.split('\n')[:-1]:
        if not re.match(pat, l):
            as_is = False
            break
    if as_is:
        return s

    # rats - we've got to do real work
    # state machine
    # start: 0
    # seen <N:>: 1
    # seen inet* : 2
    # EOF: 3
    # if we see anything else, we stay put in the current state
    # transitions: 2 --> 1  action: output a line
    #              2 --> 2  action: output a line
    #
    state = 0
    ret = ""
    # import pdb; pdb.set_trace()
    for l in s.split('\n'):
        if re.match(pat, l):
            if state == 0 or state == 1:
                state = 1
            elif state == 2:
                ret += "%s: %s %s %s\n" % (serial, ifname, proto, addr)
                state = 1
            serial, ifname = l.split(':')[0:2]
        elif l.lstrip().startswith('inet'):
            assert(state != 0)
            if state == 1:
                state = 2
            elif state == 2:
                ret += "%s: %s %s %s\n" % (serial, ifname, proto, addr)
                state = 2
            proto, addr = l.lstrip().split()[0:2]
    if state == 2:
        ret += "%s: %s %s %s\n" % (serial, ifname, proto, addr)
        state = 3
    return ret

def if_ip_from_sosreport(ip_addr_f):
    """Parse the ip_-o_addr file or ip_address file from the sosreport and
    get a dict associating the if name with the ip - separate entries
    for inet and inet6.
    """

    s = str(ip_addr_f.read(), 'iso8859-1')
    d = _dict_const()
    # if it's an ip_address file, convert it to ip_-o_addr format
    s = ip_address_to_ip_o_addr(s)
    for l in s.split('\n'):
        fields = l.split()
        if not fields:
            continue
        ifname = fields[1]
        ifproto = fields[2]
        ifip = fields[3].split('/')[0]
        if ifproto not in d:
            d[ifproto] = []
        d[ifproto].append(_dict_const(ifname=ifname, ipaddr=ifip))

    return d

def hostnames_if_ip_from_sosreport(sos):
    """Return a dict with hostname info (both short and fqdn) and
    ip addresses of all the network interfaces we find at sosreport time."""

    sostb = tarfile.open(fileobj=sos)
    hostname_files = [x for x in sostb.getnames() if x.find('sos_commands/general/hostname') >= 0]

    # Fetch the hostname -f and hostname file contents
    hostname_f_file = [x for x in hostname_files if x.endswith('hostname_-f')]
    if hostname_f_file:
        try:
            hostname_f = str(sostb.extractfile(hostname_f_file[0]).read(), 'iso8859-1')[:-1]
        except IOError as e:
            raise SosreportHostname("Failure to fetch a hostname-f from the sosreport")
        if hostname_f == 'hostname: Name or service not known':
            hostname_f = ""
    else:
        hostname_f = ""
    hostname_s_file = [x for x in hostname_files if x.endswith('hostname')]
    if hostname_s_file:
        try:
            hostname_s = str(sostb.extractfile(hostname_s_file[0]).read(), 'iso8859-1')[:-1]
        except IOError as e:
            raise SosreportHostname("Failure to fetch a hostname from the sosreport")
    else:
        hostname_s = ""

    if not hostname_f and not hostname_s:
        raise SosreportHostname("We do not have a hostname recorded in the sosreport")

    # import pdb; pdb.set_trace()
    if hostname_f and hostname_s:
        if hostname_f == hostname_s:
            # Shorten the hostname if possible
            hostname_s = hostname_f.split('.')[0]
        elif hostname_f.startswith(hostname_s):
            # Already have a shortened hostname
            pass
        elif hostname_s.startswith(hostname_f):
            # switch them
            x = hostname_s
            hostname_s = hostname_f
            hostname_f = x
        elif hostname_f != "localhost":
            hostname_s = hostname_f.split('.')[0]
        elif hostname_s != "localhost":
            hostname_f = hostname_s
        else:
            assert(1 == 0)

    elif not hostname_f and hostname_s:
        # The sosreport did not include, or failed to properly collect, the
        # output from "hostname -f", so we'll just keep them the same
        hostname_f = hostname_s
        # Shorten the hostname if possible
        hostname_s = hostname_f.split('.')[0]
    elif hostname_f and not hostname_s:
        # Shorten the hostname if possible
        hostname_s = hostname_f.split('.')[0]
    else:
        # both undefined
        raise SosreportHostname("Hostname undefined (both short and long)")

    if hostname_f == "localhost" and hostname_s != "localhost":
        hostname_f = hostname_s
        hostname_s = hostname_f.split('.')[0]
    elif hostname_f != "localhost" and hostname_s == "localhost":
        hostname_s = hostname_f.split('.')[0]
    elif hostname_f == "localhost" and hostname_s == "localhost":
        raise SosreportHostname("The sosreport did not collect a hostname other than 'localhost'")
    else:
        pass

    d = _dict_const([
        ('hostname-f', hostname_f),
        ('hostname-s', hostname_s)
    ])

    # get the ip addresses for all interfaces
    ipfiles = [x for x in sostb.getnames() if x.find('sos_commands/networking/ip_') >= 0]
    ip_files = [x for x in ipfiles if x.find('sos_commands/networking/ip_-o_addr') >= 0]
    if ip_files:
        d.update(if_ip_from_sosreport(sostb.extractfile(ip_files[0])))
    else:
        # try the ip_address file instead
        ip_files = [x for x in ipfiles if x.find('sos_commands/networking/ip_address') >= 0]
        if ip_files:
            d.update(if_ip_from_sosreport(sostb.extractfile(ip_files[0])))
    return d

def mk_sosreports(tb):
    sosreports = [ x for x in tb.getnames() if x.find("sosreport") >= 0 ]
    sosreports.sort()

    sosreportlist = []
    for x in sosreports:
        if x.endswith('.md5'):
            # x is the *sosreport*.tar.xz.md5 filename
            d = _dict_const()
            sos = x[:x.rfind('.md5')]
            d['name'] = sos
            d['md5'] = tb.extractfile(x).read().decode('ascii')[:-1]
            # get hostname (short and FQDN) from sosreport
            d.update(hostnames_if_ip_from_sosreport(tb.extractfile(sos)))
            sosreportlist.append(d)

    return sosreportlist

def mk_pbench_metadata(mdconf):
    d = _dict_const()
    try:
        d['pbench-rpm-version'] = mdconf.get("pbench", "rpm-version")
    except Exception:
        d['pbench-rpm-version'] = "Unknown"
    return d

def _to_utc(l, u):
    """l is a timestamp in local time and u is one in UTC. These are
    supposed to be close, so we calculate an offset rounded to a
    half-hour and add it to the local date. We then convert back to an
    ISO format date.
    """
    lts = datetime.strptime(l, "%Y-%m-%dT%H:%M:%S")
    uts = datetime.strptime(u[:u.rfind('.')], "%Y-%m-%dT%H:%M:%S")

    offset = lts - uts
    res = round(float(offset.seconds) / 60 / 60 + offset.days * 24, 1)
    return (lts - timedelta(0, int(res*60*60), 0)).isoformat()

def fix_date_format(ts):
    """ts is a string representing a timestamp with possible formatting
    problems:

        - it might have an underscore instead of a T separating the
          date from the time - ES does not like that.

        - the date part might also be of the form YYYYMMDD instead of
          the approved format YYYY-MM-DD.

    This function fixes up those problems and returns the possibly
    modified string.
    """
    rts = ts.replace('_', 'T')
    ns = rts[rts.rfind('.'):]
    rts = rts[:rts.rfind('.')]
    try:
        val = datetime.strptime(rts, "%Y-%m-%dT%H:%M:%S")
        return rts + ns
    except ValueError as e:
        val = datetime.strptime(rts, "%Y%m%dT%H:%M:%S")
        return datetime.strftime(val, "%Y-%m-%dT%H:%M:%S") + ns
    except Exception as e:
        # on any other exception, we give up
        return "1900-01-01T00:00:00"

def mk_run_metadata(mdconf, md5sum, dirname):
    d = _dict_const()
    try:
        d.update(mdconf.items('run'))
        d.update(mdconf.items('pbench'))
        # Fix up old format dates so ES won't barf on them.
        d['start_run'] = fix_date_format(d['start_run'])
        d['end_run'] = fix_date_format(d['end_run'])
        # Convert the date to UTC: date and start-run are supposed to be close
        # (except that date may be in local time and start-run is UTC).
        d['date'] = _to_utc(fix_date_format(d['date']), d['start_run'])
        # the run id here is the md5sum that's the _id of the main document.
        # it's what ties all of the relevant documents together.
        d['id'] = md5sum
        d['tarball-dirname'] = dirname
        d['tarball-toc-prefix'] = dirname
        del d['rpm-version']
    except KeyError as e:
        print("Tarball: %s - %s is missing in metadata.log\n" % (dirname, e), file=sys.stdout)
        d = _dict_const()
    return d

def mk_user_specified_metadata(options):
    # parse the JSON string into a dict and return it
    try:
        return json.loads(options.metadata_string)
    except Exception:
        print("Cannot parse JSON string: {}\n".format(options.metadata_string))
        return {}

def mk_metadata(fname, tb, mdconf, md5sum):
    """Return a dict with metadata about a tarball"""
    mtime = datetime.utcfromtimestamp(os.stat(fname)[stat.ST_MTIME])

    mddict = _dict_const([
        ('generated-by', _NAME_),
        ('generated-by-version', _VERSION_),
        ('pbench-agent-version', mdconf.get("pbench", "rpm-version")),
        ('file-date', mtime.strftime("%Y-%m-%d")),
        ('file-name', fname),
        ('md5', md5sum)
    ])
    return mddict

def mk_run_action(ptb, options, INDEX_PREFIX, opctx):
    """Extract metadata from the named tarball and create an indexing
       action out of them.

       There are two kinds of metadata: what goes into _source[@metadata]
       is metadata about the tarball itself - not things that are *part* of
       the tarball: its name, size, md5, mtime.
       Metadata about the run are *data* to be indexed.
    """
    source = _dict_const([('@timestamp', ptb.start_run.replace('_', 'T'))])

    # debug: -T options will cause each call below to be timed
    # and the elapsed interval printed.
    debug_time_operations = options.debug_time_operations
    if debug_time_operations: ts("mk_metadata")
    source['@metadata'] = mk_metadata(ptb.tbname, ptb.tb, ptb.mdconf, ptb.md5sum)
    if options.metadata_string:
        if debug_time_operations: ts("mk_user_specified_metadata")
        source['user_specified_metadata'] = mk_user_specified_metadata(options)
    #if debug_time_operations: ts("mk_pbench_metadata")
    #source['pbench'] = mk_pbench_metadata(mdconf)
    if debug_time_operations: ts("mk_run_metadata")
    source['run'] = mk_run_metadata(ptb.mdconf, ptb.md5sum, ptb.dirname)
    if debug_time_operations: ts("mk_sosreports")
    source['sosreports'] = sos_d = mk_sosreports(ptb.tb)
    if debug_time_operations: ts("mk_tool_info")
    source['host_tools_info'] = mk_tool_info(sos_d, ptb.mdconf)

    # make a simple action for indexing
    index_name_template = "%s.run.%%d-%%02d" % INDEX_PREFIX
    action = _dict_const(
        _op_type=_op_type,
        _index=index_name_template % (int(ptb.date[0]), int(ptb.date[1])),
        _type="pbench-run",
        _id=ptb.md5sum,
        _source=source,
    )
    if debug_time_operations: ts("Done!", newline=True)
    return action

def make_all_actions(ptb, options, INDEX_PREFIX, INDEX_VERSION, opctx):
    """Driver for generating all actions on source documents for indexing
    into Elasticsearch. This generator drives the generation of the run
    source document, the table-of-contents tar ball documents, and finally
    all the tool data.
    """
    debug_time_operations = options.debug_time_operations
    if debug_time_operations: ts("mk_run_action")
    yield mk_run_action(ptb, options, INDEX_PREFIX, opctx)
    if debug_time_operations: ts("mk_toc_actions")
    for action in mk_toc_actions(ptb, options, INDEX_PREFIX, opctx):
        yield action
    if debug_time_operations: ts("mk_result_data_actions")
    for action in mk_result_data_actions(ptb, options, INDEX_PREFIX, opctx):
        yield action
    if debug_time_operations: ts("mk_tool_data_actions")
    for action in mk_tool_data_actions(ptb, options, INDEX_PREFIX, opctx):
        yield action
    if debug_time_operations: ts("", newline=True)
    return


class PbenchTarBall(object):
    def __init__(self, tbarg, tmpdir):
        self.tbname = tbarg
        self.hostname = os.path.basename(os.path.dirname(self.tbname))
        self.tb = tarfile.open(self.tbname)

        # This is the top-level name of the run - it should be the common
        # first component of every member of the tarball.
        dirname = os.path.basename(self.tbname)
        # FIXME: should we handle result tarballs that are uncompressed, or
        # compressed with a different compression mechanism from xz?
        self.dirname = dirname[:dirname.rfind('.tar.xz')]
        # ... but let's make sure.
        sampled_prefix = self.tb.getmembers()[0].name.split(os.path.sep)[0]
        if sampled_prefix != self.dirname:
            # All members of the tarball should have self.dirname as its
            # prefix.
            raise UnsupportedTarballFormat(self.tbname)

        # Verify we have a metadata.log file in the tar ball before we
        # start extracting.
        member_name = "%s/metadata.log" % (self.dirname)
        try:
            self.tb.getmember(member_name)
        except KeyError:
            raise UnsupportedTarballFormat(self.tbname)

        # The caller of index-pbench is expected to clean up the temporary
        # directory.
        self.tb.extractall(path=tmpdir)
        self.extracted_root = tmpdir
        if not os.path.isdir(os.path.join(self.extracted_root, self.dirname)):
            raise UnsupportedTarballFormat(self.tbname)
        try:
            self.mdconf = PbenchTarBall.get_mdconfig(
                    os.path.join(self.extracted_root, member_name))
        except Exception:
            raise BadMDLogFormat(self.tbname)
        # We get the start date out of the metadata log.
        try:
            # N.B. the start_run timestamp is in UTC.
            self.start_run = fix_date_format(self.mdconf.get('run', 'start_run'))
            split_date_str = self.start_run.split('T')
            # List of constituent date components, year [0], month [1], day [2]
            self.date = [ int(x) for x in split_date_str[0].split("-") ]
            self.end_run = fix_date_format(self.mdconf.get('run', 'end_run'))
        except Exception:
            raise BadDate(self.tbname)
        # Open the MD5 file of the tarball and read the MD5 sum from it.
        self.md5sum = open("%s.md5" % (self.tbname)).read().split()[0]

    @staticmethod
    def get_mdconfig(mdf):
        """Get metadata from a metadata.log file.

        metadata.log is in the format expected by configparser,
        so we just get and return a configparser object."""
        mdfconfig = SafeConfigParser()
        mdfconfig.read(mdf)

        # FIXME: verify start_run and end_run dictionary items are present
        d = _dict_const()
        d.update(mdfconfig.items('run'))
        if "start_run" not in d or "end_run" not in d:
            raise Exception('Missing start_run or end_run elements')
        return mdfconfig


def get_es_hosts(config):
    """
    Return list of dicts (a single dict for now) -
    that's what ES is expecting.
    """
    try:
        URL = config.get('Server', 'server')
    except NoSectionError:
        print("Need a [Server] section with host and port defined in %s"
              " configuration file" % (" ".join(config.__files__)),
                file=sys.stderr)
        return None
    except NoOptionError:
        host = config.get('Server', 'host')
        port = config.get('Server', 'port')
    else:
        host, port = URL.rsplit(':', 1)
    timeoutobj = Timeout(total=1200, connect=10, read=_read_timeout)
    return [_dict_const(host=host, port=port, timeout=timeoutobj),]

###########################################################################
# main: create index from template if necessary, prepare the action and ship
# it to ES for indexing.

def main(options, args):
    # ^$!@!#%# compatibility
    # FileNotFoundError is python 3.3 and the travis-ci hosts still (2015-10-01) run
    # python 3.2
    filenotfounderror = getattr(__builtins__, 'FileNotFoundError', IOError)
    opctx = []

    try:
        config=SafeConfigParser()

        cfg_name = options.cfg_name
        if not cfg_name:
            raise ConfigFileNotSpecified("No config file specified: set ES_CONFIG_PATH env variable or use --config <file> on the command line")

        try:
            config.read(cfg_name)
            INDEX_PREFIX = config.get('Settings', 'index_prefix')
            INDEX_VERSION = config.get('Settings', 'index_version')
        except Exception as e:
            raise ConfigFileError(e)

        if not options.tmpdir:
            raise Exception("No temporary directory specified")

        if options.debug_unittest:
            es = None
            import collections
            global _dict_const
            _dict_const = collections.OrderedDict
        else:
            hosts = get_es_hosts(config)
            es = Elasticsearch(hosts, max_retries=0)

        # prepare the actions - the tarball name is the only argument.
        ptb = PbenchTarBall(args[0], options.tmpdir)

        # Construct the generator for emitting all actions.  The `opctx`
        # dictionary is passed along to each generator so that it can add its
        # context for error handling to the list.
        actions = make_all_actions(
                ptb, options, INDEX_PREFIX, INDEX_VERSION, opctx)

        # Create the various index templates
        if options.debug_time_operations: ts("es_template")
        es_template(es, options, INDEX_PREFIX, INDEX_VERSION, config, dbg=_DEBUG)

        # returns 0 or 1
        if options.indexing_errors:
            ie_filename = options.indexing_errors
        else:
            ie_filename = os.path.join(options.tmpdir, "index-pbench.error-log.json")
        with open(ie_filename, "w") as fp:
            if options.debug_time_operations: ts("es_index")
            res = es_index(es, actions, fp, dbg=_DEBUG)
            beg, end, successes, duplicates, failures, retries = res
            print("\tdone indexing (end ts: %s, duration: %.2fs,"
                    " successes: %d, duplicates: %d, failures: %d, retries: %d)" % (
                tstos(end), end - beg, successes, duplicates, failures, retries))
            sys.stdout.flush()
            res = 1 if failures > 0 else 0

    except ConfigFileNotSpecified as e:
        print(e, file=sys.stderr)
        res = 2

    except ConfigFileError as e:
        print(e, file=sys.stderr)
        res = 3

    except UnsupportedTarballFormat as e:
        print("Unsupported Tarball Format - no metadata.log: ", e, file=sys.stderr)
        res = 4

    except BadDate as e:
        print("Bad Date: ", e, file=sys.stderr)
        res = 5

    except filenotfounderror as e:
        print("No such file: ", e, file=sys.stderr)
        res = 6

    except BadMDLogFormat as e:
        print("The metadata.log file is curdled in tarball: ", e, file=sys.stderr)
        res = 7

    except MappingFileError as e:
        print(e, file=sys.stderr)
        res = 8

    except TemplateError as e:
        print(repr(e), file=sys.stderr)
        res = 9

    except SosreportHostname as e:
        print("Bad hostname in sosreport: ", e, file=sys.stderr)
        res = 10

    except tarfile.TarError as e:
        print("Can't unpack tarball into {}: {}".format(options.tmpdir, e), file=sys.stderr)
        res = 11

    # this is Spinal Tap
    except Exception as e:
        print("Other error", e, file=sys.stderr)
        import traceback
        print(traceback.format_exc())
        res = 12

    finally:
        dump = False
        for ctx in opctx:
            if ctx['counters']:
                dump = True
        if dump:
            print("** Errors encountered while indexing:")
            print(json.dumps(opctx, indent=4, sort_keys=True))
        if options.debug_time_operations: ts("Done", newline=True)

    # status codes: these are used by pbench-index to segregate tarballs into
    #               classes: should we retry (perhaps after fixing bugs in the
    #               indexing) or should we just forget about them?
    #       0 - normal, successful exit, no errors
    #       1 - Operational error while indexing
    #       2 - Configuration file not specified
    #       3 - Bad configuration file
    #       4 - Tar ball does not contain a metadata.log file
    #       5 - Bad start run date value encountered
    #       6 - File Not Found error
    #       7 - Bad metadata.log file encountered
    #       8 - Error reading a mapping file for Elasticsearch templates
    #       9 - Error creating one of the Elasticsearch templates
    #      10 - Bad hostname in a sosreport
    #      11 - Failure unpacking the tar ball
    #      12 - generic error, needs to be investigated and can be retried after
    #           any indexing bugs are fixed.
    return res

###########################################################################
# Options handling

prog_options = [
    make_option("-C", "--config", dest="cfg_name", help="Specify config file"),
    make_option("-E", "--indexing-errors", dest="indexing_errors", help="Name of a file to write JSON documents that fail to index"),
    make_option("-D", "--temp-directory", dest="tmpdir", help="Temporary directory to use while indexing"),
    make_option("-M", "--metadata", dest="metadata_string", help="Specify additional metadata (e.g. for browbeat) as a JSON document string"),
    # options for debuggging and unit testing
    make_option("-U", "--unittest", action="store_true", dest="debug_unittest", help="Run in unittest mode"),
    make_option("-T", "--time-ops", action="store_true", dest="debug_time_operations", help="Time action making routines"),
]

if __name__ == '__main__':
    parser = OptionParser("Usage: index-pbench [--config <path-to-config-file>] [--metadata \"<JSON doc>\"] [--working-directory <dir>] <path-to-tarball>")
    for o in prog_options:
        parser.add_option(o)
    parser.set_defaults(cfg_name = os.environ.get("ES_CONFIG_PATH"))
    parser.set_defaults(tmpdir = os.environ.get("TMPDIR"))

    (options, args) = parser.parse_args()

    status = main(options, args)
    sys.exit(status)