/**
 * Screenshots for Fresh start in a crewmate DM on the Members page.
 *
 * Drives the REAL MembersPage capture entry (capture/members-page.html) with
 * the shared gateway-free API stub. radar's thread holds an older
 * conversation and reads idle (the `idle` capture frame); the fresh-start
 * route answers a reset time after all of it. Every frame asserts its state
 * before it is written, including a 320px frame where the button must not
 * overlap the Profile pill.
 *
 * Usage (two shells, from website/):
 *   npx vite --host 127.0.0.1 --port 6835 --strictPort
 *   node scripts/capture-members-fresh-start.mjs http://127.0.0.1:6835 <outdir>
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { MEMBERS, routeMembersApi } from './lib/members-fixtures.mjs'

const positional = process.argv.slice(2).filter(a => !a.startsWith('--'))
const BASE = positional[0] || 'http://127.0.0.1:6835'
const OUT = positional[1] || '../temp-screenshots/members-fresh-start'
mkdirSync(OUT, { recursive: true })

const ts = (i) => new Date(Date.UTC(2026, 9, 10, 5, 0, 0) + i * 60_000).toISOString()
const RESET_AT = new Date(Date.UTC(2026, 9, 10, 6, 0, 0)).toISOString()
const MESSAGES = [
  { role: 'user', content: 'Take the scroll bug.', cls: '', ts: ts(0) },
  { role: 'assistant', content: 'Taken. Reading the pane code first.', cls: '', ts: ts(1) },
  { role: 'user', content: 'Any ETA?', cls: '', ts: ts(2) },
  { role: 'assistant', content: 'A first fix within the hour.', cls: '', ts: ts(3) },
]
const OLD_TEXT = 'A first fix within the hour.'
// An idle crewmate: the stop step has nothing to stop in these frames.
const IDLE = MEMBERS.map(m => ({ ...m, running: false }))

const browser = await chromium.launch()
let failed = false
function check(name, ok, detail = '') {
  console.log(`${name}: ${ok ? 'OK' : 'MISMATCH'} ${detail}`)
  if (!ok) failed = true
}

async function openDm(theme, width) {
  const page = await browser.newPage({ viewport: { width, height: 760 }, deviceScaleFactor: 1 })
  await routeMembersApi(page, { messages: MESSAGES, running: false, has_more: false, total: MESSAGES.length }, { members: IDLE })
  await page.route(u => new URL(u).pathname === '/api/teams', route => route.fulfill({ status: 200, contentType: 'application/json', body: '{"teams":[]}' }))
  await page.route(u => new URL(u).pathname === '/api/members/radar/fresh-start', route => route.fulfill({
    status: 200, contentType: 'application/json', body: JSON.stringify({ slot: 'member-radar', reset_at: RESET_AT, outcome: 'cleared' }),
  }))
  await page.goto(`${BASE}/capture/members-page.html?theme=${theme}&route=${encodeURIComponent('/members?member=radar')}`)
  await page.waitForSelector('[data-capture-root]')
  await page.getByText(OLD_TEXT).waitFor()
  await page.evaluate(() => window.dispatchEvent(new CustomEvent('capture:frame', { detail: { kind: 'idle', slot: 'member-radar' } })))
  await page.waitForTimeout(600)
  return page
}

for (const theme of ['dark', 'light']) {
  const page = await openDm(theme, 1280)
  const button = page.getByTestId('member-fresh-start')
  const label = (await button.innerText()).trim()
  check(`01-${theme}: visible "Fresh start…" label`, label === 'Fresh start…', JSON.stringify(label))
  check(`01-${theme}: crewmate idle`, !(await page.getByTestId('member-pill-activity').innerText()).includes('Working'))
  await page.screenshot({ path: `${OUT}/01-button-${theme}.png` })

  await button.click()
  const dialog = page.getByRole('dialog')
  await dialog.waitFor()
  await page.waitForTimeout(400)
  const text = await dialog.innerText()
  check(`02-${theme}: themed confirm names what stays`, text.includes('Give this crewmate a fresh start?') && text.includes('messages stay here'), JSON.stringify(text))
  await page.screenshot({ path: `${OUT}/02-confirm-${theme}.png` })

  await dialog.getByRole('button', { name: 'Start fresh' }).click()
  await page.getByTestId('earlier-conversation').waitFor()
  await page.waitForTimeout(400)
  const toggle = page.getByTestId('earlier-conversation-toggle')
  const closed = await toggle.innerText()
  check(`03-${theme}: folded, says when and how many`, (await page.getByText(OLD_TEXT).count()) === 0
    && closed.includes('Started fresh at') && closed.includes('Show earlier messages (4)'), JSON.stringify(closed))
  await page.mouse.move(5, 5)
  await page.screenshot({ path: `${OUT}/03-folded-${theme}.png` })

  await toggle.click()
  await page.getByText(OLD_TEXT).waitFor()
  await page.waitForTimeout(300)
  const open = await toggle.innerText()
  check(`04-${theme}: opened, says hide`, (await toggle.getAttribute('aria-expanded')) === 'true' && open.includes('Hide earlier messages'), JSON.stringify(open))
  await page.mouse.move(5, 5)
  await page.screenshot({ path: `${OUT}/04-opened-${theme}.png` })
  await page.close()
}

// Narrow: icon only, and it must not overlap the Profile pill.
{
  const page = await openDm('dark', 320)
  const b = await page.getByTestId('member-fresh-start').boundingBox()
  const pill = await page.getByTestId('member-identity-pill').boundingBox()
  const overlap = b && pill && b.x < pill.x + pill.width && pill.x < b.x + b.width
  check('05-narrow: icon only', (await page.getByTestId('member-fresh-start').innerText()).trim() === '')
  check('05-narrow: no overlap with the Profile pill', !overlap, JSON.stringify({ b, pill }))
  await page.screenshot({ path: `${OUT}/05-narrow-dark.png` })
  await page.close()
}
await browser.close()
if (failed) process.exit(1)
console.log('all frames verified')
