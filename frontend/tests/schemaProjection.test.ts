import assert from 'node:assert'
import test from 'node:test'
import {
  MAX_COLUMNS_PER_TABLE,
  MAX_DESCRIPTION_LENGTH,
  MAX_IDENTIFIER_LENGTH,
  MAX_SCHEMA_TABLES,
  SchemaProjectionError,
  projectToModelFacingSchema,
} from '../src/services/schemaProjection.ts'

test('Schema Projection - Valid Schema Transformations', async (t) => {
  await t.test('projects complete raw agent schema into valid ModelFacingSchemaContext', () => {
    const rawAgentSchema = {
      database_name: 'CorpDW',
      is_model_facing: true,
      total_tables: 2,
      total_columns: 5,
      total_foreign_keys: 1,
      tables: [
        {
          schema_name: 'dbo',
          table_name: 'Departments',
          description: 'Department directory\r\nand hierarchy',
          columns: [
            {
              name: 'DepartmentID',
              data_type: 'int',
              nullable: false,
              primary_key: true,
              default: null,
              is_identity: true,
              comment: 'Primary identifier\nfor department',
            },
            {
              name: 'Name',
              data_type: 'nvarchar(100)',
              nullable: false,
              primary_key: false,
              default: "''",
              is_identity: false,
              comment: null,
            },
          ],
          primary_keys: ['DepartmentID'],
          foreign_keys: [],
          indexes: [{ name: 'IX_Departments_Name', columns: ['Name'], is_unique: true }],
          unique_constraints: [{ name: 'UQ_Dept_Name', columns: ['Name'] }],
        },
        {
          schema_name: 'hr',
          table_name: 'Employees',
          columns: [
            {
              name: 'EmployeeID',
              data_type: 'int',
              nullable: false,
              primary_key: true,
            },
            {
              name: 'DepartmentID',
              data_type: 'int',
              nullable: true,
            },
            {
              name: 'Title',
              data_type: 'varchar(50)',
            },
          ],
          foreign_keys: [
            {
              source_table: 'Employees',
              source_column: 'DepartmentID',
              target_table: 'Departments',
              target_column: 'DepartmentID',
              name: 'FK_Employees_Departments',
            },
          ],
        },
      ],
    }

    const projected = projectToModelFacingSchema(rawAgentSchema)

    // Verify top-level structure
    assert.strictEqual(projected.database_name, 'CorpDW')
    assert.strictEqual(projected.tables.length, 2)

    // Verify forbidden top-level fields are absent
    assert.strictEqual('is_model_facing' in projected, false)
    assert.strictEqual('total_tables' in projected, false)
    assert.strictEqual('total_columns' in projected, false)
    assert.strictEqual('total_foreign_keys' in projected, false)

    // Table 1: Departments
    const dept = projected.tables[0]
    assert.strictEqual(dept.schema_name, 'dbo')
    assert.strictEqual(dept.table_name, 'Departments')
    assert.strictEqual(dept.description, 'Department directory and hierarchy')
    assert.deepStrictEqual(dept.primary_keys, ['DepartmentID'])
    assert.deepStrictEqual(dept.foreign_keys, [])
    assert.strictEqual('indexes' in dept, false)
    assert.strictEqual('unique_constraints' in dept, false)

    // Columns in Departments
    assert.strictEqual(dept.columns.length, 2)
    assert.strictEqual(dept.columns[0].name, 'DepartmentID')
    assert.strictEqual(dept.columns[0].data_type, 'int')
    assert.strictEqual(dept.columns[0].nullable, false)
    assert.strictEqual(dept.columns[0].primary_key, true)
    assert.strictEqual(dept.columns[0].comment, 'Primary identifier for department')
    assert.strictEqual('default' in dept.columns[0], false)
    assert.strictEqual('is_identity' in dept.columns[0], false)

    assert.strictEqual(dept.columns[1].name, 'Name')
    assert.strictEqual(dept.columns[1].data_type, 'nvarchar(100)')
    assert.strictEqual(dept.columns[1].nullable, false)
    assert.strictEqual(dept.columns[1].primary_key, false)
    assert.strictEqual('comment' in dept.columns[1], false)

    // Table 2: Employees (verifying defaults)
    const emp = projected.tables[1]
    assert.strictEqual(emp.schema_name, 'hr')
    assert.strictEqual(emp.table_name, 'Employees')
    assert.deepStrictEqual(emp.primary_keys, ['EmployeeID']) // inferred from column primary_key: true
    assert.strictEqual(emp.foreign_keys?.length, 1)
    assert.deepStrictEqual(emp.foreign_keys?.[0], {
      source_table: 'Employees',
      source_column: 'DepartmentID',
      target_table: 'Departments',
      target_column: 'DepartmentID',
      name: 'FK_Employees_Departments',
    })

    // Third column defaults: nullable=true, primary_key=false
    assert.strictEqual(emp.columns[2].nullable, true)
    assert.strictEqual(emp.columns[2].primary_key, false)
  })

  await t.test('preserves immutability of raw input object', () => {
    const raw = {
      database_name: 'ReadOnlyDB',
      is_model_facing: true,
      tables: [
        {
          table_name: 'T1',
          columns: [{ name: 'C1', data_type: 'int' }],
          indexes: [{ name: 'IX_1', columns: ['C1'] }],
        },
      ],
    }
    const rawCopy = JSON.parse(JSON.stringify(raw))
    projectToModelFacingSchema(raw)
    assert.deepStrictEqual(raw, rawCopy)
  })
})

test('Schema Projection - Stripping Forbidden Extra Fields', async (t) => {
  await t.test('strips all unexpected properties across root, tables, and columns', () => {
    const dirtyInput = {
      database_name: 'CleanDB',
      extra_root_prop: 'bad',
      cached_timestamp: 12345678,
      tables: [
        {
          table_name: 'T1',
          extra_table_prop: 999,
          indexes: [{ name: 'IX' }],
          unique_constraints: [{ name: 'UQ' }],
          columns: [
            {
              name: 'Col1',
              data_type: 'int',
              extra_col_prop: 'forbidden',
              default: '0',
              is_identity: true,
              collation: 'SQL_Latin1_General_CP1_CI_AS',
            },
          ],
        },
      ],
    }

    const projected = projectToModelFacingSchema(dirtyInput)
    const jsonStr = JSON.stringify(projected)

    assert.strictEqual(jsonStr.includes('extra_root_prop'), false)
    assert.strictEqual(jsonStr.includes('cached_timestamp'), false)
    assert.strictEqual(jsonStr.includes('extra_table_prop'), false)
    assert.strictEqual(jsonStr.includes('indexes'), false)
    assert.strictEqual(jsonStr.includes('unique_constraints'), false)
    assert.strictEqual(jsonStr.includes('extra_col_prop'), false)
    assert.strictEqual(jsonStr.includes('default'), false)
    assert.strictEqual(jsonStr.includes('is_identity'), false)
    assert.strictEqual(jsonStr.includes('collation'), false)

    // Allowed keys only
    assert.deepStrictEqual(Object.keys(projected), ['database_name', 'tables'])
    assert.deepStrictEqual(Object.keys(projected.tables[0]), [
      'table_name',
      'schema_name',
      'columns',
      'primary_keys',
      'foreign_keys',
    ])
    assert.deepStrictEqual(Object.keys(projected.tables[0].columns[0]), [
      'name',
      'data_type',
      'nullable',
      'primary_key',
    ])
  })
})

test('Schema Projection - Required and Optional Fields Handling', async (t) => {
  await t.test('defaults schema_name to "dbo" when missing or empty', () => {
    const input = {
      database_name: 'TestDB',
      tables: [{ table_name: 'TestTable', columns: [{ name: 'ID', data_type: 'int' }] }],
    }
    const res = projectToModelFacingSchema(input)
    assert.strictEqual(res.tables[0].schema_name, 'dbo')
  })

  await t.test('trims whitespace on all identifier strings', () => {
    const input = {
      database_name: '  TrimmedDB  ',
      tables: [
        {
          schema_name: '  dbo  ',
          table_name: '  Users  ',
          columns: [{ name: '  UserID  ', data_type: '  bigint  ' }],
          primary_keys: ['  UserID  '],
        },
      ],
    }
    const res = projectToModelFacingSchema(input)
    assert.strictEqual(res.database_name, 'TrimmedDB')
    assert.strictEqual(res.tables[0].schema_name, 'dbo')
    assert.strictEqual(res.tables[0].table_name, 'Users')
    assert.strictEqual(res.tables[0].columns[0].name, 'UserID')
    assert.strictEqual(res.tables[0].columns[0].data_type, 'bigint')
    assert.deepStrictEqual(res.tables[0].primary_keys, ['UserID'])
  })

  await t.test('handles foreign keys with and without optional name', () => {
    const input = {
      database_name: 'FKDB',
      tables: [
        {
          table_name: 'Orders',
          columns: [{ name: 'OrderID', data_type: 'int' }],
          foreign_keys: [
            {
              source_table: 'Orders',
              source_column: 'CustID',
              target_table: 'Customers',
              target_column: 'ID',
              name: 'FK_Orders_Customers',
            },
            {
              source_table: 'Orders',
              source_column: 'ShipperID',
              target_table: 'Shippers',
              target_column: 'ID',
            },
          ],
        },
      ],
    }
    const res = projectToModelFacingSchema(input)
    assert.strictEqual(res.tables[0].foreign_keys?.length, 2)
    assert.strictEqual(res.tables[0].foreign_keys?.[0].name, 'FK_Orders_Customers')
    assert.strictEqual(res.tables[0].foreign_keys?.[1].name, undefined)
  })

  await t.test('clamps descriptions and comments to MAX_DESCRIPTION_LENGTH', () => {
    const longText = 'A'.repeat(500)
    const input = {
      database_name: 'ClampDB',
      tables: [
        {
          table_name: 'T1',
          description: longText,
          columns: [{ name: 'C1', data_type: 'int', comment: longText }],
        },
      ],
    }
    const res = projectToModelFacingSchema(input)
    assert.strictEqual(res.tables[0].description?.length, MAX_DESCRIPTION_LENGTH)
    assert.strictEqual(res.tables[0].columns[0].comment?.length, MAX_DESCRIPTION_LENGTH)
  })
})

test('Schema Projection - Malformed and Empty Inputs', async (t) => {
  await t.test('rejects null, undefined, primitives, and arrays at top level', () => {
    assert.throws(() => projectToModelFacingSchema(null), SchemaProjectionError)
    assert.throws(() => projectToModelFacingSchema(undefined), SchemaProjectionError)
    assert.throws(() => projectToModelFacingSchema('string'), SchemaProjectionError)
    assert.throws(() => projectToModelFacingSchema(123), SchemaProjectionError)
    assert.throws(() => projectToModelFacingSchema([]), SchemaProjectionError)
  })

  await t.test('rejects missing or empty database_name', () => {
    assert.throws(
      () => projectToModelFacingSchema({ tables: [{ table_name: 'T', columns: [{ name: 'C', data_type: 'int' }] }] }),
      /database_name must be a non-empty string/
    )
    assert.throws(
      () => projectToModelFacingSchema({ database_name: '', tables: [{ table_name: 'T', columns: [{ name: 'C', data_type: 'int' }] }] }),
      /database_name must be a non-empty string/
    )
    assert.throws(
      () => projectToModelFacingSchema({ database_name: '   ', tables: [{ table_name: 'T', columns: [{ name: 'C', data_type: 'int' }] }] }),
      /database_name must be a non-empty string/
    )
  })

  await t.test('rejects missing or empty tables array', () => {
    assert.throws(
      () => projectToModelFacingSchema({ database_name: 'DB' }),
      /tables must be an array/
    )
    assert.throws(
      () => projectToModelFacingSchema({ database_name: 'DB', tables: [] }),
      /tables array must contain at least 1 table/
    )
  })

  await t.test('rejects table with missing or empty table_name', () => {
    assert.throws(
      () => projectToModelFacingSchema({ database_name: 'DB', tables: [{ columns: [{ name: 'C', data_type: 'int' }] }] }),
      /table_name must be a non-empty string/
    )
    assert.throws(
      () => projectToModelFacingSchema({ database_name: 'DB', tables: [{ table_name: '  ', columns: [{ name: 'C', data_type: 'int' }] }] }),
      /table_name must be a non-empty string/
    )
  })

  await t.test('rejects table with missing or empty columns array', () => {
    assert.throws(
      () => projectToModelFacingSchema({ database_name: 'DB', tables: [{ table_name: 'T' }] }),
      /must contain at least 1 column/
    )
    assert.throws(
      () => projectToModelFacingSchema({ database_name: 'DB', tables: [{ table_name: 'T', columns: [] }] }),
      /must contain at least 1 column/
    )
  })

  await t.test('rejects column with missing name or data_type', () => {
    assert.throws(
      () => projectToModelFacingSchema({ database_name: 'DB', tables: [{ table_name: 'T', columns: [{ data_type: 'int' }] }] }),
      /must have a non-empty string name/
    )
    assert.throws(
      () => projectToModelFacingSchema({ database_name: 'DB', tables: [{ table_name: 'T', columns: [{ name: 'C' }] }] }),
      /must have a non-empty string data_type/
    )
  })

  await t.test('rejects duplicate table names case-insensitively', () => {
    assert.throws(
      () =>
        projectToModelFacingSchema({
          database_name: 'DB',
          tables: [
            { table_name: 'Orders', columns: [{ name: 'ID', data_type: 'int' }] },
            { table_name: 'orders', columns: [{ name: 'ID', data_type: 'int' }] },
          ],
        }),
      /Duplicate table name detected: "orders"/
    )
  })

  await t.test('rejects duplicate column names case-insensitively within same table', () => {
    assert.throws(
      () =>
        projectToModelFacingSchema({
          database_name: 'DB',
          tables: [
            {
              table_name: 'Users',
              columns: [
                { name: 'Email', data_type: 'varchar(100)' },
                { name: 'email', data_type: 'varchar(100)' },
              ],
            },
          ],
        }),
      /Duplicate column name "email"/
    )
  })
})

test('Schema Projection - Bounds Enforcement', async (t) => {
  await t.test('rejects database_name exceeding MAX_IDENTIFIER_LENGTH', () => {
    const longDb = 'D'.repeat(MAX_IDENTIFIER_LENGTH + 1)
    assert.throws(
      () => projectToModelFacingSchema({ database_name: longDb, tables: [{ table_name: 'T', columns: [{ name: 'C', data_type: 'int' }] }] }),
      /database_name exceeds maximum length/
    )
  })

  await t.test('rejects tables count exceeding MAX_SCHEMA_TABLES (50)', () => {
    const tables = []
    for (let i = 0; i <= MAX_SCHEMA_TABLES; i++) {
      tables.push({ table_name: `T_${i}`, columns: [{ name: 'C', data_type: 'int' }] })
    }
    assert.throws(
      () => projectToModelFacingSchema({ database_name: 'DB', tables }),
      /tables array exceeds maximum allowed count of 50/
    )
  })

  await t.test('rejects columns count exceeding MAX_COLUMNS_PER_TABLE (100)', () => {
    const columns = []
    for (let i = 0; i <= MAX_COLUMNS_PER_TABLE; i++) {
      columns.push({ name: `C_${i}`, data_type: 'int' })
    }
    assert.throws(
      () => projectToModelFacingSchema({ database_name: 'DB', tables: [{ table_name: 'T', columns }] }),
      /exceeds maximum allowed columns of 100/
    )
  })

  await t.test('prunes orphaned primary keys that do not match any column', () => {
    const projected = projectToModelFacingSchema({
      database_name: 'DB',
      tables: [
        {
          table_name: 'Users',
          columns: [{ name: 'id', data_type: 'int', primary_key: true }],
          primary_keys: ['id', 'ghost_id'],
        },
      ],
    })
    assert.deepStrictEqual(projected.tables[0].primary_keys, ['id'])
  })
})
