from pydantic import BaseModel, field_validator, ConfigDict
from typing import Optional


class TableSchemaIn(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    schema_name: str
    db: str
    table_name: str
    desc: str = ""
    type: str = ""
    types: list[str] = []

    @field_validator("schema_name", "db", "table_name")
    @classmethod
    def validate_non_empty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("字段不能为空")
        return v


class TableSchemaUpdate(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    desc: Optional[str] = None
    type: Optional[str] = None
    types: Optional[list[str]] = None


class BatchSyncIn(BaseModel):
    tables: list[TableSchemaIn]


class VectorOut(BaseModel):
    schema_name: str
    db: str
    table_name: str
    doc: str = ""
    types: list[str] = []
    distance: Optional[float] = None