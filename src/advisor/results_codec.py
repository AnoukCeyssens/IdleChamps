"""Formation Recommendations as plain JSON, for the Store's results file.

Generic over the advisor's frozen dataclasses and enums, so fields later tickets add are written
without changes here. Catalogue entities (champions, adventures, patrons, feats) are written as
references by id and read back from the Catalogue the results were computed on (same definitions
checksum); anything the Catalogue doesn't hold as-is is written by value. Only classes from the
`advisor` package are ever constructed when reading.
"""

from __future__ import annotations

import dataclasses
import enum
import importlib
from collections.abc import Mapping
from typing import Any

from advisor.catalogue import Adventure, Catalogue, Champion, Feat, Patron


def _indexes(catalogue: Catalogue) -> dict[type, Mapping[int, Any]]:
    return {
        Champion: {c.id: c for c in catalogue.champions},
        Adventure: {a.id: a for a in catalogue.adventures},
        Patron: dict(catalogue.patrons),
        Feat: dict(catalogue.feats),
    }


class Encoder:
    def __init__(self, catalogue: Catalogue) -> None:
        self._index = _indexes(catalogue)

    def __call__(self, value: Any) -> Any:
        """JSON-ready data for `value`; TypeError for anything it can't write."""
        if value is None or isinstance(value, (bool, int, float, str)):
            return value
        if isinstance(value, enum.Enum):
            return {"$enum": _name(type(value)), "value": self(value.value)}
        if dataclasses.is_dataclass(value) and not isinstance(value, type):
            index = self._index.get(type(value))
            if index is not None and index.get(getattr(value, "id", None)) == value:
                return {"$ref": type(value).__name__, "id": value.id}
            fields = {f.name: self(getattr(value, f.name)) for f in dataclasses.fields(value) if f.init}
            return {"$dc": _name(type(value)), "fields": fields}
        if isinstance(value, (tuple, list)):
            return [self(v) for v in value]
        if isinstance(value, (set, frozenset)):
            return {"$set": [self(v) for v in value]}
        if isinstance(value, Mapping):
            return {"$map": [[self(k), self(v)] for k, v in value.items()]}
        raise TypeError(f"can't write {type(value).__name__}")


class Decoder:
    def __init__(self, catalogue: Catalogue) -> None:
        self._index = {cls.__name__: index for cls, index in _indexes(catalogue).items()}

    def __call__(self, data: Any) -> Any:
        """The value `Encoder` wrote; KeyError/TypeError/ValueError when it can't be read back."""
        if isinstance(data, list):
            return tuple(self(v) for v in data)
        if not isinstance(data, dict):
            return data
        if "$ref" in data:
            return self._index[data["$ref"]][data["id"]]
        if "$set" in data:
            return frozenset(self(v) for v in data["$set"])
        if "$map" in data:
            return {self(k): self(v) for k, v in data["$map"]}
        if "$enum" in data:
            return _class(data["$enum"])(self(data["value"]))
        if "$dc" in data:
            cls = _class(data["$dc"])
            if not dataclasses.is_dataclass(cls):
                raise TypeError(f"{data['$dc']} is not a dataclass")
            return cls(**{k: self(v) for k, v in data["fields"].items()})
        raise ValueError("unknown value")


def _name(cls: type) -> str:
    return f"{cls.__module__}:{cls.__qualname__}"


def _class(name: str) -> type:
    module, _, qualname = name.partition(":")
    if module != "advisor" and not module.startswith("advisor."):
        raise ValueError(f"not an advisor class: {name}")
    found: Any = importlib.import_module(module)
    for part in qualname.split("."):
        found = getattr(found, part)
    if not isinstance(found, type):
        raise TypeError(f"not a class: {name}")
    return found
