"""User-owned desktop layouts, stored alongside the existing per-coin preferences."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator


class Rect(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)
    w: float = Field(gt=0, le=1)
    h: float = Field(gt=0, le=1)

    @model_validator(mode="after")
    def within_desk(self):
        if self.x + self.w > 1.000001 or self.y + self.h > 1.000001:
            raise ValueError("Модуль должен находиться внутри рабочего стола")
        return self


class Module(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    id: str = Field(min_length=1, max_length=80)
    type: Literal["tape", "cvd", "oiChart", "oi", "delta", "streams", "walls", "alerts"]
    title: str = Field(default="", max_length=80)
    rect: Rect
    settings: dict[str, JsonValue] = Field(default_factory=dict)


class Space(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=80)
    name: str = Field(min_length=1, max_length=80)
    mode: Literal["free", "magnet"] = "magnet"
    modules: list[Module] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def non_blank_name(cls, value):
        value = value.strip()
        if not value:
            raise ValueError("Введите название рабочего пространства")
        return value


class Layout(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    schema: Literal[1]
    updatedAt: int = Field(ge=0)
    activeId: str | None
    spaces: list[Space]

    @model_validator(mode="after")
    def unique_ids(self):
        space_ids = [s.id for s in self.spaces]
        module_ids = [m.id for s in self.spaces for m in s.modules]
        if len(space_ids) != len(set(space_ids)) or len(module_ids) != len(set(module_ids)):
            raise ValueError("Идентификаторы пространств и модулей должны быть уникальными")
        if self.activeId is not None and self.activeId not in space_ids:
            raise ValueError("Рабочее пространство не найдено")
        if self.spaces and self.activeId is None:
            raise ValueError("Укажите активное рабочее пространство")
        return self
