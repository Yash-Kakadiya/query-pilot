import assert from 'node:assert'
import test from 'node:test'
import {
  AGENT_CUSTOM_HEADER,
  AGENT_CUSTOM_HEADER_VALUE,
  AgentClientError,
  DEFAULT_AGENT_BASE_PATH,
  MAX_BOOTSTRAP_TOKEN_LENGTH,
  MAX_SQL_QUERY_LENGTH,
  MIN_BOOTSTRAP_TOKEN_LENGTH,
  bootstrapAgent,
  executeProfileQuery,
  getAgentBaseUrl,
  getProfileSchema,
  getProfiles,
  getProjectedProfileSchema,
  logoutAgent,
  resolveAgentEndpoint,
  rotateAgentToken,
} from '../src/services/agentClient.ts'

function setTestAgentBaseUrl(url: string | undefined): void {
  ;(globalThis as unknown as { __TEST_VITE_AGENT_BASE_URL__?: string }).__TEST_VITE_AGENT_BASE_URL__ = url
}

// ==============================================================================
// 1. URL and Base Path Resolution
// ==============================================================================

test('Local Agent Client - Base URL and Endpoint Resolution', async (t) => {
  t.afterEach(() => {
    setTestAgentBaseUrl(undefined)
  })

  await t.test('resolves default proxy path when VITE_AGENT_BASE_URL is undefined', () => {
    assert.strictEqual(getAgentBaseUrl(undefined), DEFAULT_AGENT_BASE_PATH)
    assert.strictEqual(resolveAgentEndpoint('/v1/profiles', undefined), '/agent/v1/profiles')
    assert.strictEqual(resolveAgentEndpoint('/v1/auth/bootstrap', undefined), '/agent/v1/auth/bootstrap')
  })

  await t.test('resolves default proxy path when VITE_AGENT_BASE_URL is empty or whitespace', () => {
    assert.strictEqual(getAgentBaseUrl(''), DEFAULT_AGENT_BASE_PATH)
    assert.strictEqual(getAgentBaseUrl('   '), DEFAULT_AGENT_BASE_PATH)
    assert.strictEqual(resolveAgentEndpoint('/v1/profiles', ''), '/agent/v1/profiles')
  })

  await t.test('supports direct loopback URL for development or split-origin setups', () => {
    assert.strictEqual(getAgentBaseUrl('http://127.0.0.1:8765'), 'http://127.0.0.1:8765')
    assert.strictEqual(
      resolveAgentEndpoint('/v1/profiles', 'http://127.0.0.1:8765'),
      'http://127.0.0.1:8765/v1/profiles'
    )
    assert.strictEqual(
      resolveAgentEndpoint('/v1/auth/bootstrap', 'http://127.0.0.1:8765'),
      'http://127.0.0.1:8765/v1/auth/bootstrap'
    )
  })

  await t.test('normalizes trailing slashes on base URLs', () => {
    assert.strictEqual(getAgentBaseUrl('http://127.0.0.1:8765/'), 'http://127.0.0.1:8765')
    assert.strictEqual(getAgentBaseUrl('/agent///'), '/agent')
    assert.strictEqual(
      resolveAgentEndpoint('/v1/profiles', 'http://127.0.0.1:8765/'),
      'http://127.0.0.1:8765/v1/profiles'
    )
  })

  await t.test('prevents duplicate /agent prefix when both base and path contain it', () => {
    assert.strictEqual(resolveAgentEndpoint('/agent/v1/profiles', '/agent'), '/agent/v1/profiles')
    assert.strictEqual(
      resolveAgentEndpoint('/agent/v1/profiles', 'http://127.0.0.1:8765/agent'),
      'http://127.0.0.1:8765/agent/v1/profiles'
    )
  })

  await t.test('rejects malformed explicitly configured URLs', () => {
    const invalidCases = ['not-a-valid-url', 'ftp://agent.local', 'javascript:alert(1)', 'http://']
    for (const invalid of invalidCases) {
      assert.throws(
        () => resolveAgentEndpoint('/v1/profiles', invalid),
        (err: unknown) => {
          assert(err instanceof AgentClientError)
          assert.strictEqual(err.kind, 'validation')
          assert(err.message.includes('Invalid VITE_AGENT_BASE_URL configured'))
          return true
        }
      )
    }
  })
})

// ==============================================================================
// 2. Bootstrap & Ambient Cookie Authentication Flow
// ==============================================================================

test('Local Agent Client - Bootstrap Authentication Flow', async (t) => {
  const originalFetch = globalThis.fetch

  t.afterEach(() => {
    globalThis.fetch = originalFetch
    setTestAgentBaseUrl(undefined)
  })

  const validToken = 'test-bootstrap-token-123456789'

  await t.test('bootstrapAgent() transmits token, custom header, and credentials: "include"', async () => {
    let capturedUrl: string | URL | Request | undefined
    let capturedOptions: RequestInit | undefined

    globalThis.fetch = (async (url: string | URL | Request, options?: RequestInit) => {
      capturedUrl = url
      capturedOptions = options
      return new Response(
        JSON.stringify({
          status: 'authenticated',
          message: 'Bootstrap authentication successful. Ambient loopback session established.',
        }),
        {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }
      )
    }) as typeof fetch

    const resp = await bootstrapAgent(validToken)
    assert.strictEqual(capturedUrl, '/agent/v1/auth/bootstrap')
    assert.strictEqual(capturedOptions?.method, 'POST')
    assert.strictEqual(capturedOptions?.credentials, 'include')

    const headers = capturedOptions?.headers as Record<string, string>
    assert.strictEqual(headers[AGENT_CUSTOM_HEADER], AGENT_CUSTOM_HEADER_VALUE)
    assert.strictEqual(headers['Content-Type'], 'application/json')
    assert.strictEqual(headers['Accept'], 'application/json')

    const body = JSON.parse(String(capturedOptions?.body))
    assert.deepStrictEqual(body, { bootstrap_token: validToken })
    assert.strictEqual(resp.status, 'authenticated')
  })

  await t.test('bootstrapAgent() attaches optional requestId when provided', async () => {
    let capturedOptions: RequestInit | undefined

    globalThis.fetch = (async (_url: string | URL | Request, options?: RequestInit) => {
      capturedOptions = options
      return new Response(JSON.stringify({ status: 'authenticated', message: 'ok' }), { status: 200 })
    }) as typeof fetch

    await bootstrapAgent(validToken, { requestId: 'req-boot-99' })
    const headers = capturedOptions?.headers as Record<string, string>
    assert.strictEqual(headers['X-Request-ID'], 'req-boot-99')
  })

  await t.test('bootstrapAgent() enforces token length constraints before network request', async () => {
    // Empty
    await assert.rejects(
      async () => bootstrapAgent(''),
      (err: unknown) => err instanceof AgentClientError && err.kind === 'validation' && /non-empty/.test(err.message)
    )

    // Too short (< 16 chars)
    await assert.rejects(
      async () => bootstrapAgent('short-tok'),
      (err: unknown) =>
        err instanceof AgentClientError &&
        err.kind === 'validation' &&
        err.message.includes(`between ${MIN_BOOTSTRAP_TOKEN_LENGTH} and ${MAX_BOOTSTRAP_TOKEN_LENGTH}`)
    )

    // Too long (> 128 chars)
    const longToken = 'a'.repeat(MAX_BOOTSTRAP_TOKEN_LENGTH + 1)
    await assert.rejects(
      async () => bootstrapAgent(longToken),
      (err: unknown) =>
        err instanceof AgentClientError &&
        err.kind === 'validation' &&
        err.message.includes(`between ${MIN_BOOTSTRAP_TOKEN_LENGTH} and ${MAX_BOOTSTRAP_TOKEN_LENGTH}`)
    )
  })

  await t.test('verifies no bootstrap token or session credential is stored in localStorage or sessionStorage', async () => {
    globalThis.fetch = (async () => {
      return new Response(JSON.stringify({ status: 'authenticated', message: 'ok' }), { status: 200 })
    }) as typeof fetch

    await bootstrapAgent(validToken)

    if (typeof localStorage !== 'undefined') {
      assert.strictEqual(localStorage.getItem('qpt_session'), null)
      assert.strictEqual(localStorage.getItem('qp_agent_session'), null)
      assert.strictEqual(localStorage.getItem('bootstrap_token'), null)
    }
    if (typeof sessionStorage !== 'undefined') {
      assert.strictEqual(sessionStorage.getItem('qpt_session'), null)
      assert.strictEqual(sessionStorage.getItem('qp_agent_session'), null)
      assert.strictEqual(sessionStorage.getItem('bootstrap_token'), null)
    }
  })
})

// ==============================================================================
// 3. Logout and Token Rotation
// ==============================================================================

test('Local Agent Client - Logout and Token Rotation', async (t) => {
  const originalFetch = globalThis.fetch

  t.afterEach(() => {
    globalThis.fetch = originalFetch
  })

  await t.test('logoutAgent() sends POST, credentials: "include", and custom CSRF header', async () => {
    let capturedUrl: string | URL | Request | undefined
    let capturedOptions: RequestInit | undefined

    globalThis.fetch = (async (url: string | URL | Request, options?: RequestInit) => {
      capturedUrl = url
      capturedOptions = options
      return new Response(
        JSON.stringify({ status: 'logged_out', message: 'Session successfully terminated.' }),
        { status: 200 }
      )
    }) as typeof fetch

    const res = await logoutAgent({ requestId: 'req-logout-1' })
    assert.strictEqual(capturedUrl, '/agent/v1/auth/logout')
    assert.strictEqual(capturedOptions?.method, 'POST')
    assert.strictEqual(capturedOptions?.credentials, 'include')
    const headers = capturedOptions?.headers as Record<string, string>
    assert.strictEqual(headers[AGENT_CUSTOM_HEADER], AGENT_CUSTOM_HEADER_VALUE)
    assert.strictEqual(headers['X-Request-ID'], 'req-logout-1')
    assert.strictEqual(res.status, 'logged_out')
  })

  await t.test('rotateAgentToken() sends POST, credentials: "include", and custom CSRF header', async () => {
    let capturedUrl: string | URL | Request | undefined
    let capturedOptions: RequestInit | undefined

    globalThis.fetch = (async (url: string | URL | Request, options?: RequestInit) => {
      capturedUrl = url
      capturedOptions = options
      return new Response(
        JSON.stringify({ status: 'token_rotated', message: 'All active sessions invalidated.' }),
        { status: 200 }
      )
    }) as typeof fetch

    const res = await rotateAgentToken()
    assert.strictEqual(capturedUrl, '/agent/v1/auth/rotate-token')
    assert.strictEqual(capturedOptions?.method, 'POST')
    assert.strictEqual(capturedOptions?.credentials, 'include')
    const headers = capturedOptions?.headers as Record<string, string>
    assert.strictEqual(headers[AGENT_CUSTOM_HEADER], AGENT_CUSTOM_HEADER_VALUE)
    assert.strictEqual(res.status, 'token_rotated')
  })
})

// ==============================================================================
// 4. Profile Listing
// ==============================================================================

test('Local Agent Client - Profile Discovery', async (t) => {
  const originalFetch = globalThis.fetch

  t.afterEach(() => {
    globalThis.fetch = originalFetch
  })

  await t.test('getProfiles() sends GET with credentials: "include" and without CSRF header', async () => {
    let capturedUrl: string | URL | Request | undefined
    let capturedOptions: RequestInit | undefined

    const mockProfiles = {
      profiles: [
        {
          profile_id: 'corp_analytics',
          display_name: 'Corporate Analytics DW',
          schema_name: 'dbo',
          timeout_seconds: 10,
          max_row_limit: 50,
          has_allowlist: true,
          allowed_tables: ['Customers', 'Orders'],
        },
      ],
    }

    globalThis.fetch = (async (url: string | URL | Request, options?: RequestInit) => {
      capturedUrl = url
      capturedOptions = options
      return new Response(JSON.stringify(mockProfiles), { status: 200 })
    }) as typeof fetch

    const result = await getProfiles({ requestId: 'req-profiles-1' })
    assert.strictEqual(capturedUrl, '/agent/v1/profiles')
    assert.strictEqual(capturedOptions?.method, 'GET')
    assert.strictEqual(capturedOptions?.credentials, 'include')

    const headers = capturedOptions?.headers as Record<string, string>
    assert.strictEqual(headers['Accept'], 'application/json')
    assert.strictEqual(headers['X-Request-ID'], 'req-profiles-1')
    assert.strictEqual(headers[AGENT_CUSTOM_HEADER], undefined, 'GET requests must not send mutating CSRF header')

    assert.strictEqual(result.profiles.length, 1)
    assert.strictEqual(result.profiles[0].profile_id, 'corp_analytics')
    assert.strictEqual(result.profiles[0].has_allowlist, true)
  })
})

// ==============================================================================
// 5. Dynamic Schema Introspection & Projection
// ==============================================================================

test('Local Agent Client - Dynamic Schema Introspection', async (t) => {
  const originalFetch = globalThis.fetch

  t.afterEach(() => {
    globalThis.fetch = originalFetch
  })

  const rawAgentSchema = {
    database_name: 'CorpDW',
    is_model_facing: true,
    total_tables: 1,
    total_columns: 2,
    total_foreign_keys: 0,
    tables: [
      {
        schema_name: 'dbo',
        table_name: 'Customers',
        description: 'Customer directory',
        columns: [
          { name: 'CustomerID', data_type: 'int', nullable: false, primary_key: true, default: null, is_identity: true },
          { name: 'CompanyName', data_type: 'varchar(100)', nullable: false, primary_key: false },
        ],
        primary_keys: ['CustomerID'],
        foreign_keys: [],
        indexes: [{ name: 'IX_Cust', columns: ['CompanyName'] }],
      },
    ],
  }

  await t.test('getProfileSchema() returns raw schema by default', async () => {
    let capturedUrl: string | URL | Request | undefined

    globalThis.fetch = (async (url: string | URL | Request) => {
      capturedUrl = url
      return new Response(JSON.stringify(rawAgentSchema), { status: 200 })
    }) as typeof fetch

    const schema = await getProfileSchema('corp_analytics')
    assert.strictEqual(capturedUrl, '/agent/v1/profiles/corp_analytics/schema')
    assert.strictEqual(schema.database_name, 'CorpDW')
    // Raw agent fields present
    assert.strictEqual((schema as Record<string, unknown>).total_tables, 1)
    assert.strictEqual((schema as Record<string, unknown>).is_model_facing, true)
  })

  await t.test('getProfileSchema() with project: true applies projectToModelFacingSchema', async () => {
    globalThis.fetch = (async () => {
      return new Response(JSON.stringify(rawAgentSchema), { status: 200 })
    }) as typeof fetch

    const projected = await getProfileSchema('corp_analytics', { project: true })
    assert.strictEqual(projected.database_name, 'CorpDW')
    assert.strictEqual(projected.tables.length, 1)
    // Extra forbidden fields stripped by projection
    assert.strictEqual('is_model_facing' in projected, false)
    assert.strictEqual('total_tables' in projected, false)
    assert.strictEqual('indexes' in projected.tables[0], false)
    assert.strictEqual('default' in projected.tables[0].columns[0], false)
  })

  await t.test('getProjectedProfileSchema() helper always returns projected schema', async () => {
    globalThis.fetch = (async () => {
      return new Response(JSON.stringify(rawAgentSchema), { status: 200 })
    }) as typeof fetch

    const projected = await getProjectedProfileSchema('corp_analytics')
    assert.strictEqual(projected.database_name, 'CorpDW')
    assert.strictEqual('is_model_facing' in projected, false)
    assert.strictEqual(projected.tables[0].columns.length, 2)
  })

  await t.test('safely encodes profile identifiers with spaces and special characters', async () => {
    let capturedUrl: string | URL | Request | undefined

    globalThis.fetch = (async (url: string | URL | Request) => {
      capturedUrl = url
      return new Response(JSON.stringify(rawAgentSchema), { status: 200 })
    }) as typeof fetch

    await getProfileSchema('corp/analytics#1 & dev')
    assert.strictEqual(capturedUrl, '/agent/v1/profiles/corp%2Fanalytics%231%20%26%20dev/schema')
  })

  await t.test('rejects empty or whitespace profileId', async () => {
    await assert.rejects(
      async () => getProfileSchema(''),
      (err: unknown) => err instanceof AgentClientError && err.kind === 'validation' && /non-empty/.test(err.message)
    )
    await assert.rejects(
      async () => getProfileSchema('   '),
      (err: unknown) => err instanceof AgentClientError && err.kind === 'validation'
    )
  })
})

// ==============================================================================
// 6. Query Execution
// ==============================================================================

test('Local Agent Client - Query Execution', async (t) => {
  const originalFetch = globalThis.fetch

  t.afterEach(() => {
    globalThis.fetch = originalFetch
  })

  const mockExecResponse = {
    columns: ['CustomerID', 'CompanyName'],
    rows: [{ CustomerID: 1, CompanyName: 'Acme Corp' }],
    row_count: 1,
    truncated: false,
    execution_time_ms: 12.5,
    request_id: 'req-exec-42',
  }

  await t.test('executeProfileQuery() sends exact { sql, request_id } request shape and credentials', async () => {
    let capturedUrl: string | URL | Request | undefined
    let capturedOptions: RequestInit | undefined

    globalThis.fetch = (async (url: string | URL | Request, options?: RequestInit) => {
      capturedUrl = url
      capturedOptions = options
      return new Response(JSON.stringify(mockExecResponse), { status: 200 })
    }) as typeof fetch

    const sql = 'SELECT CustomerID, CompanyName FROM dbo.Customers'
    const result = await executeProfileQuery('corp_analytics', sql, 'req-exec-42')

    assert.strictEqual(capturedUrl, '/agent/v1/profiles/corp_analytics/execute')
    assert.strictEqual(capturedOptions?.method, 'POST')
    assert.strictEqual(capturedOptions?.credentials, 'include')

    const headers = capturedOptions?.headers as Record<string, string>
    assert.strictEqual(headers[AGENT_CUSTOM_HEADER], AGENT_CUSTOM_HEADER_VALUE)
    assert.strictEqual(headers['Content-Type'], 'application/json')
    assert.strictEqual(headers['X-Request-ID'], 'req-exec-42')

    const body = JSON.parse(String(capturedOptions?.body))
    assert.deepStrictEqual(body, {
      sql,
      request_id: 'req-exec-42',
    })

    assert.strictEqual(result.columns.length, 2)
    assert.strictEqual(result.row_count, 1)
    assert.strictEqual(result.rows[0].CompanyName, 'Acme Corp')
    assert.strictEqual(result.request_id, 'req-exec-42')
  })

  await t.test('omits request_id from body if none provided', async () => {
    let capturedOptions: RequestInit | undefined

    globalThis.fetch = (async (_url: string | URL | Request, options?: RequestInit) => {
      capturedOptions = options
      return new Response(JSON.stringify(mockExecResponse), { status: 200 })
    }) as typeof fetch

    await executeProfileQuery('corp_analytics', 'SELECT 1')
    const body = JSON.parse(String(capturedOptions?.body))
    assert.strictEqual('request_id' in body, false)
  })

  await t.test('rejects empty SQL candidate string', async () => {
    await assert.rejects(
      async () => executeProfileQuery('corp_analytics', ''),
      (err: unknown) => err instanceof AgentClientError && err.kind === 'validation' && /empty/.test(err.message)
    )
    await assert.rejects(
      async () => executeProfileQuery('corp_analytics', '   \n  '),
      (err: unknown) => err instanceof AgentClientError && err.kind === 'validation'
    )
  })

  await t.test('rejects SQL candidate exceeding maximum length', async () => {
    const hugeSql = 'SELECT ' + 'x'.repeat(MAX_SQL_QUERY_LENGTH + 1)
    await assert.rejects(
      async () => executeProfileQuery('corp_analytics', hugeSql),
      (err: unknown) =>
        err instanceof AgentClientError && err.kind === 'validation' && /maximum allowed length/.test(err.message)
    )
  })
})

// ==============================================================================
// 7. Error Classification & Safeguards
// ==============================================================================

test('Local Agent Client - Error Classification', async (t) => {
  const originalFetch = globalThis.fetch

  t.afterEach(() => {
    globalThis.fetch = originalFetch
  })

  await t.test('HTTP 401 is classified as "authentication"', async () => {
    globalThis.fetch = (async () => {
      return new Response(
        JSON.stringify({ detail: 'Session has expired or is invalid. Please re-authenticate.' }),
        { status: 401, headers: { 'Content-Type': 'application/json' } }
      )
    }) as typeof fetch

    await assert.rejects(
      async () => getProfiles(),
      (err: unknown) => {
        assert(err instanceof AgentClientError)
        assert.strictEqual(err.kind, 'authentication')
        assert.strictEqual(err.statusCode, 401)
        assert(err.message.includes('Session has expired'))
        return true
      }
    )
  })

  await t.test('HTTP 403 is classified as "forbidden"', async () => {
    globalThis.fetch = (async () => {
      return new Response(
        JSON.stringify({
          error: 'FORBIDDEN_ORIGIN',
          detail: "Origin 'http://evil.com' is not permitted.",
        }),
        { status: 403, headers: { 'Content-Type': 'application/json' } }
      )
    }) as typeof fetch

    await assert.rejects(
      async () => getProfiles(),
      (err: unknown) => {
        assert(err instanceof AgentClientError)
        assert.strictEqual(err.kind, 'forbidden')
        assert.strictEqual(err.statusCode, 403)
        assert.strictEqual(err.errorCode, 'FORBIDDEN_ORIGIN')
        return true
      }
    )
  })

  await t.test('HTTP 404 is classified as "not_found"', async () => {
    globalThis.fetch = (async () => {
      return new Response(
        JSON.stringify({ detail: "Profile 'unknown_db' not found." }),
        { status: 404, headers: { 'Content-Type': 'application/json' } }
      )
    }) as typeof fetch

    await assert.rejects(
      async () => getProfileSchema('unknown_db'),
      (err: unknown) => {
        assert(err instanceof AgentClientError)
        assert.strictEqual(err.kind, 'not_found')
        assert.strictEqual(err.statusCode, 404)
        assert(err.message.includes("Profile 'unknown_db' not found."))
        return true
      }
    )
  })

  await t.test('HTTP 504 is classified as "timeout"', async () => {
    globalThis.fetch = (async () => {
      return new Response(
        JSON.stringify({ detail: 'Query execution exceeded statement timeout of 10s.' }),
        { status: 504, headers: { 'Content-Type': 'application/json' } }
      )
    }) as typeof fetch

    await assert.rejects(
      async () => executeProfileQuery('corp_analytics', 'SELECT 1'),
      (err: unknown) => {
        assert(err instanceof AgentClientError)
        assert.strictEqual(err.kind, 'timeout')
        assert.strictEqual(err.statusCode, 504)
        assert(err.message.includes('timeout'))
        return true
      }
    )
  })

  await t.test('HTTP 429 is classified as "rate_limit"', async () => {
    globalThis.fetch = (async () => {
      return new Response(
        JSON.stringify({ error: 'CONCURRENCY_LIMIT_EXCEEDED', detail: 'Agent concurrency limit exceeded.' }),
        { status: 429, headers: { 'Content-Type': 'application/json' } }
      )
    }) as typeof fetch

    await assert.rejects(
      async () => getProfiles(),
      (err: unknown) => {
        assert(err instanceof AgentClientError)
        assert.strictEqual(err.kind, 'rate_limit')
        assert.strictEqual(err.statusCode, 429)
        return true
      }
    )
  })

  await t.test('HTTP 413 is classified as "payload_limit"', async () => {
    globalThis.fetch = (async () => {
      return new Response(
        JSON.stringify({ detail: 'Request body exceeds 65536 bytes limit.' }),
        { status: 413, headers: { 'Content-Type': 'application/json' } }
      )
    }) as typeof fetch

    await assert.rejects(
      async () => executeProfileQuery('corp_analytics', 'SELECT 1'),
      (err: unknown) => {
        assert(err instanceof AgentClientError)
        assert.strictEqual(err.kind, 'payload_limit')
        assert.strictEqual(err.statusCode, 413)
        return true
      }
    )
  })

  await t.test('HTTP 400 with missing custom header is classified as "validation"', async () => {
    globalThis.fetch = (async () => {
      return new Response(
        JSON.stringify({
          error: 'MISSING_CUSTOM_HEADER',
          detail: "State-changing requests require 'X-QueryPilot-Request: 1' header.",
        }),
        { status: 400, headers: { 'Content-Type': 'application/json' } }
      )
    }) as typeof fetch

    await assert.rejects(
      async () => logoutAgent(),
      (err: unknown) => {
        assert(err instanceof AgentClientError)
        assert.strictEqual(err.kind, 'validation')
        assert.strictEqual(err.statusCode, 400)
        assert.strictEqual(err.errorCode, 'MISSING_CUSTOM_HEADER')
        return true
      }
    )
  })

  await t.test('HTTP 500 is classified as "server"', async () => {
    globalThis.fetch = (async () => {
      return new Response(
        JSON.stringify({ detail: 'Database connection failed unexpectedly.' }),
        { status: 500, headers: { 'Content-Type': 'application/json' } }
      )
    }) as typeof fetch

    await assert.rejects(
      async () => getProfiles(),
      (err: unknown) => {
        assert(err instanceof AgentClientError)
        assert.strictEqual(err.kind, 'server')
        assert.strictEqual(err.statusCode, 500)
        return true
      }
    )
  })

  await t.test('network rejection (fetch failed) is classified as "disconnected"', async () => {
    globalThis.fetch = (async () => {
      throw new TypeError('fetch failed: connect ECONNREFUSED 127.0.0.1:8765')
    }) as typeof fetch

    await assert.rejects(
      async () => getProfiles(),
      (err: unknown) => {
        assert(err instanceof AgentClientError)
        assert.strictEqual(err.kind, 'disconnected')
        assert(err.message.includes('Unable to connect to QueryPilot Local Agent daemon'))
        return true
      }
    )
  })

  await t.test('malformed non-JSON 200 response is classified as "parse"', async () => {
    globalThis.fetch = (async () => {
      return new Response('<html><body>Gateway error</body></html>', { status: 200 })
    }) as typeof fetch

    await assert.rejects(
      async () => getProfiles(),
      (err: unknown) => {
        assert(err instanceof AgentClientError)
        assert.strictEqual(err.kind, 'parse')
        assert(err.message.includes('Invalid JSON response'))
        return true
      }
    )
  })

  await t.test('sanitizes password patterns in error responses', async () => {
    globalThis.fetch = (async () => {
      return new Response(
        JSON.stringify({ detail: 'Connection to db failed: password=superSecretPassword123 is invalid' }),
        { status: 500, headers: { 'Content-Type': 'application/json' } }
      )
    }) as typeof fetch

    await assert.rejects(
      async () => getProfiles(),
      (err: unknown) => {
        assert(err instanceof AgentClientError)
        assert(!err.message.includes('superSecretPassword123'))
        assert(err.message.includes('[REDACTED]'))
        return true
      }
    )
  })

  await t.test('propagates AbortError without converting to disconnected', async () => {
    const controller = new AbortController()
    controller.abort()

    globalThis.fetch = (async (_url: string | URL | Request, options?: RequestInit) => {
      if (options?.signal?.aborted) {
        throw new DOMException('The user aborted a request.', 'AbortError')
      }
      return new Response(JSON.stringify({}), { status: 200 })
    }) as typeof fetch

    await assert.rejects(
      async () => getProfiles({ signal: controller.signal }),
      (err: unknown) => {
        assert(err instanceof DOMException)
        assert.strictEqual(err.name, 'AbortError')
        return true
      }
    )
  })
})
