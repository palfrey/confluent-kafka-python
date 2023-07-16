# Copyright 2022 Confluent Inc.
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

from typing import List, Optional
from ..cimpl import KafkaError

import six

class ValidationUtil:
    @staticmethod
    def check_multiple_not_none(obj: object, vars_to_check: List[str]) -> None:
        for param in vars_to_check:
            ValidationUtil.check_not_none(obj, param)

    @staticmethod
    def check_not_none(obj: object, param: str) -> None:
        if getattr(obj, param) is None:
            raise ValueError("Expected %s to be not None" % (param,))

    @staticmethod
    def check_multiple_is_string(obj: object, vars_to_check: List[str]) -> None:
        for param in vars_to_check:
            ValidationUtil.check_is_string(obj, param)

    @staticmethod
    def check_is_string(obj: object, param: str) -> None:
        param_value = getattr(obj, param)
        if param_value is not None and not isinstance(param_value, six.string_types):
            raise TypeError("Expected %s to be a string" % (param,))

    @staticmethod
    def check_kafka_errors(errors: Optional[List]) -> None:
        if not isinstance(errors, list):
            raise TypeError("errors should be None or a list")
        for error in errors:
            if not isinstance(error, KafkaError):
                raise TypeError("Expected list of KafkaError")

    @staticmethod
    def check_kafka_error(error: object) -> None:
        if not isinstance(error, KafkaError):
            raise TypeError("Expected error to be a KafkaError")
