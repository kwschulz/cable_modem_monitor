"""System info section config.

Multi-source system_info with format-discriminated sources.
Per PARSING_SPEC.md System Info section.
"""

from __future__ import annotations

from functools import reduce
from operator import or_
from typing import TYPE_CHECKING, Annotated, Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Discriminator, Tag, model_validator

from .common import FilterValue, _check_field_type
from .format_registry import DecodeKind, FormatModel


class HTMLFieldMapping(BaseModel):
    """A single field extracted from HTML via label text, element id, or CSS."""

    model_config = ConfigDict(extra="forbid")
    field: str
    type: str
    format: str = ""
    label: str = ""
    id: str = ""
    css: str = ""
    pattern: str = ""
    attribute: str = ""
    map: dict[str, str] | None = None
    scale: int | float | None = None

    @model_validator(mode="after")
    def validate_has_locator(self) -> HTMLFieldMapping:
        """Ensure at least one locator (label, id, or css) is provided."""
        if not self.label and not self.id and not self.css:
            raise ValueError("html_fields mapping requires at least one of: label, id, css")
        return self

    @model_validator(mode="after")
    def validate_field_type(self) -> HTMLFieldMapping:
        """Ensure type is a valid FIELD_TYPES value."""
        _check_field_type(self.type)
        return self


class HTMLFieldsSource(BaseModel):
    """html_fields source for system_info."""

    format_tag: ClassVar[str] = "html_fields"
    decode_kind: ClassVar[DecodeKind] = "html"
    transports: ClassVar[frozenset[str]] = frozenset({"http"})

    model_config = ConfigDict(extra="forbid")
    format: Literal["html_fields"]
    resource: str
    fields: list[HTMLFieldMapping]


class HNAPFieldMapping(BaseModel):
    """A single field from an HNAP response."""

    model_config = ConfigDict(extra="forbid")
    source: str
    field: str
    type: str
    format: str = ""
    map: dict[str, str] | None = None
    scale: int | float | None = None

    @model_validator(mode="after")
    def validate_field_type(self) -> HNAPFieldMapping:
        """Ensure type is a valid FIELD_TYPES value."""
        _check_field_type(self.type)
        return self


class HNAPSystemInfoSource(BaseModel):
    """HNAP source for system_info."""

    format_tag: ClassVar[str] = "hnap"
    decode_kind: ClassVar[DecodeKind] = "hnap"
    transports: ClassVar[frozenset[str]] = frozenset({"hnap"})

    model_config = ConfigDict(extra="forbid")
    format: Literal["hnap"]
    response_key: str
    fields: list[HNAPFieldMapping]


class JSSystemInfoFieldMapping(BaseModel):
    """A field extracted from a JS function for system_info."""

    model_config = ConfigDict(extra="forbid")
    offset: int
    field: str
    type: str
    format: str = ""
    map: dict[str, str] | None = None
    scale: int | float | None = None

    @model_validator(mode="after")
    def validate_field_type(self) -> JSSystemInfoFieldMapping:
        """Ensure type is a valid FIELD_TYPES value."""
        _check_field_type(self.type)
        return self


class JSSystemInfoFunction(BaseModel):
    """A JS function that produces system_info fields.

    When ``name`` is empty, the variable is searched at top-level script
    scope instead of inside a named function body. This supports modems
    where ``tagValueList`` is a global variable assignment.
    """

    model_config = ConfigDict(extra="forbid")
    name: str = ""
    delimiter: str
    fields: list[JSSystemInfoFieldMapping]


class JSSystemInfoSource(BaseModel):
    """JavaScript-embedded source for system_info."""

    format_tag: ClassVar[str] = "javascript"
    decode_kind: ClassVar[DecodeKind] = "html"
    transports: ClassVar[frozenset[str]] = frozenset({"http"})

    model_config = ConfigDict(extra="forbid")
    format: Literal["javascript"]
    resource: str
    functions: list[JSSystemInfoFunction]


class JSONSystemInfoFieldMapping(BaseModel):
    """A field extracted from a JSON response for system_info."""

    model_config = ConfigDict(extra="forbid")
    key: str
    field: str
    type: str
    format: str = ""
    path: str = ""
    map: dict[str, str] | None = None
    scale: int | float | None = None

    @model_validator(mode="after")
    def validate_field_type(self) -> JSONSystemInfoFieldMapping:
        """Ensure type is a valid FIELD_TYPES value."""
        _check_field_type(self.type)
        return self


class JSONChildAggregate(BaseModel):
    """Aggregate a value across repeated items in a JSON array.

    Navigates ``array_path`` to a list, unwraps each item's
    ``item_path`` wrapper object when the firmware nests one, keeps
    items matching ``filter``, and takes the ``max`` of the named key.
    Produces a single system_info field.

    Used for DOCSIS service flow extraction (e.g., max provisioned
    speed per direction from a service flow array).

    ``map`` normalizes the raw values of the ``filter`` keys before
    comparison, so wire spellings stay in parser.yaml rather than
    becoming per-modem behaviour in Core.
    """

    model_config = ConfigDict(extra="forbid")
    array_path: str
    item_path: str = ""
    filter: dict[str, FilterValue]
    map: dict[str, str] | None = None
    max: str
    field: str
    type: str
    scale: int | float | None = None

    @model_validator(mode="after")
    def validate_field_type(self) -> JSONChildAggregate:
        """Ensure type is a valid FIELD_TYPES value."""
        _check_field_type(self.type)
        return self


class JSONSystemInfoSource(BaseModel):
    """JSON API source for system_info.

    When the response is a root-level JSON array, the loader wraps it
    as ``{"_raw": [...]}``.  Set ``array_path`` (e.g., ``"_raw"``) to
    navigate to the array and use its first element as the source
    object for field lookups — same concept as the channel parser's
    ``array_path``.
    """

    format_tag: ClassVar[str] = "json"
    decode_kind: ClassVar[DecodeKind] = "json"
    transports: ClassVar[frozenset[str]] = frozenset({"http"})

    model_config = ConfigDict(extra="forbid")
    format: Literal["json"]
    resource: str
    encoding: str = ""
    array_path: str = ""
    fields: list[JSONSystemInfoFieldMapping]
    child_aggregates: list[JSONChildAggregate] = []


class JSVarsFieldMapping(BaseModel):
    """A field extracted from a JS variable assignment for system_info.

    Maps a JS variable name (``source``) to a system_info field name.
    Pattern matches: ``var x = 'value'`` or ``x = 'value'``.
    """

    model_config = ConfigDict(extra="forbid")
    source: str
    field: str
    type: str
    format: str = ""
    map: dict[str, str] | None = None
    scale: int | float | None = None

    @model_validator(mode="after")
    def validate_field_type(self) -> JSVarsFieldMapping:
        """Ensure type is a valid FIELD_TYPES value."""
        _check_field_type(self.type)
        return self


class JSVarsSystemInfoSource(BaseModel):
    """JavaScript variable assignment source for system_info.

    Extracts values from simple JS variable assignments in HTML script
    tags: ``var x = 'value'`` or ``x = 'value'``. Unlike the
    ``javascript`` format (which parses ``tagValueList`` delimited
    strings), this handles standalone named variables.
    """

    format_tag: ClassVar[str] = "javascript_vars"
    decode_kind: ClassVar[DecodeKind] = "html"
    transports: ClassVar[frozenset[str]] = frozenset({"http"})

    model_config = ConfigDict(extra="forbid")
    format: Literal["javascript_vars"]
    resource: str
    fields: list[JSVarsFieldMapping]


class XMLSystemInfoFieldMapping(BaseModel):
    """A field extracted from an XML element for system_info."""

    model_config = ConfigDict(extra="forbid")
    source: str
    field: str
    type: str
    format: str = ""
    map: dict[str, str] | None = None
    scale: int | float | None = None

    @model_validator(mode="after")
    def validate_field_type(self) -> XMLSystemInfoFieldMapping:
        """Ensure type is a valid FIELD_TYPES value."""
        _check_field_type(self.type)
        return self


class XMLChildAggregate(BaseModel):
    """Aggregate a value from repeated XML child elements.

    Iterates ``child_element`` entries under the root, filters by
    ``filter`` key-value pairs, and takes the ``max`` of the named
    sub-element. Produces a single system_info field.

    Used for DOCSIS service flow extraction (e.g., max provisioned
    speed per direction from ``<serviceflow>`` elements).

    ``map`` normalizes the raw text of the ``filter`` keys before
    comparison, so wire spellings stay in parser.yaml rather than
    becoming per-modem behaviour in Core.
    """

    model_config = ConfigDict(extra="forbid")
    child_element: str
    filter: dict[str, str]
    map: dict[str, str] | None = None
    max: str
    field: str
    type: str
    scale: int | float | None = None

    @model_validator(mode="after")
    def validate_field_type(self) -> XMLChildAggregate:
        """Ensure type is a valid FIELD_TYPES value."""
        _check_field_type(self.type)
        return self


class XMLSystemInfoSource(BaseModel):
    """XML element source for system_info."""

    format_tag: ClassVar[str] = "xml"
    decode_kind: ClassVar[DecodeKind] = "xml"
    transports: ClassVar[frozenset[str]] = frozenset({"cbn"})

    model_config = ConfigDict(extra="forbid")
    format: Literal["xml"]
    resource: str
    root_element: str
    fields: list[XMLSystemInfoFieldMapping]
    child_aggregates: list[XMLChildAggregate] = []


def _get_source_format(data: Any) -> str:
    """Extract format from source data for discrimination."""
    if isinstance(data, dict):
        return str(data.get("format", ""))
    return str(getattr(data, "format", ""))


# Single source of truth for system_info source format metadata.
# Adding a source = define the model with its ClassVars and append here.
#
# The static SystemInfoSource alias (visible to mypy/Pyright via the
# TYPE_CHECKING block below) must enumerate the same models in the
# same order. ``test_system_info_source_registry_alignment`` enforces
# this — the static alias and the runtime list cannot drift.
SYSTEM_INFO_SOURCE_MODELS: list[type[FormatModel]] = [
    HTMLFieldsSource,
    HNAPSystemInfoSource,
    JSSystemInfoSource,
    JSVarsSystemInfoSource,
    JSONSystemInfoSource,
    XMLSystemInfoSource,
]


if TYPE_CHECKING:
    SystemInfoSource = Annotated[
        Annotated[HTMLFieldsSource, Tag("html_fields")]
        | Annotated[HNAPSystemInfoSource, Tag("hnap")]
        | Annotated[JSSystemInfoSource, Tag("javascript")]
        | Annotated[JSVarsSystemInfoSource, Tag("javascript_vars")]
        | Annotated[JSONSystemInfoSource, Tag("json")]
        | Annotated[XMLSystemInfoSource, Tag("xml")],
        Discriminator(_get_source_format),
    ]
else:
    SystemInfoSource = Annotated[
        reduce(or_, (Annotated[m, Tag(m.format_tag)] for m in SYSTEM_INFO_SOURCE_MODELS)),
        Discriminator(_get_source_format),
    ]


class SystemInfoSection(BaseModel):
    """system_info section config -- multi-source."""

    model_config = ConfigDict(extra="forbid")
    sources: list[SystemInfoSource]
