"""Versioned input contracts. Persistence and authorization are separate checks."""

from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StringConstraints,
    model_validator,
)

from evorec.domain.models import Strategy

ItemId = Annotated[
    str,
    StringConstraints(
        strict=True,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    ),
]
Title = Annotated[
    str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=300)
]
Category = Annotated[
    str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=100)
]
Description = Annotated[str, StringConstraints(strict=True, max_length=10000)]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["0.1"] = "0.1"


class CatalogItem(Contract):
    item_id: ItemId
    title: Title
    category: Category
    description: Description = ""
    image_url: Annotated[str, StringConstraints(strict=True, max_length=2048)] | None = None


class CatalogImportInput(Contract):
    batch_id: UUID
    items: Annotated[list[CatalogItem], Field(min_length=1, max_length=1000)]

    @model_validator(mode="after")
    def unique_item_ids(self) -> Self:
        ids = [item.item_id for item in self.items]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate item_id within batch; reject the whole batch")
        return self


class RecommendationInput(Contract):
    session_id: UUID
    expected_history_version: Annotated[StrictInt, Field(ge=0)]
    strategy: Strategy = Strategy.POPULAR
    k: Annotated[StrictInt, Field(ge=1, le=50)] = 10


class FeedbackInput(Contract):
    event_id: UUID
    session_id: UUID
    request_id: UUID
    item_id: ItemId
    kind: Literal["detail_view", "exposure", "favorite_set", "hide_set"]
    observed_at: AwareDatetime
    desired_state: StrictBool | None = None
    visible_ratio: Annotated[float, Field(strict=True, ge=0, le=1, allow_inf_nan=False)] | None = None
    visible_duration_ms: Annotated[StrictInt, Field(ge=0)] | None = None

    @model_validator(mode="after")
    def event_semantics(self) -> Self:
        state_event = self.kind in {"favorite_set", "hide_set"}
        if state_event and self.desired_state is None:
            raise ValueError("state-setting feedback requires explicit desired_state")
        if not state_event and self.desired_state is not None:
            raise ValueError("this feedback kind does not accept desired_state")

        if self.kind == "exposure":
            if self.visible_ratio is None or self.visible_duration_ms is None:
                raise ValueError("exposure requires visibility and duration evidence")
            if self.visible_ratio < 0.5 or self.visible_duration_ms < 1000:
                raise ValueError("exposure requires at least 50% visibility for 1000 ms")
        elif self.visible_ratio is not None or self.visible_duration_ms is not None:
            raise ValueError("visibility evidence is only accepted for exposure")
        return self
