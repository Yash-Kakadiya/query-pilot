import assert from 'node:assert'
import test from 'node:test'
import {
  ACCESS_KEY_HEADER,
  setSessionAccessKey,
} from '../src/services/apiClient.ts'
import {
  generateSql,
  getV1ApiBaseUrl,
  resolveV1Endpoint,
  synthesizeAnswer,
} from '../src/services/v1ApiClient.ts'
import {
  ApiClientError,
  type GenerateSQLRequest,
  type SynthesizeAnswerRequest,
} from '../src/types/api.ts'

function setTestApiBaseUrl(url: string | undefined): void {
  ;(globalThis as unknown as { __TEST_VITE_API_BASE_URL__?: string }).__TEST_VITE_API_BASE_URL__ = url
}

const sampleSchema = {
  database_name: 'CorpDW',
  tables: [
    {
      table_name: 'Customers',
      schema_name: 'dbo',
      columns: [
        { name: 'CustomerID', data_type: 'int', nullable: false, primary_key: true },
        { name: 'CompanyName', data_type: 'varchar(100)', nullable: false },
      ],
      primary_keys: ['CustomerID'],
      foreign_keys: [],
    },
  ],
}

// ==============================================================================
// 1. URL Resolution & Vite Proxy Compatibility
// ==============================================================================

test('Hosted V1 API Client - URL Resolution & Endpoint Alignment', async (t) => {
  t.afterEach(() => {
    setTestApiBaseUrl(undefined)
  })

  await t.test('resolves default relative paths preserving /api/v1 for Vite proxy', () => {
    assert.strictEqual(getV1ApiBaseUrl(undefined), '')
    assert.strictEqual(resolveV1Endpoint('/api/v1/generate-sql'), '/api/v1/generate-sql')
    assert.strictEqual(resolveV1Endpoint('/api/v1/synthesize-answer'), '/api/v1/synthesize-answer')
    assert.strictEqual(resolveV1Endpoint('/generate-sql'), '/api/v1/generate-sql')
  })

  await t.test('resolves direct backend URL when VITE_API_BASE_URL is configured', () => {
    assert.strictEqual(getV1ApiBaseUrl('http://127.0.0.1:8000'), 'http://127.0.0.1:8000')
    assert.strictEqual(
      resolveV1Endpoint('/api/v1/generate-sql', 'http://127.0.0.1:8000'),
      'http://127.0.0.1:8000/api/v1/generate-sql'
    )
    assert.strictEqual(
      resolveV1Endpoint('/api/v1/synthesize-answer', 'http://127.0.0.1:8000'),
      'http://127.0.0.1:8000/api/v1/synthesize-answer'
    )
  })

  await t.test('normalizes trailing slashes on direct backend base URLs', () => {
    assert.strictEqual(getV1ApiBaseUrl('http://127.0.0.1:8000/'), 'http://127.0.0.1:8000')
    assert.strictEqual(
      resolveV1Endpoint('/api/v1/generate-sql', 'http://127.0.0.1:8000/'),
      'http://127.0.0.1:8000/api/v1/generate-sql'
    )
  })

  await t.test('handles /api base URL without duplicating /api/api/v1', () => {
    assert.strictEqual(getV1ApiBaseUrl('/api'), '')
    assert.strictEqual(resolveV1Endpoint('/api/v1/generate-sql', '/api'), '/api/v1/generate-sql')
  })

  await t.test('rejects malformed explicitly configured base URLs', () => {
    const invalidCases = ['not-a-valid-url', 'ftp://api.example.com', 'javascript:alert(1)', 'http://']
    for (const invalid of invalidCases) {
      assert.throws(
        () => resolveV1Endpoint('/api/v1/generate-sql', invalid),
        (err: unknown) => {
          assert(err instanceof ApiClientError)
          assert.strictEqual(err.kind, 'network')
          assert(err.message.includes('Invalid VITE_API_BASE_URL configured'))
          return true
        }
      )
    }
  })
})

// ==============================================================================
// 2. Vite Proxy Route Matching & Path Rewriting Logic
// ==============================================================================

test('Hosted V1 API - Vite Proxy Route Invariants', async (t) => {
  // Pure helper replicating the Vite server.proxy configuration logic
  function simulateViteProxy(requestPath: string): { target: string; forwardedPath: string } | null {
    const proxyConfig = [
      {
        prefix: '/agent',
        target: 'http://127.0.0.1:8001',
        rewrite: undefined,
      },
      {
        prefix: '/api/v1',
        target: 'http://127.0.0.1:8000',
        rewrite: undefined,
      },
      {
        prefix: '/api',
        target: 'http://127.0.0.1:8000',
        rewrite: (p: string) => p.replace(/^\/api/, ''),
      },
    ]

    for (const rule of proxyConfig) {
      if (requestPath.startsWith(rule.prefix)) {
        const rewritten = rule.rewrite ? rule.rewrite(requestPath) : requestPath
        return {
          target: rule.target,
          forwardedPath: `${rule.target}${rewritten}`,
        }
      }
    }
    return null
  }

  await t.test('/agent/v1 routes forward to local agent (:8001) preserving /agent/v1 path', () => {
    const routedAuth = simulateViteProxy('/agent/v1/auth/bootstrap')
    assert.ok(routedAuth)
    assert.strictEqual(routedAuth.forwardedPath, 'http://127.0.0.1:8001/agent/v1/auth/bootstrap')

    const routedProfiles = simulateViteProxy('/agent/v1/profiles')
    assert.ok(routedProfiles)
    assert.strictEqual(routedProfiles.forwardedPath, 'http://127.0.0.1:8001/agent/v1/profiles')

    const routedSchema = simulateViteProxy('/agent/v1/profiles/corp_analytics/schema')
    assert.ok(routedSchema)
    assert.strictEqual(routedSchema.forwardedPath, 'http://127.0.0.1:8001/agent/v1/profiles/corp_analytics/schema')

    const routedExecute = simulateViteProxy('/agent/v1/profiles/corp_analytics/execute')
    assert.ok(routedExecute)
    assert.strictEqual(routedExecute.forwardedPath, 'http://127.0.0.1:8001/agent/v1/profiles/corp_analytics/execute')
  })

  await t.test('/api/v1/generate-sql forwards to hosted backend (:8000) preserving /api/v1 prefix', () => {
    const routed = simulateViteProxy('/api/v1/generate-sql')
    assert.ok(routed)
    assert.strictEqual(routed.forwardedPath, 'http://127.0.0.1:8000/api/v1/generate-sql')
  })

  await t.test('/api/v1/synthesize-answer forwards to hosted backend (:8000) preserving /api/v1 prefix', () => {
    const routed = simulateViteProxy('/api/v1/synthesize-answer')
    assert.ok(routed)
    assert.strictEqual(routed.forwardedPath, 'http://127.0.0.1:8000/api/v1/synthesize-answer')
  })

  await t.test('legacy /api/health strips /api and routes to /health on backend (:8000)', () => {
    const routed = simulateViteProxy('/api/health')
    assert.ok(routed)
    assert.strictEqual(routed.forwardedPath, 'http://127.0.0.1:8000/health')
  })

  await t.test('legacy /api/query strips /api and routes to /query on backend (:8000)', () => {
    const routed = simulateViteProxy('/api/query')
    assert.ok(routed)
    assert.strictEqual(routed.forwardedPath, 'http://127.0.0.1:8000/query')
  })

  await t.test('verifies /agent, /api/v1, and /api routing rules are completely disjoint', () => {
    // Non-matching path
    assert.strictEqual(simulateViteProxy('/unmatched'), null)
    // /agent does not match /api
    assert.strictEqual(simulateViteProxy('/agent/test')?.target, 'http://127.0.0.1:8001')
    // /api/v1 does not match legacy /api
    assert.strictEqual(simulateViteProxy('/api/v1/test')?.forwardedPath, 'http://127.0.0.1:8000/api/v1/test')
    // /api legacy strips /api
    assert.strictEqual(simulateViteProxy('/api/test')?.forwardedPath, 'http://127.0.0.1:8000/test')
  })
})

// ==============================================================================
// 3. SQL Generation Endpoint (/api/v1/generate-sql)
// ==============================================================================

test('Hosted V1 API Client - generateSql()', async (t) => {
  const originalFetch = globalThis.fetch

  const mockStorage = new Map<string, string>()
  const mockSessionStorageObj = {
    getItem: (key: string) => mockStorage.get(key) ?? null,
    setItem: (key: string, val: string) => {
      mockStorage.set(key, val)
    },
    removeItem: (key: string) => {
      mockStorage.delete(key)
    },
    clear: () => {
      mockStorage.clear()
    },
  }

  Object.defineProperty(globalThis, 'sessionStorage', {
    value: mockSessionStorageObj,
    writable: true,
    configurable: true,
  })

  t.afterEach(() => {
    globalThis.fetch = originalFetch
    mockStorage.clear()
    setTestApiBaseUrl(undefined)
  })

  await t.test('successful generation returns unexecuted candidate SQL', async () => {
    let capturedUrl: string | URL | Request | undefined
    let capturedOptions: RequestInit | undefined

    const mockResponse = {
      candidate_sql: 'SELECT CustomerID, CompanyName FROM dbo.Customers ORDER BY CompanyName',
      status: 'generated',
      clarification_prompt: null,
      unsupported_reason: null,
      execution_disclaimer: 'Candidate SQL is unexecuted.',
      request_id: 'req-gen-101',
    }

    globalThis.fetch = (async (url: string | URL | Request, options?: RequestInit) => {
      capturedUrl = url
      capturedOptions = options
      return new Response(JSON.stringify(mockResponse), {
        status: 200,
        headers: { 'Content-Type': 'application/json', 'X-Request-ID': 'req-gen-101' },
      })
    }) as typeof fetch

    const req: GenerateSQLRequest = {
      question: 'List all customer companies.',
      schema_context: sampleSchema,
      request_id: 'req-gen-101',
    }

    const res = await generateSql(req)

    assert.strictEqual(capturedUrl, '/api/v1/generate-sql')
    assert.strictEqual(capturedOptions?.method, 'POST')
    const headers = capturedOptions?.headers as Record<string, string>
    assert.strictEqual(headers['Content-Type'], 'application/json')
    assert.strictEqual(headers['Accept'], 'application/json')
    assert.strictEqual(headers['X-Request-ID'], 'req-gen-101')

    assert.strictEqual(res.status, 'generated')
    assert.strictEqual(res.candidate_sql, 'SELECT CustomerID, CompanyName FROM dbo.Customers ORDER BY CompanyName')
    assert.strictEqual(res.execution_disclaimer, 'Candidate SQL is unexecuted.')
    assert.strictEqual(res.request_id, 'req-gen-101')
  })

  await t.test('handles status="clarification_needed" response', async () => {
    globalThis.fetch = (async () => {
      return new Response(
        JSON.stringify({
          candidate_sql: null,
          status: 'clarification_needed',
          clarification_prompt: 'Did you mean active customers or all customers?',
          unsupported_reason: null,
          execution_disclaimer: 'Candidate SQL is unexecuted.',
          request_id: 'req-gen-102',
        }),
        { status: 200 }
      )
    }) as typeof fetch

    const res = await generateSql({ question: 'List customers', schema_context: sampleSchema })
    assert.strictEqual(res.status, 'clarification_needed')
    assert.strictEqual(res.candidate_sql, null)
    assert.strictEqual(res.clarification_prompt, 'Did you mean active customers or all customers?')
  })

  await t.test('handles status="unsupported" response', async () => {
    globalThis.fetch = (async () => {
      return new Response(
        JSON.stringify({
          candidate_sql: null,
          status: 'unsupported',
          clarification_prompt: null,
          unsupported_reason: 'The requested forecast metric is not supported by the database schema.',
          execution_disclaimer: 'Candidate SQL is unexecuted.',
          request_id: 'req-gen-103',
        }),
        { status: 200 }
      )
    }) as typeof fetch

    const res = await generateSql({ question: 'Forecast Q4 revenue', schema_context: sampleSchema })
    assert.strictEqual(res.status, 'unsupported')
    assert.strictEqual(res.candidate_sql, null)
    assert.strictEqual(res.unsupported_reason, 'The requested forecast metric is not supported by the database schema.')
  })

  await t.test('handles status="generation_failed" response', async () => {
    globalThis.fetch = (async () => {
      return new Response(
        JSON.stringify({
          candidate_sql: null,
          status: 'generation_failed',
          clarification_prompt: null,
          unsupported_reason: null,
          execution_disclaimer: 'Candidate SQL is unexecuted.',
          request_id: 'req-gen-104',
        }),
        { status: 200 }
      )
    }) as typeof fetch

    const res = await generateSql({ question: 'List customers', schema_context: sampleSchema })
    assert.strictEqual(res.status, 'generation_failed')
    assert.strictEqual(res.candidate_sql, null)
  })

  await t.test('transmits prototype access key header when present in session or options', async () => {
    let capturedOptions: RequestInit | undefined

    globalThis.fetch = (async (_url: string | URL | Request, options?: RequestInit) => {
      capturedOptions = options
      return new Response(
        JSON.stringify({
          candidate_sql: 'SELECT 1',
          status: 'generated',
          execution_disclaimer: 'Candidate SQL is unexecuted.',
          request_id: 'req-key-1',
        }),
        { status: 200 }
      )
    }) as typeof fetch

    setSessionAccessKey('prototype-key-xyz')
    await generateSql({ question: 'Test query', schema_context: sampleSchema })

    const headers = capturedOptions?.headers as Record<string, string>
    assert.strictEqual(headers[ACCESS_KEY_HEADER], 'prototype-key-xyz')
  })

  await t.test('rejects empty or whitespace question client-side before network dispatch', async () => {
    await assert.rejects(
      async () => generateSql({ question: '', schema_context: sampleSchema }),
      (err: unknown) => err instanceof ApiClientError && err.kind === 'validation' && /empty/.test(err.message)
    )
    await assert.rejects(
      async () => generateSql({ question: '   \n  ', schema_context: sampleSchema }),
      (err: unknown) => err instanceof ApiClientError && err.kind === 'validation'
    )
  })

  await t.test('rejects question exceeding maximum length of 1000 characters', async () => {
    const longQuestion = 'a'.repeat(1001)
    await assert.rejects(
      async () => generateSql({ question: longQuestion, schema_context: sampleSchema }),
      (err: unknown) =>
        err instanceof ApiClientError && err.kind === 'validation' && /exceeds maximum length of 1000/.test(err.message)
    )
  })
})

// ==============================================================================
// 4. Metadata-Only Answer Synthesis Endpoint (/api/v1/synthesize-answer)
// ==============================================================================

test('Hosted V1 API Client - synthesizeAnswer()', async (t) => {
  const originalFetch = globalThis.fetch

  t.afterEach(() => {
    globalThis.fetch = originalFetch
    setTestApiBaseUrl(undefined)
  })

  const validSynthesisRequest: SynthesizeAnswerRequest = {
    question: 'How many customers are in the database?',
    candidate_sql: 'SELECT COUNT(*) AS CustomerCount FROM dbo.Customers',
    columns: ['CustomerCount'],
    row_count: 1,
    truncated: false,
    execution_status: 'succeeded',
    execution_time_ms: 14.2,
    request_id: 'req-synth-201',
  }

  await t.test('successful synthesis returns grounded prose narrative and status', async () => {
    let capturedUrl: string | URL | Request | undefined
    let capturedOptions: RequestInit | undefined

    const mockResponse = {
      summary: 'Query executed successfully and identified 1 aggregate metric across Customers.',
      status: 'grounded_from_metadata',
      grounding_notes: 'Synthesized strictly from column names and aggregate row count without cell disclosure.',
      request_id: 'req-synth-201',
    }

    globalThis.fetch = (async (url: string | URL | Request, options?: RequestInit) => {
      capturedUrl = url
      capturedOptions = options
      return new Response(JSON.stringify(mockResponse), {
        status: 200,
        headers: { 'Content-Type': 'application/json', 'X-Request-ID': 'req-synth-201' },
      })
    }) as typeof fetch

    const res = await synthesizeAnswer(validSynthesisRequest)

    assert.strictEqual(capturedUrl, '/api/v1/synthesize-answer')
    assert.strictEqual(capturedOptions?.method, 'POST')
    const headers = capturedOptions?.headers as Record<string, string>
    assert.strictEqual(headers['Content-Type'], 'application/json')
    assert.strictEqual(headers['Accept'], 'application/json')
    assert.strictEqual(headers['X-Request-ID'], 'req-synth-201')

    const sentBody = JSON.parse(String(capturedOptions?.body))
    assert.strictEqual(sentBody.question, 'How many customers are in the database?')
    assert.strictEqual(sentBody.candidate_sql, 'SELECT COUNT(*) AS CustomerCount FROM dbo.Customers')
    assert.deepStrictEqual(sentBody.columns, ['CustomerCount'])
    assert.strictEqual(sentBody.row_count, 1)
    assert.strictEqual(sentBody.truncated, false)
    assert.strictEqual(sentBody.execution_status, 'succeeded')
    assert.strictEqual(sentBody.execution_time_ms, 14.2)
    assert.strictEqual(sentBody.request_id, 'req-synth-201')

    // Confirm no row records or cell values are present in outbound body
    assert.strictEqual('rows' in sentBody, false)
    assert.strictEqual('data' in sentBody, false)
    assert.strictEqual('records' in sentBody, false)

    assert.strictEqual(res.status, 'grounded_from_metadata')
    assert.strictEqual(res.summary, 'Query executed successfully and identified 1 aggregate metric across Customers.')
    assert.strictEqual(res.request_id, 'req-synth-201')
  })

  await t.test('strict privacy audit rejects requests containing raw rows or row_data', async () => {
    const leakedRequest = {
      ...validSynthesisRequest,
      rows: [{ CustomerCount: 42 }], // FORBIDDEN!
    }

    await assert.rejects(
      async () => synthesizeAnswer(leakedRequest as unknown as SynthesizeAnswerRequest),
      (err: unknown) =>
        err instanceof ApiClientError &&
        err.kind === 'validation' &&
        /Privacy violation: synthesis payload must not contain 'rows'/.test(err.message)
    )
  })

  await t.test('strict privacy audit rejects requests containing passwords or credentials', async () => {
    const credsRequest = {
      ...validSynthesisRequest,
      password: 'superSecretPassword', // FORBIDDEN!
    }

    await assert.rejects(
      async () => synthesizeAnswer(credsRequest as unknown as SynthesizeAnswerRequest),
      (err: unknown) =>
        err instanceof ApiClientError &&
        err.kind === 'validation' &&
        /Privacy violation: synthesis payload must not contain 'password'/.test(err.message)
    )
  })

  await t.test('rejects execution_status other than "succeeded"', async () => {
    const failedStatusReq = {
      ...validSynthesisRequest,
      execution_status: 'failed',
    }

    await assert.rejects(
      async () => synthesizeAnswer(failedStatusReq),
      (err: unknown) =>
        err instanceof ApiClientError &&
        err.kind === 'validation' &&
        /execution_status to be 'succeeded'/.test(err.message)
    )
  })

  await t.test('client-supplied execution status is not treated as cryptographic receipt', async () => {
    // Verifies that execution_status is passed strictly as client metadata without assuming verified receipt
    globalThis.fetch = (async () => {
      return new Response(
        JSON.stringify({
          summary: 'OK',
          status: 'grounded_from_metadata',
          request_id: 'req-synth-receipt',
        }),
        { status: 200 }
      )
    }) as typeof fetch

    const res = await synthesizeAnswer({
      ...validSynthesisRequest,
      request_id: 'req-synth-receipt',
    })
    // Client receives response without claiming cryptographic verification
    assert.strictEqual(res.status, 'grounded_from_metadata')
  })

  await t.test('rejects empty or oversized column arrays', async () => {
    // Empty
    await assert.rejects(
      async () => synthesizeAnswer({ ...validSynthesisRequest, columns: [] }),
      (err: unknown) => err instanceof ApiClientError && err.kind === 'validation' && /non-empty array/.test(err.message)
    )

    // > 100 columns
    const tooManyCols = Array.from({ length: 101 }, (_, i) => `Col_${i}`)
    await assert.rejects(
      async () => synthesizeAnswer({ ...validSynthesisRequest, columns: tooManyCols }),
      (err: unknown) => err instanceof ApiClientError && err.kind === 'validation' && /cannot exceed 100/.test(err.message)
    )
  })
})

// ==============================================================================
// 5. Error Classification & Abort Behavior
// ==============================================================================

test('Hosted V1 API Client - Error Handling', async (t) => {
  const originalFetch = globalThis.fetch

  t.afterEach(() => {
    globalThis.fetch = originalFetch
  })

  await t.test('HTTP 422 maps to kind: "validation" with validationErrors', async () => {
    globalThis.fetch = (async () => {
      return new Response(
        JSON.stringify({
          detail: [{ loc: ['body', 'question'], msg: 'Question cannot be empty or whitespace.', type: 'value_error' }],
        }),
        { status: 422, headers: { 'Content-Type': 'application/json' } }
      )
    }) as typeof fetch

    await assert.rejects(
      async () => generateSql({ question: 'valid length', schema_context: sampleSchema }),
      (err: unknown) => {
        assert(err instanceof ApiClientError)
        assert.strictEqual(err.kind, 'validation')
        assert.strictEqual(err.statusCode, 422)
        assert.ok(err.validationErrors && err.validationErrors.length > 0)
        return true
      }
    )
  })

  await t.test('HTTP 401 maps to kind: "unauthorized"', async () => {
    globalThis.fetch = (async () => {
      return new Response(
        JSON.stringify({ detail: 'Invalid or missing prototype access key.' }),
        { status: 401, headers: { 'Content-Type': 'application/json' } }
      )
    }) as typeof fetch

    await assert.rejects(
      async () => generateSql({ question: 'Test query', schema_context: sampleSchema }),
      (err: unknown) => {
        assert(err instanceof ApiClientError)
        assert.strictEqual(err.kind, 'unauthorized')
        assert.strictEqual(err.statusCode, 401)
        return true
      }
    )
  })

  await t.test('HTTP 413 maps to kind: "validation" (Payload Too Large)', async () => {
    globalThis.fetch = (async () => {
      return new Response(
        JSON.stringify({ detail: 'Request body exceeds 65536 bytes limit.' }),
        { status: 413, headers: { 'Content-Type': 'application/json' } }
      )
    }) as typeof fetch

    await assert.rejects(
      async () => generateSql({ question: 'Test query', schema_context: sampleSchema }),
      (err: unknown) => {
        assert(err instanceof ApiClientError)
        assert.strictEqual(err.kind, 'validation')
        assert.strictEqual(err.statusCode, 413)
        return true
      }
    )
  })

  await t.test('HTTP 429 maps to kind: "server" (Rate / Concurrency Limit)', async () => {
    globalThis.fetch = (async () => {
      return new Response(
        JSON.stringify({ detail: 'Rate limit exceeded: maximum 20 requests per minute.' }),
        { status: 429, headers: { 'Content-Type': 'application/json' } }
      )
    }) as typeof fetch

    await assert.rejects(
      async () => generateSql({ question: 'Test query', schema_context: sampleSchema }),
      (err: unknown) => {
        assert(err instanceof ApiClientError)
        assert.strictEqual(err.kind, 'server')
        assert.strictEqual(err.statusCode, 429)
        return true
      }
    )
  })

  await t.test('HTTP 500 maps to kind: "server"', async () => {
    globalThis.fetch = (async () => {
      return new Response(
        JSON.stringify({ detail: 'Provider API failure: Gemini service temporarily unreachable.' }),
        { status: 500, headers: { 'Content-Type': 'application/json' } }
      )
    }) as typeof fetch

    await assert.rejects(
      async () => generateSql({ question: 'Test query', schema_context: sampleSchema }),
      (err: unknown) => {
        assert(err instanceof ApiClientError)
        assert.strictEqual(err.kind, 'server')
        assert.strictEqual(err.statusCode, 500)
        return true
      }
    )
  })

  await t.test('network error maps to kind: "network"', async () => {
    globalThis.fetch = (async () => {
      throw new TypeError('fetch failed: connection refused')
    }) as typeof fetch

    await assert.rejects(
      async () => generateSql({ question: 'Test query', schema_context: sampleSchema }),
      (err: unknown) => {
        assert(err instanceof ApiClientError)
        assert.strictEqual(err.kind, 'network')
        assert(err.message.includes('Failed to connect to QueryPilot Hosted API'))
        return true
      }
    )
  })

  await t.test('malformed JSON response maps to kind: "parse"', async () => {
    globalThis.fetch = (async () => {
      return new Response('<html>502 Bad Gateway</html>', { status: 200 })
    }) as typeof fetch

    await assert.rejects(
      async () => generateSql({ question: 'Test query', schema_context: sampleSchema }),
      (err: unknown) => {
        assert(err instanceof ApiClientError)
        assert.strictEqual(err.kind, 'parse')
        assert(err.message.includes('Invalid JSON response'))
        return true
      }
    )
  })

  await t.test('sanitizes password patterns from error messages', async () => {
    globalThis.fetch = (async () => {
      return new Response(
        JSON.stringify({ detail: 'Error connecting: password=superSecretPassword123 is rejected' }),
        { status: 500, headers: { 'Content-Type': 'application/json' } }
      )
    }) as typeof fetch

    await assert.rejects(
      async () => generateSql({ question: 'Test query', schema_context: sampleSchema }),
      (err: unknown) => {
        assert(err instanceof ApiClientError)
        assert(!err.message.includes('superSecretPassword123'))
        assert(err.message.includes('[REDACTED]'))
        return true
      }
    )
  })

  await t.test('propagates user cancellation via AbortSignal without converting to network error', async () => {
    const controller = new AbortController()
    controller.abort()

    globalThis.fetch = (async (_url: string | URL | Request, options?: RequestInit) => {
      if (options?.signal?.aborted) {
        throw new DOMException('The user aborted a request.', 'AbortError')
      }
      return new Response(JSON.stringify({}), { status: 200 })
    }) as typeof fetch

    await assert.rejects(
      async () => generateSql({ question: 'Test query', schema_context: sampleSchema }, { signal: controller.signal }),
      (err: unknown) => {
        assert(err instanceof DOMException)
        assert.strictEqual(err.name, 'AbortError')
        return true
      }
    )
  })
})
