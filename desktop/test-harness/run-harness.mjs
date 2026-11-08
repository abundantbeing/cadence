/**
 * End-to-end checks for desktop/plugin.js scope behaviour (jsdom + React).
 * Run from repo: cd desktop/test-harness && npm install && npm test
 */
import { mkdir, readFile, writeFile, symlink, rm } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
import { pathToFileURL } from 'node:url'

import { JSDOM } from 'jsdom'
import { createElement as h } from 'react'

const dom = new JSDOM('<!doctype html><html><body></body></html>', {
  url: 'http://localhost/',
  pretendToBeVisual: true
})
globalThis.window = dom.window
globalThis.document = dom.window.document
globalThis.localStorage = dom.window.localStorage
globalThis.CustomEvent = dom.window.CustomEvent
globalThis.ResizeObserver =
  dom.window.ResizeObserver ||
  class {
    observe() {}
    unobserve() {}
    disconnect() {}
  }

const { render, screen, waitFor, fireEvent } = await import('@testing-library/react')

const __dirname = dirname(fileURLToPath(import.meta.url))
const pluginPath = join(__dirname, '..', 'plugin.js')
const desktopNm = join(__dirname, '..', 'node_modules')
await mkdir(join(desktopNm, '@hermes'), { recursive: true })
async function linkForce(target, linkPath) {
  try {
    await rm(linkPath, { force: true })
  } catch {
    /* missing */
  }
  await symlink(target, linkPath)
}
await linkForce(join(__dirname, 'stubs', 'plugin-sdk'), join(desktopNm, '@hermes', 'plugin-sdk'))
await linkForce(join(__dirname, 'node_modules', 'react'), join(desktopNm, 'react'))
await linkForce(join(__dirname, 'node_modules', 'react-dom'), join(desktopNm, 'react-dom'))

process.env.CADENCE_TEST_HARNESS = '1'

function parseScope(argv) {
  const i = argv.indexOf('--scope')
  return i >= 0 && argv[i + 1] ? argv[i + 1] : null
}

function minimalSummary(scope) {
  const base = {
    totals: { tokens: 1, sessions: 0, peak_tokens: 0, peak_day: null },
    cells: [],
    longest_stretch: null,
    weeks: null,
    models: [],
    sources: [],
    profiles: [],
    spans: {
      all: { tokens: 1, models: [], sources: [], rhythm: null, profiles: [] },
      30: { tokens: 1, models: [], sources: [], rhythm: null, profiles: [] },
      90: { tokens: 1, models: [], sources: [], rhythm: null, profiles: [] }
    },
    included_profiles: scope === 'all' ? ['a', 'b'] : []
  }
  if (scope === 'all') {
    return {
      scope: 'all',
      streaks: { current: 9, longest: 9, active_days: 2 },
      ...base
    }
  }
  return {
    scope: 'profile',
    streaks: { current: 2, longest: 2, active_days: 2 },
    ...base
  }
}

let activityCalls = []
let failAllUntilProfilePin = false

globalThis.__cadenceActivityImpl = async argv => {
  activityCalls.push([...argv])
  if (argv[0] !== 'summary') {
    return { sessions: [] }
  }
  const s = parseScope(argv)
  if (failAllUntilProfilePin && s !== 'profile') {
    throw new Error('no profile session stores found')
  }
  return minimalSummary(s === 'all' ? 'all' : 'profile')
}

const sdkUrl = pathToFileURL(join(__dirname, 'stubs', 'plugin-sdk', 'index.js')).href
const { clearQueryCache } = await import(sdkUrl)

const pluginUrl = pathToFileURL(pluginPath).href + '?t=' + Date.now()
const mod = await import(pluginUrl)
const harness = mod.__cadenceHarness
if (!harness) {
  throw new Error('CADENCE_TEST_HARNESS did not expose __cadenceHarness')
}
const { ActivityPage, StatusChip, SCOPE_KEY } = harness

function assert(cond, msg) {
  if (!cond) throw new Error(msg || 'assertion failed')
}

// --- Test 1: null scope + failing all load shows fallback and recovers with --scope profile ---
document.body.innerHTML = '<div id="root"></div>'
localStorage.clear()
failAllUntilProfilePin = true
activityCalls = []
const t1 = render(h(ActivityPage), { container: document.getElementById('root') })
await waitFor(() => {
  screen.getByText('Activity could not load')
})
screen.getByText('Show this profile only')
fireEvent.click(screen.getByText('Show this profile only'))
await waitFor(() => {
  assert(!screen.queryByText('Activity could not load'), 'error view should clear after recovery')
})
screen.getByRole('heading', { name: 'Cadence' })
const profileCalls = activityCalls.filter(args => args[0] === 'summary' && parseScope(args) === 'profile')
assert(profileCalls.length >= 1, 'expected at least one summary with --scope profile after fallback')
t1.unmount()
failAllUntilProfilePin = false

// --- Test 2: chip streak tracks page scope switch ---
document.body.innerHTML = '<div id="p"></div><div id="c"></div>'
localStorage.clear()
activityCalls = []
render(h(ActivityPage), { container: document.getElementById('p') })
render(h(StatusChip), { container: document.getElementById('c') })
await waitFor(() => {
  screen.getByText('2 day streak')
})
const buttons = [...document.getElementById('p').querySelectorAll('button')]
const allProfilesBtn = buttons.find(b => b.textContent === 'All profiles')
assert(allProfilesBtn, 'All profiles control missing')
fireEvent.click(allProfilesBtn)
await waitFor(() => {
  screen.getByText('9 day streak')
})
assert(localStorage.getItem(SCOPE_KEY) === 'all', 'scope should persist as all')

// --- Test 3: chip stays mounted across scope bump; late page mount must not refetch ---
document.body.innerHTML = '<div id="c"></div><div id="p"></div>'
localStorage.clear()
activityCalls = []
clearQueryCache()
render(h(StatusChip), { container: document.getElementById('c') })
await waitFor(() => {
  screen.getByText('2 day streak')
})
const pageMount = render(h(ActivityPage), { container: document.getElementById('p') })
await waitFor(() => {
  screen.getByRole('heading', { name: 'Cadence' })
})
const beforeBump = activityCalls.filter(args => args[0] === 'summary').length
const allBtn = [...document.getElementById('p').querySelectorAll('button')].find(b => b.textContent === 'All profiles')
fireEvent.click(allBtn)
await waitFor(() => {
  screen.getByText('9 day streak')
})
const afterBump = activityCalls.filter(args => args[0] === 'summary').length
assert(afterBump > beforeBump, 'scope switch should have refreshed summary at least once')
pageMount.unmount()
const withChipOnly = activityCalls.filter(args => args[0] === 'summary').length
const latePage = document.createElement('div')
latePage.id = 'p-late'
document.body.appendChild(latePage)
render(h(ActivityPage), { container: latePage })
await waitFor(() => {
  screen.getByRole('heading', { name: 'Cadence' })
})
const afterLatePageMount = activityCalls.filter(args => args[0] === 'summary').length
assert(
  afterLatePageMount === withChipOnly,
  `late page mount should not refetch summary (got ${afterLatePageMount}, was ${withChipOnly})`
)

console.log('cadence desktop harness: ok')
console.log('activity call patterns (sample):', JSON.stringify(activityCalls.slice(0, 12)))
