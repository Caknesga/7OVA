import React, { useEffect, useRef, useState } from 'react'
import { api, post } from './api.js'
import { RunActivity } from './RunActivity.jsx'

const mockTarget = 'qa_persona'
const modeLabels = { mock: 'Mock', mock_openai: 'OpenAI + Mock', practice: 'Official practice' }
const pollInterval = 1000
const usd = (amount) => Number.isFinite(amount)
  ? amount.toLocaleString(undefined, { style: 'currency', currency: 'USD', maximumFractionDigits: 6 })
  : '—'
const title = (value) => String(value).replaceAll('_', ' ').replace(/^./, (first) => first.toUpperCase())

function savedStart() {
  try {
    return JSON.parse(sessionStorage.getItem('qa-start-request'))
  } catch {
    return null
  }
}

function rememberStart(request) {
  try {
    if (request) sessionStorage.setItem('qa-start-request', JSON.stringify(request))
    else sessionStorage.removeItem('qa-start-request')
  } catch {
    // In-memory retry still applies if browser storage is unavailable.
  }
}

export function AgentRuns({ targets, sessions }) {
  const [config, setConfig] = useState(null)
  const [runs, setRuns] = useState([])
  const [selected, setSelected] = useState('')
  const [snapshot, setSnapshot] = useState(null)
  const [mode, setMode] = useState('mock')
  const [target, setTarget] = useState('')
  const [sessionId, setSessionId] = useState('')
  const [touches, setTouches] = useState('2')
  const [confirmed, setConfirmed] = useState(false)
  const [error, setError] = useState('')
  const [pollError, setPollError] = useState('')
  const [busy, setBusy] = useState('')
  const [requestedStop, setRequestedStop] = useState('')
  const [reload, setReload] = useState(0)
  const [previousStart] = useState(savedStart)
  const pendingStart = useRef(previousStart)
  const pendingStop = useRef(null)

  const practice = mode === 'practice'
  const paid = mode !== 'mock'
  const modelAvailable = practice ? config?.real_available : config?.openai_available
  const activeSessions = sessions.filter((session) => session.status === 'active')
  const run = snapshot?.id === selected ? snapshot : runs.find((item) => item.id === selected)
  const stopping = run?.status === 'running' && (run.stop_requested || requestedStop === run.id)
  const canStart = config && !busy
    && (!paid || (modelAvailable && config.remaining_usd > 0 && confirmed))
    && (!practice || (
      targets.some((item) => item.id === target)
    && activeSessions.some((item) => item.session_id === sessionId)
    ))

  useEffect(() => {
    const controller = new AbortController()
    Promise.all([
      api('/qa/config', { signal: controller.signal }),
      api('/qa/runs', { signal: controller.signal }),
    ]).then(([settings, history]) => {
      if (controller.signal.aborted) return
      setConfig(settings)
      setRuns(history)
      setSelected((previous) => history.some((item) => item.id === previous) ? previous : (history[0]?.id || ''))
    }).catch((problem) => {
      if (!controller.signal.aborted) setError(problem.message)
    })
    return () => controller.abort()
  }, [reload])

  useEffect(() => {
    if (!selected) return
    const controller = new AbortController()
    let timer

    async function inspect() {
      let delay = 0
      try {
        const next = await api('/qa/runs/' + encodeURIComponent(selected), { signal: controller.signal })
        if (controller.signal.aborted) return
        setSnapshot(next)
        setPollError('')
        setRuns((previous) => previous.map((item) => item.id === next.id ? next : item))
        if (next.status === 'running') delay = pollInterval
        const settings = await api('/qa/config', { signal: controller.signal })
        if (!controller.signal.aborted) setConfig(settings)
      } catch (problem) {
        if (controller.signal.aborted) return
        setPollError(problem.message)
        delay = pollInterval * 2
      } finally {
        if (delay && !controller.signal.aborted) timer = setTimeout(inspect, delay)
      }
    }

    void inspect()
    return () => {
      controller.abort()
      clearTimeout(timer)
    }
  }, [selected, reload])

  function refresh() {
    setError('')
    setPollError('')
    setReload((previous) => previous + 1)
  }

  async function start(event) {
    event.preventDefault()
    if (!canStart) return
    const maxTouches = Number(touches)
    if (!Number.isInteger(maxTouches) || maxTouches < 1 || maxTouches > 10) return
    const payload = {
      mode,
      target: practice ? target : mockTarget,
      session_id: practice ? sessionId : null,
      max_touches: maxTouches,
      confirm_practice: practice && confirmed,
    }
    const fingerprint = JSON.stringify(payload)
    if (pendingStart.current?.fingerprint !== fingerprint) {
      pendingStart.current = { fingerprint, key: crypto.randomUUID() }
      rememberStart(pendingStart.current)
    }
    setBusy('start')
    setError('')
    setPollError('')
    try {
      const started = await post('/qa/runs', pendingStart.current.key, payload)
      pendingStart.current = null
      rememberStart(null)
      setRuns((previous) => [started, ...previous.filter((item) => item.id !== started.id)])
      setSnapshot(started)
      setSelected(started.id)
      setConfirmed(false)
    } catch (problem) {
      setError(problem.message)
    } finally {
      setBusy('')
    }
  }

  async function stop() {
    if (busy || stopping || run?.status !== 'running') return
    if (pendingStop.current?.id !== run.id) pendingStop.current = { id: run.id, key: crypto.randomUUID() }
    setBusy('stop')
    setError('')
    try {
      await post('/qa/runs/' + encodeURIComponent(run.id) + '/stop', pendingStop.current.key)
      pendingStop.current = null
      setRequestedStop(run.id)
      refresh()
    } catch (problem) {
      setError(problem.message)
    } finally {
      setBusy('')
    }
  }

  return <section aria-label="AI Agent runs">
    <form className="compose live-start" onSubmit={start}>
      <fieldset disabled={!!busy}>
        <legend>Configure a QA run</legend>
        <div className="grid gap-3 sm:grid-cols-2">
          <label>Simulator mode<select value={mode} onChange={(event) => {
            setMode(event.target.value)
            setConfirmed(false)
          }}>
            <option value="mock">Mock</option>
            <option value="mock_openai" disabled={!config?.openai_available}>OpenAI + Mock</option>
            <option value="practice" disabled={!config?.real_available}>Official practice</option>
          </select></label>
          <label>Maximum touches<input type="number" min="1" max="10" step="1" required value={touches}
            onChange={(event) => { setTouches(event.target.value); setConfirmed(false) }} /></label>
          <label>Target<select required value={practice ? target : mockTarget}
            onChange={(event) => { setTarget(event.target.value); setConfirmed(false) }}>
            {practice ? <>
              <option value="">Choose a practice target</option>
              {targets.map((item) => <option key={item.id} value={item.id}>{item.name || item.id}</option>)}
            </> : <option value={mockTarget}>Mock QA persona</option>}
          </select></label>
          <label>Session<select required={practice} value={practice ? sessionId : ''}
            onChange={(event) => { setSessionId(event.target.value); setConfirmed(false) }}>
            {practice ? <>
              <option value="">Choose an existing practice session</option>
              {activeSessions.map((item) => <option key={item.session_id} value={item.session_id}>{item.session_id}</option>)}
            </> : <option value="">New isolated mock session</option>}
          </select></label>
        </div>
        <p className="qa-note">{paid ? config?.model : 'Mock fixtures'} · Approved QA phrases</p>
        {config?.configuration_error && <p className="muted">
          {config.configuration_error}
        </p>}
        {paid && config?.remaining_usd <= 0 && <p className="qa-warning">The local estimated budget is exhausted.</p>}
        {practice && !activeSessions.length && <p className="qa-note">Create a practice session using New session before starting.</p>}
        {paid && <label className="qa-consent">
          <input type="checkbox" checked={confirmed} onChange={(event) => setConfirmed(event.target.checked)} />
          {practice ? 'Allow official simulator touches and paid OpenAI requests.'
            : 'Allow paid OpenAI requests for this fictional mock session.'}
        </label>}
        <button className="primary" disabled={!canStart}>{busy === 'start' ? 'Starting…' : 'Start run'}</button>
      </fieldset>
    </form>

    {config && <section className="qa-section" aria-label="Estimated OpenAI spending">
      <dl className="qa-spend">
        <div><dt>Estimated project cost</dt><dd>{usd(config.estimated_total_usd)} / {usd(config.budget_usd)}</dd></div>
        <div><dt>Remaining estimate</dt><dd>{usd(config.remaining_usd)}</dd></div>
        <div><dt>Per-run limit</dt><dd>{usd(config.max_run_cost_usd)}</dd></div>
      </dl>
      {config.soft_limit_reached && <p className="qa-warning" role="status">Estimated spending reached the {usd(config.soft_limit_usd)} warning threshold.</p>}
      <p className="muted">Local estimates are separate from OpenAI billing.</p>
    </section>}

    {error && <p className="live-error" role="alert">{error}</p>}
    {pollError && <p className="live-error" role="alert">{pollError}</p>}
    <div className="compose live-selector">
      <label>Run history<select value={selected} disabled={!!busy || !runs.length} onChange={(event) => {
        setSelected(event.target.value)
        setPollError('')
      }}>
        {!runs.length && <option value="">No recorded runs</option>}
        {runs.map((item) => <option key={item.id} value={item.id}>
          {modeLabels[item.mode] || title(item.mode)} · {item.target} · {title(item.status)} · {item.id}
        </option>)}
      </select></label>
      <div className="actions">
        <button type="button" disabled={!!busy} onClick={refresh}>Refresh runs</button>
        <button type="button" disabled={!!busy || stopping || run?.status !== 'running'} onClick={stop}>
          {busy === 'stop' ? 'Stopping…' : stopping ? 'Stop requested' : 'Stop run'}
        </button>
      </div>
    </div>
    {run && <RunActivity key={run.id} run={run} formatCost={usd} />}
  </section>
}
