import assert from 'node:assert'
import { register } from 'node:module'
import test from 'node:test'
import type { QueryRequest } from '../src/types/api.ts'

const loaderCode = `
export async function load(url, context, nextLoad) {
  const result = await nextLoad(url, context);
  if (url.includes('apiClient.ts')) {
    let src = typeof result.source === 'string' ? result.source : Buffer.from(result.source).toString('utf8');
    src = src.replace(
      '(import.meta as { env?: Record<string, string | undefined> })?.env?.VITE_API_BASE_URL',
      '((globalThis as any).__TEST_VITE_API_BASE_URL__ !== undefined ? (globalThis as any).__TEST_VITE_API_BASE_URL__ : (import.meta as { env?: Record<string, string | undefined> })?.env?.VITE_API_BASE_URL)'
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

const { ApiClientError } = await import('../src/types/api.ts')
const { getApiBaseUrl, resolveEndpoint, getHealth, postQuery } = await import(
  '../src/services/apiClient.ts'
)

function setTestApiBaseUrl(url: string | undefined): void {
  ;(globalThis as unknown as { __TEST_VITE_API_BASE_URL__?: string }).__TEST_VITE_API_BASE_URL__ = url
}

test('Frontend API Client - Base URL and Endpoint Resolution', async (t) => {
  await t.test('resolves default proxy paths when VITE_API_BASE_URL is undefined', () => {
    assert.strictEqual(getApiBaseUrl(undefined), '/api')
    assert.strictEqual(resolveEndpoint('/health', undefined), '/api/health')
    assert.strictEqual(resolveEndpoint('/query', undefined), '/api/query')
  })

  await t.test('resolves default proxy paths when VITE_API_BASE_URL is empty or whitespace', () => {
    assert.strictEqual(getApiBaseUrl(''), '/api')
    assert.strictEqual(getApiBaseUrl('   '), '/api')
    assert.strictEqual(resolveEndpoint('/health', ''), '/api/health')
    assert.strictEqual(resolveEndpoint('/query', '   '), '/api/query')
  })

  await t.test('supports direct backend URL for split-origin deployments', () => {
    assert.strictEqual(getApiBaseUrl('http://127.0.0.1:8000'), 'http://127.0.0.1:8000')
    assert.strictEqual(resolveEndpoint('/health', 'http://127.0.0.1:8000'), 'http://127.0.0.1:8000/health')
    assert.strictEqual(resolveEndpoint('/query', 'http://127.0.0.1:8000'), 'http://127.0.0.1:8000/query')
    assert.strictEqual(resolveEndpoint('/query', 'https://api.querypilot.example.com'), 'https://api.querypilot.example.com/query')
  })

  await t.test('normalizes trailing slashes on base URLs', () => {
    assert.strictEqual(getApiBaseUrl('http://127.0.0.1:8000/'), 'http://127.0.0.1:8000')
    assert.strictEqual(getApiBaseUrl('https://api.example.com///'), 'https://api.example.com')
    assert.strictEqual(getApiBaseUrl('/api/'), '/api')
    assert.strictEqual(resolveEndpoint('/health', 'http://127.0.0.1:8000/'), 'http://127.0.0.1:8000/health')
    assert.strictEqual(resolveEndpoint('/query', 'https://api.example.com///'), 'https://api.example.com/query')
  })

  await t.test('normalizes endpoint paths without leading slash', () => {
    assert.strictEqual(resolveEndpoint('health', 'http://127.0.0.1:8000'), 'http://127.0.0.1:8000/health')
    assert.strictEqual(resolveEndpoint('query', undefined), '/api/query')
  })

  await t.test('supports custom relative proxy paths starting with slash', () => {
    assert.strictEqual(getApiBaseUrl('/custom-api/'), '/custom-api')
    assert.strictEqual(resolveEndpoint('/health', '/custom-api'), '/custom-api/health')
    assert.strictEqual(resolveEndpoint('query', '/custom-api/'), '/custom-api/query')
  })

  await t.test('strictly rejects malformed explicitly configured URLs', () => {
    const invalidCases = [
      'not-a-valid-url',
      'ftp://files.example.com',
      'javascript:alert(1)',
      'http://',
      'https://',
    ]

    for (const invalidUrl of invalidCases) {
      assert.throws(
        () => resolveEndpoint('/query', invalidUrl),
        (err: unknown) => {
          assert(err instanceof ApiClientError, `Expected ApiClientError for ${invalidUrl}`)
          assert.strictEqual(err.kind, 'network')
          assert(err.message.includes('Invalid VITE_API_BASE_URL configured'))
          return true
        }
      )
    }
  })
})

test('Frontend API Client - Public Request Methods (getHealth & postQuery)', async (t) => {
  const originalFetch = globalThis.fetch

  t.afterEach(() => {
    globalThis.fetch = originalFetch
    setTestApiBaseUrl(undefined)
  })

  await t.test('getHealth() default configuration calls fetch("/api/health")', async () => {
    let capturedUrl: string | URL | Request | undefined
    let capturedOptions: RequestInit | undefined

    globalThis.fetch = (async (url: string | URL | Request, options?: RequestInit) => {
      capturedUrl = url
      capturedOptions = options
      return new Response(
        JSON.stringify({
          status: 'healthy',
          version: '0.1.0',
          dialect: 'T-SQL',
          tracing_enabled: false,
        }),
        {
          status: 200,
          headers: { 'Content-Type': 'application/json', 'X-Request-ID': 'req-health-01' },
        }
      )
    }) as typeof fetch

    const health = await getHealth()
    assert.strictEqual(capturedUrl, '/api/health')
    assert.strictEqual(capturedOptions?.method, 'GET')
    const headers = capturedOptions?.headers as Record<string, string>
    assert.strictEqual(headers['Accept'], 'application/json')
    assert.strictEqual(health.status, 'healthy')
    assert.strictEqual(health.version, '0.1.0')
    assert.strictEqual(health.dialect, 'T-SQL')
    assert.strictEqual(health.tracing_enabled, false)
  })

  await t.test('getHealth() configured API base URL calls fetch("http://127.0.0.1:8000/health")', async () => {
    let capturedUrl: string | URL | Request | undefined
    setTestApiBaseUrl('http://127.0.0.1:8000')

    globalThis.fetch = (async (url: string | URL | Request) => {
      capturedUrl = url
      return new Response(
        JSON.stringify({ status: 'healthy', version: '0.1.0', dialect: 'T-SQL', tracing_enabled: false }),
        { status: 200, headers: { 'Content-Type': 'application/json' } }
      )
    }) as typeof fetch

    const health = await getHealth()
    assert.strictEqual(capturedUrl, 'http://127.0.0.1:8000/health')
    assert.strictEqual(health.status, 'healthy')
  })

  await t.test('getHealth() successful response returns health result and non-200 handles X-Request-ID', async () => {
    // 1. Success case
    globalThis.fetch = (async () => {
      return new Response(
        JSON.stringify({
          status: 'healthy',
          version: '0.1.0',
          dialect: 'T-SQL',
          tracing_enabled: true,
        }),
        {
          status: 200,
          headers: { 'Content-Type': 'application/json', 'X-Request-ID': 'req-h-success-123' },
        }
      )
    }) as typeof fetch

    const health = await getHealth()
    assert.strictEqual(health.status, 'healthy')
    assert.strictEqual(health.tracing_enabled, true)

    // 2. Failure case with X-Request-ID
    globalThis.fetch = (async () => {
      return new Response('Service Unavailable', {
        status: 503,
        headers: { 'X-Request-ID': 'req-h-err-503' },
      })
    }) as typeof fetch

    await assert.rejects(
      getHealth(),
      (err: unknown) => {
        assert(err instanceof ApiClientError)
        assert.strictEqual(err.kind, 'server')
        assert.strictEqual(err.statusCode, 503)
        assert.strictEqual(err.requestId, 'req-h-err-503')
        return true
      }
    )
  })

  await t.test('postQuery() default configuration calls fetch("/api/query")', async () => {
    let capturedUrl: string | URL | Request | undefined
    let capturedOptions: RequestInit | undefined

    globalThis.fetch = (async (url: string | URL | Request, options?: RequestInit) => {
      capturedUrl = url
      capturedOptions = options
      return new Response(
        JSON.stringify({
          status: 'SUCCESS',
          question: 'How many students?',
          columns: ['Total'],
          rows: [{ Total: 40 }],
          row_count: 1,
          truncated: false,
          grounded_answer: { text: '40 students', status: 'TEMPLATE_DETERMINISTIC', is_mechanically_verified: true },
        }),
        {
          status: 200,
          headers: { 'Content-Type': 'application/json', 'X-Request-ID': 'req-query-01' },
        }
      )
    }) as typeof fetch

    const queryReq: QueryRequest = { question: 'How many students?' }
    const result = await postQuery(queryReq)

    assert.strictEqual(capturedUrl, '/api/query')
    assert.strictEqual(capturedOptions?.method, 'POST')
    assert.strictEqual(result.status, 'SUCCESS')
    assert.strictEqual(result.row_count, 1)
  })

  await t.test('postQuery() configured base URL with trailing slash calls fetch("https://api.example.com/query")', async () => {
    let capturedUrl: string | URL | Request | undefined
    setTestApiBaseUrl('https://api.example.com/')

    globalThis.fetch = (async (url: string | URL | Request) => {
      capturedUrl = url
      return new Response(
        JSON.stringify({
          status: 'SUCCESS',
          question: 'Total courses?',
          columns: ['Count'],
          rows: [{ Count: 10 }],
          row_count: 1,
          truncated: false,
        }),
        {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }
      )
    }) as typeof fetch

    const result = await postQuery({ question: 'Total courses?' })
    assert.strictEqual(capturedUrl, 'https://api.example.com/query')
    assert.strictEqual(result.status, 'SUCCESS')
  })

  await t.test('postQuery() verifies POST method, expected headers, serialized request body, and request-ID behavior', async () => {
    let capturedUrl: string | URL | Request | undefined
    let capturedOptions: RequestInit | undefined

    globalThis.fetch = (async (url: string | URL | Request, options?: RequestInit) => {
      capturedUrl = url
      capturedOptions = options
      return new Response(
        JSON.stringify({
          status: 'SUCCESS',
          question: 'List active instructors',
          columns: ['Name'],
          rows: [{ Name: 'Dr. Smith' }],
          row_count: 1,
          truncated: false,
          request_id: null,
        }),
        {
          status: 200,
          headers: { 'Content-Type': 'application/json', 'X-Request-ID': 'srv-res-id-999' },
        }
      )
    }) as typeof fetch

    const queryReq: QueryRequest = { question: 'List active instructors' }
    const result = await postQuery(queryReq, { requestId: 'req-client-custom-77' })

    assert.strictEqual(capturedUrl, '/api/query')
    assert.strictEqual(capturedOptions?.method, 'POST')
    const headers = capturedOptions?.headers as Record<string, string>
    assert.strictEqual(headers['Content-Type'], 'application/json')
    assert.strictEqual(headers['Accept'], 'application/json')
    assert.strictEqual(headers['X-Request-ID'], 'req-client-custom-77')
    assert.strictEqual(capturedOptions?.body, JSON.stringify(queryReq))

    // Validates response normalization extracts response header X-Request-ID when payload request_id is null
    assert.strictEqual(result.request_id, 'srv-res-id-999')
    assert.strictEqual(result.status, 'SUCCESS')
  })

  await t.test('postQuery() error handling covers HTTP 422 with ApiClientError', async () => {
    globalThis.fetch = (async () => {
      return new Response(
        JSON.stringify({
          detail: [{ loc: ['body', 'question'], msg: 'Question cannot be empty', type: 'value_error' }],
        }),
        {
          status: 422,
          headers: { 'Content-Type': 'application/json', 'X-Request-ID': 'req-val-422' },
        }
      )
    }) as typeof fetch

    await assert.rejects(
      postQuery({ question: ' ' }),
      (err: unknown) => {
        assert(err instanceof ApiClientError)
        assert.strictEqual(err.kind, 'validation')
        assert.strictEqual(err.statusCode, 422)
        assert.strictEqual(err.requestId, 'req-val-422')
        assert.strictEqual(err.validationErrors?.[0]?.msg, 'Question cannot be empty')
        assert(err.message.includes('Validation error: Question cannot be empty'))
        return true
      }
    )
  })

  await t.test('postQuery() error handling covers HTTP 500 with ApiClientError', async () => {
    globalThis.fetch = (async () => {
      return new Response(
        JSON.stringify({ detail: 'An internal server error occurred.' }),
        {
          status: 500,
          headers: { 'Content-Type': 'application/json', 'X-Request-ID': 'req-srv-500' },
        }
      )
    }) as typeof fetch

    await assert.rejects(
      postQuery({ question: 'Trigger 500' }),
      (err: unknown) => {
        assert(err instanceof ApiClientError)
        assert.strictEqual(err.kind, 'server')
        assert.strictEqual(err.statusCode, 500)
        assert.strictEqual(err.requestId, 'req-srv-500')
        assert.strictEqual(err.message, 'An internal server error occurred.')
        return true
      }
    )
  })

  await t.test('malformed explicitly configured API URLs fail as expected without sending a network request', async () => {
    let fetchCalled = false
    globalThis.fetch = (async () => {
      fetchCalled = true
      return new Response('{}', { status: 200 })
    }) as typeof fetch
    setTestApiBaseUrl('invalid-url-scheme')

    await assert.rejects(
      getHealth(),
      (err: unknown) => {
        assert(err instanceof ApiClientError)
        assert.strictEqual(err.kind, 'network')
        assert(err.message.includes('Invalid VITE_API_BASE_URL configured'))
        return true
      }
    )
    assert.strictEqual(fetchCalled, false)

    await assert.rejects(
      postQuery({ question: 'test question' }),
      (err: unknown) => {
        assert(err instanceof ApiClientError)
        assert.strictEqual(err.kind, 'network')
        assert(err.message.includes('Invalid VITE_API_BASE_URL configured'))
        return true
      }
    )
    assert.strictEqual(fetchCalled, false)
  })
})
