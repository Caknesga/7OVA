import React from 'react'
import { AgentRuns } from './AgentRuns.jsx'

export function App() {
  return <div className="shell">
    <header className="topbar">
      <div className="brand">
        <span className="mark" aria-hidden="true">r8</span>
        <span>revel8 <span className="brand-divider">/</span> START Hack 2026</span>
      </div>
    </header>
    <main className="page">
      <h1>Agent workflow</h1>
      <AgentRuns />
    </main>
  </div>
}
