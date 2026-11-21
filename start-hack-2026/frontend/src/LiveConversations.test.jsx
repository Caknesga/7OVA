import React, { useState } from 'react'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { LiveConversations } from './LiveConversations.jsx'

const answered = {
  conversation_id: 'c_fixture',
  to: 'robin',
  channel: 'live_chat',
  status: 'answered',
  greeting: 'Hello, how can I help?',
  ended: false,
  turns_used: 0,
  max_turns: 10,
  turns: [],
  events: [],
}

function Harness(props) {
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const [target, setTarget] = useState('robin')

  async function run(name, job) {
    setBusy(name)
    setError('')
    try {
      await job()
    } catch (problem) {
      setError(problem.message)
    } finally {
      setBusy('')
    }
  }

  return <>
    {error && <p role="alert">{error}</p>}
    <LiveConversations session={{ session_id: 's_fixture', status: 'active' }}
      targets={[{ id: 'robin', name: 'Robin Park' }, { id: 'sam', name: 'Sam Lee' }]}
      target={target} setTarget={setTarget}
      ready visible remaining={29} busy={busy} run={run} {...props} />
  </>
}

function json(data, status = 200) {
  return new Response(JSON.stringify(data), { status })
}

function click(name) {
  fireEvent.click(screen.getByRole('button', { name }))
}

function fillTurn(say) {
  fireEvent.change(screen.getByRole('textbox', { name: 'Your turn' }), { target: { value: say } })
}

describe('Live Conversations', () => {
  let fetchMock

  beforeEach(() => {
    sessionStorage.clear()
    fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)
  })

  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
  })

  it('starts a selected channel, renders the greeting and replies, and ends the conversation', async () => {
    const onChange = vi.fn()
    const { rerender } = render(<Harness conversations={[]} onChange={onChange} />)
    fireEvent.change(screen.getByRole('combobox', { name: 'Live target' }), { target: { value: 'sam' } })
    fireEvent.change(screen.getByRole('combobox', { name: 'Live channel' }), { target: { value: 'live_chat' } })
    let respond
    fetchMock.mockReturnValueOnce(new Promise((resolve) => { respond = resolve }))
    click('Start Conversation')
    expect(screen.getByRole('button', { name: 'Starting…' }).disabled).toBe(true)
    const [startPath, startOptions] = fetchMock.mock.calls[0]
    expect(startPath).toBe('/api/sessions/s_fixture/conversations')
    expect(startOptions.method).toBe('POST')
    expect(startOptions.headers['Idempotency-Key']).toBeTruthy()
    expect(JSON.parse(startOptions.body)).toEqual({ to: 'sam', channel: 'live_chat' })
    respond(json({ conversation_id: answered.conversation_id, touches_used: 1 }, 201))
    await waitFor(() => expect(onChange).toHaveBeenCalledWith({ conversation_id: 'c_fixture', touches_used: 1 }))

    rerender(<Harness conversations={[{ ...answered, to: 'sam' }]} onChange={onChange} />)
    expect(screen.getByRole('log').textContent).toContain(answered.greeting)
    expect(screen.getByText('0 / 10')).toBeTruthy()
    fetchMock.mockResolvedValueOnce(json({ conversation_id: 'c_fixture', turns_used: 1 }))
    fillTurn('  A practice question.  ')
    click('Send Turn')
    await waitFor(() => expect(onChange).toHaveBeenCalledTimes(2))
    const [turnPath, turnOptions] = fetchMock.mock.calls[1]
    expect(turnPath).toBe('/api/conversations/c_fixture/turns')
    expect(JSON.parse(turnOptions.body)).toEqual({ say: 'A practice question.' })

    const current = {
      ...answered, to: 'sam', turns_used: 1,
      turns: [{ number: 1, say: 'A practice question.', persona_says: 'Thanks for the turn.' }],
      events: [{ id: 'e_fixture', type: 'link.opened', conversation_id: 'c_fixture', url: 'https://example.invalid' }],
    }
    rerender(<Harness conversations={[current]} onChange={onChange} />)
    expect(screen.getByRole('log').textContent).toContain('You · Turn 1A practice question.')
    expect(screen.getByRole('log').textContent).toContain('Simulator · Turn 1Thanks for the turn.')
    expect(screen.getByText('1 / 10')).toBeTruthy()
    expect(screen.getByText('Link opened')).toBeTruthy()
    expect(screen.getByText('https://example.invalid').tagName).toBe('P')
    fetchMock.mockResolvedValueOnce(json({ conversation_id: 'c_fixture', ended: true }))
    click('End Conversation')
    await waitFor(() => expect(onChange).toHaveBeenCalledTimes(3))
    expect(fetchMock.mock.calls[2][0]).toBe('/api/conversations/c_fixture/end')
    expect(fetchMock.mock.calls[2][1].body).toBeUndefined()
    rerender(<Harness conversations={[{ ...current, ended: true, status: 'ended' }]} onChange={onChange} />)
    expect(screen.getByText('ended')).toBeTruthy()
    expect(screen.getByRole('button', { name: 'End Conversation' }).disabled).toBe(true)
    expect(screen.getByRole('button', { name: 'Send Turn' }).disabled).toBe(true)
  })

  it('disables new turns at the ten-turn limit while allowing the user to end the conversation', () => {
    render(<Harness conversations={[{ ...answered, turns_used: 10 }]} onChange={vi.fn()} />)
    expect(screen.getByText('Turn limit reached')).toBeTruthy()
    expect(screen.getByText('10 / 10')).toBeTruthy()
    expect(screen.getByRole('textbox', { name: 'Your turn' }).disabled).toBe(true)
    expect(screen.getByRole('button', { name: 'Send Turn' }).disabled).toBe(true)
    expect(screen.getByRole('button', { name: 'End Conversation' }).disabled).toBe(false)
    click('Send Turn')
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it.each([
    { kind: 'Start', opener: 'Start Conversation', conversations: [] },
    { kind: 'Turn', opener: 'Send Turn', conversations: [answered] },
  ])('retries a timed-out $kind with the same payload and key after a reload', async ({ kind, opener, conversations }) => {
    const onChange = vi.fn()
    fetchMock.mockResolvedValueOnce(json({ detail: 'Simulator timed out after 120 seconds.' }, 504))
    const { unmount } = render(<Harness conversations={conversations} onChange={onChange} />)
    if (kind === 'Turn') fillTurn('One practice turn.')
    click(opener)
    const message = await screen.findByRole('alert')
    expect(message.textContent).toContain('Retry ' + kind)
    const [path, options] = fetchMock.mock.calls[0]

    unmount()
    render(<Harness conversations={conversations} onChange={onChange} />)
    if (kind === 'Turn') expect(screen.getByRole('textbox', { name: 'Your turn' }).readOnly).toBe(true)
    fetchMock.mockResolvedValueOnce(json({ conversation_id: 'c_fixture' }))
    click('Retry ' + kind)
    await waitFor(() => expect(onChange).toHaveBeenCalledOnce())
    expect(fetchMock.mock.calls[1]).toEqual([path, options])
    expect(sessionStorage.getItem('practice-console:live:s_fixture')).toBeNull()
  })
})
