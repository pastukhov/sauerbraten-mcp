"""Closed public tool schemas; native validation remains the final authority."""

from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator

Counter = Annotated[str, Field(pattern=r"^(0|[1-9][0-9]{0,19})$")]
Uuid = Annotated[
  str, Field(pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
]
Int3 = Annotated[list[int], Field(min_length=3, max_length=3)]
Float3 = Annotated[list[float], Field(min_length=3, max_length=3)]
Attrs = Annotated[
  list[Annotated[int, Field(ge=0, le=32767)]], Field(min_length=5, max_length=5)
]


class Model(BaseModel):
  model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class Empty(Model):
  pass


class Mutation(Model):
  request_id: Uuid
  map_epoch: Uuid
  expected_revision: Counter


class Bounds(Model):
  origin: Int3
  extent: Int3


class Selection(Model):
  origin: Int3
  count: Int3
  grid: Annotated[int, Field(ge=1)]
  orient: Annotated[int, Field(ge=0, le=5)]


class TextureRef(Model):
  map_epoch: Uuid
  vslot_generation: Counter
  index: Annotated[int, Field(ge=0)]


class ModelRef(Model):
  map_epoch: Uuid
  mapmodel_generation: Counter
  index: Annotated[int, Field(ge=0)]


class EntityRef(Model):
  map_epoch: Uuid
  generation: Counter
  index: Annotated[int, Field(ge=0, lt=10000)]


class RegionRead(Model):
  bounds: Bounds
  cursor: str | None = None


class Catalog(Model):
  offset: Annotated[int, Field(ge=0)] = 0
  limit: Annotated[int, Field(ge=1, le=256)] = 256
  map_epoch: Uuid | None = None
  generation: Counter | None = None


class MapCatalog(Model):
  search: str = ""
  offset: Annotated[int, Field(ge=0)] = 0
  limit: Annotated[int, Field(ge=1, le=256)] = 256


class GeometryFill(Mutation):
  selection: Selection
  mode: Literal["solid", "empty"]
  texture_ref: TextureRef | None = None

  @model_validator(mode="after")
  def solid_texture(self):
    if self.mode == "solid" and self.texture_ref is None:
      raise ValueError("solid fill requires texture_ref")
    return self


class TextureApply(Mutation):
  selection: Selection
  texture_ref: TextureRef
  faces: Literal["all", 0, 1, 2, 3, 4, 5]


class MaterialApply(Mutation):
  selection: Selection
  material: Literal[
    "air", "water", "lava", "glass", "clip", "noclip", "gameclip", "death"
  ]


class EntityList(Model):
  bounds: Bounds | None = None
  type: str | None = None
  offset: Annotated[int, Field(ge=0, le=10000)] = 0
  limit: Annotated[int, Field(ge=1, le=256)] = 256
  map_epoch: Uuid | None = None
  revision: Counter | None = None


class EntityCreate(Mutation):
  type: Literal["light", "playerstart", "mapmodel"]
  position: Float3
  attrs: Attrs
  model_ref: ModelRef | None = None


class EntityUpdate(Mutation):
  entity_ref: EntityRef
  position: Float3
  attrs: Attrs
  model_ref: ModelRef | None = None


class EntityDelete(Mutation):
  entity_ref: EntityRef


class CameraSet(Mutation):
  position: Float3
  yaw: Annotated[float, Field(ge=0, lt=360)]
  pitch: Annotated[float, Field(ge=-90, le=90)]
  fov: Annotated[int, Field(ge=30, le=120)] = 100


class Capture(Model):
  map_epoch: Uuid
  expected_revision: Counter
  view_revision: Counter


class MapNew(Mutation):
  scale: Annotated[int, Field(ge=10, le=16)]


class MapOpen(Mutation):
  map_id: str | None = None
  artifact_id: str | None = None

  @model_validator(mode="after")
  def one_target(self):
    if bool(self.map_id) == bool(self.artifact_id):
      raise ValueError("provide exactly one map_id or artifact_id")
    return self


class SaveAs(Mutation):
  slug: Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9-]{0,31}$")]


class Status(Model):
  request_id: Uuid


MODELS = {
  "editor_state": Empty,
  "operation_status": Status,
  "map_new": MapNew,
  "map_open": MapOpen,
  "map_catalog": MapCatalog,
  "region_read": RegionRead,
  "geometry_fill": GeometryFill,
  "texture_catalog": Catalog,
  "mapmodel_catalog": Catalog,
  "texture_apply": TextureApply,
  "material_apply": MaterialApply,
  "entity_list": EntityList,
  "entity_create": EntityCreate,
  "entity_update": EntityUpdate,
  "entity_delete": EntityDelete,
  "history_undo": Mutation,
  "history_redo": Mutation,
  "camera_set": CameraSet,
  "view_capture": Capture,
  "map_save_as": SaveAs,
}


def schemas():
  return {name: model.model_json_schema() for name, model in MODELS.items()}


def validate(name, params):
  return MODELS[name].model_validate(params).model_dump(exclude_none=True)
