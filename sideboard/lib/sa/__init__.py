from __future__ import unicode_literals
import json
import uuid
import re
import types
import inspect

import sqlalchemy
from sqlalchemy import event
from sqlalchemy.dialects import sqlite  # TODO: improve our import overrides such that this is no longer necessary
from sqlalchemy.ext import declarative
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import sessionmaker, configure_mappers
from sqlalchemy.types import TypeDecorator, String, DateTime, CHAR, Unicode

from sideboard.lib import log, config

__all__ = [b'UUID', b'JSON', b'CoerceUTF8', b'declarative_base', b'SessionManager',
           b'CrudException', b'crudable', b'crud_validation', b'text_length_validation', b'regex_validation']


def _camelcase_to_underscore(value):
    """ Converts camelCase string to underscore_separated (aka joined_lower).

    >>> _camelcase_to_underscore('fooBarBaz')
    'foo_bar_baz'
    >>> _camelcase_to_underscore('fooBarBazXYZ')
    'foo_bar_baz_xyz'
    """
    s1 = re.sub(r'(.)([A-Z][a-z]+)', r'\1_\2', value)
    return re.sub(r'([a-z0-9])([A-Z])', r'\1_\2', s1).lower()


def _underscore_to_camelcase(value, cap_segment=None):
    """ Converts underscore_separated string (aka joined_lower) into camelCase string.

    >>> _underscore_to_camelcase('foo_bar_baz')
    'FooBarBaz'
    >>> _underscore_to_camelcase('foo_bar_baz', cap_segment=0)
    'FOOBarBaz'
    >>> _underscore_to_camelcase('foo_bar_baz', cap_segment=1)
    'FooBARBaz'
    >>> _underscore_to_camelcase('foo_bar_baz', cap_segment=1000)
    'FooBarBaz'
    """
    return "".join([s.title() if idx != cap_segment else s.upper() for idx, s in enumerate(value.split('_'))])


class CoerceUTF8(TypeDecorator):
    """
    Safely coerce Python bytestrings to Unicode
    before passing off to the database.
    """
    impl = Unicode

    def process_bind_param(self, value, dialect):
        if isinstance(value, str):
            value = value.decode('utf-8')
        return value


class UUID(TypeDecorator):
    """
    Platform-independent UUID type.
    Uses Postgresql's UUID type, otherwise uses
    CHAR(32), storing as stringified hex values.
    """
    impl = CHAR

    def load_dialect_impl(self, dialect):
        if dialect.name == 'postgresql':
            return dialect.type_descriptor(postgresql.UUID())
        else:
            return dialect.type_descriptor(CHAR(32))

    def process_bind_param(self, value, dialect):
        if value is None:
            return value
        elif dialect.name == 'postgresql':
            return str(value)
        else:
            if not isinstance(value, uuid.UUID):
                return '%.32x' % uuid.UUID(value)
            else:
                return '%.32x' % value

    def process_result_value(self, value, dialect):
        if value is None:
            return value
        else:
            return str(uuid.UUID(value))


class JSON(TypeDecorator):
    impl = String

    def __init__(self, comparator=None):
        self.comparator = comparator
        super(JSON, self).__init__()

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        elif isinstance(value, basestring):
            return value
        else:
            return json.dumps(value)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return json.loads(str(value))

    def copy_value(self, value):
        if self.mutable:
            return json.loads(json.dumps(value))
        else:
            return value

    def compare_values(self, x, y):
        if self.comparator:
            return self.comparator(x, y)
        else:
            return x == y


try:
    from pytz import UTC
except ImportError:
    pass
else:
    class UTCDateTime(TypeDecorator):
        impl = DateTime

        def process_bind_param(self, value, engine):
            if value is not None:
                return value.astimezone(UTC).replace(tzinfo=None)

        def process_result_value(self, value, engine):
            if value is not None:
                return value.replace(tzinfo=UTC)

    __all__.append(b'UTCDateTime')


def declarative_base(klass):
    class Mixed(klass, CrudMixin):
        pass

    Mixed = declarative.declarative_base(cls=Mixed)
    Mixed.BaseClass = _SessionInitializer.BaseClass = Mixed
    Mixed.__tablename__ = declarative.declared_attr(lambda cls: _camelcase_to_underscore(cls.__name__))
    return Mixed


class _SessionInitializer(type):
    def __new__(cls, name, bases, attrs):
        SessionClass = type.__new__(cls, name, bases, attrs)
        if hasattr(SessionClass, 'engine'):
            assert hasattr(SessionClass, 'BaseClass'), 'no BaseClass specified and @declarative_base was never invoked'
            if not hasattr(SessionClass, 'session_factory'):
                SessionClass.session_factory = sessionmaker(bind=SessionClass.engine, autoflush=False, autocommit=False)
            SessionClass.initialize_db()
            SessionClass.crud = make_crud_service(SessionClass)
        return SessionClass


class SessionManager(object):
    __metaclass__ = _SessionInitializer

    class SessionMixin(object):
        pass

    def __init__(self):
        self.session = self.session_factory()
        for name, val in self.SessionMixin.__dict__.items():
            if not name.startswith('__'):
                assert not hasattr(self.session, name) and hasattr(val, '__call__')
                setattr(self.session, name, types.MethodType(val, self.session))

    def __enter__(self):
        return self.session

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            if exc_type is None:
                self.session.commit()
        finally:
            self.session.close()

    def __del__(self):
        if self.session.transaction._connections:
            log.error('SessionManager went out of scope without underlying connection being closed; did you forget to use it as a context manager?')
            self.session.close()

    @classmethod
    def initialize_db(cls, drop=False):
        configure_mappers()
        cls.BaseClass.metadata.bind = cls.engine
        if drop:
            cls.BaseClass.metadata.drop_all(cls.engine, checkfirst=True)
        cls.BaseClass.metadata.create_all(cls.engine, checkfirst=True)

    @classmethod
    def all_models(cls):
        return cls.BaseClass.__subclasses__()   # TODO: subclasses of subclasses; this needs to be recursive or something

    @classmethod
    def resolve_model(cls, name):
        if inspect.isclass(name) and issubclass(name, cls.BaseClass):
            return name

        subclasses = {ModelClass.__name__: ModelClass for ModelClass in cls.all_models()}
        permutations = [name, _underscore_to_camelcase(name), _underscore_to_camelcase(name, cap_segment=0)]
        for name in permutations:
            if name in subclasses:
                return subclasses[name]

            if name.lower().endswith('s'):
                singular = name.rstrip('sS')
                if singular in subclasses:
                    return subclasses[singular]

            if name.lower().endswith('ies'):
                singular = name[:-3] + 'sy'  # TODO: sy looks like a typo, and we need to either make this better or get rid of it
                if singular in subclasses:
                    return subclasses[singular]

        for name in permutations:
            if name in cls.BaseClass.metadata.tables:
                return cls.BaseClass.metadata.tables[name]

        raise ValueError('Unrecognized model: {}'.format(name))

from sideboard.lib.sa._crud import CrudMixin, make_crud_service, crudable, CrudException, crud_validation, text_length_validation, regex_validation
