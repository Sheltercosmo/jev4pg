"""Review and atomically apply PostgreSQL source attachments."""

from typing import Literal

from pydantic import Field, StrictBool, StrictInt, StrictStr, model_validator
from sqlalchemy import text, update

from ..ir import Strict
from ..ledger import digest, uid
from . import schema
from .source_catalog import (
    database_identity,
    describe_relationships,
    inspect_source,
    insert_attachment,
)


class SourceSpec(Strict):
    name: StrictStr = Field(min_length=1, max_length=120)
    schema_name: StrictStr = Field(alias="schema", min_length=1, max_length=63)
    table: StrictStr = Field(min_length=1, max_length=63)
    columns: list[StrictStr] | None = Field(default=None, min_length=1, max_length=64)
    description: StrictStr | None = Field(default=None, max_length=4000)
    rebind: StrictBool = False

    @model_validator(mode="after")
    def valid_name(self):
        if not self.name.strip() or self.name.startswith("_sdd"):
            raise ValueError("Invalid logical dataset name")
        return self


class SourceManifest(Strict):
    version: StrictInt = Field(ge=1, le=1)
    sources: list[SourceSpec] = Field(min_length=1, max_length=256)

    @model_validator(mode="after")
    def unique_names(self):
        names = [source.name.casefold() for source in self.sources]
        if len(names) != len(set(names)):
            raise ValueError("Manifest dataset names must be unique ignoring letter case")
        return self


class PlannedSource(Strict):
    source: SourceSpec
    action: Literal["create", "keep", "rebind", "conflict"]
    dataset_id: StrictStr = Field(pattern=r"^[0-9a-f]{32}$")
    previous_id: StrictStr | None = None
    previous_fingerprint: StrictStr | None = None
    definition: dict
    columns: list[dict]
    description: StrictStr
    reason: StrictStr | None = None


class SourcePlan(Strict):
    version: StrictInt = Field(ge=1, le=1)
    tenant: StrictStr = Field(min_length=1, max_length=100)
    database_identity: dict
    sources: list[PlannedSource] = Field(min_length=1, max_length=256)
    relationships: list[dict]
    fingerprint: StrictStr = ""


def attachment_fingerprint(dataset):
    return digest(
        {
            key: dataset.get(key)
            for key in (
                "id",
                "name",
                "description",
                "schema_name",
                "table_name",
                "columns",
                "primary_key",
                "writable",
                "source_binding",
            )
        }
    )


def matches(dataset, spec, definition, columns, description):
    return (
        dataset.get("source_binding") == definition
        and dataset["name"] == spec.name
        and dataset["schema_name"] == spec.schema_name
        and dataset["table_name"] == spec.table
        and dataset["columns"] == columns
        and dataset["description"] == description
        and dataset["primary_key"] == definition["primary_key"]
        and not dataset["writable"]
    )


def relationships(entries):
    datasets = [
        {
            "id": entry.dataset_id,
            "name": entry.source.name,
            "source_binding": entry.definition,
            "primary_key": entry.definition["primary_key"],
            "links": [],
        }
        for entry in entries
    ]
    return [
        {"source": dataset["name"], **relationship}
        for dataset in describe_relationships(datasets)
        for relationship in dataset["source_relationships"]
    ]


class SourceOnboarding:
    def __init__(self, catalog):
        self.catalog = catalog

    def preview(self, tenant, manifest):
        manifest = SourceManifest.model_validate(manifest)
        with self.catalog.db.transaction(tenant) as connection:
            if connection.dialect.name != "postgresql":
                raise ValueError("Source onboarding requires PostgreSQL")
            connection.exec_driver_sql("SET TRANSACTION READ ONLY")
            connection.exec_driver_sql("SET LOCAL statement_timeout='10000ms'")
            origin = database_identity(connection)
            existing = {
                item["name"].casefold(): item
                for item in self.catalog.list(tenant, connection=connection)
            }
            entries = []
            for spec in sorted(manifest.sources, key=lambda item: item.name.casefold()):
                definition, columns, comment = inspect_source(
                    connection, spec.schema_name, spec.table, spec.columns
                )
                description = comment if spec.description is None else spec.description
                current = existing.get(spec.name.casefold())
                action, reason = "create", None
                if current:
                    if matches(current, spec, definition, columns, description):
                        action = "keep"
                    elif not current.get("source_binding"):
                        action, reason = (
                            "conflict",
                            "The logical name belongs to an imported dataset",
                        )
                    elif spec.rebind:
                        action = "rebind"
                    else:
                        action, reason = (
                            "conflict",
                            "The attachment differs; set rebind to true and review a new plan",
                        )
                entries.append(
                    PlannedSource(
                        source=spec,
                        action=action,
                        dataset_id=current["id"] if action == "keep" else uid(),
                        previous_id=current["id"] if current else None,
                        previous_fingerprint=attachment_fingerprint(current) if current else None,
                        definition=definition,
                        columns=columns,
                        description=description,
                        reason=reason,
                    )
                )
            plan = SourcePlan(
                version=1,
                tenant=tenant,
                database_identity=origin,
                sources=entries,
                relationships=relationships(entries),
            )
            plan.fingerprint = digest(plan.model_dump(by_alias=True, exclude={"fingerprint"}))
            return plan.model_dump(by_alias=True)

    def apply(self, tenant, proposal):
        plan = SourcePlan.model_validate(proposal)
        if plan.tenant != tenant:
            raise ValueError("The reviewed plan belongs to a different tenant")
        if digest(plan.model_dump(by_alias=True, exclude={"fingerprint"})) != plan.fingerprint:
            raise ValueError("The reviewed plan was edited; generate a new preview")
        SourceManifest(version=1, sources=[entry.source for entry in plan.sources])
        if any(entry.action == "conflict" for entry in plan.sources):
            raise ValueError("Resolve source conflicts and generate a new preview")
        if relationships(plan.sources) != plan.relationships:
            raise ValueError("The reviewed relationships do not match the source contracts")
        with self.catalog.db.transaction(tenant) as connection:
            if connection.dialect.name != "postgresql":
                raise ValueError("Source onboarding requires PostgreSQL")
            connection.exec_driver_sql("SET LOCAL statement_timeout='10000ms'")
            connection.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
                {"key": tenant + ":datasets"},
            )
            if database_identity(connection) != plan.database_identity:
                raise ValueError("The source database changed; generate a new preview")
            existing = {
                item["name"].casefold(): item
                for item in self.catalog.list(tenant, connection=connection)
            }
            pending, retained = [], []
            for entry in plan.sources:
                spec = entry.source
                definition, columns, comment = inspect_source(
                    connection, spec.schema_name, spec.table, spec.columns
                )
                description = comment if spec.description is None else spec.description
                if (definition, columns, description) != (
                    entry.definition,
                    entry.columns,
                    entry.description,
                ):
                    raise ValueError(
                        f"Source {spec.name} changed after preview; generate a new plan"
                    )
                current = existing.get(spec.name.casefold())
                if (
                    current
                    and current["id"] == entry.dataset_id
                    and matches(current, spec, definition, columns, description)
                ):
                    retained.append(entry.dataset_id)
                    continue
                if entry.action == "keep" or (entry.action == "create" and current is not None):
                    raise ValueError(f"Catalog entry {spec.name} changed after preview")
                if entry.action == "rebind" and (
                    not spec.rebind
                    or current is None
                    or not current.get("source_binding")
                    or current["id"] != entry.previous_id
                    or attachment_fingerprint(current) != entry.previous_fingerprint
                ):
                    raise ValueError(f"Attachment {spec.name} changed after preview")
                pending.append(entry)
            for entry in pending:
                if entry.action == "rebind":
                    connection.execute(
                        update(schema.source_bindings)
                        .where(
                            schema.source_bindings.c.tenant == tenant,
                            schema.source_bindings.c.dataset_id == entry.previous_id,
                        )
                        .values(active=0)
                    )
                    connection.execute(
                        update(schema.datasets)
                        .where(
                            schema.datasets.c.tenant == tenant,
                            schema.datasets.c.id == entry.previous_id,
                        )
                        .values(name="_sdd_detached_" + entry.previous_id)
                    )
                spec = entry.source
                insert_attachment(
                    connection,
                    tenant,
                    entry.dataset_id,
                    spec.name,
                    spec.schema_name,
                    spec.table,
                    entry.definition,
                    entry.columns,
                    entry.description,
                )
            return {
                "applied": True,
                "fingerprint": plan.fingerprint,
                "created": [entry.dataset_id for entry in pending],
                "retained": retained,
            }
