import React from 'react'
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { App } from './Dashboard.jsx'

const liveRun = {
  id: 'live-run', mode: 'practice', status: 'completed', target: 'practice-persona',
  session_id: 'saved-session', max_touches: 2, touches_used: 1,
}

function serve(handler) {
  const fetch = vi.fn(async (path) => ({ ok: true, json: async () => handler(path) }))
  vi.stubGlobal('fetch', fetch)
  return fetch
}

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

it('loads the live workflow immediately and hides mock history', async () => {
  const fetch = serve((path) => {
    if (path === '/api/qa/runs') return [{ id: 'demo-run', mode: 'mock' }, { id: 'model-demo', mode: 'mock_openai' }, liveRun]
    return { ...liveRun, timeline: [{ state: 'PLAN', detail: { decision: 'stop' } }] }
  })
  render(<App />)

  expect(screen.getByRole('heading', { level: 1 }).textContent).toBe('Agent workflow')
  expect(await screen.findByRole('heading', { name: 'Planning details' })).toBeTruthy()
  expect(screen.getByText('Backend connected')).toBeTruthy()
  expect(screen.queryByRole('option', { name: /demo/ })).toBeNull()
  expect(fetch.mock.calls.map(([path]) => path)).toEqual(['/api/qa/runs', '/api/qa/runs/live-run'])
})

it('follows an in-progress run with read-only requests', async () => {
  const summary = { ...liveRun, status: 'running', state: 'RESEARCH' }
  let detailReads = 0
  const fetch = serve((path) => {
    if (path === '/api/qa/runs') return [summary]
    detailReads += 1
    return detailReads === 1
      ? { ...summary, timeline: [{ state: 'RESEARCH', detail: {} }] }
      : { ...liveRun, stop_reason: 'Run finished', timeline: [{ state: 'STOP', detail: {} }] }
  })
  render(<App />)

  expect(await screen.findByRole('heading', { name: 'Research details' })).toBeTruthy()
  expect(screen.getByRole('button', { name: 'Research' }).getAttribute('aria-current')).toBe('step')
  expect(await screen.findByText('Run finished', {}, { timeout: 2500 })).toBeTruthy()
  expect(detailReads).toBe(2)
  expect(fetch.mock.calls.every(([, options]) => options.method === 'GET')).toBe(true)
})

it('loads transcripts and events only after expanding session activity', async () => {
  const responses = {
    '/api/qa/runs': [liveRun],
    '/api/qa/runs/live-run': { ...liveRun, timeline: [{ state: 'STOP', detail: { status: 'completed' } }] },
    '/api/sessions': [{ session_id: 'saved-session' }],
    '/api/scenarios': [],
    '/api/sessions/saved-session/history': {
      messages: [], events: [{ id: 'event-1', type: 'reply', body: 'Saved reply.' }],
    },
    '/api/sessions/saved-session/conversations': [{
      conversation_id: 'chat-1', channel: 'live_chat', greeting: 'Saved greeting.', status: 'ended',
      turns_used: 0, max_turns: 10, events: [], turns: [],
    }],
  }
  const fetch = serve((path) => responses[path])
  render(<App />)

  expect(await screen.findByRole('heading', { name: 'Finish details' })).toBeTruthy()
  expect(fetch.mock.calls.some(([path]) => path.startsWith('/api/sessions'))).toBe(false)
  const expander = screen.getByText('Session activity').closest('details')
  expander.open = true
  fireEvent(expander, new Event('toggle'))

  expect(await screen.findByText('Saved reply.')).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: 'Events', exact: true }))
  expect(screen.getByRole('region', { name: 'Observed activity' })).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: 'Live conversations' }))
  const transcript = await screen.findByRole('log', { name: 'Live conversation transcript' })
  expect(within(transcript).getByText('Saved greeting.')).toBeTruthy()
})

it('shows a connection error and recovers to the live empty state on refresh', async () => {
  vi.stubGlobal('fetch', vi.fn()
    .mockRejectedValueOnce(new Error('Connection refused'))
    .mockResolvedValue({ ok: true, json: async () => [] }))
  render(<App />)

  expect(await screen.findByRole('alert')).toBeTruthy()
  expect(screen.getByText('Backend unavailable')).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))
  expect(await screen.findByText('No live runs recorded.')).toBeTruthy()
  expect(screen.queryByRole('alert')).toBeNull()
})
