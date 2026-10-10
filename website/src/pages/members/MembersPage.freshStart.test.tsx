import { describe, it, expect, vi, beforeEach } from 'vitest'
import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { renderWithProviders } from '../../test/helpers'

/* Fresh start on a crewmate's pinned thread: one confirm, then one call to the
 * member route. On success the pane folds the rows from before the server's
 * reset time; the thread keeps its slot key. Same api mock shape as
 * MembersPage.loop.test.tsx. */
vi.mock('../../api/client', () => ({
  api: {
    members: vi.fn(),
    teams: { list: vi.fn(() => Promise.resolve({ teams: [] })) },
    memberThread: vi.fn((slug: string) =>
      Promise.resolve({ slot_key: 'member-' + slug, slug, member: slug, created: true }),
    ),
    memberActivity: vi.fn(() => Promise.resolve({ slug: '', member: '', capped: false, entries: [] })),
    crons: vi.fn(() => Promise.resolve({ jobs: [] })),
    webhooks: vi.fn(() => Promise.resolve({ tokens: [] })),
    defaultAgent: vi.fn(() => Promise.resolve({ default_agent: '' })),
    autonudgeList: vi.fn(() => Promise.resolve({ enabled: true, loops: [] })),
    memberPanel: vi.fn(() => Promise.resolve({ panel: null, html: null })),
    memberFreshStart: vi.fn(),
  },
}))

vi.mock('../../components/ChatPane', () => ({
  default: ({ slotKey, foldBefore }: { slotKey: string; foldBefore?: string }) => (
    <div data-testid="chat-pane-stub" data-fold-before={foldBefore ?? ''}>{slotKey}</div>
  ),
}))

import { api } from '../../api/client'
import MembersPage from './MembersPage'

const RESET_AT = '2026-10-10T06:00:00+00:00'
const THREAD_OPEN_TIMEOUT_MS = 5000

function row(name: string) {
  return {
    name, slug: name, slot_key: `member-${name}`, running: false, kiro_agent: name,
    workspace: 'default', memory_store: 'default', model: '', source: 'kirocrew', starred: false,
  }
}

async function openAlpha() {
  ;(api.members as ReturnType<typeof vi.fn>).mockResolvedValue({ members: [row('alpha'), row('beta')] })
  renderWithProviders(<MembersPage />, { route: '/members?member=alpha' })
  // Roster read -> activation effect -> memberThread mutation -> pane render:
  // a chain of async hops, so it gets more than the default one second.
  await waitFor(() => expect(screen.getByTestId('chat-pane-stub').textContent).toBe('member-alpha'), { timeout: THREAD_OPEN_TIMEOUT_MS })
  return screen.findByTestId('member-fresh-start')
}

/** Press Fresh start and return the themed confirm it opens. */
async function pressAndAsk(button: HTMLElement) {
  fireEvent.click(button)
  const dialog = await screen.findByRole('dialog')
  expect(within(dialog).getByText('Give this crewmate a fresh start?')).toBeTruthy()
  expect(within(dialog).getByText(/messages stay here/i)).toBeTruthy()
  return dialog
}

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
})

describe('MembersPage — Fresh start', () => {
  it('asks first, and a cancel changes nothing', async () => {
    const button = await openAlpha()
    // The ellipsis says a dialog follows; the tooltip says what is kept.
    expect(button).toHaveAccessibleName('Fresh start…')
    expect(button.textContent).toContain('Fresh start…')
    expect(button.getAttribute('title')).toMatch(/messages are kept/i)
    const dialog = await pressAndAsk(button)
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(api.memberFreshStart).not.toHaveBeenCalled()
    expect(screen.getByTestId('chat-pane-stub').getAttribute('data-fold-before')).toBe('')
  })

  it('a confirmed press calls the member route and folds at the server time, same slot', async () => {
    vi.mocked(api.memberFreshStart).mockResolvedValue({ slot: 'member-alpha', reset_at: RESET_AT, outcome: 'cleared' })
    const button = await openAlpha()
    const dialog = await pressAndAsk(button)
    fireEvent.click(within(dialog).getByRole('button', { name: 'Start fresh' }))
    await waitFor(() => expect(api.memberFreshStart).toHaveBeenCalledWith('alpha'))
    await waitFor(() =>
      expect(screen.getByTestId('chat-pane-stub').getAttribute('data-fold-before')).toBe(RESET_AT),
    )
    expect(screen.getByTestId('chat-pane-stub').textContent).toBe('member-alpha')
    // No second thread was opened for it.
    expect(api.memberThread).toHaveBeenCalledTimes(1)
  })

  it('a clear that is still queued folds nothing', async () => {
    vi.mocked(api.memberFreshStart).mockResolvedValue({ slot: 'member-alpha', reset_at: RESET_AT, outcome: 'queued' })
    const button = await openAlpha()
    const dialog = await pressAndAsk(button)
    fireEvent.click(within(dialog).getByRole('button', { name: 'Start fresh' }))
    await waitFor(() => expect(api.memberFreshStart).toHaveBeenCalledWith('alpha'))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(screen.getByTestId('chat-pane-stub').getAttribute('data-fold-before')).toBe('')
  })

  it('a refused press says so and keeps every row unfolded', async () => {
    vi.mocked(api.memberFreshStart).mockRejectedValue(new Error('HTTP 409'))
    const button = await openAlpha()
    const dialog = await pressAndAsk(button)
    fireEvent.click(within(dialog).getByRole('button', { name: 'Start fresh' }))
    const notice = await screen.findByTestId('member-fresh-start-error')
    expect(within(notice).getByText(/conversation was kept/i)).toBeTruthy()
    expect(screen.getByTestId('chat-pane-stub').getAttribute('data-fold-before')).toBe('')
  })
})
