import React, { useState } from 'react'
import { EventList, label } from './activity.jsx'

export function SavedConversations({ conversations, targets }) {
  const [selectedId, setSelectedId] = useState('')
  const selected = conversations.find((item) => item.conversation_id === selectedId) || conversations.at(-1)

  if (!selected) return <div className="feed">
    <div className="empty"><b>No live conversations recorded</b></div>
  </div>

  return <>
    <div className="compose live-selector">
      <label>Saved conversation<select value={selected.conversation_id} onChange={(event) => setSelectedId(event.target.value)}>
        {[...conversations].reverse().map((item) => <option key={item.conversation_id} value={item.conversation_id}>
          {targets.find((person) => person.id === item.to)?.name || item.to} · {label(item.channel)} · {item.conversation_id}
        </option>)}
      </select></label>
    </div>
    <dl className="live-facts">
      <div><dt>Status</dt><dd>{label(selected.status)}</dd></div>
      <div><dt>Conversation ID</dt><dd className="session-id">{selected.conversation_id}</dd></div>
      <div><dt>Turns</dt><dd>{selected.turns_used} / {selected.max_turns}</dd></div>
    </dl>
    <div className="feed live-feed" role="log" aria-label="Live conversation transcript">
      {!selected.greeting && !selected.turns?.length && <p className="muted">Transcript text unavailable in this view.</p>}
      {selected.greeting && <article className="bubble reply">
        <div className="metadata">Simulator · Greeting</div><p>{selected.greeting}</p>
      </article>}
      {(selected.turns || []).map((turn) => <React.Fragment key={turn.number}>
        <article className="bubble sent"><div className="metadata">Recorded · Turn {turn.number}</div><p>{turn.say}</p></article>
        {turn.persona_says && <article className="bubble reply">
          <div className="metadata">Simulator · Turn {turn.number}</div><p>{turn.persona_says}</p>
        </article>}
      </React.Fragment>)}
    </div>
    <section className="live-events" aria-labelledby="live-events-heading">
      <h3 id="live-events-heading">Conversation events <span className="muted">{selected.events?.length || 0}</span></h3>
      {selected.events?.length ? <EventList events={selected.events} /> : <p className="muted">No events recorded.</p>}
    </section>
  </>
}
