/**
 * A folder's conductor-chat menu item, and the rendering it leaves alone.
 *
 * The feature is one menu item: a folder whose sessions were all opened by one of
 * them offers that session at the top of the folder's actions menu, and choosing
 * it opens that conversation. It is a menu item rather than a button beside the
 * row's other two because a row of actions is capped at two controls.
 *
 * The item NAMES the session, which is the assertion that matters most about its
 * text: a label reading only "conductor" left a first-time reader unable to say
 * what the click opens, with two other chat affordances on the same row.
 *
 * Tens of thousands of people already read this list, so the contract asserted
 * hardest here is the one with no feature in it. Nothing on the resting sidebar
 * moves: the hover group still holds exactly its two controls, a conducted folder
 * still draws the folder glyph, still counts what it counted, and still draws a
 * card for every session filed in it — the conductor's included.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'
import type { ChatFolder, ChatSlot } from '../types'
import type { RootState } from '../store'

vi.mock('framer-motion', async () => {
  const React = await import('react')
  const FRAMER_PROPS = new Set([
    'layout', 'layoutId', 'layoutScroll', 'initial', 'animate', 'exit',
    'transition', 'variants', 'whileHover', 'whileTap', 'whileInView',
    'drag', 'dragConstraints', 'dragElastic', 'onAnimationComplete',
  ])
  const make = (tag: string) =>
    React.forwardRef((props: Record<string, unknown>, ref: React.Ref<unknown>) => {
      const clean: Record<string, unknown> = {}
      for (const k of Object.keys(props)) {
        if (k === 'children') continue
        if (k === 'layoutId') { clean['data-layout-id'] = props[k]; continue }
        if (FRAMER_PROPS.has(k)) continue
        clean[k] = props[k]
      }
      return React.createElement(tag, { ...clean, ref }, props.children as React.ReactNode)
    })
  const motion = new Proxy({}, { get: (_t, tag: string) => make(tag) })
  return {
    motion,
    AnimatePresence: ({ children }: { children?: React.ReactNode }) => React.createElement(React.Fragment, null, children),
    LayoutGroup: ({ children }: { children?: React.ReactNode }) => React.createElement(React.Fragment, null, children),
  }
})

vi.mock('../components/ProjectPicker', () => ({ default: () => null }))

vi.mock('../pages/chat/ChatSettings', () => ({
  loadChatConfig: () => ({ tagColumnsEnabled: false, hideEmptyFolderBody: false, confirmCloseSession: false }),
  saveChatConfig: vi.fn(),
}))

// `useIsMobile` resolves at module load, so the hook itself is mocked rather
// than the viewport: this file asserts the DESKTOP folder row, whose menu is a
// flyout. At phone width the same items are inlined under a caption instead.
vi.mock('../hooks/useIsMobile', () => ({
  MOBILE_BREAKPOINT: 768,
  useIsMobile: () => false,
}))

const mocks = vi.hoisted(() => ({ chatFolders: vi.fn() }))

vi.mock('../api/client', () => ({
  SEARCH_MIN_CHARS: 2,
  api: new Proxy(mocks as Record<string, unknown>, {
    get: (target, prop: string) => (prop in target ? target[prop] : vi.fn().mockResolvedValue([])),
  }),
}))

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockImplementation((q: string) => ({
    matches: false, media: q, onchange: null,
    addListener: vi.fn(), removeListener: vi.fn(),
    addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn(),
  })),
})

import ChatSidebar from '../pages/ChatSidebar'

const LANE = 'folder-lane'
const PLAIN = 'folder-plain'
const LEAD = 'k-lead'
const W1 = 'k-worker-1'
const W2 = 'k-worker-2'

const folders: ChatFolder[] = [
  { id: LANE, name: 'remote-crew', order: 0 },
  { id: PLAIN, name: 'docs', order: 1 },
]

/**
 * One conducted folder and one ordinary folder.
 *
 * No timestamps anywhere, deliberately: a row with no activity instant is never
 * stale, so every row stays in the live list and no assertion depends on where
 * `now` happens to sit. A single old `created` was enough to sweep a card into
 * the collapsed stale section, which reads exactly like the feature having
 * removed it.
 */
const slots: ChatSlot[] = [
  { key: LEAD, title: 'Remote crew rollout', messages: 9, running: false, folder_id: LANE },
  { key: W1, title: 'Fargate task definition', messages: 3, running: false, folder_id: LANE, parent: { slot: LEAD, key: LEAD } },
  { key: W2, title: 'Rotate remote crew token', messages: 2, running: false, folder_id: LANE, parent: { slot: LEAD, key: LEAD } },
  { key: 'k-plain-a', title: 'Blog draft', messages: 4, running: false, folder_id: PLAIN },
  { key: 'k-plain-b', title: 'Release notes', messages: 1, running: false, folder_id: PLAIN },
]

/**
 * Mount the sidebar. SYNCHRONOUS, and that is load-bearing.
 *
 * A Radix menu in jsdom opens only while the render is still in its first tick:
 * nothing here holds the focus it grabs, so the first macrotask both tears an
 * open menu down and leaves a later open gesture with nothing to open. So a test
 * that drives the menu must mount, open and assert with no await between them,
 * and call `settle` afterwards to let the lane finish painting.
 */
function renderSidebar(slotData: ChatSlot[] = slots, onSelectSlot?: (key: string) => void) {
  const store = createTestStore({
    dashboard: {
      status: {}, connected: true, slots: slotData, approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as unknown as RootState['dashboard'],
    chat: { activeSlot: null, automations: {} } as unknown as RootState['chat'],
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  qc.setQueryData(['chat-tags'], [])
  qc.setQueryData(['tag-columns'], [])
  qc.setQueryData(['chat-folders'], folders)
  qc.setQueryData(['kirocrewConfig'], { dashboard: {} })
  mocks.chatFolders.mockImplementation(() => Promise.resolve(folders))
  const utils = render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatSidebar
              slots={slotData} activeSlot={null} unreadSlots={[]}
              history={[]} historyHasMore={false} defaultAgent="" installedAgents={[]}
              onSelectSlot={onSelectSlot}
            />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
  return utils
}

/**
 * Wait for the lane to finish painting.
 *
 * `ThemeProvider` settles its own state after mount, so a test that returns
 * before the rows are on screen leaves an act() warning behind, and one that
 * asserts before them reads a half-rendered lane.
 */
async function settle(utils: { findByText: (text: string) => Promise<HTMLElement> }) {
  await utils.findByText('Blog draft')
}

/** The folder row's own box, the only element carrying `data-folder-row`. */
function rowOf(container: HTMLElement, folderId: string): HTMLElement {
  const row = container.querySelector(`[data-folder-row="${folderId}"]`)
  if (!row) throw new Error(`no folder row for ${folderId}`)
  return row as HTMLElement
}

/**
 * A session CARD, as the ordinary lanes draw it.
 *
 * `data-slot-key`, which sits on the outermost row element the card renders —
 * NOT `data-session-row`, which carries the row IDENTITY (origin-qualified for a
 * federated row) and so does not match a bare slot key.
 */
function cardOf(container: HTMLElement, key: string): Element | null {
  return container.querySelector(`[data-slot-key="${key}"]`)
}

/**
 * Open the folder's ⋯ menu, SYNCHRONOUSLY.
 *
 * Radix tears the menu down on the first macrotask because nothing in jsdom
 * holds the focus it grabs, so every caller must drive the item it wants in the
 * same tick with no await in between. Rename is the anchor item: it is in the
 * menu on main, so a silent no-open cannot pass a later query.
 */
function openFolderMenu(folderId: string) {
  // Keyboard, because a Radix trigger needs a real PointerEvent jsdom has not
  // got. Rename is the anchor: it is in this menu on main, so a silent no-open
  // cannot pass a later query.
  fireEvent.keyDown(document.querySelector(`[data-testid="folder-menu-${folderId}"]`)!, { key: 'Enter' })
  expect(document.querySelector(`[data-testid="folder-rename-${folderId}"]`)).not.toBeNull()
}

const menuItem = (folderId: string, suffix = ''): HTMLElement | null =>
  document.querySelector(`[data-testid="folder-conductor-chat-${folderId}${suffix}"]`)

beforeEach(() => {
  localStorage.clear()
  mocks.chatFolders.mockImplementation(() => Promise.resolve(folders))
})
afterEach(() => vi.clearAllMocks())

describe('the folder menu item', () => {
  it('names the session it opens, and says what that session is', async () => {
    // Not "Chat with conductor": the label carries the session's own title and
    // the second line explains what that session is to the folder, because the
    // word on its own told a first-time reader nothing.
    const utils = renderSidebar()
    openFolderMenu(LANE)
    const item = menuItem(LANE)
    expect(item).not.toBeNull()
    expect(item!.textContent).toContain('Remote crew rollout')
    expect(item!.textContent).toContain('started this folder')
    await settle(utils)
  })

  it('is the first item in that menu', async () => {
    // First, because it is the one thing a reader opens the menu of a running
    // folder for; the housekeeping items below it are unchanged and unmoved.
    const utils = renderSidebar()
    openFolderMenu(LANE)
    const items = Array.from(document.querySelectorAll('[role="menuitem"]'))
    expect(items[0]?.getAttribute('data-testid')).toBe(`folder-conductor-chat-${LANE}`)
    await settle(utils)
  })

  it('is absent for a folder with no conductor', async () => {
    // `docs` holds two hand-made sessions: nobody opened anybody, so there is
    // nothing to open and the menu is the menu on main.
    const utils = renderSidebar()
    openFolderMenu(PLAIN)
    expect(menuItem(PLAIN)).toBeNull()
    await settle(utils)
  })

  it('opens the conductor when chosen', async () => {
    const onSelectSlot = vi.fn()
    const utils = renderSidebar(slots, onSelectSlot)
    openFolderMenu(LANE)
    fireEvent.click(menuItem(LANE)!)
    expect(onSelectSlot).toHaveBeenCalledWith(LEAD)
    await settle(utils)
  })

  it('is offered by the row\'s right-click menu too', async () => {
    // Both menus render from one list, so the action cannot be in one and not
    // the other. The context copy's testids carry a `-ctx` suffix.
    const utils = renderSidebar()
    fireEvent.contextMenu(rowOf(utils.container, LANE), { clientX: 40, clientY: 20 })
    expect(menuItem(LANE, '-ctx')).not.toBeNull()
    await settle(utils)
  })
})

describe('what a conducted folder must still render', () => {
  it('leaves the hover group at its two controls', async () => {
    // The whole reason this is a menu item: the row's action group holds the ⋯
    // trigger and new-chat, and a third peer control there is over the cap. With
    // the menu closed the folder row offers nothing it does not offer on main.
    const utils = renderSidebar()
    await settle(utils)
    const row = rowOf(utils.container, LANE)
    const actions = Array.from(row.querySelectorAll('button'))
      .filter(b => b.getAttribute('data-testid')?.startsWith('folder-menu-')
        || b.getAttribute('data-testid')?.startsWith('folder-new-chat-'))
    expect(actions).toHaveLength(2)
    expect(menuItem(LANE)).toBeNull()
  })

  it('draws a card for every session in it, the conductor included', async () => {
    const { container, findByText } = renderSidebar()
    await settle({ findByText })
    expect(cardOf(container, LEAD)).not.toBeNull()
    expect(cardOf(container, W1)).not.toBeNull()
    expect(cardOf(container, W2)).not.toBeNull()
  })

  it('keeps the folder glyph and the direct-child count', async () => {
    const { container, findByText } = renderSidebar()
    await settle({ findByText })
    // The lucide shape, not a mark standing in for it.
    const shape = container.querySelector(`[data-testid="folder-collapse-${LANE}-shape"]`)
    expect(shape?.tagName.toLowerCase()).toBe('svg')
    expect(rowOf(container, LANE).textContent).toContain('3')
  })
})

describe('a folder with no conductor', () => {
  it('renders the row and its cards exactly as it did before', async () => {
    const { container, findByText } = renderSidebar()
    await settle({ findByText })
    const shape = container.querySelector(`[data-testid="folder-collapse-${PLAIN}-shape"]`)
    expect(shape?.tagName.toLowerCase()).toBe('svg')
    expect(rowOf(container, PLAIN).textContent).toContain('2')
    expect(cardOf(container, 'k-plain-a')).not.toBeNull()
    expect(cardOf(container, 'k-plain-b')).not.toBeNull()
  })
})
