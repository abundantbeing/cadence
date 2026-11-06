import { useEffect, useState, createElement } from 'react'

export const host = { navigate() {}, request() {} }
export const ROUTES_AREA = 'routes'
export const SIDEBAR_NAV_AREA = 'sidebar'
export const PALETTE_AREA = 'palette'
export const STATUSBAR_AREAS = { right: 'status-right' }

export function Button(props) {
  const { children, ...rest } = props
  return createElement('button', { type: 'button', ...rest }, children)
}

export function Skeleton(props) {
  return createElement('div', { 'data-testid': 'skeleton', ...props })
}

export function useQuery({ queryKey, queryFn, enabled = true }) {
  const [state, setState] = useState({
    isLoading: Boolean(enabled),
    error: null,
    data: undefined
  })
  const key = JSON.stringify(queryKey)
  useEffect(() => {
    if (!enabled) {
      setState({ isLoading: false, error: null, data: undefined })
      return undefined
    }
    let cancelled = false
    setState({ isLoading: true, error: null, data: undefined })
    Promise.resolve(queryFn())
      .then(data => {
        if (!cancelled) setState({ isLoading: false, error: null, data })
      })
      .catch(error => {
        if (!cancelled) setState({ isLoading: false, error, data: undefined })
      })
    return () => {
      cancelled = true
    }
  }, [key, enabled])
  return {
    ...state,
    refetch: async () => {
      try {
        const data = await queryFn()
        setState({ isLoading: false, error: null, data })
        return { data }
      } catch (error) {
        setState({ isLoading: false, error, data: undefined })
        throw error
      }
    }
  }
}
