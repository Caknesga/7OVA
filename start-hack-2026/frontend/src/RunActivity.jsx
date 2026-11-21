import React, { useId, useState } from 'react'
import { label } from './activity.jsx'

const stages = [
  { id: 'setup', name: 'Setup', states: ['START', 'RESEARCH'] },
  { id: 'plan', name: 'Plan action', states: ['PLAN', 'GENERATE'] },
  { id: 'simulator', name: 'Simulator', states: ['COMMUNICATE'] },
  { id: 'evaluate', name: 'Evaluate', states: ['EVALUATE', 'MEMORY', 'CONTINUE'] },
  { id: 'finish', name: 'Finish', states: ['STOP'] },
]

const sentences = {
  start_conversation: 'Start conversation',
  continue_conversation: 'Send turn',
  end_conversation: 'End conversation',
  send_message: 'Send message',
  stop: 'Stop run',
  reply: 'Reply received',
  silence: 'No response observed',
  ended: 'Conversation closed',
  observable_events: 'Simulator events recorded',
}

const title = (value) => label(String(value ?? '')).replace(/^./, (first) => first.toUpperCase())
const describe = (value) => Object.hasOwn(sentences, value) ? sentences[value] : title(value)
const countLabel = (count, singular, plural = singular + 's') => `${count} ${count === 1 ? singular : plural}`

function displayTime(value) {
  const date = new Date(value)
  return value && !Number.isNaN(date.getTime()) ? date.toLocaleString() : ''
}

function RecordValue({ value }) {
  if (Array.isArray(value)) {
    return value.length ? <ul className="qa-record-list">{value.map((item, index) =>
      <li key={index}><RecordValue value={item} /></li>,
    )}</ul> : <span className="muted">None</span>
  }
  if (value !== null && typeof value === 'object') {
    const fields = Object.entries(value)
    return fields.length ? <dl className="qa-record">{fields.map(([key, entry]) =>
      <div key={key}><dt>{title(key)}</dt><dd><RecordValue value={entry} /></dd></div>,
    )}</dl> : null
  }
  return <span>{value == null ? '—' : typeof value === 'boolean' ? (value ? 'Yes' : 'No') : String(value)}</span>
}

function stageSummary(stage, entries, run, active) {
  if (stage.id === 'finish' && run.status !== 'running') return title(run.status)
  if (!entries.length) return active ? 'In progress' : run.status === 'running' ? 'Waiting' : 'No recorded step'
  switch (stage.id) {
    case 'setup':
      return run.profile?.name || (run.mode === 'practice' ? 'Practice session' : 'Mock session')
    case 'plan': {
      const decisions = entries.filter(({ detail }) => detail?.decision)
      return decisions.length ? countLabel(decisions.length, 'decision') : 'Checking allowed actions'
    }
    case 'simulator': {
      const retries = entries.filter(({ detail }) => detail?.retry).length
      const attempts = countLabel(entries.length - retries, 'action')
      return retries ? `${attempts} · ${countLabel(retries, 'retry', 'retries')}` : attempts
    }
    case 'evaluate': {
      const observations = entries.filter(({ state, detail }) => state === 'EVALUATE' && detail?.observation)
      const replies = observations.filter(({ detail }) => detail.observation === 'reply').length
      return replies ? countLabel(replies, 'reply', 'replies')
        : observations.at(-1) ? describe(observations.at(-1).detail.observation) : 'Reviewing result'
    }
    default:
      return 'Run ended'
  }
}

function StepRecord({ entry, classification }) {
  const { state } = entry
  const detail = entry.detail || {}
  switch (state) {
    case 'START':
      return <li><strong>{detail.mode === 'mock_openai' ? 'OpenAI mock run started'
        : detail.mode === 'mock' ? 'Mock run started' : 'Run started'}</strong></li>
    case 'RESEARCH':
      return <li><strong>{detail.profile?.name || 'Load simulator profile'}</strong>
        {detail.source && <span>{title(detail.source)}</span>}
        {detail.profile?.role && <span>{detail.profile.role}</span>}
        {Number.isFinite(detail.remaining_touches) && <span>{countLabel(detail.remaining_touches, 'touch', 'touches')} available</span>}
      </li>
    case 'PLAN':
      if (!detail.decision && !detail.allowed?.length) return null
      return <li><strong>{detail.decision ? describe(detail.decision) : 'Check allowed actions'}</strong>
        {detail.channel && <span>{title(detail.channel)}</span>}
        {detail.allowed?.length > 0 && <span>{detail.allowed.map(describe).join(', ')}</span>}
      </li>
    case 'GENERATE':
      return <li><strong>{detail.repeated_phrase_skipped ? 'Repeated test phrase skipped' : 'Prepare test phrase'}</strong>
        {detail.action && <span>{describe(detail.action)}</span>}
      </li>
    case 'COMMUNICATE':
      return <li>
        <strong>{detail.retry ? `Retry: ${describe(detail.action)}` : detail.action ? describe(detail.action) : 'Simulator request'}</strong>
        {detail.channel && <span>{title(detail.channel)}</span>}
        {detail.retry && detail.http_status && <span>HTTP {detail.http_status}</span>}
        {detail.text && <blockquote>{detail.text}</blockquote>}
      </li>
    case 'EVALUATE':
      return <li><strong>{detail.observation ? describe(detail.observation) : 'Result recorded'}</strong>
        {detail.action && <span>{describe(detail.action)}</span>}
        {Number.isInteger(detail.remaining_touches) && <span>{countLabel(detail.remaining_touches, 'run touch', 'run touches')} left</span>}
        {detail.event_types?.length > 0 && <span>Events: {detail.event_types.map(label).join(', ')}</span>}
        {detail.replies?.map((reply, index) => <blockquote key={index}>{reply}</blockquote>)}
        {detail.observation === 'reply' && detail.replies?.length === 0 && <span>Reply text unavailable in local history.</span>}
        {classification && <span>Reply classified as {label(classification)}</span>}
      </li>
    case 'MEMORY':
      return detail.classification ? <li><strong>Reply classified as {label(detail.classification)}</strong></li> : null
    case 'STOP':
      return <li><strong>{title(detail.status || 'Run ended')}</strong>{detail.reason && <span>{detail.reason}</span>}</li>
    default:
      return null
  }
}

function isClassificationFor(result, memory) {
  return result?.state === 'EVALUATE' && memory?.state === 'MEMORY'
    && result.detail?.action && result.detail.action === memory.detail?.action
}

export function RunActivity({ run, formatCost }) {
  const [expanded, setExpanded] = useState()
  const [rawOpen, setRawOpen] = useState(false)
  const detailsId = useId()
  const timeline = run.timeline || []
  const showReplies = run.mode === 'practice' && run.status !== 'running'
    && timeline.some(({ detail }) => detail?.replies?.length)
  const selectedId = expanded === undefined && showReplies ? 'evaluate' : expanded
  const selected = stages.find(({ id }) => id === selectedId)
  const entries = (stage) => timeline.filter(({ state }) => stage.states.includes(state))
  const current = run.status === 'running' && (timeline.at(-1)?.state || run.state)
  const remaining = timeline.findLast(({ detail }) => Number.isInteger(detail?.remaining_touches))?.detail.remaining_touches
    ?? Math.max(0, run.max_touches - (run.touches_used ?? 0))

  return <>
    <dl className="live-facts qa-run-summary" aria-label="Run summary">
      <div><dt>Status</dt><dd data-status={run.status}>{title(run.status)}</dd></div>
      <div><dt>Touches used</dt><dd>{run.touches_used ?? 0} / {run.max_touches}</dd></div>
      <div><dt>Run touches left</dt><dd>{remaining}</dd></div>
      {run.stop_reason && <div className="qa-run-reason"><dt>Stop reason</dt><dd>{run.stop_reason}</dd></div>}
    </dl>

    <section className="qa-section qa-flow-section" aria-label="Agent data flow">
      <div className="qa-flow-heading">
        <h3>Agent data flow</h3>
        <p className="muted">Starting a conversation or sending a message uses one touch. Turns inside a conversation use none.</p>
      </div>
      <div className="qa-flow">
        <ol className="qa-flow-stages">
          {stages.map((stage) => {
            const recorded = entries(stage)
            const active = stage.states.includes(current)
            const available = recorded.length > 0
            const nameId = detailsId + stage.id
            return <li key={stage.id}>
              <button type="button" className="qa-flow-node" data-recorded={available} aria-current={active ? 'step' : undefined}
                disabled={!available} aria-expanded={selectedId === stage.id} aria-controls={detailsId}
                aria-labelledby={nameId} aria-describedby={nameId + '-summary'}
                onClick={() => setExpanded(selectedId === stage.id ? null : stage.id)}>
                <strong id={nameId}>{stage.name}</strong>
                <span id={nameId + '-summary'}>{stageSummary(stage, recorded, run, active)}</span>
              </button>
            </li>
          })}
        </ol>
        {timeline.some(({ state }) => state === 'CONTINUE') &&
          <p className="qa-flow-loop"><span aria-hidden="true">↶</span> Result feeds the next decision</p>}
      </div>
      {!timeline.length && <p className="qa-flow-empty muted">Waiting for recorded activity.</p>}
      <div id={detailsId} className="qa-flow-details" hidden={!selected}>
        {selected && <>
          <h4>{selected.name} details</h4>
          {selected.id === 'evaluate' && run.mode === 'practice' && run.status === 'running' &&
            <p className="muted">Saved reply text appears here when the run finishes.</p>}
          <ol className="qa-step-records">{entries(selected).map((entry, index, records) => {
            if (isClassificationFor(records[index - 1], entry)) return null
            const next = records[index + 1]
            const classification = isClassificationFor(entry, next) ? next.detail.classification : undefined
            return <StepRecord key={entry.id ?? index} entry={entry} classification={classification} />
          })}</ol>
        </>}
      </div>
    </section>

    <details className="qa-section qa-technical">
      <summary>Technical details</summary>
      <div className="qa-technical-body">
        <RecordValue value={{
          session: run.session_id,
          model: run.model,
          estimated_run_cost: formatCost(run.usage?.estimated_usd ?? 0),
          input_tokens: run.usage?.input_tokens ?? 0,
          cached_input_tokens: run.usage?.cached_tokens ?? 0,
          output_tokens: run.usage?.output_tokens ?? 0,
          ...(run.profile && { simulator_profile: run.profile }),
        }} />
        {timeline.length > 0 && <details className="qa-raw-log" onToggle={(event) => setRawOpen(event.currentTarget.open)}>
          <summary>Raw timeline</summary>
          {rawOpen && <ol className="qa-timeline" aria-label="Raw timeline">
            {timeline.map((entry, index) => <li key={entry.id ?? index}>
              <div className="qa-step"><strong>{entry.state}</strong>
                {displayTime(entry.created_at) && <time dateTime={entry.created_at}>{displayTime(entry.created_at)}</time>}
              </div>
              <RecordValue value={entry.detail} />
            </li>)}
          </ol>}
        </details>}
      </div>
    </details>
  </>
}
