import React, { useEffect, useRef, useState } from 'react'
import { createRoot } from 'react-dom/client'
import './style.css'

const emptyHistory = { messages: [], events: [] }
const eventNames = { 'link.opened': 'Link opened', 'page.submitted': 'Page submitted', 'attachment.opened': 'Attachment opened' }
const label = (value) => value?.replaceAll('_', ' ') || 'Unknown'

async function api(path, options) {
  const response = await fetch('/api' + path, options)
  const data = await response.json()
  if (!response.ok) {
    const error = new Error(typeof data.detail === 'string' ? data.detail : 'Check the request fields and try again.')
    error.status = response.status
    throw error
  }
  return data
}

function post(path, key, body) {
  return api(path, {
    method: 'POST',
    headers: { 'Idempotency-Key': key, 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
}

function App() {
  const [scenario, setScenario] = useState(null)
  const [channels, setChannels] = useState([])
  const [sessions, setSessions] = useState([])
  const [session, setSession] = useState(null)
  const [history, setHistory] = useState(emptyHistory)
  const [busy, setBusy] = useState('Connecting')
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [tab, setTab] = useState('conversation')
  const [target, setTarget] = useState('')
  const [channel, setChannel] = useState('')
  const [subject, setSubject] = useState('')
  const [body, setBody] = useState('')
  const [live, setLive] = useState(false)
  const createKey = useRef(null)
  const messageKey = useRef(null)
  const targets = scenario?.targets || []
  const replies = history.events.filter((event) => event.type === 'reply')
  const remaining = session && Number.isFinite(session.touch_budget) && Number.isFinite(session.touches_used)
    ? Math.max(0, session.touch_budget - session.touches_used) : null

  async function run(name, job) {
    setBusy(name)
    setError('')
    setNotice('')
    try {
      await job()
    } catch (problem) {
      setError(problem.message)
    } finally {
      setBusy('')
    }
  }

  async function inspect(id, summary = null) {
    setSession((previous) => previous?.session_id === id ? previous : summary)
    setHistory(emptyHistory)
    setLive(false)
    const local = await api('/sessions/' + id + '/history')
    setHistory(local)
    const details = await api('/sessions/' + id)
    setSession(details)
    await api('/sessions/' + id + '/events')
    setHistory(await api('/sessions/' + id + '/history'))
    setLive(true)
  }

  async function refresh() {
    const available = await api('/scenarios')
    setScenario(available[0] || null)
    setTarget((previous) => available[0]?.targets?.some((item) => item.id === previous)
      ? previous : available[0]?.targets?.[0]?.id || '')
    const availableChannels = await api('/channels')
    setChannels(availableChannels)
    setChannel((previous) => availableChannels.includes(previous) ? previous : availableChannels[0] || '')
    const list = await api('/sessions')
    setSessions(list)
    const selected = list.find((item) => item.session_id === session?.session_id) || list[0]
    if (selected) {
      await inspect(selected.session_id, selected)
    } else {
      setSession(null)
      setHistory(emptyHistory)
      setLive(false)
    }
  }

  useEffect(() => { void run('Connecting', refresh) }, [])

  async function createSession() {
    const key = createKey.current || crypto.randomUUID()
    createKey.current = key
    let created
    try {
      created = await post('/sessions', key)
    } catch (problem) {
      if ([409, 502, 503, 504].includes(problem.status) || problem.status === undefined) {
        try {
          setSessions(await api('/sessions'))
          createKey.current = null
        } catch {
          // Keep the key until the session list can be refreshed.
        }
        throw new Error(problem.message + ' Check the session list before trying again.')
      }
      throw problem
    }
    createKey.current = null
    setSessions((previous) => [created, ...previous.filter((item) => item.session_id !== created.session_id)])
    await inspect(created.session_id, created)
  }

  async function send(event) {
    event.preventDefault()
    await run('Sending message', async () => {
      const id = session.session_id
      const payload = { to: target, channel, subject, body }
      const fingerprint = JSON.stringify([id, payload])
      const key = messageKey.current?.fingerprint === fingerprint ? messageKey.current.key : crypto.randomUUID()
      messageKey.current = { fingerprint, key }
      const result = await post('/sessions/' + id + '/messages', key, payload)
      messageKey.current = null
      setSubject('')
      setBody('')
      setSession((previous) => ({ ...previous, touches_used: result.touches_used ?? previous.touches_used }))
      setNotice('Message sent to the practice simulator.')
      setHistory(await api('/sessions/' + id + '/history'))
    })
  }

  function replyCard(event) {
    return <article className="bubble reply" key={event.id}>
      <div className="metadata">Simulator reply · {label(event.channel)}</div>
      {event.subject && <strong>{event.subject}</strong>}
      <p>{event.body}</p>
    </article>
  }

  return <div className="shell">
    <header className="topbar">
      <div className="brand"><span className="mark" aria-hidden="true">r8</span><span>revel8 <span className="brand-divider">/</span> START Hack 2026</span></div>
      <span className="scope"><span aria-hidden="true" /> Practice simulator</span>
    </header>

    <main className="page">
      <section className="heading">
        <div><h1>Practice console</h1><p>Explore targets, manage sessions, and review simulator responses.</p></div>
        <div className="actions">
          <button type="button" disabled={!!busy} onClick={() => run('Refreshing', refresh)}>Refresh</button>
          <button type="button" className="primary" disabled={!!busy || !scenario?.open} onClick={() => run('Creating session', createSession)}>New session</button>
        </div>
      </section>

      <div aria-live="polite" className="feedback">
        {busy && <p role="status">{busy}…</p>}
        {notice && <p role="status">{notice}</p>}
        {error && <p role="alert" className="error">{error}</p>}
      </div>

      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        {[
          ['Remaining touches', remaining === null ? '—' : remaining, session ? 'Current session' : 'Select a session'],
          ['Sent messages', history.messages.length, 'Recorded locally'],
          ['Replies', replies.length, 'From simulator'],
          ['Observable events', history.events.length - replies.length, 'Excludes replies'],
        ].map(([name, value, caption]) => <div className="panel metric" key={name}>
          <h2>{name}</h2><strong>{value}</strong><p>{caption}</p>
        </div>)}
      </div>

      <div className="workspace">
        <aside className="rail">
          <section className="panel sidebar">
            <h2>Targets <span>{targets.length}</span></h2>
            {targets.length ? <ul>
              {targets.map((item) => <li key={item.id}>
                <button type="button" className="rail-button" aria-pressed={target === item.id} disabled={!!busy} onClick={() => setTarget(item.id)}>
                  <span className="avatar" aria-hidden="true">{item.name?.slice(0, 1) || '?'}</span>
                  <span><b>{item.name || item.id}</b><small>{item.role}</small></span>
                </button>
              </li>)}
            </ul> : <p className="muted">No practice targets loaded.</p>}
          </section>
          <section className="panel sidebar">
            <h2>Sessions <span>{sessions.length}</span></h2>
            {sessions.length ? <ul>
              {sessions.map((item) => <li key={item.session_id}>
                <button type="button" className="rail-button" aria-pressed={session?.session_id === item.session_id}
                  disabled={!!busy} onClick={() => run('Loading session', () => inspect(item.session_id, item))}>
                  <span><b className="session-id">{item.session_id}</b><small>{label(item.status)}</small></span>
                </button>
              </li>)}
            </ul> : <p className="muted">No practice sessions loaded.</p>}
          </section>
        </aside>

        <section className="panel conversation">
          <div className="conversation-head">
            <div><h2>Session activity</h2><p>{session ? session.session_id + ' · ' + label(session.status) : 'Select or create a practice session.'}</p></div>
            <div className="tabs" aria-label="Activity view">
              <button type="button" aria-pressed={tab === 'conversation'} onClick={() => setTab('conversation')}>Conversation</button>
              <button type="button" aria-pressed={tab === 'events'} onClick={() => setTab('events')}>Events</button>
            </div>
          </div>

          <div className="feed">
            {tab === 'conversation'
              ? <>
                  {!history.messages.length && !replies.length && <div className="empty"><b>No conversation yet</b><p>Sent messages and simulator replies appear here.</p></div>}
                  {history.messages.map((message) => <React.Fragment key={message.message_id}>
                    <article className="bubble sent">
                      <div className="metadata">Sent to {targets.find((item) => item.id === message.to)?.name || message.to} · {label(message.channel)}</div>
                      {message.subject && <strong>{message.subject}</strong>}
                      <p>{message.body}</p>
                    </article>
                    {replies.filter((event) => event.in_reply_to === message.message_id).map(replyCard)}
                  </React.Fragment>)}
                  {replies.filter((event) => !history.messages.some((message) => event.in_reply_to === message.message_id)).map(replyCard)}
                </>
              : history.events.length
                ? <ol className="event-list">{history.events.map((event) => <li key={event.id}>
                    <b>{eventNames[event.type] || label(event.type)}</b>
                    <small>{event.in_reply_to || event.message_id || event.id}</small>
                    <p>{event.body || event.url || event.fields?.join(', ') || event.attachment_id || event.page_id}</p>
                  </li>)}</ol>
                : <div className="empty"><b>No events recorded</b><p>Replies and observable events from this session appear here.</p></div>}
          </div>

          {session && <form className="compose" onSubmit={send}>
            <fieldset disabled={!!busy || !live || session.status !== 'active' || remaining === 0}>
              <legend>Send a message</legend>
              <div className="grid gap-3 sm:grid-cols-2">
                <label>Target<select required value={target} onChange={(event) => setTarget(event.target.value)}>
                  <option value="" disabled>Choose a target</option>
                  {targets.map((item) => <option key={item.id} value={item.id}>{item.name || item.id}</option>)}
                </select></label>
                <label>Channel<select required value={channel} onChange={(event) => setChannel(event.target.value)}>
                  <option value="" disabled>Choose a channel</option>
                  {channels.map((item) => <option key={item} value={item}>{label(item)}</option>)}
                </select></label>
              </div>
              <label>Subject <span className="muted">(optional)</span><input maxLength={300} value={subject} onChange={(event) => setSubject(event.target.value)} /></label>
              <label>Message<textarea required maxLength={16000} rows={3} placeholder="Write a plain-text message" value={body} onChange={(event) => setBody(event.target.value)} /></label>
              <button className="primary" disabled={!body.trim() || !target || !channel}>Send to simulator</button>
            </fieldset>
          </form>}
        </section>
      </div>
    </main>
  </div>
}

createRoot(document.getElementById('root')).render(<App />)
