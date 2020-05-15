"""
Feature class for objects that interact with the Labber API.
Currently, the Labber interface is pretty naive, using the highest-level
API available. After I really get it "working," it would be nice to speed
things up by passing in-memory data buffers to reduce the number of writes,
etc.
"""

import asyncio
import copy
from functools import wraps
import inspect
import numbers
import os
import platform
import re
import tempfile
from typing import *

import numpy as np
import mongoengine as me
import Labber
from Labber import ScriptTools as st

from dysart.labber.labber_serialize import load_labber_scenario_as_dict
from dysart.labber.labber_serialize import save_labber_scenario_from_dict
import dysart.labber.labber_util as labber_util
from dysart.feature import Feature, exposed
import dysart.messages.errors as errors
import toplevel.conf as conf

# Set path to executable. This should be done not-here, but it needs to be put
# somewhere for now.

if platform.system() == 'Darwin':
    st.setExePath(os.path.join(os.path.sep, 'Applications', 'Labber'))
    MAX_PATH = os.statvfs('/').f_namemax
elif platform.system() == 'Linux':
    st.setExePath(os.path.join(os.path.sep, 'usr', 'share', 'Labber', 'Program'))
    MAX_PATH = os.statvfs('/').f_namemax
elif platform.system() == 'Windows':
    st.setExePath(os.path.join('C:\\', 'Program Files', 'Labber', 'Program'))
    MAX_PATH = 260  # This magic constant is a piece of Windows lore.
else:
    raise errors.UnsupportedPlatformError

# This mutex is used to synchronize calls to the Labber API
LOCK = asyncio.Lock()

# NOTE: This lower-case name is intended! This class is meant to be used as a
# method decorator.
class result(exposed):
    """This decorator class annotates a result-yielding method of a Labber feature.
    """

    is_refresh = True
    exposed = True
    
    RESERVED_PARAMETERS = ['index', 'no_cache']

    def __init__(self, fn: Callable) -> None:
        """This accepts a 'result-granting' function and returns a refresh function
        whose return value is cached into the `results` field of `feature` with key
        the name of the wrapped function.

        TODO: Some bad assumptions are being made here:
        * It _forces_ users to remember to name an argument `index`. Nobody will
            remember this. This is a terrible api.
        * It assumes that such a function wants to use only a _single_ result.

        TODO: think about it and assign `index` correctly...
        """

        # First of all, check that the function signature is legal, not
        # not trampling over the needs of @result.
        for param in inspect.signature(fn).parameters:
            if param in self.RESERVED_PARAMETERS:
                raise errors.ReservedParameterError(param)

        # Wrap the function to make it refresh
        self.obj = None

        @wraps(fn)
        def wrapped_fn(*args, **kwargs):
            feature = args[0]  # TODO: is this good practice?

            # Select result number. Don't forward it to fn
            # if this argument is passed!
            index = kwargs.pop('index', -1)
            # Recompute a value instead of fetching it from cache.
            no_cache = kwargs.pop('no_cache', False)
            
            # Ensure that `results` is long enough.
            if index < 0:
                index = len(feature.log_history) + index
            while len(feature.results) <= index:
                feature.results.append({})

            if no_cache:
                # Clear the cache for _all_ results at this index.
                feature.results[index] = {}

            # This is how we indirectly pass index state to the method
            feature.log = feature.log_history[index]

            try:
                return feature.results[index][fn.__name__]
            except KeyError:
                return_value = fn(*args, **kwargs)
                feature.results[index][fn.__name__] = return_value
                feature.save()
                return return_value

        wrapped_fn.is_result = True
        self.wrapped_fn = wrapped_fn
        self.__name__ = wrapped_fn.__name__  # TODO: using `wraps` right?
        self.__doc__ = wrapped_fn.__doc__

    def __get__(self, obj, objtype):
        """Hack to bind this callable to the parent object.
        """
        self.obj = obj
        return self

    def __call__(self, *args, **kwargs):
        return self.wrapped_fn(self.obj, *args, **kwargs)


class LogHistory:
    """Abstracts history of Labber output files as an array-like object. This
    should be considered mostly an implementation detail of the Result class.

    TODO: should this subclass an abc?
    TODO: should this support slicing? Yeah, probably. That would be awesome.
    TODO: should/can we assume that there are never any holes in the history?
          currently _assumes that there are no holes._
    TODO: cache size is currently unbounded.

    """

    def __init__(self, feature_id: str, labber_data_dir: str, log_name_template: str):
        self.feature_id = feature_id
        # Sanitize data dirs that may contain e.g. '~'
        self.labber_data_dir = os.path.expanduser(labber_data_dir)
        os.makedirs(self.labber_data_dir, exist_ok=True)

        self.log_name_template = log_name_template
        self.log_cache = {}  # contains logs that are held in memory

    def __getitem__(self, index: Union[int, slice])\
            -> "Optional[Union[Labber.LogFile, List[Labber.LogFile]]]":  # not sure of type?
        if isinstance(index, int):
            log_path = self.log_path(index)
            if not os.path.isfile(log_path):
                raise IndexError('Labber logfile with index {} cannot be found'.format(index))
            if log_path not in self.log_cache:
                log_file = Labber.LogFile(self.log_path(index))
                self.log_cache[log_path] = log_file
            return self.log_cache[log_path]
        elif isinstance(index, slice):
            # TODO is a less naive implementation possible here?
            # TODO test this, e.g. with slice [:]--it is known not to work.
            return [self[i] for i in range(index.start, index.stop, index.step or 1)]
        else:
            raise TypeError

    def __contains__(self, index: int) -> bool:
        """Check whether an index is used"""
        return self.log_name(index) in os.listdir(self.labber_data_dir)

    def __iter__(self):
        self._n = 0
        return self

    def __next__(self):
        if self._n <= len(self.get_log_paths()):
            log = self.__getitem__(self._n)
            self._n += 1
            return log
        else:
            raise StopIteration

    def __len__(self) -> int:
        """Gets the number of extant log files."""
        return sum([(1 if self.is_log(fn) else 0)
                    for fn in os.listdir(self.labber_data_dir)])

    def __bool__(self) -> bool:
        """Returns True iff there is at least one entry"""
        return any([(1 if self.is_log(fn) else 0)
                    for fn in os.listdir(self.labber_data_dir)])

    def log_name(self, index: int) -> str:
        """Gets the log name associated with an index"""
        return f'_{self.feature_id}_{index}'.join(
            os.path.splitext(self.log_name_template))

    def log_path(self, index: int) -> str:
        """Gets the log path associated with an index"""
        return os.path.join(self.labber_data_dir, self.log_name(index))

    def get_index(self, file_name: str) -> Optional[int]:
        """Gets the index of a filename if it is an output log name, or None if
        it is not."""
        root, ext = os.path.splitext(self.log_name_template)
        pattern = f'^{root}_{self.feature_id}_(\\d+){ext}$'
        m = re.search(pattern, file_name)
        return int(m.groups()[0]) if m else None

    def is_log(self, file_name: str) -> bool:
        """Checks if a file name corresponds to an output file in the history"""
        return self.get_index(file_name) is not None

    def get_log_paths(self) -> List[str]:
        """Gets all the logs saved in the labber data directory"""
        return [os.path.join(self.labber_data_dir, p)
                for p in os.listdir(self.labber_data_dir)
                if self.is_log(p)]

    def next_log_path(self) -> str:
        """Gets the path of the next log file to be created"""
        return self.log_path(len(self))


class LabberFeature(Feature):
    """Feature class specialized for integration with Labber.
    Init takes the address of a labber server as an argument.
    """

    # Deserialized template file
    template = me.DictField(default={})
    template_diffs = me.DictField(default={})
    # TODO note Mongodb docs on performance of ReferenceFields
    results = me.ListField(me.DictField(), default=list)
    template_file_path = ''
    output_file_path = ''

    def __init__(self, **kwargs):

        super().__init__(**kwargs)
        # Check to see if the template file has been saved in the DySART database;
        # if not, deserialize the .hdf5 on disk.
        if not self.template:
            self.deserialize_template()

        # Deprecated by Simon's changes to Labber API?
        self.config = st.MeasurementObject(self.template_file_path,
                                           self.output_file_path)

        self.log_history = LogHistory(self.id,
                                      conf.config['labber_data_dir'],
                                      os.path.split(self.output_file_path)[-1])

    def __params__(self):
        """Fixes parameters that may be inherited from parents.

        Returns: A dictionary containing the new parameter settings

        """
        return {}

    async def __call__(self):
        """Thinly wrap the Labber API
        """
        # Handle the keyword arguments by appropriately modifying the config
        # file. This is sort of a stopgap; I'm not really sure it behaves how
        # we want in production.
        # Make call to Labber!
        self.labber_input_file = self.emit_labber_input_file()
        self.labber_output_file = self.log_history.next_log_path()
        async with LOCK:
            self.config.performMeasurement()
        # Clean up: tempfile no longer needed.
        os.unlink(self.labber_input_file)

    def expiry_override(self) -> bool:
        """A hard override function that may be overridden (excuse me) by
        subclasses to provide additional incontrovertible expiry conditions,
        such as the absence of an existing measurement result.

        Returns:

        """
        expired = super().expiry_override()
        return expired or (len(self.log_history) == 0)

    def result_methods(self) -> List[callable]:
        """Gets a list of all the methods of this class annotated with @result
        """
        return [getattr(self, name) for name in dir(self)
                if isinstance(getattr(self, name, None), result)]

    def _repr_dict_(self) -> dict:
        """Overriding Feature.repr_table, this method returns a formatted report on the
        easily-representable (i.e. scalar-valued) result methods.
        """
        table = {
            'id': self.id,
        }
        results = self.all_results()
        for key, val in results.items():
            if not isinstance(val, numbers.Number):
                results[key] = 'Non-numeric result'
        table['results'] = results
        table['diffs'] = self.template_diffs
        return table

    def all_results(self, index=-1) -> dict:
        """Returns a dict containing all the result values, even if they haven't been
        computed before."""
        d = {}
        for method in self.result_methods():
            d[method.__name__] = method(index=index)
        return d

    def get_results_history(self, result_name: str):
        """Returns a generator containing all the historical values measured for a
        single result method"""

        # use reversed range rather than negative indices to avoid double
        # counting if new results are added between __next__() calls
        return (self.results[index].get(result_name)
                for index in reversed(range(len(self.results))))

    def deserialize_template(self):
        """Unmarshall the template file.
        """

        """
        # Check if it's an .hdf5 or .json: for now, do this naively
        # by looking at the file extension.
        if self.template_file_path.endswith('.hdf5'):
            self.template = import_h5(self.template_file_path)
        elif self.template_file_path.endswith('.json'):
            with open('self.template_file_path', 'r') as f:
                self.template = json.loads(f.read())
        """
        self.template = load_labber_scenario_as_dict(self.template_file_path,
                            decode_complex=False)

    @exposed
    def set_value(self, label, value):
        """Simply wrap the Labber API
        """

        # Explicit type-checking (and behavior dependent on the result) seems really
        # not in the spirit of duck-typing.
        # I'm actually really not super happy with this, but it's what people asked for.
        # TODO Maybe I should argue against it.
        if isinstance(value, list):
            canonicalized_value = value
        elif isinstance(value, np.ndarray):
            canonicalized_value = list(value)
        elif isinstance(value, tuple):
            canonicalized_value = value
        elif isinstance(value, (int, float, complex)):
            canonicalized_value = value
        #    self.config.updateValue(label, canonicalized_value)
        else:
            Exception("I don't know what to do with this value")

        self.template_diffs[label] = canonicalized_value
        self.manual_expiration_switch = True
        self.save()

    @exposed
    def unset_value(self, label: str) -> None:
        """Remove a label from the diffs

        Args:
            label: The label of a value to set

        Returns:

        """
        del self.template_diffs[label]
        self.manual_expiration_switch = True
        self.save()

    @exposed
    def unset_diffs(self) -> None:
        """Drop all diffs currently set.
        """
        self.template_diffs = {}
        self.manual_expiration_switch = True
        self.save()

    def merge_configs(self):
        """Merge the template and diff configuration dictionaries, in preparation
        for serialization
        """
        new_config = copy.deepcopy(self.template)
        diffs = {**self.template_diffs, **self.__params__()}
        for diff_key, diff_val in diffs.items():
            # Resolve a 3-tuple as (start, stop, n_pts).
            # For now, let's *only* handle linear interpolation
            if isinstance(diff_val, tuple):
                labber_util.merge_tuple(new_config, diff_key, diff_val)
            elif isinstance(diff_val, (int, float)):
                labber_util.merge_scalar(new_config, diff_key, diff_val)
        return new_config

    def emit_labber_input_file(self) -> str:
        """TODO write a real docstring here
        Write a temporary .hdf5 input for Labber to consume by attempting to
        combine the template and template_diffs. Under Unix this gets written
        to a tempfile in the enclosing /proc subtree. Windows should use a
        spooled file, which I think "really exists" on that platform.

        Returns a path to the resulting tempfile.

        TODO UPDATE: the /proc tree doesn't exist on MacOS. Must use a
        different interface on that platform.
        """
        if hasattr(self, 'temp') and not self.temp.closed:
            self.temp.close()
        temp_dir = '/tmp' if platform.system() in ['Linux', 'Darwin']\
                          else 'C:\\Windows\\Temp'
        temp = tempfile.NamedTemporaryFile(delete=False,
            mode='w+b', dir=temp_dir, suffix='.labber')
        fn = temp.name
        temp.close()

        # Merge the template and diffs; write to the tempfile
        save_labber_scenario_from_dict(fn, self.merge_configs())
        return fn

    @exposed
    def diffs(self):
        """TODO write a real docstring here
        TODO write a real method here
        Pretty-print all the user-specified configuration parameters that
        differ from the template file
        """
        return self.template_diffs

    @property
    def labber_input_file(self):
        return self.config.sCfgFileIn

    @labber_input_file.setter
    def labber_input_file(self, x):
        self.config.sCfgFileIn = x

    @property
    def labber_output_file(self):
        return self.config.sCfgFileOut

    @labber_output_file.setter
    def labber_output_file(self, x):
        self.config.sCfgFileOut = x
