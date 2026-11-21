import React from 'react'

const eventNames = { 'link.opened': 'Link opened', 'page.submitted': 'Page submitted', 'attachment.opened': 'Attachment opened' }
export const label = (value) => value?.replaceAll('_', ' ') || 'Unknown'

export function EventList({ events }) {
  return <ol className="event-list">{events.map((event) => <li key={event.id}>
    <b>{eventNames[event.type] || label(event.type)}</b>
    <small>{event.in_reply_to || event.message_id || event.conversation_id || event.id}</small>
    <p>{event.body || event.url || (Array.isArray(event.fields) && event.fields.join(', ')) || event.attachment_id || event.page_id}</p>
  </li>)}</ol>
}
