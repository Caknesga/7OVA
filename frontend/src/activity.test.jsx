import React from 'react'
import { cleanup, render, screen, within } from '@testing-library/react'
import { afterEach, expect, it } from 'vitest'
import { EventSummary } from './activity.jsx'

afterEach(cleanup)

it('counts simulator event types and keeps unknown events separate', () => {
  const types = ['reply', 'reply', 'link.opened', 'page.submitted', 'attachment.opened', 'other']
  render(<EventSummary events={types.map((type) => ({ type }))} />)
  const summary = screen.getByRole('region', { name: 'Observed activity' })
  expect(within(summary).getByRole('heading', { name: 'Replies' }).parentElement.textContent).toBe('Replies2')
  expect(within(summary).getAllByText('1')).toHaveLength(3)
  expect(within(summary).getByText('Other recorded events: 1')).toBeTruthy()
})

it('reports an empty event log without inferring any response', () => {
  render(<EventSummary events={[]} />)
  expect(screen.getByText('No simulator events recorded for this session.')).toBeTruthy()
  expect(screen.getAllByText('0')).toHaveLength(4)
})
