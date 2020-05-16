"""
Top-level DySART feature class. Defines dependency-solving behavior at a high
level of abstraction, deferring implementation of update conditions to user-
defined subclasses.

The Feature interface is designed to be as nearly invisible as possible. The
rationale goes like this: someone---somewhere, somewhen---has to explicitly
specify the device-property dependency graph. This person (the "user") should be
protected only from thinking about more than one system layer _at a time_.
Later on, some other scientist might like to take a far-downstream measurement
without having to think about _anything_ more than the highest layer defined by
the last person who touched the system.
"""

import datetime as dt
import enum
import hashlib
import inspect
import sys
from typing import *

import mongoengine as me

import dysart.messages.messages as messages
from dysart.records import RequestRecord


class ExpirationStatus(enum.Enum):
    FRESH = enum.auto()
    EXPIRED = enum.auto()


class exposed:
    """This decorator class annotates a method that is exposed by the client-
    facing API.
    """

    exposed = True

    def __init__(self, fn: Callable) -> None:
        self.fn = fn
        self.__name__ = fn.__name__
        self.__doc__ = fn.__doc__

    def __get__(self, obj, objtype):
        """Hack to bind this callable to the parent object.
        """
        self.obj = obj
        return self

    def __call__(self, *args, **kwargs):
        return self.fn(self.obj, *args, **kwargs)


def refresh(fn):
    """Decorator that flags a method as having dependencies and possibly requiring
    a refresh operation. Refresh methods are always exposed.
    """
    fn.exposed = True
    fn.is_refresh = True
    return fn


class Feature(me.Document):
    """
    Device or component feature class. Each measurement should be implemented
    as an instance of this class.
    """

    call_message = 'updating feature'
    meta = {'allow_inheritance': True}

    # Feature payload. One of the drawbacks of using mongoengine is that the
    # structure of the payload is pretty constrained, and on top of that I
    # don't really know how it is represented in the database.
    # Hence "data" as a dict blob.
    id = me.StringField(default='', required=True, primary_key=True)
    manual_expiration_switch = me.BooleanField(default=False)
    is_stale_func = me.StringField(max_length=60)
    refresh_func = me.StringField(max_length=60)
    parent_ids = me.DictField(default={})

    def __init__(self, **kwargs):
        # Create a new document
        super().__init__(**kwargs)
        self.save()

    @exposed
    def tree(self) -> str:
        """Produce a pretty-printed tree of all this Feature's
        dependents.
        """
        return messages.tree(self, lambda obj: self.parents.values())

    def _properties(self):
        """
        TODO real _properties docstring
        Return a list of all the refresh methods of the feature
        """
        class_dict = self.__class__.__dict__
        return [p for p in class_dict if hasattr(class_dict[p], 'is_refresh') and
                not p.startswith('_')]  # ignore self._collections

    @exposed
    def properties(self):
        """
        TODO real properties docstring
        TODO rename this; it may be confused with the property decorator
        Pretty-print a human-readable description of all the object's property
        methods
        """
        for prop in self._properties():
            messages.pprint_func(prop, self.__class__.__dict__[prop].__doc__)

    @exposed
    def call_records(self, **kwargs) -> me.QuerySet:
        """Return a list of CallRecords associated with this Feature.

        """
        try:
            return CallRecord.objects(feature=self, **kwargs)
        except me.errors.InvalidQueryError as e:
            # TODO: use a standard error-reporting method in the messages module.
            print("Invalid call record field: allowedfields are TODO", sys.stderr)

    def pprint_call_records(self) -> list:
        """Pretty-print a list of CallRecords associated with this Feature
        """
        for record in self.call_records():
            print(record)

    def __call__(self):
        """Feature is callable. This method does whatever is needed to update an
        expired feature. By default, calling the instance only refreshes and
        logs. Unless overwritten, just updates the time-since-refresh and
        returns itself. This strikes me as a convenient expression of intent
        in some ways, but it's also a little unpythonic: "explicit is better
        than implicit".

        """
        return

    @exposed
    def set_expired(self, is_expired: bool = True) -> None:
        """Provide an interface to manually set the expiration state of a feature.
        """
        self.manual_expiration_switch = is_expired
        self.save()
    
    async def exec_async_dunder(self, hook: str, *args) -> Any:
        """Executes a named hook, if one exists, whether it is synchronous or
        async. Otherwise, do nothing.
        
        Args:
            hook: The name of the hook (without dunders)
            *args: Arguments to the hook

        Returns: Return value, if any

        """
        hook = getattr(self, f"__{hook}__", None)
        if hook:
            if inspect.iscoroutinefunction(hook):
                return await hook(*args)
            else:
                return hook(*args)

    async def exec_feature(self, record):
        """

        Args:
            record:

        Returns:

        """
        record.setup()
        try:
            await self.exec_async_dunder('pre_hook', record)
            # Call the feature.
            return_value = await self.exec_async_dunder('call')
            await self.exec_async_dunder('validation_hook', record, return_value)
            self.manual_expiration_switch = False
            self.save()

            # Propagate expiration to children, no matter what. This ensures that,
            # even if they aren't presently scheduled, the children will be aware
            # that their parent has expired.
            for child in self.children:
                child.set_expired()

            record.conclude(CallStatus.DONE)
        except Exception as e:
            record.conclude(CallStatus.FAILED)
            raise e
        await self.exec_async_dunder('post_hook', record)
        return return_value

    @property
    def parents(self) -> Dict[str, "Feature"]:
        """How parents should be accessed by clients. This property is a mapping
        from parent keys, which identify the relationship, purpose or intent of a
        parent object, to the corresponding parent objects.

        """
        return {key: self._get_parent(key) for key in self.parent_ids}

    def _get_parent(self, parent_key: str):
        """Returns the parent associated with a parent key. This is an internal
        method used by the `parents` property.

        Args:
            parent_key: The parent key to look up
        Returns: the parent Feature in the context that is passed in.

        Raises: KeyError

        """
        return self.ctx[self.parent_ids[parent_key]]

    @property
    def children(self) -> List["Feature"]:
        """Retrieves all features in the project that point to this feature as
        a parent.

        Returns: the children of this feature.

        Todo:
            This is an uncareful implementation that takes O(|graph|) time.
            We don't care about such small overhead for now. This could be
            "fixed" in two ways: we could run a database query (which will
            in any case have a larger prefactor), or have each Feature also
            hold references to each of its children.
        """
        return [feature for id, feature in self.ctx.items()
                if self in feature.parents.values()]

    def expiry_override(self) -> bool:
        """A hard override function that may be overridden (excuse me) by
        subclasses to provide additional incontrovertible expiry conditions,
        such as the absence of an existing measurement result.

        Returns:

        """
        return self.manual_expiration_switch

    @exposed
    async def expired(self) -> bool:
        """Says whether this function has either been flagged expired, or has
        met the condition declared by its expiration hook.

        Returns: True if expired, False if not.

        """
        exp_hook_res = (await self.exec_async_dunder('expiration_hook'))
        exp_hook_expired = exp_hook_res == ExpirationStatus.EXPIRED
        return exp_hook_expired or self.expiry_override()

    async def expired_ancestors(self) -> OrderedDict:
        """Returns a list of ancestors that are expired, in topological-sorted
        order, deduplicated, and possibly including the callee.

        Returns:

        """
        acc = OrderedDict({})
        parent_ancestors = [await parent.expired_ancestors()
                            for parent in self.parents.values()]
        for ancestors in parent_ancestors:
            acc.update(ancestors)

        # If any parent is expired, or this one has been flagged, schedule this for refresh.
        expired = await self.expired()
        if expired or len(acc) > 0:
            acc[self] = True
        return acc

    @exposed
    async def awaiting_refresh(self) -> bool:
        """Is any of this feature's ancestors (inclusive!) is expired, it is
        ready to be scheduled for refresh.

        Returns: True if awaiting refresh, False otherwise.

        """
        return len(await self.expired_ancestors()) > 0

    def add_parents(self, new_parents: Dict):
        """Insert dependencies into the feature's parents dictionary and
        write to the database.
        """
        for parent_key, parent_id in new_parents.items():
            self.parent_ids[parent_key] = parent_id
        self.save()

    def exposed_methods(self) -> List[callable]:
        """Gets a list of all the methods of this class annotated with @result
        """
        return [getattr(self, name) for name in dir(self)
                if isinstance(getattr(self, name, None), exposed)]


class CallStatus(enum.Enum):
    START = enum.auto()
    DONE = enum.auto()
    FAILED = enum.auto()
    HALTED = enum.auto()
    WARNING = enum.auto()


class CallRecord(me.Document):
    """
    Uniquely identified (with high probability) by a 40-character hexadecimal
    string.
    
    Todo:
        I can't see how to put this into another source file (say, records.py)
        without having a circular import, but that seems like a desirable thing
        to do.
    """

    meta = {'allow_inheritance': True}

    UUID_LEN = 40

    # Which attributes are included in self.__str__()?
    printed_attrs = ['feature',
                     'start_time',
                     'stop_time',
                     'exit_status',
                     'info',]

    uuid = me.StringField(default='', max_length=UUID_LEN, required=True, primary_key=True)
    feature = me.ReferenceField(Feature, required=True)
    request = me.ReferenceField(RequestRecord, required=False)
    template = me.StringField(default='', required=False)
    template_diffs = me.DictField(default={}, required=False)
    start_time = me.DateTimeField()
    stop_time = me.DateTimeField()
    exit_status = me.StringField(max_length=16)
    info = me.StringField(default='')

    def __init__(self, feature: Feature, request):
        super().__init__()
        self.feature = feature
        if hasattr(feature, 'template_path'):
            self.template = feature.template_path
        if hasattr(feature, 'template_diffs'):
            self.template_diffs = feature.template_diffs
        self.request = request
        self.save()

    def __setattr__(self, key, value):
        """The `exit_status` field should be a string, so handle this
        case in a special way: accept only CallStatus enums, but
        """
        if key == 'exit_status' and type(value) == CallStatus:
            super().__setattr__(key, value.name)
        else:
            super().__setattr__(key, value)

    def setup(self):
        """ This is run to setup the call record before the actual call is
        issued.

        Note that the contents of this method *cannot* go into init, as this
        causes a slightly subtle but disastrous bug that leads to exponential
        blowup in the number of CallRecords, as each time a record is accessed
        it forks once it receives a new uuid.
        """
        self.start_time = dt.datetime.now()
        self.__gen_uuid()
        self.save()

    def conclude(self, status: CallStatus):
        """ ...and this one tears it down
        """
        self.exit_status = status
        self.stop_time = dt.datetime.now()
        self.save()

    def __str__(self):
        s = self.uuid[:16] + '...\n'
        printed_attrs = [attr for attr in CallRecord.printed_attrs
                         if hasattr(self, attr)]
        max_attr_len = max(map(len, printed_attrs))
        for attr in printed_attrs:
            val = getattr(self, attr)
            if isinstance(val, Feature):
                val = val.id
            s += ' ' + messages.cstr(attr, 'italic') + ' ' * (max_attr_len - len(attr))\
                     + ' : {}\n'.format(val)
        return s

    def __gen_uuid(self):
        """Creates a unique ID for the call record and assigned.

        Must be called after assigning a start time.

        Note that I am not using a hash function instead of the `uuid` module
        from the standard library because we specifically want a pseudorandom
        string with non-predictable values even in the most significant digits
        """
        h = hashlib.sha1(self.feature.id.encode('utf-8'))
        h.update(str(self.start_time).encode('utf-8'))
        if self.request:
            h.update(self.request.uuid.encode('utf-8'))
        self.uuid = h.hexdigest()[:CallRecord.UUID_LEN]


def get_records_by_uid_pre(uid_pre):
    """Takes a uid prefix and searches for a record whose uid contains this
    substring.
    """
    matches = CallRecord.objects(uid__istartswith=uid_pre)
    return matches


def get_records_by_uid_sub(uid_sub):
    """Takes a uid /sub/string and searches for a record whose uid contains this
    substring.
    """
    matches = CallRecord.objects(uid__icontains=uid_sub)
    return matches
