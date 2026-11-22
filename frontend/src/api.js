export async function readBackend(path, { signal } = {}) {
  let response
  try {
    response = await fetch('/api' + path, { method: 'GET', cache: 'no-store', signal })
  } catch (error) {
    if (signal?.aborted) throw error
    throw new Error('Could not reach the workflow backend.')
  }

  const data = await response.json().catch(() => null)
  if (!response.ok || data === null) {
    const message = response.status >= 500 ? 'Could not reach the workflow backend.' : `Could not load ${path}.`
    throw new Error(typeof data?.detail === 'string' ? data.detail : message)
  }
  return data
}
