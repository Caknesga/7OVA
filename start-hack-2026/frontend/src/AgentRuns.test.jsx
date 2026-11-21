import React from 'react'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { AgentRuns } from './AgentRuns.jsx'

const config = {
  model: 'test-model',
  real_available: true,
  budget_usd: 20,
  soft_limit_usd: 15,
  max_run_cost_usd: 2,
  estimated_total_usd: 15.14,
  remaining_usd: 4.86,
  soft_limit_reached: true,
}
const running = {
  id: 'r_demo', mode: 'mock', target: 'qa_persona', session_id: 'mock_demo',
  model: 'mock', max_touches: 2, touches_used: 0, state: 'START', status: 'running',
}
const complete = {
  ...running, state: 'STOP', status: 'completed', touches_used: 1, stop_reason: 'QA sequence complete.',
  usage: { input_tokens: 15, cached_tokens: 5, output_tokens: 3, estimated_usd: 0 },
  timeline: [{
    id: 1, state: 'COMMUNICATE', created_at: '2026-11-21T14:00:00Z',
    detail: { text: 'Hello from the QA test.', reply: 'Mock reply.' },
  }],
}

function json(body, status = 200) {
  return Promise.resolve(new Response(JSON.stringify(body), { status }))
}

function click(name) {
  fireEvent.click(screen.getByRole('button', { name }))
}

beforeEach(() => sessionStorage.clear())
afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

it('starts an isolated mock run and polls its recorded transcript without simulator credentials', async () => {
  let inspections = 0
  const fetchMock = vi.fn((path, options) => {
    if (path === '/api/qa/config') return json({ ...config, real_available: false })
    if (path === '/api/qa/runs') return options?.method === 'POST' ? json(running, 201) : json([])
    if (path === '/api/qa/runs/r_demo') return json(inspections++ ? complete : running)
    throw new Error('Unexpected dashboard request ' + path)
  })
  vi.stubGlobal('fetch', fetchMock)
  render(<AgentRuns targets={[]} sessions={[]} />)
  await waitFor(() => expect(screen.getByRole('button', { name: 'Start run' }).disabled).toBe(false))
  click('Start run')
  const [, options] = fetchMock.mock.calls.find(([path, request]) =>
    path === '/api/qa/runs' && request?.method === 'POST',
  )
  expect(options.headers['Idempotency-Key']).toBeTruthy()
  expect(JSON.parse(options.body)).toEqual({
    mode: 'mock', target: 'qa_persona', session_id: null, max_touches: 2, confirm_practice: false,
  })
  const timeline = await screen.findByRole('region', { name: 'Activity timeline' })
  expect(await within(timeline).findByText('Mock reply.', {}, { timeout: 2000 })).toBeTruthy()
  expect(screen.getByText('QA sequence complete.')).toBeTruthy()
  expect(screen.getByRole('button', { name: 'Stop run' }).disabled).toBe(true)
})

it('requires an existing active session, a target and explicit consent for official practice', async () => {
  const official = { ...complete, mode: 'practice', target: 'robin', session_id: 's_active', model: config.model }
  const fetchMock = vi.fn((path, options) => {
    if (path === '/api/qa/config') return json(config)
    if (path === '/api/qa/runs') return options?.method === 'POST' ? json(official, 201) : json([])
    if (path === '/api/qa/runs/r_demo') return json(official)
    throw new Error('Unexpected dashboard request ' + path)
  })
  vi.stubGlobal('fetch', fetchMock)
  render(<AgentRuns targets={[{ id: 'robin', name: 'Robin' }]}
    sessions={[{ session_id: 's_active', status: 'active' }, { session_id: 's_ended', status: 'ended' }]} />)
  await screen.findByText('Local estimates are separate from OpenAI billing.')
  fireEvent.change(screen.getByLabelText('Simulator mode'), { target: { value: 'practice' } })
  expect(screen.getByRole('button', { name: 'Start run' }).disabled).toBe(true)
  expect(screen.queryByRole('option', { name: 's_ended' })).toBeNull()
  fireEvent.change(screen.getByLabelText('Target'), { target: { value: 'robin' } })
  fireEvent.change(screen.getByLabelText('Session'), { target: { value: 's_active' } })
  const consent = screen.getByRole('checkbox')
  fireEvent.click(consent)
  fireEvent.change(screen.getByLabelText('Maximum touches'), { target: { value: '3' } })
  expect(consent.checked).toBe(false)
  click('Start run')
  expect(fetchMock.mock.calls.some(([, options]) => options?.method === 'POST')).toBe(false)
  fireEvent.click(consent)
  click('Start run')
  await screen.findByText('QA sequence complete.')
  const [, options] = fetchMock.mock.calls.find(([, request]) => request?.method === 'POST')
  expect(JSON.parse(options.body)).toEqual({
    mode: 'practice', target: 'robin', session_id: 's_active', max_touches: 3, confirm_practice: true,
  })
  expect(screen.getByText(/Estimated spending reached the .* warning threshold/)).toBeTruthy()
})

it('preserves the start key after a timeout and stops the selected run', async () => {
  let first = true
  let stopped = false
  const terminal = { ...running, state: 'STOP', status: 'stopped', stop_reason: 'Stopped by operator.' }
  const fetchMock = vi.fn((path, options) => {
    if (path === '/api/qa/config') return json(config)
    if (path === '/api/qa/runs' && options?.method === 'POST') {
      if (first) {
        first = false
        return json({ detail: 'Retry the start.' }, 504)
      }
      return json(running, 201)
    }
    if (path === '/api/qa/runs') return json(stopped ? [terminal] : [])
    if (path === '/api/qa/runs/r_demo') return json(stopped ? terminal : running)
    if (path === '/api/qa/runs/r_demo/stop' && options?.method === 'POST') {
      stopped = true
      return json(terminal)
    }
    throw new Error('Unexpected dashboard request ' + path)
  })
  vi.stubGlobal('fetch', fetchMock)
  const { unmount } = render(<AgentRuns targets={[]} sessions={[]} />)
  await screen.findByText('Local estimates are separate from OpenAI billing.')
  click('Start run')
  expect((await screen.findByRole('alert')).textContent).toBe('Retry the start.')
  unmount()

  render(<AgentRuns targets={[]} sessions={[]} />)
  await screen.findByText('Local estimates are separate from OpenAI billing.')
  click('Start run')
  await waitFor(() => expect(screen.getByRole('button', { name: 'Stop run' }).disabled).toBe(false))
  const starts = fetchMock.mock.calls.filter(([path, request]) => path === '/api/qa/runs' && request?.method === 'POST')
  expect(starts).toHaveLength(2)
  expect(starts[1][1]).toEqual(starts[0][1])
  click('Stop run')
  expect(await screen.findByText('Stopped by operator.')).toBeTruthy()
  expect(screen.getByRole('button', { name: 'Stop run' }).disabled).toBe(true)
})
