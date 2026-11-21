import React, { useEffect, useRef, useState } from 'react'
import { EventList, label } from './activity.jsx'
import { post } from './api.js'

const channels = {
  phone_call: 'Phone call',
  video_call: 'Video call',
  teams_call: 'Teams call',
  live_chat: 'Live chat',
}
const retryable = (error) => error.status === undefined || error.status === 429 || error.status >= 500

export function LiveConversations({
  session, targets, target, setTarget, conversations, ready, remaining, busy, run, onChange, visible,
}) {
  const sid = session.session_id
  const storageKey = 'practice-console:live:' + sid
  const [pending, setPending] = useState(() => {
    try {
      return JSON.parse(sessionStorage.getItem(storageKey))
    } catch {
      return null
    }
  })
  const [selectedId, setSelectedId] = useState('')
  const [channel, setChannel] = useState('phone_call')
  const [draft, setDraft] = useState('')
  const feed = useRef(null)
  const pendingStart = pending?.kind === 'start' ? pending : null
  const pendingTurn = pending?.kind === 'turn' ? pending : null
  const selected = conversations.find((item) => item.conversation_id === (pendingTurn?.cid || selectedId))
    || conversations.at(-1)
  const startTarget = pendingStart?.payload.to || target
  const startChannel = pendingStart?.payload.channel || channel
  const say = pendingTurn?.payload.say ?? draft
  const activeSession = ready && session.status === 'active'
  const atLimit = selected && selected.turns_used >= selected.max_turns
  const canStart = !busy && !pendingTurn && (pendingStart || (activeSession && remaining !== 0 && startTarget))
  const canTurn = !busy && !pendingStart && (pendingTurn || (activeSession && selected && !selected.ended && !atLimit))

  useEffect(() => {
    if (visible && feed.current) feed.current.scrollTop = feed.current.scrollHeight
  }, [visible, selected?.conversation_id, selected?.turns.length])

  function remember(command) {
    setPending(command)
    // Keep an unresolved request available after a tab refresh, without changing its key.
    try {
      if (command) sessionStorage.setItem(storageKey, JSON.stringify(command))
      else sessionStorage.removeItem(storageKey)
    } catch {
      // The in-memory key still covers retries on this page when browser storage is unavailable.
    }
  }

  async function submit(command) {
    remember(command)
    const path = command.kind === 'start'
      ? '/sessions/' + sid + '/conversations'
      : '/conversations/' + command.cid + '/turns'
    let receipt
    try {
      receipt = await post(path, command.key, command.payload)
    } catch (error) {
      if (retryable(error)) {
        const action = command.kind === 'start' ? 'Retry Start' : 'Retry Turn'
        throw new Error(error.message + ' Use ' + action + ' to recover this request.')
      }
      if (command.kind === 'turn') setDraft(command.payload.say)
      remember(null)
      if (error.status === 409) await onChange().catch(() => {})
      throw error
    }
    remember(null)
    setSelectedId(receipt.conversation_id)
    setDraft('')
    await onChange(receipt)
  }

  function start(event) {
    event.preventDefault()
    if (!canStart) return
    const command = pendingStart || {
      kind: 'start', key: crypto.randomUUID(), payload: { to: startTarget, channel: startChannel },
    }
    void run('Starting conversation', () => submit(command))
  }

  function sendTurn(event) {
    event.preventDefault()
    if (!canTurn || !say.trim()) return
    const command = pendingTurn || {
      kind: 'turn', key: crypto.randomUUID(), cid: selected.conversation_id, payload: { say: say.trim() },
    }
    void run('Sending turn', () => submit(command))
  }

  function end() {
    void run('Ending conversation', async () => {
      const receipt = await post('/conversations/' + selected.conversation_id + '/end', crypto.randomUUID())
      await onChange(receipt)
    })
  }

  return <>
    <form className="compose live-start" onSubmit={start}>
      <fieldset disabled={!!busy || !!pendingTurn}>
        <legend>Start a live conversation</legend>
        <div className="grid gap-3 sm:grid-cols-2">
          <label>Live target
            <select required value={startTarget} disabled={!!pendingStart} onChange={(event) => setTarget(event.target.value)}>
              <option value="" disabled>Choose a target</option>
              {targets.map((item) => <option key={item.id} value={item.id}>{item.name || item.id}</option>)}
            </select>
          </label>
          <label>Live channel
            <select value={startChannel} disabled={!!pendingStart} onChange={(event) => setChannel(event.target.value)}>
              {Object.entries(channels).map(([value, name]) => <option key={value} value={value}>{name}</option>)}
            </select>
          </label>
        </div>
        <div className="live-start-actions">
          <button type="submit" className="primary" disabled={!canStart}>
            {busy === 'Starting conversation' ? 'Starting…' : pendingStart ? 'Retry Start' : 'Start Conversation'}
          </button>
          <span className="muted">One touch per conversation · Up to 10 text turns</span>
        </div>
      </fieldset>
    </form>

    {!selected && <div className="feed">
      <div className="empty"><b>No live conversations yet</b><p>Start one to see the simulator greeting.</p></div>
    </div>}

    {selected && <>
      <div className="compose live-selector">
        <label>Saved conversation
          <select value={selected.conversation_id} disabled={!!busy || !!pending} onChange={(event) => {
            setSelectedId(event.target.value)
            setDraft('')
          }}>
            {[...conversations].reverse().map((item) => <option key={item.conversation_id} value={item.conversation_id}>
              {targets.find((person) => person.id === item.to)?.name || item.to} · {channels[item.channel]} · {item.conversation_id}
            </option>)}
          </select>
        </label>
        <div className="actions">
          <button type="button" disabled={!!busy || !!pending || !ready || selected.ended} onClick={end}>
            {busy === 'Ending conversation' ? 'Ending…' : 'End Conversation'}
          </button>
        </div>
      </div>
      <dl className="live-facts">
        <div><dt>Status</dt><dd>{atLimit && !selected.ended ? 'Turn limit reached' : label(selected.status)}</dd></div>
        <div><dt>Conversation ID</dt><dd className="session-id">{selected.conversation_id}</dd></div>
        <div><dt>Turns</dt><dd>{selected.turns_used} / {selected.max_turns}</dd></div>
      </dl>
      <div className="feed live-feed" ref={feed} role="log" aria-label="Live conversation transcript">
        {selected.greeting && <article className="bubble reply">
          <div className="metadata">Simulator · Greeting</div><p>{selected.greeting}</p>
        </article>}
        {selected.turns.map((turn) => <React.Fragment key={turn.number}>
          <article className="bubble sent"><div className="metadata">You · Turn {turn.number}</div><p>{turn.say}</p></article>
          {turn.persona_says && <article className="bubble reply">
            <div className="metadata">Simulator · Turn {turn.number}</div><p>{turn.persona_says}</p>
          </article>}
        </React.Fragment>)}
      </div>
    </>}

    {(selected || pendingTurn) && <form className="compose" onSubmit={sendTurn}>
      <label>Your turn
        <textarea required maxLength={4000} rows={3} value={say} disabled={!canTurn}
          readOnly={!!pendingTurn} onChange={(event) => setDraft(event.target.value)}
          placeholder="Type what you want to say" />
      </label>
      <button type="submit" className="primary" disabled={!canTurn || !say.trim()}>
        {busy === 'Sending turn' ? 'Sending…' : pendingTurn ? 'Retry Turn' : 'Send Turn'}
      </button>
    </form>}
    {selected && <section className="live-events" aria-labelledby="live-events-heading">
      <h3 id="live-events-heading">Conversation events <span className="muted">{selected.events.length}</span></h3>
      {selected.events.length > 0 ? <EventList events={selected.events} /> : <p className="muted">No events recorded.</p>}
    </section>}
  </>
}
