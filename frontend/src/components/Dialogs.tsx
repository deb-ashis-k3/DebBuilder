import { useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { api } from '../api'
import { Icon, toast, useDialog } from '../lib'
import type { Folder, Workflow } from '../types'

// ---------------------------------------------------------------------------
// Folder helpers
// ---------------------------------------------------------------------------

/** Folders from the top level down to `id` (inclusive). */
export function folderPath(folders: Folder[], id: string | null | undefined): Folder[] {
  const byId = new Map(folders.map((f) => [f.id, f]))
  const out: Folder[] = []
  let cur = id ? byId.get(id) : undefined
  while (cur && out.length < 50) {
    out.unshift(cur)
    cur = cur.parent_id ? byId.get(cur.parent_id) : undefined
  }
  return out
}

/** The folder and all its descendants' ids. */
export function folderSubtree(folders: Folder[], id: string): Set<string> {
  const out = new Set([id])
  let grew = true
  while (grew) {
    grew = false
    for (const f of folders) if (f.parent_id && out.has(f.parent_id) && !out.has(f.id)) { out.add(f.id); grew = true }
  }
  return out
}

// ---------------------------------------------------------------------------
// Action menu (⋯)
// ---------------------------------------------------------------------------

export interface MenuItem { label: string; icon?: string; onClick?: () => unknown; danger?: boolean; divider?: boolean; hint?: string }

export function Menu({ items, trigger, title = 'Actions', className = 'btn ghost sm icon' }: {
  items: MenuItem[]; trigger?: ReactNode; title?: string; className?: string
}) {
  const [open, setOpen] = useState(false)
  const [pos, setPos] = useState<{ top: number; left: number; up: boolean }>()
  const btn = useRef<HTMLButtonElement>(null)
  const menu = useRef<HTMLDivElement>(null)

  useLayoutEffect(() => {
    if (!open) return
    const r = btn.current!.getBoundingClientRect()
    const up = window.innerHeight - r.bottom < 260 && r.top > 260
    setPos({ top: up ? r.top - 4 : r.bottom + 4, left: Math.max(8, Math.min(r.right - 210, window.innerWidth - 218)), up })
  }, [open])
  useEffect(() => {
    if (!open) return
    const away = (e: MouseEvent) => { if (!menu.current?.contains(e.target as Node) && !btn.current?.contains(e.target as Node)) setOpen(false) }
    // Esc closes the menu, not the dialog / panel it sits in.
    const esc = (e: KeyboardEvent) => { if (e.key === 'Escape') { e.preventDefault(); setOpen(false); btn.current?.focus() } }
    const close = () => setOpen(false)
    document.addEventListener('mousedown', away)
    document.addEventListener('keydown', esc)
    window.addEventListener('scroll', close, true)
    window.addEventListener('resize', close)
    return () => {
      document.removeEventListener('mousedown', away); document.removeEventListener('keydown', esc)
      window.removeEventListener('scroll', close, true); window.removeEventListener('resize', close)
    }
  }, [open])

  return (
    <>
      <button ref={btn} className={className} title={title} aria-haspopup="menu"
        onClick={(e) => { e.stopPropagation(); setOpen(!open) }}>
        {trigger ?? <Icon name="ellipsis" size={16} />}
      </button>
      {open && pos && createPortal(
        <div ref={menu} className="act-menu" role="menu" onClick={(e) => e.stopPropagation()}
          style={{ left: pos.left, ...(pos.up ? { bottom: window.innerHeight - pos.top } : { top: pos.top }) }}>
          {items.map((it, i) => it.divider ? <div key={i} className="act-sep" /> : (
            <button key={i} role="menuitem" className={`act-item ${it.danger ? 'danger' : ''}`}
              onClick={() => { setOpen(false); Promise.resolve().then(() => it.onClick?.()).catch((e) => toast(errText(e), 'error')) }}>
              {it.icon && <Icon name={it.icon} size={15} />}<span>{it.label}</span>{it.hint && <span className="act-hint">{it.hint}</span>}
            </button>
          ))}
        </div>,
        document.body,
      )}
    </>
  )
}

// ---------------------------------------------------------------------------
// Dialog shell
// ---------------------------------------------------------------------------

export function Dialog({ title, children, footer, onClose, wide }: { title: string; children: ReactNode; footer: ReactNode; onClose: () => void; wide?: boolean }) {
  const ref = useDialog(onClose)
  return createPortal(
    <div className="overlay" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div ref={ref} className="modal" style={wide ? { width: 'min(640px, 100%)' } : undefined} role="dialog" aria-modal="true" aria-label={title}>
        <div className="modal-head"><h2>{title}</h2><button className="btn ghost sm icon" onClick={onClose} title="Close" aria-label="Close"><Icon name="x" size={16} /></button></div>
        <div className="modal-body">{children}</div>
        <div className="modal-foot">{footer}</div>
      </div>
    </div>,
    document.body,
  )
}

export function PromptDialog({ title, label, initial = '', confirm = 'Save', onSubmit, onClose }: {
  title: string; label: string; initial?: string; confirm?: string; onSubmit: (v: string) => Promise<unknown> | void; onClose: () => void
}) {
  const [v, setV] = useState(initial)
  const [busy, setBusy] = useState(false)
  const submit = async () => {
    if (!v.trim()) return
    setBusy(true)
    try { await onSubmit(v.trim()); onClose() } catch (e) { toast(String(e), 'error') } finally { setBusy(false) }
  }
  return (
    <Dialog title={title} onClose={onClose} footer={<>
      <button className="btn" onClick={onClose}>Cancel</button>
      <button className="btn primary" disabled={!v.trim() || busy} onClick={submit}>{confirm}</button>
    </>}>
      <div className="field">
        <label>{label}</label>
        <input className="input" autoFocus value={v} onChange={(e) => setV(e.target.value)} onKeyDown={(e) => e.key === 'Enter' && submit()}
          onFocus={(e) => e.target.select()} />
      </div>
    </Dialog>
  )
}

/** Pick a destination folder ("Home" = top level). `exclude` hides folders you can't move into. */
export function MoveDialog({ title, folders, current, exclude, onPick, onClose }: {
  title: string; folders: Folder[]; current?: string | null; exclude?: Set<string>
  onPick: (folderId: string | null) => Promise<unknown> | void; onClose: () => void
}) {
  const [sel, setSel] = useState<string | null>(current ?? null)
  const [q, setQ] = useState('')
  const rows = useMemo(() => {
    const kids = new Map<string | null, Folder[]>()
    for (const f of folders) {
      if (exclude?.has(f.id)) continue
      kids.set(f.parent_id, [...(kids.get(f.parent_id) ?? []), f])
    }
    const out: { f: Folder; depth: number }[] = []
    const walk = (pid: string | null, depth: number) => {
      for (const f of (kids.get(pid) ?? []).sort((a, b) => a.name.localeCompare(b.name))) { out.push({ f, depth }); walk(f.id, depth + 1) }
    }
    walk(null, 0)
    return q ? out.filter((r) => r.f.name.toLowerCase().includes(q.toLowerCase())).map((r) => ({ ...r, depth: 0 })) : out
  }, [folders, exclude, q])
  return (
    <Dialog title={title} onClose={onClose} footer={<>
      <button className="btn" onClick={onClose}>Cancel</button>
      <button className="btn primary" disabled={sel === (current ?? null)} onClick={async () => {
        try { await onPick(sel); onClose() } catch (e) { toast(String(e), 'error') }
      }}>Move here</button>
    </>}>
      {folders.length > 6 && (
        <div className="search" style={{ marginBottom: 10 }}><Icon name="search" size={14} />
          <input className="input" placeholder="Search folders" value={q} onChange={(e) => setQ(e.target.value)} /></div>
      )}
      <div className="folder-pick">
        {!q && (
          <button className={`folder-pick-row ${sel === null ? 'on' : ''}`} onClick={() => setSel(null)}>
            <Icon name="house" size={15} /> Home (top level)
          </button>
        )}
        {rows.map(({ f, depth }) => (
          <button key={f.id} className={`folder-pick-row ${sel === f.id ? 'on' : ''}`} style={{ paddingLeft: 12 + (q ? 0 : (depth + 1) * 18) }} onClick={() => setSel(f.id)}>
            <Icon name="folder" size={15} /> {f.name}{f.id === current && <span className="muted small"> (current)</span>}
          </button>
        ))}
        {rows.length === 0 && q && <div className="muted small" style={{ padding: 10 }}>No folders match.</div>}
      </div>
    </Dialog>
  )
}

export function DeleteFolderDialog({ folder, folders, workflows, onDone, onClose }: {
  folder: Folder; folders: Folder[]; workflows: Workflow[]; onDone: () => void; onClose: () => void
}) {
  const sub = folderSubtree(folders, folder.id)
  const wfCount = workflows.filter((w) => w.folder_id && sub.has(w.folder_id)).length
  const subCount = sub.size - 1
  const empty = wfCount === 0 && subCount === 0
  const [mode, setMode] = useState<'move' | 'delete'>('move')
  const [confirmText, setConfirmText] = useState('')
  const parent = folders.find((f) => f.id === folder.parent_id)
  const ok = mode === 'move' || empty || confirmText === 'delete'
  return (
    <Dialog title={`Delete "${folder.name}"`} onClose={onClose} footer={<>
      <button className="btn" onClick={onClose}>Cancel</button>
      <button className="btn danger" disabled={!ok} onClick={async () => {
        try { await api.deleteFolder(folder.id, empty ? 'move' : mode); toast('Folder deleted'); onDone(); onClose() } catch (e) { toast(String(e), 'error') }
      }}>Delete folder</button>
    </>}>
      {empty ? <p>This folder is empty.</p> : (
        <>
          <p>It contains <b>{wfCount} workflow{wfCount === 1 ? '' : 's'}</b>{subCount ? <> and <b>{subCount} subfolder{subCount === 1 ? '' : 's'}</b></> : null}. What should happen to them?</p>
          <label className="radio-row"><input type="radio" checked={mode === 'move'} onChange={() => setMode('move')} />
            Move them to <b>{parent ? parent.name : 'Home'}</b></label>
          <label className="radio-row"><input type="radio" checked={mode === 'delete'} onChange={() => setMode('delete')} />
            Delete everything inside, including workflows and their executions</label>
          {mode === 'delete' && (
            <div className="field" style={{ marginTop: 10 }}>
              <label>Type <code>delete</code> to confirm</label>
              <input className="input" value={confirmText} onChange={(e) => setConfirmText(e.target.value)} autoFocus />
            </div>
          )}
        </>
      )}
    </Dialog>
  )
}

/** Import one or many workflow JSON files, a URL, or pasted JSON into a folder. */
export function ImportDialog({ folderId, onImported, onClose }: {
  folderId: string | null; onImported: (wfs: Workflow[]) => void; onClose: () => void
}) {
  const [tab, setTab] = useState<'file' | 'url' | 'paste'>('file')
  const [files, setFiles] = useState<File[]>([])
  const [url, setUrl] = useState('')
  const [text, setText] = useState('')
  const [busy, setBusy] = useState(false)
  const [drag, setDrag] = useState(false)
  const input = useRef<HTMLInputElement>(null)

  const go = async () => {
    setBusy(true)
    try {
      let created: Workflow[] = []
      if (tab === 'file') {
        // All or nothing: read and check every file first, then one request (the server validates all before creating any).
        const all: unknown[] = []
        for (const f of files) {
          let data: unknown
          try { data = JSON.parse(await f.text()) } catch { throw new Error(`${f.name} is not valid JSON. Nothing was imported.`) }
          const list = (data && typeof data === 'object' && Array.isArray((data as { workflows?: unknown }).workflows))
            ? (data as { workflows: unknown[] }).workflows : Array.isArray(data) ? data : [data]
          const bad = list.find((w) => !w || typeof w !== 'object' || !Array.isArray((w as { nodes?: unknown }).nodes))
          if (!list.length || bad !== undefined) throw new Error(`${f.name} is not a DebBuilder workflow file (expected "nodes" and "edges"). Nothing was imported.`)
          all.push(...list)
        }
        created = await api.importWorkflows({ data: all, folder_id: folderId })
      } else if (tab === 'url') {
        created = await api.importWorkflows({ url, folder_id: folderId })
      } else {
        created = await api.importWorkflows({ data: JSON.parse(text), folder_id: folderId })
      }
      toast(`Imported ${created.length} workflow${created.length === 1 ? '' : 's'}`)
      onImported(created)
      onClose()
    } catch (e) {
      toast(`Import failed: ${e instanceof SyntaxError ? 'invalid JSON' : String(e).replace(/^Error: /, '')}`, 'error')
    } finally { setBusy(false) }
  }
  const ready = tab === 'file' ? files.length > 0 : tab === 'url' ? /^https?:\/\//.test(url) : text.trim().length > 0

  return (
    <Dialog title="Import workflows" wide onClose={onClose} footer={<>
      <button className="btn" onClick={onClose}>Cancel</button>
      <button className="btn primary" disabled={!ready || busy} onClick={go}><Icon name="download" size={15} /> Import</button>
    </>}>
      <div className="tabs" style={{ marginBottom: 14, alignSelf: 'flex-start', display: 'inline-flex' }}>
        {([['file', 'From file'], ['url', 'From URL'], ['paste', 'Paste JSON']] as const).map(([k, l]) => (
          <button key={k} className={`tab ${tab === k ? 'active' : ''}`} onClick={() => setTab(k)}>{l}</button>
        ))}
      </div>
      {tab === 'file' && (
        <div className={`dropzone ${drag ? 'over' : ''}`} onClick={() => input.current?.click()}
          onDragOver={(e) => { e.preventDefault(); setDrag(true) }} onDragLeave={() => setDrag(false)}
          onDrop={(e) => { e.preventDefault(); setDrag(false); setFiles([...e.dataTransfer.files].filter((f) => f.name.endsWith('.json'))) }}>
          <Icon name="file-braces" size={28} />
          {files.length ? <b>{files.map((f) => f.name).join(', ')}</b> : <b>Drop .json files here or click to choose</b>}
          <span className="muted small">Single workflow exports and bulk exports (several workflows in one file) both work.</span>
          <input ref={input} type="file" accept=".json,application/json" multiple hidden onChange={(e) => setFiles([...(e.target.files ?? [])])} />
        </div>
      )}
      {tab === 'url' && (
        <div className="field"><label>URL of a workflow JSON file</label>
          <input className="input" autoFocus placeholder="https://raw.githubusercontent.com/…/workflow.json" value={url} onChange={(e) => setUrl(e.target.value)} /></div>
      )}
      {tab === 'paste' && (
        <div className="field"><label>Workflow JSON</label>
          <textarea className="textarea code" rows={10} autoFocus placeholder='{"name": "…", "nodes": [...], "edges": [...]}' value={text} onChange={(e) => setText(e.target.value)} /></div>
      )}
    </Dialog>
  )
}

// ---------------------------------------------------------------------------
// In-app confirm (replaces the browser's confirm() popup)
// ---------------------------------------------------------------------------

export const errText = (e: unknown) => String(e instanceof Error ? e.message : e).replace(/^Error: /, '')

interface ConfirmOpts { title: string; message?: ReactNode; confirmLabel?: string; danger?: boolean }
let showConfirm: ((o: ConfirmOpts & { resolve: (ok: boolean) => void }) => void) | null = null

/** `if (await confirmDialog({...})) …` — resolves true on confirm, false on cancel/Escape. */
export function confirmDialog(o: ConfirmOpts): Promise<boolean> {
  return new Promise((resolve) => (showConfirm ? showConfirm({ ...o, resolve }) : resolve(window.confirm(o.title))))
}

/** Mounted once at the app root. */
export function ConfirmHost() {
  const [req, setReq] = useState<(ConfirmOpts & { resolve: (ok: boolean) => void }) | null>(null)
  useEffect(() => {
    showConfirm = setReq
    return () => { showConfirm = null }
  }, [])
  if (!req) return null
  const done = (ok: boolean) => { req.resolve(ok); setReq(null) }
  return (
    <Dialog title={req.title} onClose={() => done(false)} footer={<>
      <button className="btn" onClick={() => done(false)}>Cancel</button>
      <button className={`btn ${req.danger ? 'danger solid' : 'primary'}`} autoFocus onClick={() => done(true)}>{req.confirmLabel ?? 'Confirm'}</button>
    </>}>
      {req.message && <div className="confirm-msg">{req.message}</div>}
    </Dialog>
  )
}
