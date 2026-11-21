import React, { useEffect, useRef, useState } from 'react'
import { createRoot } from 'react-dom/client'
import { EventList, EventSummary, label } from './activity.jsx'
import { api, post } from './api.js'
import { LiveConversations } from './LiveConversations.jsx'
import { AgentRuns } from './AgentRuns.jsx'
import './style.css'

const emptyHistory = { messages: [], events: [] }

function App() {
  const [scenario, setScenario] = useState(null)
  const [channels, setChannels] = useState([])
  const [sessions, setSessions] = useState([])
  const [session, setSession] = useState(null)
  const [history, setHistory] = useState(emptyHistory)
  const [conversations, setConversations] = useState([])
  const [liveHistoryError, setLiveHistoryError] = useState('')
  const [busy, setBusy] = useState('Connecting')
  const [error, setError] = useState(null)
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
    setError(null)
    setNotice('')
    try {
      await job()
    } catch (problem) {
      setError({ message: problem.message, during: name })
    } finally {
      setBusy('')
    }
  }

  async function loadActivity(id) {
    const [local, calls] = await Promise.allSettled([
      api('/sessions/' + id + '/history'),
      api('/sessions/' + id + '/conversations'),
    ])
    if (local.status === 'rejected') throw local.reason
    setHistory(local.value)
    if (calls.status === 'rejected') {
      setLiveHistoryError(calls.reason.status === 404
        ? 'Restart FastAPI to load the live conversation routes, then refresh.'
        : calls.reason.message)
      return false
    }
    setLiveHistoryError('')
    setConversations(calls.value)
    return true
  }

  async function conversationChanged(receipt) {
    if (Number.isFinite(receipt?.touches_used)) {
      setSession((previous) => ({
        ...previous,
        touches_used: Math.max(previous.touches_used ?? 0, receipt.touches_used),
      }))
    }
    const loaded = await loadActivity(session.session_id).catch(() => false)
    if (!loaded) setLiveHistoryError('Could not reload saved activity. Use Refresh to reopen the session.')
  }

  async function inspect(id, summary = null) {
    setSession((previous) => previous?.session_id === id ? previous : summary)
    setHistory(emptyHistory)
    setConversations([])
    setLiveHistoryError('')
    setLive(false)
    await loadActivity(id)
    const details = await api('/sessions/' + id)
    setSession(details)
    await api('/sessions/' + id + '/events')
    await loadActivity(id)
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
      setConversations([])
      setLiveHistoryError('')
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
        {error && (tab !== 'agent' || error.during !== 'Connecting') &&
          <p role="alert" className="error">{error.message}</p>}
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
            <div><h2>{tab === 'agent' ? 'AI Agent runs' : 'Session activity'}</h2>
              <p>{tab === 'agent' ? 'Run and review simulator QA.' :
                session ? session.session_id + ' · ' + label(session.status) : 'Select or create a practice session.'}</p></div>
            <div className="tabs" aria-label="Activity view">
              <button type="button" aria-pressed={tab === 'conversation'} onClick={() => setTab('conversation')}>Conversation</button>
              <button type="button" aria-pressed={tab === 'events'} onClick={() => setTab('events')}>Events</button>
              <button type="button" aria-pressed={tab === 'live'} onClick={() => setTab('live')}>Live Conversations</button>
              <button type="button" aria-pressed={tab === 'agent'} onClick={() => setTab('agent')}>AI Agent</button>
            </div>
          </div>

          {session && <div hidden={tab !== 'live'} aria-busy={!!busy}>
            {liveHistoryError && <p className="live-error" role="alert">{liveHistoryError}</p>}
            <LiveConversations key={session.session_id} session={session} targets={targets} target={target}
              setTarget={setTarget} conversations={conversations} ready={live && !liveHistoryError} remaining={remaining}
              busy={busy} run={run} onChange={conversationChanged} visible={tab === 'live'} />
          </div>}
          {tab === 'live' && !session && <div className="feed">
            <div className="empty"><b>No session selected</b><p>Select or create a practice session to start a live conversation.</p></div>
          </div>}

          {tab === 'agent' && <AgentRuns targets={targets} sessions={sessions} />}

          {(tab === 'conversation' || tab === 'events') && <div className="feed">
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
              : <>
                  <EventSummary events={history.events} />
                  {history.events.length > 0 && <EventList events={history.events} />}
                </>}
          </div>}

          {session && (tab === 'conversation' || tab === 'events') && <form className="compose" onSubmit={send}>
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
