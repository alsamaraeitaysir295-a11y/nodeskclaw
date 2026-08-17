"""Instance template schemas."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class TemplateItemInput(BaseModel):
    type: Literal["gene", "genome"]
    slug: str


class TemplateItemRef(BaseModel):
    type: Literal["gene", "genome"]
    slug: str
    name: str
    short_description: str | None = None
    icon: str | None = None
    gene_count: int | None = None


class InstanceTemplateCreate(BaseModel):
    name: str = Field(..., max_length=128)
    slug: str = Field(..., max_length=128)
    description: str | None = None
    short_description: str | None = Field(None, max_length=256)
    icon: str | None = Field(None, max_length=32)
    items: list[TemplateItemInput] = Field(default_factory=list)
    gene_slugs: list[str] | None = None


class InstanceTemplateFromInstance(BaseModel):
    name: str = Field(..., max_length=128)
    slug: str = Field(..., max_length=128)
    description: str | None = None
    short_description: str | None = Field(None, max_length=256)
    icon: str | None = Field(None, max_length=32)


class InstanceTemplateUpdate(BaseModel):
    name: str | None = Field(None, max_length=128)
    description: str | None = None
    short_description: str | None = Field(None, max_length=256)
    icon: str | None = Field(None, max_length=32)
    items: list[TemplateItemInput] | None = None
    gene_slugs: list[str] | None = None


class TemplateForkRequest(BaseModel):
    target: Literal["personal", "org", "public"]


class GeneRef(BaseModel):
    slug: str
    name: str
    short_description: str | None = None
    category: str | None = None
    icon: str | None = None


class InstanceTemplateInfo(BaseModel):
    id: str
    name: str
    slug: str
    description: str | None = None
    short_description: str | None = None
    icon: str | None = None
    gene_slugs: list[str] = []
    genes: list[GeneRef] = []
    items: list[TemplateItemRef] = []
    source_instance_id: str | None = None
    is_published: bool = True
    is_featured: bool = False
    use_count: int = 0
    created_by: str | None = None
    org_id: str | None = None
    visibility: str | None = None
    review_status: str | None = None
    created_at: datetime | None = None

    model_config = {"from_attributes": True}
