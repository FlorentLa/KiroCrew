import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, act, waitFor } from '@testing-library/react'
import AssistantMessage from '../pages/chat/AssistantMessage'

vi.mock('../components/MarkdownRenderer', () => ({
  default: ({ content }: { content: string }) => <div data-testid="md">{content}</div>,
}))
vi.mock('../hooks/useSmoothStream', () => ({ useSmoothStream: (content: string) => content }))
vi.mock('../utils/clipboard', () => ({
  copyToClipboard: vi.fn().mockResolvedValue(true),
  copyRichToClipboard: vi.fn().mockResolvedValue(true),
}))
import { copyRichToClipboard, copyToClipboard } from '../utils/clipboard'

const REPLY = '# Plan\n\n- **one**\n- two\n\nSee [docs](https://example.com).'

beforeEach(() => {
  vi.mocked(copyToClipboard).mockReset().mockResolvedValue(true)
  vi.mocked(copyRichToClipboard).mockReset().mockResolvedValue(true)
})

const openCopyMenu = () => fireEvent.pointerDown(
  screen.getByTestId('copy-format-trigger'), { button: 0, ctrlKey: false, pointerType: 'mouse' },
)

describe('AssistantMessage copy format menu', () => {
  it('keeps a click on Copy copying the Markdown source', async () => {
    render(<AssistantMessage content={REPLY} isStreaming={false} slotRunning={false} />)
    fireEvent.click(screen.getByRole('button', { name: 'Copy' }))
    await act(async () => {})
    expect(copyToClipboard).toHaveBeenCalledWith(REPLY)
    expect(copyRichToClipboard).not.toHaveBeenCalled()
  })

  it('offers rich text and Markdown from a labelled caret beside Copy', () => {
    render(<AssistantMessage content={REPLY} isStreaming={false} slotRunning={false} />)
    const caret = screen.getByRole('button', { name: 'Copy options' })
    expect(caret).toHaveAttribute('aria-haspopup', 'menu')
    openCopyMenu()
    expect(screen.getByRole('menu', { name: 'Copy options' })).toBeInTheDocument()
    expect(screen.getAllByRole('menuitem').map(i => i.textContent)).toEqual(['Copy as rich text', 'Copy as Markdown'])
  })

  it('Copy as rich text writes clean HTML with the Markdown as plain text', async () => {
    render(<AssistantMessage content={REPLY} isStreaming={false} slotRunning={false} />)
    openCopyMenu()
    fireEvent.click(screen.getByRole('menuitem', { name: 'Copy as rich text' }))
    await act(async () => {})
    expect(copyRichToClipboard).toHaveBeenCalledTimes(1)
    const [html, plain] = vi.mocked(copyRichToClipboard).mock.calls[0]
    expect(plain).toBe(REPLY)
    expect(html).toContain('<h1>Plan</h1>')
    expect(html).toContain('<strong>one</strong>')
    expect(html).toContain('<a href="https://example.com">docs</a>')
    expect(html).not.toMatch(/class=|style=/)
    expect(copyToClipboard).not.toHaveBeenCalled()
  })

  it('Copy as Markdown copies the source, as the button does', async () => {
    render(<AssistantMessage content={REPLY} isStreaming={false} slotRunning={false} />)
    openCopyMenu()
    fireEvent.click(screen.getByRole('menuitem', { name: 'Copy as Markdown' }))
    await act(async () => {})
    expect(copyToClipboard).toHaveBeenCalledWith(REPLY)
    expect(copyRichToClipboard).not.toHaveBeenCalled()
  })

  it('is keyboard operable and returns focus to the caret on close', async () => {
    render(<AssistantMessage content={REPLY} isStreaming={false} slotRunning={false} />)
    const caret = screen.getByTestId('copy-format-trigger')
    caret.focus()
    fireEvent.keyDown(caret, { key: 'Enter' })
    const menu = await screen.findByRole('menu', { name: 'Copy options' })
    fireEvent.keyDown(menu, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('menu')).not.toBeInTheDocument())
    expect(document.activeElement).toBe(caret)
  })

  it('a refused rich write raises the copy-failed notice', async () => {
    vi.mocked(copyRichToClipboard).mockResolvedValueOnce(false)
    render(<AssistantMessage content={REPLY} isStreaming={false} slotRunning={false} />)
    openCopyMenu()
    fireEvent.click(screen.getByRole('menuitem', { name: 'Copy as rich text' }))
    await act(async () => {})
    expect(screen.getByRole('alert')).toBeInTheDocument()
  })

  it('lists Copy as rich text beside Copy text when Copy lives in More', async () => {
    render(<AssistantMessage content="Done." isStreaming={false} slotRunning={false} onSpeak={vi.fn()} />)
    expect(screen.queryByTestId('copy-format-trigger')).not.toBeInTheDocument()
    fireEvent.pointerDown(screen.getByTitle('More actions'), { button: 0, ctrlKey: false, pointerType: 'mouse' })
    const labels = screen.getAllByRole('menuitem').map(i => i.textContent)
    expect(labels.slice(0, 2)).toEqual(['Copy text', 'Copy as rich text'])
    fireEvent.click(screen.getByTestId('copy-rich-menu-item'))
    await act(async () => {})
    expect(copyRichToClipboard).toHaveBeenCalledWith('<p>Done.</p>', 'Done.')
  })
})
