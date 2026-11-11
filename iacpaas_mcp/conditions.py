"""Guaranteed interval predicates. Missing, unknown and zero are different values."""
from decimal import Decimal
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator


class CompositionCondition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    element: str = Field(min_length=1, max_length=40)
    op: Literal["lt", "le", "gt", "ge", "present", "absent"]
    value: float | None = Field(default=None, ge=0, le=100, allow_inf_nan=False)
    include_unlisted: bool = False

    @model_validator(mode="after")
    def valid_predicate(self):
        numeric = self.op not in {"present", "absent"}
        if numeric != (self.value is not None):
            raise ValueError("Для числового сравнения нужен порог; present/absent не принимают порог")
        if self.include_unlisted and self.op not in {"lt", "le"}:
            raise ValueError("include_unlisted допустим только для lt/le")
        return self


def satisfies(component, condition):
    if condition.op == "present":
        return component is not None
    if condition.op == "absent":
        return component is None
    if component is None:
        return condition.include_unlisted
    if component.get("unit") != "%":
        return False
    side = "upper" if condition.op in {"lt", "le"} else "lower"
    bound = component.get(side)
    if bound is None:
        return False
    value = Decimal(str(condition.value))
    return {"lt": lambda: bound < value, "le": lambda: bound <= value,
            "gt": lambda: bound > value, "ge": lambda: bound >= value}[condition.op]()
