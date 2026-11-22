import React from 'react'
import { AgentRuns } from './AgentRuns.jsx'

export function App() {
  return <div className="shell">
    <header className="topbar">
      <div className="brand">
        <span className="mark" aria-hidden="true">7</span>
        <span>7_Social_Hack</span>
      </div>
    </header>
    <main className="page">
      <h1>Agent workflow</h1>
      <AgentRuns />
    </main>
  </div>
}
