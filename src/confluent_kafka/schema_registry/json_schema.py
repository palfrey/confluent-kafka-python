#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# Copyright 2020 Confluent Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from io import BytesIO

import json
import struct
from typing import Any, Callable, Dict, Optional, Set, cast

from jsonschema import validate, ValidationError

from confluent_kafka.schema_registry import (_MAGIC_BYTE,
                                             Schema,
                                             topic_subject_name_strategy)
from .schema_registry_client import SchemaRegistryClient
from confluent_kafka.serialization import (SerializationContext, SerializationError,
                                           Deserializer,
                                           Serializer)


class _ContextStringIO(BytesIO):
    """
    Wrapper to allow use of StringIO via 'with' constructs.
    """

    def __enter__(self) -> "_ContextStringIO":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()


class JSONSerializer(Serializer):
    """
    Serializer that outputs JSON encoded data with Confluent Schema Registry framing.

    Configuration properties:

    +---------------------------+----------+----------------------------------------------------+
    | Property Name             | Type     | Description                                        |
    +===========================+==========+====================================================+
    |                           |          | If True, automatically register the configured     |
    | ``auto.register.schemas`` | bool     | schema with Confluent Schema Registry if it has    |
    |                           |          | not previously been associated with the relevant   |
    |                           |          | subject (determined via subject.name.strategy).    |
    |                           |          |                                                    |
    |                           |          | Defaults to True.                                  |
    |                           |          |                                                    |
    |                           |          | Raises SchemaRegistryError if the schema was not   |
    |                           |          | registered against the subject, or could not be    |
    |                           |          | successfully registered.                           |
    +---------------------------+----------+----------------------------------------------------+
    |                           |          | Whether to normalize schemas, which will           |
    | ``normalize.schemas``     | bool     | transform schemas to have a consistent format,     |
    |                           |          | including ordering properties and references.      |
    +---------------------------+----------+----------------------------------------------------+
    |                           |          | Whether to use the latest subject version for      |
    | ``use.latest.version``    | bool     | serialization.                                     |
    |                           |          |                                                    |
    |                           |          | WARNING: There is no check that the latest         |
    |                           |          | schema is backwards compatible with the object     |
    |                           |          | being serialized.                                  |
    |                           |          |                                                    |
    |                           |          | Defaults to False.                                 |
    +---------------------------+----------+----------------------------------------------------+
    |                           |          | Callable(SerializationContext, str) -> str         |
    |                           |          |                                                    |
    | ``subject.name.strategy`` | callable | Defines how Schema Registry subject names are      |
    |                           |          | constructed. Standard naming strategies are        |
    |                           |          | defined in the confluent_kafka.schema_registry     |
    |                           |          | namespace.                                         |
    |                           |          |                                                    |
    |                           |          | Defaults to topic_subject_name_strategy.           |
    +---------------------------+----------+----------------------------------------------------+

    Schemas are registered against subject names in Confluent Schema Registry that
    define a scope in which the schemas can be evolved. By default, the subject name
    is formed by concatenating the topic name with the message field (key or value)
    separated by a hyphen.

    i.e. {topic name}-{message field}

    Alternative naming strategies may be configured with the property
    ``subject.name.strategy``.

    Supported subject name strategies:

    +--------------------------------------+------------------------------+
    | Subject Name Strategy                | Output Format                |
    +======================================+==============================+
    | topic_subject_name_strategy(default) | {topic name}-{message field} |
    +--------------------------------------+------------------------------+
    | topic_record_subject_name_strategy   | {topic name}-{record name}   |
    +--------------------------------------+------------------------------+
    | record_subject_name_strategy         | {record name}                |
    +--------------------------------------+------------------------------+

    See `Subject name strategy <https://docs.confluent.io/current/schema-registry/serializer-formatter.html#subject-name-strategy>`_ for additional details.

    Notes:
        The ``title`` annotation, referred to elsewhere as a record name
        is not strictly required by the JSON Schema specification. It is
        however required by this serializer in order to register the schema
        with Confluent Schema Registry.

        Prior to serialization, all objects must first be converted to
        a dict instance. This may be handled manually prior to calling
        :py:func:`Producer.produce()` or by registering a `to_dict`
        callable with JSONSerializer.

    Args:
        schema_str (str): `JSON Schema definition. <https://json-schema.org/understanding-json-schema/reference/generic.html>`_

        schema_registry_client (SchemaRegistryClient): Schema Registry
            client instance.

        to_dict (callable, optional): Callable(object, SerializationContext) -> dict.
            Converts object to a dict.

        conf (dict): JsonSerializer configuration.
    """  # noqa: E501
    __slots__ = ['_hash', '_auto_register', '_normalize_schemas', '_use_latest_version',
                 '_known_subjects', '_parsed_schema', '_registry', '_schema', '_schema_id',
                 '_schema_name', '_subject_name_func', '_to_dict']

    _default_conf = {'auto.register.schemas': True,
                     'normalize.schemas': False,
                     'use.latest.version': False,
                     'subject.name.strategy': topic_subject_name_strategy}

    def __init__(self, schema_str: str, schema_registry_client: SchemaRegistryClient, to_dict: Optional[Callable[[object, SerializationContext], Dict]]=None, conf: Optional[Dict]=None):
        self._registry = schema_registry_client
        self._schema_id: Optional[int] = None
        self._known_subjects: Set[str] = set()

        if to_dict is not None and not callable(to_dict):
            raise ValueError("to_dict must be callable with the signature "
                             "to_dict(object, SerializationContext)->dict")

        self._to_dict = to_dict

        conf_copy = self._default_conf.copy()
        if conf is not None:
            conf_copy.update(conf)

        self._auto_register = conf_copy.pop('auto.register.schemas')
        if not isinstance(self._auto_register, bool):
            raise ValueError("auto.register.schemas must be a boolean value")

        self._normalize_schemas = conf_copy.pop('normalize.schemas')
        if not isinstance(self._normalize_schemas, bool):
            raise ValueError("normalize.schemas must be a boolean value")

        self._use_latest_version = conf_copy.pop('use.latest.version')
        if not isinstance(self._use_latest_version, bool):
            raise ValueError("use.latest.version must be a boolean value")
        if self._use_latest_version and self._auto_register:
            raise ValueError("cannot enable both use.latest.version and auto.register.schemas")

        self._subject_name_func: Callable = cast(Callable, conf_copy.pop('subject.name.strategy'))
        if not callable(self._subject_name_func):
            raise ValueError("subject.name.strategy must be callable")

        if len(conf_copy) > 0:
            raise ValueError("Unrecognized properties: {}"
                             .format(", ".join(conf_copy.keys())))

        schema_dict = json.loads(schema_str)
        schema_name = schema_dict.get('title', None)
        if schema_name is None:
            raise ValueError("Missing required JSON schema annotation title")

        self._schema_name = schema_name
        self._parsed_schema = schema_dict
        self._schema = Schema(schema_str, schema_type="JSON")

    def __call__(self, obj: Any, ctx: SerializationContext) -> Optional[bytes]:
        """
        Serializes an object to JSON, prepending it with Confluent Schema Registry
        framing.

        Args:
            obj (object): The object instance to serialize.

            ctx (SerializationContext): Metadata relevant to the serialization
                operation.

        Raises:
            SerializerError if any error occurs serializing obj.

        Returns:
            bytes: None if obj is None, else a byte array containing the JSON
            serialized data with Confluent Schema Registry framing.
        """

        if obj is None:
            return None

        subject = self._subject_name_func(ctx, self._schema_name)

        if subject not in self._known_subjects:
            if self._use_latest_version:
                latest_schema = self._registry.get_latest_version(subject)
                self._schema_id = latest_schema.schema_id

            else:
                # Check to ensure this schema has been registered under subject_name.
                assert isinstance(self._normalize_schemas, bool)
                if self._auto_register:
                    # The schema name will always be the same. We can't however register
                    # a schema without a subject so we set the schema_id here to handle
                    # the initial registration.
                    self._schema_id = self._registry.register_schema(subject,
                                                                     self._schema,
                                                                     self._normalize_schemas)
                else:
                    registered_schema = self._registry.lookup_schema(subject,
                                                                     self._schema,
                                                                     self._normalize_schemas)
                    self._schema_id = registered_schema.schema_id
            self._known_subjects.add(subject)

        if self._to_dict is not None:
            value = self._to_dict(obj, ctx)
        else:
            value = obj

        try:
            validate(instance=value, schema=self._parsed_schema)
        except ValidationError as ve:
            raise SerializationError(ve.message)

        with _ContextStringIO() as fo:
            # Write the magic byte and schema ID in network byte order (big endian)
            fo.write(struct.pack('>bI', _MAGIC_BYTE, self._schema_id))
            # JSON dump always writes a str never bytes
            # https://docs.python.org/3/library/json.html
            fo.write(json.dumps(value).encode('utf8'))

            return fo.getvalue()


class JSONDeserializer(Deserializer):
    """
    Deserializer for JSON encoded data with Confluent Schema Registry
    framing.

    Args:
        schema_str (str): `JSON schema definition <https://json-schema.org/understanding-json-schema/reference/generic.html>`_ use for validating records.

        from_dict (callable, optional): Callable(dict, SerializationContext) -> object.
            Converts a dict to a Python object instance.
    """  # noqa: E501

    __slots__ = ['_parsed_schema', '_from_dict']

    def __init__(self, schema_str: str, from_dict: Optional[Callable]=None):
        self._parsed_schema = json.loads(schema_str)

        if from_dict is not None and not callable(from_dict):
            raise ValueError("from_dict must be callable with the signature"
                             " from_dict(dict, SerializationContext) -> object")

        self._from_dict = from_dict

    def __call__(self, data: Optional[bytes], ctx: Optional[SerializationContext]=None) -> Any:
        """
        Deserialize a JSON encoded record with Confluent Schema Registry framing to
        a dict, or object instance according to from_dict if from_dict is specified.

        Args:
            data (bytes): A JSON serialized record with Confluent Schema Regsitry framing.

            ctx (SerializationContext): Metadata relevant to the serialization operation.

        Returns:
            A dict, or object instance according to from_dict if from_dict is specified.

        Raises:
            SerializerError: If there was an error reading the Confluent framing data, or
               if ``data`` was not successfully validated with the configured schema.
        """

        if data is None:
            return None

        if len(data) <= 5:
            raise SerializationError("Expecting data framing of length 6 bytes or "
                                     "more but total data size is {} bytes. This "
                                     "message was not produced with a Confluent "
                                     "Schema Registry serializer".format(len(data)))

        with _ContextStringIO(data) as payload:
            magic, schema_id = struct.unpack('>bI', payload.read(5))
            if magic != _MAGIC_BYTE:
                raise SerializationError("Unexpected magic byte {}. This message "
                                         "was not produced with a Confluent "
                                         "Schema Registry serializer".format(magic))

            # JSON documents are self-describing; no need to query schema
            obj_dict = json.loads(payload.read())

            try:
                validate(instance=obj_dict, schema=self._parsed_schema)
            except ValidationError as ve:
                raise SerializationError(ve.message)

            if self._from_dict is not None:
                return self._from_dict(obj_dict, ctx)

            return obj_dict
