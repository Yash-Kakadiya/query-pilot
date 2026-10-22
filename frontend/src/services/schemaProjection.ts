/**
 * Pure transformation helper projecting raw introspected database schema metadata
 * (e.g. from local agent GET /agent/v1/profiles/{id}/schema) into the strict, bounded
 * ModelFacingSchemaContext contract required by the hosted POST /api/v1/generate-sql endpoint.
 *
 * Security & Model Invariants:
 * - Hosted API schema models enforce Pydantic `extra="forbid"`.
 * - Strips all internal or non-model fields: `is_model_facing`, `total_tables`, `total_columns`,
 *   `total_foreign_keys`, `indexes`, `unique_constraints`, `default`, `is_identity`.
 * - Normalizes and bounds identifier strings, nullability, primary keys, and foreign keys.
 * - Replaces newlines in comments with spaces and clamps description lengths.
 * - Strictly pure: does not mutate the input object.
 */

import type {
  ColumnSchemaContext,
  ForeignKeySchemaContext,
  ModelFacingSchemaContext,
  TableSchemaContext,
} from '../types/api.ts'

export const MAX_SCHEMA_TABLES = 50
export const MAX_COLUMNS_PER_TABLE = 100
export const MAX_IDENTIFIER_LENGTH = 128
export const MAX_DESCRIPTION_LENGTH = 250

export class SchemaProjectionError extends Error {
  constructor(message: string) {
    super(message)
    this.name = 'SchemaProjectionError'
  }
}

/**
 * Projects raw database schema metadata to the exact ModelFacingSchemaContext shape.
 *
 * @param raw - Raw schema metadata from agent introspection or API.
 * @returns Clean, typed ModelFacingSchemaContext without forbidden extra fields.
 * @throws SchemaProjectionError if the input is malformed, missing required fields, or exceeds bounds.
 */
export function projectToModelFacingSchema(raw: unknown): ModelFacingSchemaContext {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) {
    throw new SchemaProjectionError('Invalid schema: input must be a non-null object.')
  }

  const candidate = raw as Record<string, unknown>

  // 1. Validate database_name
  if (typeof candidate.database_name !== 'string' || !candidate.database_name.trim()) {
    throw new SchemaProjectionError('Invalid schema: database_name must be a non-empty string.')
  }
  const database_name = candidate.database_name.trim()
  if (database_name.length > MAX_IDENTIFIER_LENGTH) {
    throw new SchemaProjectionError(
      `Invalid schema: database_name exceeds maximum length of ${MAX_IDENTIFIER_LENGTH} characters.`
    )
  }

  // 2. Validate tables collection
  if (!Array.isArray(candidate.tables)) {
    throw new SchemaProjectionError('Invalid schema: tables must be an array.')
  }
  if (candidate.tables.length === 0) {
    throw new SchemaProjectionError('Invalid schema: tables array must contain at least 1 table.')
  }
  if (candidate.tables.length > MAX_SCHEMA_TABLES) {
    throw new SchemaProjectionError(
      `Invalid schema: tables array exceeds maximum allowed count of ${MAX_SCHEMA_TABLES} tables.`
    )
  }

  const seenTableNames = new Set<string>()
  const projectedTables: TableSchemaContext[] = []

  for (let tIdx = 0; tIdx < candidate.tables.length; tIdx++) {
    const rawTable = candidate.tables[tIdx]
    if (!rawTable || typeof rawTable !== 'object' || Array.isArray(rawTable)) {
      throw new SchemaProjectionError(`Invalid table at index ${tIdx}: expected a non-null object.`)
    }
    const t = rawTable as Record<string, unknown>

    // Table Name
    if (typeof t.table_name !== 'string' || !t.table_name.trim()) {
      throw new SchemaProjectionError(`Invalid table at index ${tIdx}: table_name must be a non-empty string.`)
    }
    const table_name = t.table_name.trim()
    if (table_name.length > MAX_IDENTIFIER_LENGTH) {
      throw new SchemaProjectionError(
        `Invalid table: table_name exceeds maximum length of ${MAX_IDENTIFIER_LENGTH} characters.`
      )
    }

    const lowerName = table_name.toLowerCase()
    if (seenTableNames.has(lowerName)) {
      throw new SchemaProjectionError(`Duplicate table name detected: "${table_name}".`)
    }
    seenTableNames.add(lowerName)

    // Schema Name
    let schema_name = 'dbo'
    if (typeof t.schema_name === 'string' && t.schema_name.trim()) {
      schema_name = t.schema_name.trim()
      if (schema_name.length > MAX_IDENTIFIER_LENGTH) {
        throw new SchemaProjectionError(
          `Invalid table "${table_name}": schema_name exceeds maximum length of ${MAX_IDENTIFIER_LENGTH} characters.`
        )
      }
    }

    // Columns
    if (!Array.isArray(t.columns) || t.columns.length === 0) {
      throw new SchemaProjectionError(`Table "${table_name}" must contain at least 1 column.`)
    }
    if (t.columns.length > MAX_COLUMNS_PER_TABLE) {
      throw new SchemaProjectionError(
        `Table "${table_name}" exceeds maximum allowed columns of ${MAX_COLUMNS_PER_TABLE}.`
      )
    }

    // Resolve primary keys
    const rawPks = Array.isArray(t.primary_keys) ? t.primary_keys : []
    const primary_keys: string[] = []
    const pkSetLower = new Set<string>()
    for (const pk of rawPks) {
      if (typeof pk === 'string' && pk.trim()) {
        const pkClean = pk.trim()
        const lower = pkClean.toLowerCase()
        if (!pkSetLower.has(lower)) {
          pkSetLower.add(lower)
          primary_keys.push(pkClean)
        }
      }
    }

    const seenColNames = new Set<string>()
    const projectedColumns: ColumnSchemaContext[] = []

    for (let cIdx = 0; cIdx < t.columns.length; cIdx++) {
      const rawCol = t.columns[cIdx]
      if (!rawCol || typeof rawCol !== 'object' || Array.isArray(rawCol)) {
        throw new SchemaProjectionError(
          `Invalid column at index ${cIdx} in table "${table_name}": expected a non-null object.`
        )
      }
      const c = rawCol as Record<string, unknown>

      if (typeof c.name !== 'string' || !c.name.trim()) {
        throw new SchemaProjectionError(
          `Column at index ${cIdx} in table "${table_name}" must have a non-empty string name.`
        )
      }
      const colName = c.name.trim()
      if (colName.length > MAX_IDENTIFIER_LENGTH) {
        throw new SchemaProjectionError(
          `Column "${colName}" in table "${table_name}" exceeds maximum length of ${MAX_IDENTIFIER_LENGTH} characters.`
        )
      }

      const colLower = colName.toLowerCase()
      if (seenColNames.has(colLower)) {
        throw new SchemaProjectionError(`Duplicate column name "${colName}" in table "${table_name}".`)
      }
      seenColNames.add(colLower)

      if (typeof c.data_type !== 'string' || !c.data_type.trim()) {
        throw new SchemaProjectionError(
          `Column "${colName}" in table "${table_name}" must have a non-empty string data_type.`
        )
      }
      const data_type = c.data_type.trim()

      const nullable = typeof c.nullable === 'boolean' ? c.nullable : true
      const primary_key = typeof c.primary_key === 'boolean'
        ? c.primary_key
        : pkSetLower.has(colLower)

      if (primary_key && !pkSetLower.has(colLower)) {
        pkSetLower.add(colLower)
        primary_keys.push(colName)
      }

      let comment: string | null = null
      if (typeof c.comment === 'string' && c.comment.trim()) {
        const cleanedComment = c.comment.replace(/[\r\n]+/g, ' ').trim()
        if (cleanedComment) {
          comment = cleanedComment.slice(0, MAX_DESCRIPTION_LENGTH)
        }
      }

      projectedColumns.push({
        name: colName,
        data_type,
        nullable,
        primary_key,
        ...(comment !== null ? { comment } : {}),
      })
    }

    // Foreign Keys
    const rawFks = Array.isArray(t.foreign_keys) ? t.foreign_keys : []
    const projectedFks: ForeignKeySchemaContext[] = []
    for (const fk of rawFks) {
      if (fk && typeof fk === 'object' && !Array.isArray(fk)) {
        const f = fk as Record<string, unknown>
        if (
          typeof f.source_table === 'string' &&
          typeof f.source_column === 'string' &&
          typeof f.target_table === 'string' &&
          typeof f.target_column === 'string' &&
          f.source_table.trim() &&
          f.source_column.trim() &&
          f.target_table.trim() &&
          f.target_column.trim()
        ) {
          const fkName = typeof f.name === 'string' && f.name.trim() ? f.name.trim() : null
          projectedFks.push({
            source_table: f.source_table.trim(),
            source_column: f.source_column.trim(),
            target_table: f.target_table.trim(),
            target_column: f.target_column.trim(),
            ...(fkName !== null ? { name: fkName } : {}),
          })
        }
      }
    }

    // Table Description
    let description: string | null = null
    const rawDesc = t.description ?? t.comment
    if (typeof rawDesc === 'string' && rawDesc.trim()) {
      const cleaned = rawDesc.replace(/[\r\n]+/g, ' ').trim()
      if (cleaned) {
        description = cleaned.slice(0, MAX_DESCRIPTION_LENGTH)
      }
    }

    projectedTables.push({
      table_name,
      schema_name,
      columns: projectedColumns,
      primary_keys: primary_keys.filter((pk) => seenColNames.has(pk.toLowerCase())),
      foreign_keys: projectedFks,
      ...(description !== null ? { description } : {}),
    })
  }

  return {
    database_name,
    tables: projectedTables,
  }
}
