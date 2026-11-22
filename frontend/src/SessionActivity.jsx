import React, { useEffect, useState } from 'react'
import { EventList, EventSummary, label } from './activity.jsx'
import { readBackend } from './api.js'
import { SavedConversations } from './SavedConversations.jsx'

const tabs = ['Conversation', 'Events', 'Live conversations']

function ReplyCard({ event }) {
  return <article className="bubble reply">
    <div className="metadata">Simulator reply · {label(event.channel)}</div>
    {event.subject && <strong>{event.subject}</strong>}
    <p>{event.body}</p>
  </article>
}

function Transcript({ history, targets }) {
  const { messages, events } = history
  const replies = events.filter((event) => event.type === 'reply')
  if (!messages.length && !replies.length) return <p className="muted">No messages recorded.</p>

  return <>
    {messages.map((message) => <React.Fragment key={message.message_id}>
      <article className="bubble sent">
        <div className="metadata">Sent to {targets.find((item) => item.id === message.to)?.name || message.to} · {label(message.channel)}</div>
        {message.subject && <strong>{message.subject}</strong>}
        <p>{message.body}</p>
      </article>
      {replies.filter((event) => event.in_reply_to === message.message_id).map((event) => <ReplyCard event={event} key={event.id} />)}
    </React.Fragment>)}
    {replies.filter((event) => !messages.some((message) => event.in_reply_to === message.message_id))
      .map((event) => <ReplyCard event={event} key={event.id} />)}
  </>
}

function SessionRecords({ preferredId, revision }) {
  const [library, setLibrary] = useState({ sessions: [], targets: [], loaded: false, error: '' })
  const [pinnedId, setPinnedId] = useState('')
  const [tab, setTab] = useState(tabs[0])
  const [record, setRecord] = useState(null)

  useEffect(() => {
    const controller = new AbortController()
    const { signal } = controller
    Promise.allSettled([readBackend('/sessions', { signal }), readBackend('/scenarios', { signal })])
      .then(([sessions, scenarios]) => {
        if (signal.aborted) return
        setLibrary({
          sessions: Array.isArray(sessions.value) ? sessions.value : [],
          targets: Array.isArray(scenarios.value?.[0]?.targets) ? scenarios.value[0].targets : [],
          loaded: true,
          error: sessions.reason?.message || '',
        })
      })
    return () => controller.abort()
  }, [revision])

  const { sessions, targets } = library
  const id = pinnedId || preferredId || sessions[0]?.session_id
  const live = tab === 'Live conversations'
  const path = id && `/sessions/${encodeURIComponent(id)}/${live ? 'conversations' : 'history'}`

  useEffect(() => {
    if (!path) return
    const controller = new AbortController()
    const { signal } = controller
    readBackend(path, { signal }).then((data) => {
      const valid = live ? Array.isArray(data) : Array.isArray(data.messages) && Array.isArray(data.events)
      if (!valid) throw new Error('The backend returned invalid session activity.')
      if (!signal.aborted) setRecord({ path, data })
    }).catch((error) => {
      if (!signal.aborted) setRecord({ path, error: error.message })
    })
    return () => controller.abort()
  }, [path, live, revision])

  const current = record?.path === path ? record : null
  const otherSessions = sessions.filter((session) => session.session_id !== id)
  return <>
    <div className="workflow-toolbar activity-toolbar">
      <label>Recorded session<select value={id || ''} disabled={!id} onChange={(event) => setPinnedId(event.target.value)}>
        <option value={id || ''}>{id || 'No sessions available'}</option>
        {otherSessions.map((session) => <option key={session.session_id} value={session.session_id}>{session.session_id}</option>)}
      </select></label>
      <div className="tabs" role="group" aria-label="Session activity view">
        {tabs.map((name) => <button type="button" key={name} aria-pressed={tab === name} onClick={() => setTab(name)}>{name}</button>)}
      </div>
    </div>
    {!id ? <p className="activity-message" role={library.error ? 'alert' : 'status'}>
      {library.error || (library.loaded ? 'No sessions recorded.' : 'Loading sessions…')}
    </p> : current?.error ? <p className="live-error" role="alert">{current.error}</p>
      : !current ? <p className="activity-message" role="status">Loading session activity…</p>
        : live ? <SavedConversations key={id} targets={targets} conversations={current.data} />
          : <div className="feed">
            {tab === 'Conversation' ? <Transcript history={current.data} targets={targets} /> : <>
              <EventSummary events={current.data.events} />
              {current.data.events.length > 0 && <EventList events={current.data.events} />}
            </>}
          </div>}
  </>
}

export function SessionActivity({ preferredId, revision }) {
  const [open, setOpen] = useState(false)
  return <details className="panel session-activity" onToggle={(event) => setOpen(event.currentTarget.open)}>
    <summary>Session activity</summary>
    {open && <SessionRecords preferredId={preferredId} revision={revision} />}
  </details>
}
