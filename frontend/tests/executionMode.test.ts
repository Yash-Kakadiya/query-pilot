import assert from 'node:assert'
import { register } from 'node:module'
import test from 'node:test'
import type { ExecutionMode } from '../src/types/api.ts'

const loaderCode = `
export async function load(url, context, nextLoad) {
  const result = await nextLoad(url, context);
  if (url.includes('config.ts')) {
    let src = typeof result.source === 'string' ? result.source : Buffer.from(result.source).toString('utf8');
    src = src.replace(
      '(import.meta as { env?: Record<string, string | undefined> })?.env?.VITE_DEFAULT_EXECUTION_MODE',
      '((globalThis as any).__TEST_VITE_DEFAULT_EXECUTION_MODE__ !== undefined ? (globalThis as any).__TEST_VITE_DEFAULT_EXECUTION_MODE__ : (import.meta as { env?: Record<string, string | undefined> })?.env?.VITE_DEFAULT_EXECUTION_MODE)'
    );
    return {
      format: result.format,
      source: src,
      shortCircuit: true
    };
  }
  return result;
}
`;

register('data:text/javascript,' + encodeURIComponent(loaderCode))

const { getDefaultExecutionMode, resolveInitialExecutionMode } = await import('../src/services/config.ts')

function setTestDefaultExecutionMode(mode: string | undefined): void {
  ;(globalThis as unknown as { __TEST_VITE_DEFAULT_EXECUTION_MODE__?: string }).__TEST_VITE_DEFAULT_EXECUTION_MODE__ = mode
}

test('Frontend Configuration - Default Execution Mode Resolution', async (t) => {
  t.afterEach(() => {
    setTestDefaultExecutionMode(undefined)
  })

  await t.test('defaults to "execute" when VITE_DEFAULT_EXECUTION_MODE is unset', () => {
    setTestDefaultExecutionMode(undefined)
    assert.strictEqual(getDefaultExecutionMode(), 'execute')
    assert.strictEqual(getDefaultExecutionMode(undefined), 'execute')
  })

  await t.test('defaults to "execute" when VITE_DEFAULT_EXECUTION_MODE is empty or whitespace', () => {
    setTestDefaultExecutionMode('')
    assert.strictEqual(getDefaultExecutionMode(), 'execute')

    setTestDefaultExecutionMode('   ')
    assert.strictEqual(getDefaultExecutionMode(), 'execute')

    assert.strictEqual(getDefaultExecutionMode(''), 'execute')
    assert.strictEqual(getDefaultExecutionMode('   '), 'execute')
    assert.strictEqual(getDefaultExecutionMode('\t\n'), 'execute')
  })

  await t.test('resolves "execute" mode when explicitly configured', () => {
    setTestDefaultExecutionMode('execute')
    assert.strictEqual(getDefaultExecutionMode(), 'execute')

    setTestDefaultExecutionMode('EXECUTE')
    assert.strictEqual(getDefaultExecutionMode(), 'execute')

    setTestDefaultExecutionMode('  Execute  ')
    assert.strictEqual(getDefaultExecutionMode(), 'execute')

    assert.strictEqual(getDefaultExecutionMode('execute'), 'execute')
    assert.strictEqual(getDefaultExecutionMode('EXECUTE'), 'execute')
    assert.strictEqual(getDefaultExecutionMode('  execute  '), 'execute')
  })

  await t.test('resolves "sql_only" mode when configured case-insensitively with whitespace trimming', () => {
    setTestDefaultExecutionMode('sql_only')
    assert.strictEqual(getDefaultExecutionMode(), 'sql_only')

    setTestDefaultExecutionMode('SQL_ONLY')
    assert.strictEqual(getDefaultExecutionMode(), 'sql_only')

    setTestDefaultExecutionMode('  sql_only  ')
    assert.strictEqual(getDefaultExecutionMode(), 'sql_only')

    setTestDefaultExecutionMode('Sql_Only')
    assert.strictEqual(getDefaultExecutionMode(), 'sql_only')

    assert.strictEqual(getDefaultExecutionMode('sql_only'), 'sql_only')
    assert.strictEqual(getDefaultExecutionMode('SQL_ONLY'), 'sql_only')
    assert.strictEqual(getDefaultExecutionMode('  Sql_Only  '), 'sql_only')
  })

  await t.test('safely falls back to "execute" for invalid, unrecognized, or malformed configurations', () => {
    const invalidValues = [
      'invalid',
      'sql-only',       // hyphen instead of underscore
      'sqlonly',        // missing separator
      'dry_run',
      'test',
      'true',
      'false',
      '1',
      '0',
      'undefined',
      'null',
      'EXECUTE_QUERY',
      'random_string_1234',
    ]

    for (const val of invalidValues) {
      setTestDefaultExecutionMode(val)
      assert.strictEqual(
        getDefaultExecutionMode(),
        'execute',
        `Expected fallback to "execute" for invalid environment value: "${val}"`
      )
      assert.strictEqual(
        getDefaultExecutionMode(val),
        'execute',
        `Expected fallback to "execute" for direct invalid value: "${val}"`
      )
    }
  })
})

test('Frontend UI Initialization - Fresh Load Mode Resolution', async (t) => {
  t.afterEach(() => {
    setTestDefaultExecutionMode(undefined)
  })

  await t.test('fresh load without props resolves to "execute" when environment is unset', () => {
    setTestDefaultExecutionMode(undefined)
    const initialMode = resolveInitialExecutionMode(undefined)
    assert.strictEqual(initialMode, 'execute')
  })

  await t.test('fresh load without props resolves to "sql_only" when VITE_DEFAULT_EXECUTION_MODE="sql_only"', () => {
    setTestDefaultExecutionMode('sql_only')
    const initialMode = resolveInitialExecutionMode(undefined)
    assert.strictEqual(initialMode, 'sql_only')
  })

  await t.test('fresh load without props safely falls back to "execute" when VITE_DEFAULT_EXECUTION_MODE is invalid', () => {
    setTestDefaultExecutionMode('unrecognized_mode')
    const initialMode = resolveInitialExecutionMode(undefined)
    assert.strictEqual(initialMode, 'execute')
  })

  await t.test('explicit initialExecutionMode prop takes precedence over environment variable', () => {
    setTestDefaultExecutionMode('execute')
    const initialMode = resolveInitialExecutionMode('sql_only')
    assert.strictEqual(initialMode, 'sql_only', 'Explicit prop should override environment default')
  })
})

test('Frontend UI - Mode Selection & Manual Switch Behavior', async (t) => {
  await t.test('simulating user manual mode change updates submitted executionMode', () => {
    // Mode state lifecycle simulation
    let currentMode: ExecutionMode = getDefaultExecutionMode('sql_only')
    assert.strictEqual(currentMode, 'sql_only', 'Initial fresh load is sql_only')

    // User manually toggles to 'execute'
    currentMode = 'execute'
    assert.strictEqual(currentMode, 'execute', 'User switched to execute')

    let submittedOptions: { executionMode?: ExecutionMode; includeSql?: boolean } | undefined
    const mockSubmit = (_q: string, opts?: { executionMode?: ExecutionMode; includeSql?: boolean }) => {
      submittedOptions = opts
    }

    mockSubmit('Total courses?', { executionMode: currentMode, includeSql: false })

    assert.deepStrictEqual(submittedOptions, {
      executionMode: 'execute',
      includeSql: false,
    })
  })

  await t.test('simulating user manual switch from execute to sql_only before submission', () => {
    let currentMode: ExecutionMode = getDefaultExecutionMode('execute')
    assert.strictEqual(currentMode, 'execute', 'Initial fresh load is execute')

    // User manually toggles to 'sql_only'
    currentMode = 'sql_only'
    assert.strictEqual(currentMode, 'sql_only', 'User switched to sql_only')

    let submittedOptions: { executionMode?: ExecutionMode; includeSql?: boolean } | undefined
    const mockSubmit = (_q: string, opts?: { executionMode?: ExecutionMode; includeSql?: boolean }) => {
      submittedOptions = opts
    }

    mockSubmit('Total courses?', { executionMode: currentMode, includeSql: true })

    assert.deepStrictEqual(submittedOptions, {
      executionMode: 'sql_only',
      includeSql: true,
    })
  })
})
