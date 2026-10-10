/**
 * A host can route the effort write elsewhere: the crew window writes the
 * PEER's slot through the hub proxy, because its slot key names no session on
 * this gateway. Without the seam the pick would post to THIS gateway's route.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { configureStore } from '@reduxjs/toolkit'
import React from 'react'

import ReasoningEffortDropdown from '../components/ReasoningEffortDropdown'
import { api } from '../api/client'
import chatReducer from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'

function wrap(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const store = configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
  })
  return render(<Provider store={store}><QueryClientProvider client={qc}>{ui}</QueryClientProvider></Provider>)
}

let localWrite: ReturnType<typeof vi.spyOn>

beforeEach(() => {
  vi.restoreAllMocks()
  localWrite = vi.spyOn(api, 'chatSlotReasoningEffort').mockResolvedValue({ ok: true } as never)
})

describe('ReasoningEffortDropdown writeEffort', () => {
  it('writes a pick through the host seam, never this gateway\'s slot route', async () => {
    const writeEffort = vi.fn().mockResolvedValue({ reasoning_effort: '' })
    wrap(<ReasoningEffortDropdown slot="crew-window:x" currentEffort="high" onClose={vi.fn()} embedded levelsOverride={['low', 'high']} writeEffort={writeEffort} />)
    fireEvent.click(screen.getByRole('switch', { name: 'Use default effort' }))
    await waitFor(() => expect(writeEffort).toHaveBeenCalledWith(''))
    expect(localWrite).not.toHaveBeenCalled()
  })

  it('keeps writing this gateway\'s slot route when no seam is given', async () => {
    wrap(<ReasoningEffortDropdown slot="chat-1" currentEffort="high" onClose={vi.fn()} embedded levelsOverride={['low', 'high']} />)
    fireEvent.click(screen.getByRole('switch', { name: 'Use default effort' }))
    await waitFor(() => expect(localWrite).toHaveBeenCalledWith('chat-1', ''))
  })
})
