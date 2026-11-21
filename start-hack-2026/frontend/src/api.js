export async function api(path, options) {
  let response
  try {
    response = await fetch('/api' + path, options)
  } catch {
    throw new Error('Could not reach the dashboard backend. Check that FastAPI is running.')
  }
  const data = await response.json().catch(() => null)
  if (!response.ok || data === null) {
    const message = response.status >= 500 || response.ok
      ? 'The dashboard backend is temporarily unavailable.'
      : 'Check the request fields and try again.'
    const error = new Error(typeof data?.detail === 'string' ? data.detail : message)
    error.status = response.ok ? 502 : response.status
    throw error
  }
  return data
}

export function post(path, key, body) {
  return api(path, {
    method: 'POST',
    headers: { 'Idempotency-Key': key, 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
}
