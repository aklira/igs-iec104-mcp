"""Decision D-002: definitions are read with the safe YAML loader.

The loader must refuse tags that construct Python objects; a definition file
must never run code.
"""

import pytest
from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError


def test_safe_loader_refuses_python_object_tags() -> None:
    yaml = YAML(typ="safe")
    document = "command: !!python/object/apply:os.system ['true']\n"
    with pytest.raises(YAMLError):
        yaml.load(document)


def test_safe_loader_reads_plain_definitions() -> None:
    yaml = YAML(typ="safe")
    assert yaml.load("schema_version: 1\nname: basic\n") == {
        "schema_version": 1,
        "name": "basic",
    }
