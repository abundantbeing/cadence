import {
  Button,
  host,
  PALETTE_AREA,
  ROUTES_AREA,
  SIDEBAR_NAV_AREA,
  Skeleton,
  STATUSBAR_AREAS,
  useQuery
} from '@hermes/plugin-sdk'
import { useEffect, useRef, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'cadence'
const ROUTE = '/cadence'

function compact(value) {
  const n = Number(value) || 0
  if (n >= 1e9) return `${(n / 1e9).toFixed(1).replace(/\.0$/, '')}B`
  if (n >= 1e6) return `${(n / 1e6).toFixed(1).replace(/\.0$/, '')}M`
  if (n >= 1e3) return `${(n / 1e3).toFixed(1).replace(/\.0$/, '')}K`
  return String(Math.round(n))
}

function parseCli(output) {
  const lines = String(output || '').split('\n').map(line => line.trim()).filter(Boolean)
  for (let i = lines.length - 1; i >= 0; i -= 1) {
    if (lines[i].startsWith('{')) return JSON.parse(lines[i])
  }
  throw new Error(lines.slice(-2).join(' ') || 'Activity returned no data')
}

async function activity(argv) {
  const args = ['cadence', ...argv, '--json']
  let response
  if (typeof host.requestProfile === 'function' && typeof host.profileRoutes === 'function') {
    const routes = await host.profileRoutes()
    const route = routes.find(item => item.primary) || routes.find(item => item.mode === 'local') || routes[0]
    if (route) {
      response = await host.requestProfile(
        route,
        'cli.exec',
        { argv: ['--profile', route.targetProfile, ...args], timeout: 90 },
        95000
      )
    }
  }
  response ||= await host.request('cli.exec', { argv: args, timeout: 90 })
  if (response.blocked) throw new Error(response.hint || 'The activity command was blocked')
  if (response.code) throw new Error(response.output || `Activity exited ${response.code}`)
  const payload = parseCli(response.output)
  if (payload.error) throw new Error(payload.error)
  return payload
}

function Stat({ label, value, hint, rule }) {
  return jsxs('div', {
    className: 'min-w-0',
    style: { padding: '14px 16px', boxShadow: rule ? 'inset 1px 0 0 var(--ui-stroke-tertiary)' : undefined },
    children: [
      jsx('div', { className: 'truncate text-[0.68rem] text-(--ui-text-tertiary)', children: label }),
      jsx('div', { className: 'mt-1 truncate text-[1.2rem] font-medium leading-none text-foreground', children: value }),
      jsx('div', { className: 'mt-1.5 h-4 truncate text-[0.68rem] text-(--ui-text-quaternary)', children: hint || '' })
    ]
  })
}

function level(value, max) {
  if (!value || !max) return 0
  const ratio = value / max
  if (ratio > 0.75) return 4
  if (ratio > 0.45) return 3
  if (ratio > 0.2) return 2
  return 1
}

function cellColor(step) {
  if (!step) return 'color-mix(in srgb, var(--ui-text-quaternary) 28%, transparent)'
  const mix = [0, 34, 55, 76, 100][step]
  return `color-mix(in srgb, var(--ui-accent) ${mix}%, var(--ui-bg-tertiary))`
}

function metricValue(cell, mode, running) {
  if (mode === 'weekly') return cell.weekTokens
  if (mode === 'cumulative') return running
  return cell.tokens
}

function Heatmap({ cells, mode, rangeStart, selected, onSelect }) {
  const hostRef = useRef(null)
  const [width, setWidth] = useState(0)
  useEffect(() => {
    const node = hostRef.current
    if (!node) return undefined
    const measure = () => setWidth(node.clientWidth)
    measure()
    const observer = new ResizeObserver(measure)
    observer.observe(node)
    return () => observer.disconnect()
  }, [])
  const weeks = []
  for (let i = 0; i < cells.length; i += 7) weeks.push(cells.slice(i, i + 7))
  const valued = cells.map(cell => ({ ...cell, weekTokens: 0 }))
  weeks.forEach((week, index) => {
    const total = week.reduce((sum, cell) => sum + (cell.future ? 0 : cell.tokens), 0)
    week.forEach((cell, dayIndex) => {
      valued[index * 7 + dayIndex].weekTokens = total
    })
  })
  let running = 0
  const points = valued.map(cell => {
    if (!cell.future) running += cell.tokens
    return { ...cell, cumulative: running }
  })
  const max = Math.max(1, ...points.map(cell => metricValue(cell, mode, cell.cumulative)))
  const months = []
  const monthNames = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
  points.forEach((cell, index) => {
    if (index % 7 !== 0) return
    const label = monthNames[Number(cell.day.slice(5, 7)) - 1] || ''
    const column = index / 7
    const previous = months.at(-1)
    if (previous && (previous.label === label || column - previous.column < 3)) return
    months.push({ label, column })
  })
  const gap = 3
  const labelWidth = 78
  const gutter = 12
  const count = Math.max(1, weeks.length)
  const inner = Math.max(0, width - labelWidth - gutter)
  const fitted = inner ? Math.floor((inner - gap * (count - 1)) / count) : 12
  const cell = Math.max(11, Math.min(14, fitted || 12))
  const graphWidth = labelWidth + gutter + count * cell + Math.max(0, count - 1) * gap
  const track = `${cell}px`
  return jsx('div', {
    ref: hostRef,
    className: 'w-full overflow-x-auto',
    children: jsxs('div', {
      className: 'mx-auto',
      style: { width: graphWidth },
      children: [
        jsx('div', {
          className: 'relative text-[0.68rem] leading-none text-(--ui-text-tertiary)',
          style: { height: 16, marginLeft: labelWidth + gutter, marginBottom: 6 },
          children: months.map(month => jsx('span', {
            style: { position: 'absolute', left: month.column * (cell + gap), whiteSpace: 'nowrap' },
            children: month.label
          }, `${month.label}-${month.column}`))
        }),
        jsxs('div', {
          className: 'flex items-start',
          style: { gap: gutter },
          children: [
            jsx('div', {
              className: 'shrink-0 text-right text-[0.62rem] leading-none text-(--ui-text-tertiary)',
              style: { width: labelWidth, display: 'grid', gridTemplateRows: `repeat(7, ${track})`, rowGap: gap },
              children: ['Monday', '', 'Wednesday', '', 'Friday', '', ''].map((label, index) => jsx('span', {
                style: { height: cell, lineHeight: `${cell}px`, fontSize: 11, whiteSpace: 'nowrap' },
                children: label
              }, index))
            }),
            jsx('div', {
              style: {
                display: 'grid',
                gridTemplateRows: `repeat(7, ${track})`,
                gridAutoColumns: track,
                gridAutoFlow: 'column',
                gap
              },
              children: points.map(cellPoint => {
                const step = cellPoint.future ? 0 : level(metricValue(cellPoint, mode, cellPoint.cumulative), max)
                const chosen = selected === cellPoint.day
                return jsx('button', {
                  type: 'button',
                  title: cellPoint.future ? cellPoint.day : `${cellPoint.day}: ${compact(cellPoint.tokens)} estimated tokens, ${cellPoint.sessions} sessions`,
                  'aria-label': cellPoint.day,
                  style: {
                    width: cell,
                    height: cell,
                    padding: 0,
                    border: 0,
                    borderRadius: 2,
                    display: 'block',
                    boxSizing: 'border-box',
                    background: cellPoint.future ? 'transparent' : cellColor(step),
                    opacity: !cellPoint.future && rangeStart && cellPoint.day < rangeStart ? 0.28 : 1,
                    boxShadow: chosen ? 'inset 0 0 0 1px var(--ui-text-primary, var(--foreground))' : 'none'
                  },
                  onClick: () => !cellPoint.future && onSelect(cellPoint.day)
                }, cellPoint.day)
              })
            })
          ]
        }),
        jsxs('div', {
          className: 'mt-2 flex items-center justify-end gap-1 text-[0.62rem] text-(--ui-text-tertiary)',
          style: { marginLeft: labelWidth + gutter },
          children: [
            jsx('span', { children: 'Less' }),
            [0, 1, 2, 3, 4].map(step => jsx('span', {
              'aria-hidden': 'true',
              style: {
                width: cell,
                height: cell,
                borderRadius: 2,
                background: cellColor(step),
                display: 'inline-block'
              }
            }, step)),
            jsx('span', { children: 'More' })
          ]
        })
      ]
    })
  })
}

const SLICE_COLORS = [
  'var(--ui-accent)',
  'var(--ui-warm)',
  'var(--ui-cyan)',
  'var(--ui-purple)',
  'var(--ui-green)',
  'var(--ui-orange)',
  'var(--ui-yellow)',
  'var(--ui-blue)'
]
const BREAKDOWN_VIEWS = [['chart', 'Chart'], ['bars', 'Bars'], ['table', 'Table']]

function colorAt(index) {
  return SLICE_COLORS[index % SLICE_COLORS.length]
}

function storedView(key) {
  try {
    const value = localStorage.getItem(key)
    return BREAKDOWN_VIEWS.some(([id]) => id === value) ? value : 'chart'
  } catch {
    return 'chart'
  }
}

function saveView(key, value) {
  try { localStorage.setItem(key, value) } catch { /* the toggle still works for this visit */ }
}

function topRows(rows, limit) {
  if (rows.length <= limit) return rows
  const rest = rows.slice(limit)
  const value = rest.reduce((sum, row) => sum + row.value, 0)
  const total = rows.reduce((sum, row) => sum + row.value, 0) || 1
  return rows.slice(0, limit).concat([{
    key: 'other',
    label: `${rest.length} more`,
    sub: '',
    value,
    display: `${Math.round((value / total) * 100)}%`
  }])
}

function Donut({ rows }) {
  const total = rows.reduce((sum, row) => sum + row.value, 0) || 1
  const radius = 42
  const circumference = 2 * Math.PI * radius
  let traveled = 0
  const slices = rows.filter(row => row.value > 0).map((row, index) => {
    const length = (row.value / total) * circumference
    const slice = { ...row, index, length, traveled }
    traveled += length
    return slice
  })
  const lead = slices[0]
  return jsxs('div', {
    style: { position: 'relative', width: 148, height: 148, flex: '0 0 auto' },
    children: [
      jsx('svg', {
        viewBox: '0 0 120 120',
        width: 148,
        height: 148,
        'aria-hidden': 'true',
        children: slices.map(slice => jsx('circle', {
          cx: 60,
          cy: 60,
          r: radius,
          fill: 'none',
          stroke: colorAt(slice.index),
          strokeWidth: 14,
          strokeDasharray: `${Math.max(slice.length - 1.2, 0)} ${circumference}`,
          strokeDashoffset: -slice.traveled,
          transform: 'rotate(-90 60 60)'
        }, slice.key))
      }),
      lead ? jsxs('div', {
        style: { position: 'absolute', inset: 0, display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', textAlign: 'center', pointerEvents: 'none' },
        children: [
          jsx('div', { className: 'text-[0.95rem] font-medium leading-none text-foreground', children: `${Math.round((lead.value / total) * 100)}%` }),
          jsx('div', { className: 'mt-1 max-w-[78px] truncate text-[0.62rem] text-(--ui-text-tertiary)', children: lead.label })
        ]
      }) : null
    ]
  })
}

function Legend({ rows }) {
  const total = rows.reduce((sum, row) => sum + row.value, 0) || 1
  return jsx('ul', {
    className: 'min-w-0 flex-1',
    children: rows.map((row, index) => jsxs('li', {
      className: 'flex items-center gap-2 py-1 text-[0.72rem]',
      title: `${row.label}${row.sub ? ` · ${row.sub}` : ''} · ${row.display}`,
      children: [
        jsx('span', { 'aria-hidden': 'true', style: { width: 8, height: 8, borderRadius: 2, background: colorAt(index), flex: '0 0 auto' } }),
        jsxs('span', {
          className: 'min-w-0 flex-1 truncate text-foreground',
          children: [row.label, row.sub ? jsx('span', { className: 'text-(--ui-text-tertiary)', children: ` · ${row.sub}` }) : null]
        }),
        jsx('span', { className: 'shrink-0 text-(--ui-text-tertiary)', children: `${Math.round((row.value / total) * 100)}%` })
      ]
    }, row.key))
  })
}

function Bars({ rows }) {
  const max = Math.max(1, ...rows.map(row => row.value))
  return jsx('ul', {
    children: rows.map((row, index) => jsxs('li', {
      style: { padding: '6px 0' },
      children: [
        jsxs('div', {
          className: 'mb-1 flex items-baseline justify-between gap-3 text-[0.72rem]',
          children: [
            jsx('span', { className: 'min-w-0 truncate text-foreground', title: row.sub || row.label, children: row.label }),
            jsx('span', { className: 'shrink-0 text-(--ui-text-tertiary)', children: row.display })
          ]
        }),
        jsx('div', {
          style: { height: 7, borderRadius: 99, background: 'color-mix(in srgb, var(--ui-text-quaternary) 22%, transparent)' },
          children: jsx('div', {
            style: { width: `${Math.max(2, (row.value / max) * 100)}%`, height: '100%', borderRadius: 99, background: colorAt(index) }
          })
        })
      ]
    }, row.key))
  })
}

function Table({ rows, nameLabel }) {
  return jsxs('div', {
    children: [
      jsxs('div', {
        className: 'grid text-[0.62rem] text-(--ui-text-quaternary)',
        style: { gridTemplateColumns: 'minmax(0, 1fr) auto', paddingBottom: 6 },
        children: [jsx('span', { children: nameLabel }), jsx('span', { children: 'Amount' })]
      }),
      jsx('ul', {
        style: { maxHeight: 280, overflow: 'auto' },
        children: rows.map(row => jsxs('li', {
          className: 'grid items-center gap-3 border-t border-(--ui-stroke-tertiary) text-[0.72rem]',
          style: { gridTemplateColumns: 'minmax(0, 1fr) auto', padding: '7px 0' },
          children: [
            jsxs('span', {
              className: 'min-w-0',
              children: [
                jsx('span', { className: 'block truncate text-foreground', children: row.label }),
                row.sub ? jsx('span', { className: 'block truncate text-[0.62rem] text-(--ui-text-tertiary)', children: row.sub }) : null
              ]
            }),
            jsx('span', { className: 'shrink-0 text-(--ui-text-tertiary)', children: row.display })
          ]
        }, row.key))
      })
    ]
  })
}

function Breakdown({ title, rows, empty, view, onView, nameLabel }) {
  const shown = view === 'table' ? rows : topRows(rows, 6)
  let body = jsxs('div', {
    style: { display: 'flex', alignItems: 'center', gap: 16, flexWrap: 'wrap' },
    children: [jsx(Donut, { rows: shown }), jsx(Legend, { rows: shown })]
  })
  if (!rows.length) body = jsx('div', { className: 'text-[0.72rem] text-(--ui-text-tertiary)', children: empty })
  else if (view === 'bars') body = jsx(Bars, { rows: shown })
  else if (view === 'table') body = jsx(Table, { rows, nameLabel })
  return jsxs('section', {
    className: 'rounded-xl border border-(--ui-stroke-tertiary)',
    style: { padding: 16 },
    children: [
      jsxs('div', {
        className: 'mb-3 flex items-center justify-between gap-3',
        children: [
          jsx('h2', { className: 'text-[0.82rem] font-medium text-foreground', children: title }),
          jsx('div', {
            className: 'flex gap-3 text-[0.72rem]',
            children: BREAKDOWN_VIEWS.map(([id, label]) => jsx('button', {
              type: 'button',
              className: id === view ? 'text-foreground' : 'text-(--ui-text-tertiary)',
              onClick: () => onView(id),
              children: label
            }, id))
          })
        ]
      }),
      body
    ]
  })
}

function modelRows(models) {
  return models.map(model => ({
    key: `${model.model}:${model.provider || ''}`,
    label: model.model,
    sub: model.provider || '',
    value: Number(model.tokens) || 0,
    display: `${compact(model.tokens)} · ${model.share}%`
  }))
}

function sourceRows(sources) {
  const total = sources.reduce((sum, source) => sum + (Number(source.sessions) || 0), 0) || 1
  return sources.map(source => ({
    key: source.source,
    label: source.source,
    sub: '',
    value: Number(source.sessions) || 0,
    display: `${source.sessions} sessions · ${Math.round((Number(source.sessions) / total) * 100)}%`
  }))
}

function localDay(daysAgo) {
  const date = new Date()
  date.setDate(date.getDate() - daysAgo)
  const month = String(date.getMonth() + 1).padStart(2, '0')
  const day = String(date.getDate()).padStart(2, '0')
  return `${date.getFullYear()}-${month}-${day}`
}

function ActivityPage() {
  const [range, setRange] = useState('all')
  const [mode, setMode] = useState('daily')
  const [selected, setSelected] = useState(null)
  const [modelView, setModelView] = useState(() => storedView('hermes.activity.models'))
  const [placeView, setPlaceView] = useState(() => storedView('hermes.activity.places'))
  const report = useQuery({
    queryKey: ['cadence', 'summary'],
    queryFn: () => activity(['summary']),
    staleTime: 180000
  })
  const detail = useQuery({
    queryKey: ['activity-board', 'day', selected],
    queryFn: () => activity(['day', selected]),
    enabled: Boolean(selected)
  })
  if (report.isLoading) {
    return jsx('div', { className: 'flex flex-col gap-4 p-6', children: jsx(Skeleton, { className: 'h-40' }) })
  }
  if (report.error) {
    return jsxs('div', {
      className: 'flex max-w-xl flex-col gap-2 p-6 text-[0.8rem]',
      children: [
        jsx('div', { className: 'text-foreground', children: 'Activity could not load' }),
        jsx('div', { className: 'text-(--ui-text-tertiary)', children: report.error.message }),
        jsx(Button, { size: 'xs', variant: 'text', onClick: () => void report.refetch(), children: 'Retry' })
      ]
    })
  }
  const data = report.data
  const streaks = data.streaks
  const totals = data.totals
  const span = data.spans?.[range] || data.totals
  const longest = data.longest_stretch
  const weeks = data.weeks
  const rangeStart = range === 'all' ? null : localDay(Number(range) - 1)
  const weekNote = weeks
    ? `This week ${compact(weeks.this.tokens)} across ${weeks.this.days} days. Last week ${compact(weeks.last.tokens)} across ${weeks.last.days} days.`
    : ''
  const rhythm = span.rhythm
  return jsxs('div', {
    className: 'flex h-full w-full flex-col gap-6 overflow-y-auto px-8 py-6',
    children: [
      jsxs('div', {
        className: 'flex items-end justify-between gap-4',
        children: [
          jsxs('div', {
            children: [
              jsx('h1', { className: 'text-[1.05rem] font-medium text-foreground', children: 'Cadence' }),
              jsx('p', {
                className: 'text-[0.72rem] text-(--ui-text-tertiary)',
                children: 'Days you used Hermes. A continued chat counts on the day it continued.'
              })
            ]
          }),
          jsx('div', {
            className: 'flex items-center gap-3',
            children: [
              jsx('div', {
                className: 'flex gap-3 text-[0.72rem]',
                children: [['30', '30 days'], ['90', '90 days'], ['all', 'All time']].map(([id, label]) => jsx('button', {
                  type: 'button',
                  className: id === range ? 'text-foreground' : 'text-(--ui-text-tertiary)',
                  onClick: () => setRange(id),
                  children: label
                }, id))
              }),
              jsx(Button, { size: 'xs', variant: 'text', onClick: () => void report.refetch(), children: 'Refresh' })
            ]
          })
        ]
      }),
      jsx('div', {
        className: 'overflow-hidden rounded-xl border border-(--ui-stroke-tertiary)',
        style: { display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(180px, 1fr))' },
        children: [
          jsx(Stat, { label: range === 'all' ? 'Lifetime tokens' : `${range} day tokens`, value: compact(span.tokens), hint: 'includes cache' }),
          jsx(Stat, { label: 'Peak day', value: compact(span.peak_tokens), hint: span.peak_day || 'estimated', rule: true }),
          jsx(Stat, { label: 'Longest stretch', value: longest ? longest.label : '—', hint: longest ? longest.day : 'no timed run yet', rule: true }),
          jsx(Stat, { label: 'Longest streak', value: `${streaks.longest}d`, hint: `${streaks.active_days} active days`, rule: true }),
          jsx(Stat, { label: 'Current streak', value: `${streaks.current}d`, hint: streaks.current ? 'through today or yesterday' : 'no recent day', rule: true })
        ]
      }),
      jsxs('section', {
        children: [
          jsxs('div', {
            className: 'mb-3 flex items-center justify-between gap-3',
            children: [
              jsx('h2', { className: 'text-[0.82rem] font-medium text-foreground', children: 'Token activity' }),
              jsx('div', {
                className: 'flex gap-3 text-[0.72rem]',
                children: ['daily', 'weekly', 'cumulative'].map(id => jsx('button', {
                  type: 'button',
                  className: id === mode ? 'text-foreground' : 'text-(--ui-text-tertiary)',
                  onClick: () => setMode(id),
                  children: id[0].toUpperCase() + id.slice(1)
                }, id))
              })
            ]
          }),
          jsx(Heatmap, { cells: data.cells, mode, rangeStart, selected, onSelect: day => setSelected(day === selected ? null : day) }),
          jsx('div', {
            className: 'mt-3 text-[0.72rem] text-(--ui-text-tertiary)',
            children: [
              weekNote,
              rhythm ? ` Busiest on ${rhythm.weekday} around ${rhythm.hour}.` : ''
            ].join('')
          }),
          jsx('div', {
            className: 'mt-2 text-[0.62rem] text-(--ui-text-tertiary)',
            children: 'Estimated across the days a chat was active. Not a provider invoice. Click a day for its chats.'
          })
        ]
      }),
      selected ? jsxs('section', {
        children: [
          jsx('h2', { className: 'mb-2 text-[0.82rem] font-medium text-foreground', children: selected }),
          detail.isLoading ? jsx(Skeleton, { className: 'h-16' }) : jsx('ul', {
            className: 'flex flex-col gap-1',
            children: (detail.data?.sessions || []).slice(0, 12).map(session => jsxs('li', {
              className: 'flex items-center justify-between gap-3 text-[0.72rem]',
              children: [
                jsx('span', { className: 'min-w-0 truncate text-foreground', children: session.title }),
                jsx('span', { className: 'shrink-0 text-(--ui-text-tertiary)', children: `${session.source} · ${compact(session.tokens)}` })
              ]
            }, session.id))
          })
        ]
      }) : null,
      jsx('div', {
        style: { display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(340px, 1fr))', gap: 16 },
        children: [
          jsx(Breakdown, {
            title: 'Models',
            rows: modelRows(span.models || data.models),
            empty: 'No model usage yet.',
            view: modelView,
            onView: next => { setModelView(next); saveView('hermes.activity.models', next) },
            nameLabel: 'Model'
          }),
          jsx(Breakdown, {
            title: 'Where you used Hermes',
            rows: sourceRows(span.sources || data.sources),
            empty: 'No sessions yet.',
            view: placeView,
            onView: next => { setPlaceView(next); saveView('hermes.activity.places', next) },
            nameLabel: 'Place'
          })
        ]
      })
    ]
  })
}

function StatusChip() {
  const report = useQuery({
    queryKey: ['cadence', 'summary'],
    queryFn: () => activity(['summary']),
    staleTime: 180000
  })
  const current = report.data?.streaks?.current
  return jsx('button', {
    type: 'button',
    className: 'inline-flex h-full items-center px-1.5 text-[0.6875rem] text-(--ui-text-tertiary) hover:text-foreground',
    onClick: () => host.navigate(ROUTE),
    children: current == null ? 'Cadence' : `${current} day streak`
  })
}

export default {
  id: ID,
  name: 'Cadence',
  register(ctx) {
    ctx.register({ id: 'page', area: ROUTES_AREA, data: { path: ROUTE }, render: () => jsx(ActivityPage, {}) })
    ctx.register({
      id: 'nav',
      area: SIDEBAR_NAV_AREA,
      order: 84,
      data: { path: ROUTE, label: 'Cadence', codicon: 'graph' }
    })
    ctx.register({
      id: 'open',
      area: PALETTE_AREA,
      data: {
        id: 'cadence.open',
        label: 'Open Cadence',
        keywords: ['streak', 'heatmap', 'usage', 'models'],
        run: () => host.navigate(ROUTE)
      }
    })
    ctx.register({ id: 'statusbar', area: STATUSBAR_AREAS.right, order: 120, render: () => jsx(StatusChip, {}) })
  }
}
