import { useEffect, useLayoutEffect, useMemo, useRef, useState, type CSSProperties, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { Icon } from '../lib'

export interface SelectOption { value: string; label: ReactNode; hint?: string }

/** Themed replacement for <select>: native option popups ignore CSS (OS blue highlight). */
export function Select({ value, onChange, options, placeholder = 'Select…', className = '', style, title, searchable }: {
  value: string
  onChange: (v: string) => void
  options: SelectOption[]
  placeholder?: string
  className?: string
  style?: CSSProperties
  title?: string
  searchable?: boolean
}) {
  const [open, setOpen] = useState(false)
  const [q, setQ] = useState('')
  const [hi, setHi] = useState(0)
  const [pos, setPos] = useState<{ left: number; top: number; width: number; up: boolean; max: number }>()
  const btn = useRef<HTMLButtonElement>(null)
  const menu = useRef<HTMLDivElement>(null)
  const canSearch = searchable ?? options.length > 8

  const shown = useMemo(() => {
    if (!q) return options
    const n = q.toLowerCase()
    return options.filter((o) => `${typeof o.label === 'string' ? o.label : ''} ${o.value} ${o.hint ?? ''}`.toLowerCase().includes(n))
  }, [options, q])
  const current = options.find((o) => o.value === value)

  const place = () => {
    const r = btn.current?.getBoundingClientRect()
    if (!r) return
    const below = window.innerHeight - r.bottom - 8
    const up = below < 220 && r.top > below
    setPos({ left: r.left, top: up ? r.top - 4 : r.bottom + 4, width: Math.max(r.width, 180), up, max: Math.min(320, (up ? r.top : below) - 8) })
  }

  useLayoutEffect(() => {
    if (!open) return
    place()
    setHi(Math.max(0, options.findIndex((o) => o.value === value)))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open])

  useEffect(() => {
    if (!open) return
    const away = (e: MouseEvent) => {
      if (!menu.current?.contains(e.target as Node) && !btn.current?.contains(e.target as Node)) close()
    }
    const reflow = (e: Event) => { if (!menu.current?.contains(e.target as Node)) place() }
    document.addEventListener('mousedown', away)
    window.addEventListener('resize', place)
    window.addEventListener('scroll', reflow, true)
    return () => {
      document.removeEventListener('mousedown', away)
      window.removeEventListener('resize', place)
      window.removeEventListener('scroll', reflow, true)
    }
  }, [open])

  useEffect(() => { menu.current?.querySelector('.sel-opt.hi')?.scrollIntoView({ block: 'nearest' }) }, [hi, open])

  const close = () => { setOpen(false); setQ('') }
  const pick = (o?: SelectOption) => { if (o) onChange(o.value); close(); btn.current?.focus() }
  const onKey = (e: React.KeyboardEvent) => {
    if (!open) {
      if (['ArrowDown', 'ArrowUp', 'Enter', ' '].includes(e.key)) { e.preventDefault(); setOpen(true) }
      return
    }
    if (e.key === 'ArrowDown') { e.preventDefault(); setHi((h) => Math.min(shown.length - 1, h + 1)) }
    else if (e.key === 'ArrowUp') { e.preventDefault(); setHi((h) => Math.max(0, h - 1)) }
    else if (e.key === 'Enter') { e.preventDefault(); pick(shown[hi]) }
    else if (e.key === 'Escape' || e.key === 'Tab') { e.stopPropagation(); close() }
  }

  return (
    <>
      <button type="button" ref={btn} className={`select sel-btn ${open ? 'open' : ''} ${className}`} style={style} title={title}
        onClick={() => (open ? close() : setOpen(true))} onKeyDown={onKey} aria-haspopup="listbox" aria-expanded={open}>
        <span className={`sel-val ${current ? '' : 'muted'}`}>{current?.label ?? (value || placeholder)}</span>
        <Icon name="chevron-down" size={15} className="sel-caret" />
      </button>
      {open && pos && createPortal(
        <div ref={menu} className={`sel-menu ${pos.up ? 'up' : ''}`} role="listbox" onKeyDown={onKey}
          style={{ left: Math.min(pos.left, window.innerWidth - pos.width - 8), width: pos.width, ...(pos.up ? { bottom: window.innerHeight - pos.top } : { top: pos.top }) }}>
          {canSearch && (
            <div className="sel-search">
              <Icon name="search" size={13} />
              <input autoFocus placeholder="Search…" value={q} onChange={(e) => { setQ(e.target.value); setHi(0) }} />
            </div>
          )}
          <div className="sel-list" style={{ maxHeight: pos.max - (canSearch ? 40 : 0) }}>
            {shown.length === 0 && <div className="sel-empty">No matches</div>}
            {shown.map((o, i) => (
              <div key={o.value} role="option" aria-selected={o.value === value}
                className={`sel-opt ${i === hi ? 'hi' : ''} ${o.value === value ? 'on' : ''}`}
                onMouseEnter={() => setHi(i)} onMouseDown={(e) => { e.preventDefault(); pick(o) }}>
                <span className="sel-label">{o.label}</span>
                {o.hint && <span className="sel-hint">{o.hint}</span>}
                {o.value === value && <Icon name="check" size={14} className="sel-check" />}
              </div>
            ))}
          </div>
        </div>,
        document.body,
      )}
    </>
  )
}
