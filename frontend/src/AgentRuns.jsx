import React, { useEffect, useState } from 'react'
import { label } from './activity.jsx'
import { readBackend } from './api.js'
import { RunActivity } from './RunActivity.jsx'
import { SessionActivity } from './SessionActivity.jsx'

const sampleModes = new Set(['mock', 'mock_openai'])
const formatCost = new Intl.NumberFormat(undefined, {
  style: 'currency', currency: 'USD', maximumFractionDigits: 6,
}).format

export function AgentRuns() {
  const [catalog, setCatalog] = useState({ runs: [], loaded: false, error: '' })
  const [pinnedId, setPinnedId] = useState('')
  const [snapshot, setSnapshot] = useState(null)
  const [detailError, setDetailError] = useState(null)
  const [revision, setRevision] = useState(0)

  useEffect(() => {
    const controller = new AbortController()
    const { signal } = controller
    let timer

    async function load() {
      try {
        const list = await readBackend('/qa/runs', { signal })
        if (!Array.isArray(list)) throw new Error('The backend returned an invalid run list.')
        if (signal.aborted) return
        setCatalog({ runs: list.filter((run) => run?.id && !sampleModes.has(run.mode)), loaded: true, error: '' })
      } catch (error) {
        if (signal.aborted) return
        setCatalog((previous) => ({ ...previous, error: error.message }))
      }
      timer = setTimeout(load, 4000)
    }

    void load()
    return () => {
      controller.abort()
      clearTimeout(timer)
    }
  }, [revision])

  const { runs } = catalog
  const pinned = runs.find((item) => item.id === pinnedId)
  const summary = pinned || runs[0]
  const id = summary?.id
  const knownRunning = summary?.status === 'running'

  useEffect(() => {
    if (!id) return
    const controller = new AbortController()
    const { signal } = controller
    const path = '/qa/runs/' + encodeURIComponent(id)
    let timer
    let running = knownRunning

    async function load() {
      try {
        const run = await readBackend(path, { signal })
        if (signal.aborted) return
        if (run.id !== id || (run.timeline != null && (!Array.isArray(run.timeline)
          || run.timeline.some((entry) => typeof entry?.state !== 'string')))) {
          throw new Error('The backend returned an invalid run timeline.')
        }
        setSnapshot(run)
        setDetailError(null)
        running = run.status === 'running'
      } catch (error) {
        if (signal.aborted) return
        setDetailError({ id, message: error.message })
      }
      if (running) timer = setTimeout(load, 1000)
    }

    void load()
    return () => {
      controller.abort()
      clearTimeout(timer)
    }
  }, [id, knownRunning, revision])

  const run = id && snapshot?.id === id ? snapshot : null
  const pullError = detailError?.id === id ? detailError?.message : ''
  const connection = catalog.error ? 'unavailable' : catalog.loaded ? 'connected' : 'connecting'
  const connectionLabel = {
    unavailable: 'Backend unavailable', connected: 'Backend connected', connecting: 'Connecting to backend',
  }[connection]
  let emptyMessage = pullError || catalog.error
  if (!emptyMessage && !run) {
    emptyMessage = id ? 'Loading run timeline…' : catalog.loaded ? 'No live runs recorded.' : 'Loading runs…'
  }

  return <>
    <section className="panel workflow-panel" aria-label="Workflow timeline">
      <div className="workflow-toolbar">
        <p role="status" className="connection" data-connection={connection}>{connectionLabel}</p>
        <div className="workflow-controls">
          <label>Run history<select value={pinned?.id || ''} disabled={!runs.length} onChange={(event) => {
            setDetailError(null)
            setPinnedId(event.target.value)
          }}>
            <option value="">{runs.length ? 'Follow latest run' : 'No runs available'}</option>
            {runs.map((item) => <option key={item.id} value={item.id}>
              {[item.target, label(item.id === run?.id ? run.status : item.status), item.id].filter(Boolean).join(' · ')}
            </option>)}
          </select></label>
          <button type="button" className="button" onClick={() => setRevision((value) => value + 1)}>Refresh</button>
        </div>
      </div>
      {run && (catalog.error || pullError) && <p className="live-error" role="alert">{catalog.error || pullError} Showing the last loaded timeline.</p>}
      <RunActivity key={id ?? 'empty'} run={run || summary} formatCost={formatCost}
        emptyMessage={emptyMessage} emptyError={Boolean(pullError || catalog.error)} />
    </section>
    <SessionActivity preferredId={run?.session_id || summary?.session_id} revision={revision} />
  </>
}
