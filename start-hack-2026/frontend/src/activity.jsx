import React from 'react'

const eventNames = { 'link.opened': 'Link opened', 'page.submitted': 'Page submitted', 'attachment.opened': 'Attachment opened' }
export const label = (value) => value?.replaceAll('_', ' ') || 'Unknown'

export function EventSummary({ events }) {
  const labels = [
    ['reply', 'Replies'],
    ['link.opened', 'Links opened'],
    ['page.submitted', 'Forms submitted'],
    ['attachment.opened', 'Attachments opened'],
  ]
  const counts = new Map()
  for (const event of events) {
    counts.set(event.type, (counts.get(event.type) ?? 0) + 1)
  }
  const other = events.length - labels.reduce((sum, [type]) => sum + (counts.get(type) ?? 0), 0)
  return <section aria-label="Observed activity" className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
    {labels.map(([type, title]) => <div className="panel metric" key={type}>
      <h3>{title}</h3><strong>{counts.get(type) ?? 0}</strong>
    </div>)}
    {other > 0 && <p className="muted">Other recorded events: {other}</p>}
    {events.length === 0 && <p className="muted sm:col-span-2">No simulator events recorded for this session.</p>}
  </section>
}

export function EventList({ events }) {
  return <ol className="event-list">{events.map((event) => <li key={event.id}>
    <b>{eventNames[event.type] || label(event.type)}</b>
    <small>{event.in_reply_to || event.message_id || event.conversation_id || event.id}</small>
    <p>{event.body || event.url || (Array.isArray(event.fields) && event.fields.join(', ')) || event.attachment_id || event.page_id}</p>
  </li>)}</ol>
}
