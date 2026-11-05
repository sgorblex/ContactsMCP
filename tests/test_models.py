"""Validation rules of the tool argument models."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from contacts_mcp.models import (
    CustomFieldOps,
    Email,
    ListOps,
    PartialDate,
    UserDefined,
    check_resource_name,
)


@pytest.mark.parametrize(
    "date", [{"year": 1815}, {"month": 12}, {"month": 12, "day": 10}, {"year": 1, "month": 1}]
)
def test_partial_date_accepts_a_year_or_a_month(date: dict[str, int]) -> None:
    PartialDate.model_validate(date)


@pytest.mark.parametrize("date", [{}, {"day": 10}, {"year": 1815, "day": 10}, {"month": 13}])
def test_partial_date_needs_a_year_or_a_month_and_a_day_needs_a_month(
    date: dict[str, int],
) -> None:
    with pytest.raises(ValidationError):
        PartialDate.model_validate(date)


def test_unknown_fields_are_refused_by_name() -> None:
    with pytest.raises(ValidationError, match="primay"):
        Email.model_validate({"value": "a@example.com", "primay": True})


def test_list_ops_needs_an_operation_and_replace_all_goes_alone() -> None:
    assert ListOps[Email].model_validate({"replace_all": []}).replace_all == []
    assert ListOps[Email].model_validate({"set_primary": "a@example.com"}).set_primary
    with pytest.raises(ValidationError, match="at least one"):
        ListOps[Email].model_validate({})
    with pytest.raises(ValidationError, match="replace_all alone"):
        ListOps[Email].model_validate({"replace_all": [], "remove": ["a@example.com"]})


@pytest.mark.parametrize(
    ("value", "prefix"),
    [
        ("people/c123", "people/"),
        (" people/c123 ", "people/"),
        ("contactGroups/myContacts", "contactGroups/"),
        ("otherContacts/c9", "otherContacts/"),
    ],
)
def test_resource_names_are_accepted(value: str, prefix: str) -> None:
    assert check_resource_name(value, prefix) == value.strip()


@pytest.mark.parametrize(
    ("value", "prefix"),
    [
        ("people/", "people/"),
        ("people/c1/x", "people/"),
        ("contactGroups/c1", "people/"),
        ("people/c1?x=1", "people/"),
    ],
)
def test_bad_resource_names_are_refused(value: str, prefix: str) -> None:
    with pytest.raises(ValueError, match="resource name"):
        check_resource_name(value, prefix)


def test_custom_field_ops_accept_set_alone() -> None:
    ops = CustomFieldOps(set=[UserDefined(key="a", value="1")])
    assert ops.set[0].key == "a"


def test_empty_custom_field_ops_name_set() -> None:
    with pytest.raises(ValidationError, match="replace_all, set, set_primary"):
        CustomFieldOps()


@pytest.mark.parametrize("other", [{"replace_all": []}, {"set_primary": "a"}])
def test_set_goes_without_replace_all_and_set_primary(other: dict[str, object]) -> None:
    with pytest.raises(ValidationError, match="set goes without"):
        CustomFieldOps.model_validate({"set": [{"key": "a", "value": "1"}], **other})


def test_other_fields_do_not_take_set() -> None:
    with pytest.raises(ValidationError):
        ListOps[str].model_validate({"set": ["a"]})
