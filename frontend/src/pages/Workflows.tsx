import { useEffect, useMemo, useRef, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { api, downloadJson } from '../api'
import { DeleteFolderDialog, ImportDialog, Menu, MoveDialog, PromptDialog, confirmDialog, errText, folderPath, folderSubtree } from '../components/Dialogs'
import { Select } from '../components/Select'
import { Icon, fmtDate, toast, useCatalog } from '../lib'
import type { Folder, Workflow } from '../types'
import { useAuth } from '../auth'
import { prefetchEditor } from '../prefetch'

// Template tags → representative node type (for the card's icon strip).
const TAG_NODE: Record<string, string> = {
  ai: 'ai.agent', chat: 'trigger.chat', whatsapp: 'trigger.whatsapp', voice: 'trigger.voice', livekit: 'voice.call',
  webhook: 'trigger.webhook', http: 'core.http', form: 'trigger.form', 'multi-agent': 'tool.agent', hitl: 'flow.approval',
  schedule: 'trigger.schedule', code: 'core.code',
}

type Sort = 'updated' | 'created' | 'name' | 'name_desc'
const DRAG_WF = 'application/x-debbuilder-workflows'
const DRAG_FOLDER = 'application/x-debbuilder-folder'

type Dialog =
  | { kind: 'new-folder' }
  | { kind: 'rename-folder'; folder: Folder }
  | { kind: 'move-folder'; folder: Folder }
  | { kind: 'delete-folder'; folder: Folder }
  | { kind: 'move-workflows'; ids: string[]; current: string | null }
  | { kind: 'duplicate'; wf: Workflow }
  | { kind: 'rename-workflow'; wf: Workflow }
  | { kind: 'import' }

function lsGet(k: string) { try { return localStorage.getItem(k) } catch { return null } }
function lsSet(k: string, v: string) { try { localStorage.setItem(k, v) } catch { /* private mode */ } }

export default function Workflows() {
  const navigate = useNavigate()
  const { folderId = null } = useParams<{ folderId: string }>()
  const { byType } = useCatalog()
  const canWrite = useAuth().can('workflow:write')
  const [list, setList] = useState<Workflow[] | null>(null)
  const [folders, setFolders] = useState<Folder[]>([])
  const [templates, setTemplates] = useState<{ id: string; name: string; description: string; tags: string[] }[]>([])
  const [q, setQ] = useState('')
  const [status, setStatus] = useState('')
  const [sort, setSort] = useState<Sort>(() => (lsGet('wf-sort') as Sort) || 'updated')
  const [stats, setStats] = useState<Record<string, number>>({})
  const [sel, setSel] = useState<Set<string>>(new Set())
  const [dialog, setDialog] = useState<Dialog | null>(null)
  const [dropTarget, setDropTarget] = useState<string | null>(null)
  const tplRef = useRef<HTMLDivElement>(null)

  const load = () => Promise.all([api.workflows().then(setList), api.folders().then(setFolders).catch(() => setFolders([]))])
  useEffect(() => {
    load()
    api.templates().then(setTemplates)
    api.execStats().then(setStats)
  }, [])
  useEffect(() => { setSel(new Set()) }, [folderId])

  const folder = folders.find((f) => f.id === folderId) ?? null
  const crumbs = folderPath(folders, folderId)
  const searching = q.trim().length > 0
  const canCreateHere = canWrite
  const foldersOf = (ownerId?: string | null) => folders.filter((f) => f.owner_id === ownerId)
  const subfolders = folders.filter((f) => (searching ? f.name.toLowerCase().includes(q.toLowerCase()) : f.parent_id === folderId))
    .sort((a, b) => a.name.localeCompare(b.name))

  const shown = useMemo(() => {
    const needle = q.toLowerCase()
    const rows = (list ?? []).filter((w) => (searching ? w.name.toLowerCase().includes(needle) : (w.folder_id ?? null) === folderId))
      .filter((w) => !status || (status === 'active') === w.active)
    const by: Record<Sort, (a: Workflow, b: Workflow) => number> = {
      updated: (a, b) => b.updated_at.localeCompare(a.updated_at),
      created: (a, b) => b.created_at.localeCompare(a.created_at),
      name: (a, b) => a.name.localeCompare(b.name),
      name_desc: (a, b) => b.name.localeCompare(a.name),
    }
    return rows.sort(by[sort])
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [list, q, searching, folderId, status, sort])

  const total = Object.values(stats).reduce((a, b) => a + b, 0)
  const statCards = [
    { label: 'Workflows', value: list?.length ?? '–', icon: 'workflow', color: 'var(--primary)' },
    { label: 'Active', value: list?.filter((w) => w.active).length ?? '–', icon: 'radio', color: 'var(--primary)' },
    { label: 'Executions', value: total, icon: 'activity', color: '#71717a' },
    { label: 'Failed', value: stats.error ?? 0, icon: 'circle-alert', color: '#e5484d' },
    { label: 'Awaiting approval', value: stats.waiting ?? 0, icon: 'user-check', color: '#f59e0b' },
  ]

  // ---- actions --------------------------------------------------------------
  // The editor chunk downloads while the API creates the workflow.
  const create = async () => { prefetchEditor(); navigate(`/workflow/${(await api.createWorkflow({ name: 'My workflow', folder_id: folderId })).id}`) }
  const fromTemplate = async (id: string) => { prefetchEditor(); navigate(`/workflow/${(await api.fromTemplate(id)).id}`) }
  const move = async (ids: string[], target: string | null) => {
    await api.moveWorkflows(ids, target)
    const name = target ? folders.find((f) => f.id === target)?.name : 'Home'
    toast(`Moved ${ids.length} workflow${ids.length === 1 ? '' : 's'} to ${name}`)
    setSel((s) => new Set([...s].filter((id) => !ids.includes(id))))  // keep unrelated selections
    load()
  }
  const moveFolder = async (f: Folder, target: string | null) => {
    if (target && folderSubtree(folders, f.id).has(target)) return toast("A folder can't go inside itself", 'error')
    await api.updateFolder(f.id, { parent_id: target })
    toast(`Moved folder "${f.name}"`)
    load()
  }
  const exportOne = async (w: Workflow) => downloadJson(await api.exportWorkflow(w.id), `${w.name}.json`)
  const exportMany = async (ids: string[], name: string) => downloadJson(await api.exportWorkflows({ ids }), `${name}.json`)
  const exportFolder = async (f: Folder) => {
    const data = await api.exportWorkflows({ folder_id: f.id })
    if (!data.workflows.length) return toast('This folder has no workflows to export', 'error')
    downloadJson(data, `${f.name}.json`)
  }
  const removeWorkflows = async (ids: string[]) => {
    const names = (list ?? []).filter((w) => ids.includes(w.id)).map((w) => w.name)
    if (!await confirmDialog({
      title: ids.length === 1 ? `Delete "${names[0]}"?` : `Delete ${ids.length} workflows?`,
      message: 'Their executions are deleted too. This cannot be undone.', confirmLabel: 'Delete', danger: true,
    })) return
    try {
      const r = await api.deleteWorkflows(ids)
      toast(`Deleted ${r.deleted} workflow${r.deleted === 1 ? '' : 's'}`)
      setSel(new Set())
      load()
    } catch (e) { toast(`Delete failed: ${errText(e)}`, 'error') }
  }
  const toggle = (id: string) => setSel((s) => { const n = new Set(s); if (n.has(id)) n.delete(id); else n.add(id); return n })
  const allSelected = shown.length > 0 && shown.every((w) => sel.has(w.id))

  // ---- drag & drop (workflows → folder / breadcrumb, folders → folder) ------
  const dropProps = (target: string | null) => {
    const key = target ?? '__home'
    return {
      onDragOver: (e: React.DragEvent) => {
        if (![DRAG_WF, DRAG_FOLDER].some((t) => e.dataTransfer.types.includes(t))) return
        e.preventDefault()
        e.dataTransfer.dropEffect = 'move'
        setDropTarget(key)
      },
      onDragLeave: () => setDropTarget((t) => (t === key ? null : t)),
      onDrop: (e: React.DragEvent) => {
        e.preventDefault()
        setDropTarget(null)
        const wfs = e.dataTransfer.getData(DRAG_WF)
        const fid = e.dataTransfer.getData(DRAG_FOLDER)
        if (wfs) move(JSON.parse(wfs), target).catch((err) => toast(String(err), 'error'))
        else if (fid && fid !== target) {
          const f = folders.find((x) => x.id === fid)
          if (f) moveFolder(f, target).catch((err) => toast(String(err), 'error'))
        }
      },
      'data-drop': dropTarget === key ? 'over' : undefined,
    }
  }

  const folderMenu = (f: Folder) => [
    { label: 'Open', icon: 'folder-open', onClick: () => navigate(`/folder/${f.id}`) },
    { label: 'Rename', icon: 'pencil', onClick: () => setDialog({ kind: 'rename-folder', folder: f }) },
    { label: 'Move to…', icon: 'folder-input', onClick: () => setDialog({ kind: 'move-folder', folder: f }) },
    { label: 'Export workflows', icon: 'download', onClick: () => exportFolder(f) },
    { divider: true, label: '' },
    { label: 'Delete', icon: 'trash-2', danger: true, onClick: () => setDialog({ kind: 'delete-folder', folder: f }) },
  ]
  const workflowMenu = (w: Workflow) => [
    { label: 'Open', icon: 'square-arrow-out-up-right', onClick: () => navigate(`/workflow/${w.id}`) },
    { label: 'Open in new tab', icon: 'external-link', onClick: () => window.open(`/workflow/${w.id}`, '_blank') },
    { label: 'Executions', icon: 'history', onClick: () => navigate(`/workflow/${w.id}/executions`) },
    { divider: true, label: '' },
    { label: 'Rename', icon: 'pencil', onClick: () => setDialog({ kind: 'rename-workflow', wf: w }) },
    { label: 'Duplicate', icon: 'copy', onClick: () => setDialog({ kind: 'duplicate', wf: w }) },
    { label: 'Move to…', icon: 'folder-input', onClick: () => setDialog({ kind: 'move-workflows', ids: [w.id], current: w.folder_id ?? null }) },
    { label: 'Download (export)', icon: 'download', onClick: () => exportOne(w) },
    { divider: true, label: '' },
    { label: 'Delete', icon: 'trash-2', danger: true, onClick: () => removeWorkflows([w.id]) },
  ]

  const subCount = (f: Folder) => folders.filter((x) => x.parent_id === f.id).length

  return (
    <div className="page">
      <div className="page-inner">
        {!folderId && (
          <>
            <div className="hero">
              <h1>Build AI workflows, voice agents &amp; automations</h1>
              <p>Drag nodes onto the canvas: LangGraph runs them, LangChain powers the AI, LiveKit gives them a voice and WhatsApp connects your customers.</p>
              {canWrite && <div className="row" style={{ flexWrap: 'wrap', gap: 10 }}>
                <button className="btn lg white" onClick={create}><Icon name="plus" size={17} /> Create workflow</button>
                <button className="btn lg glass" onClick={() => tplRef.current?.scrollIntoView({ behavior: 'smooth' })}><Icon name="layout-template" size={17} /> Browse templates</button>
                <button className="btn lg glass" onClick={() => setDialog({ kind: 'import' })}><Icon name="upload" size={17} /> Import</button>
              </div>}
              <div className="hero-orb">
                <div><Icon name="bot" size={26} /></div>
                <div><Icon name="mic" size={26} /></div>
                <div><Icon name="message-square" size={26} /></div>
              </div>
            </div>
            <div className="stat-row">
              {statCards.map((s) => (
                <div key={s.label} className="card stat" style={{ '--sc': s.color } as React.CSSProperties}>
                  <div className="stat-icon"><Icon name={s.icon} size={19} /></div>
                  <div><b>{s.value}</b><span>{s.label}</span></div>
                </div>
              ))}
            </div>
          </>
        )}

        {/* breadcrumbs + toolbar */}
        <div className="wf-toolbar">
          <nav className="crumbs" aria-label="Folders">
            <Link to="/" className="crumb" {...dropProps(null)}><Icon name="house" size={15} /> Home</Link>
            {crumbs.map((f) => (
              <span key={f.id} className="crumb-wrap">
                <Icon name="chevron-right" size={14} className="crumb-sep" />
                <Link to={`/folder/${f.id}`} className={`crumb ${f.id === folderId ? 'current' : ''}`} {...dropProps(f.id)}>{f.name}</Link>
              </span>
            ))}
            {folder && <Menu items={folderMenu(folder)} title="Folder actions" />}
          </nav>
          <span className="spacer" />
          {canCreateHere && <div className="wf-actions">
            <button className="btn" title="New folder" onClick={() => setDialog({ kind: 'new-folder' })}><Icon name="folder-plus" size={15} /> <span className="btn-text">New folder</span></button>
            <button className="btn" title="Import workflows" onClick={() => setDialog({ kind: 'import' })}><Icon name="upload" size={15} /> <span className="btn-text">Import</span></button>
            <button className="btn primary" title="Create workflow" onClick={create}><Icon name="plus" size={15} /> <span className="btn-text">Create workflow</span></button>
          </div>}
        </div>
        <div className="wf-filters">
          <div className="search wf-search">
            <Icon name="search" size={15} />
            <input className="input" placeholder={folderId ? 'Search all folders…' : 'Search workflows and folders…'} value={q} onChange={(e) => setQ(e.target.value)} />
          </div>
          <Select className="wf-filter" value={status} onChange={setStatus} title="Status"
            options={[{ value: '', label: 'All statuses' }, { value: 'active', label: 'Active' }, { value: 'inactive', label: 'Inactive' }]} />
          <Select className="wf-filter" value={sort} onChange={(v) => { setSort(v as Sort); lsSet('wf-sort', v) }} title="Sort"
            options={[{ value: 'updated', label: 'Last updated' }, { value: 'created', label: 'Last created' },
              { value: 'name', label: 'Name (A–Z)' }, { value: 'name_desc', label: 'Name (Z–A)' }]} />
          {shown.length > 0 && (
            <label className="wf-selall" title="Select all shown">
              <input type="checkbox" checked={allSelected} onChange={() => setSel(allSelected ? new Set() : new Set(shown.map((w) => w.id)))} /> Select all
            </label>
          )}
        </div>

        {canWrite && sel.size > 0 && (
          <div className="bulk-bar">
            <b>{sel.size} selected</b>
            <button className="btn sm" onClick={() => setDialog({ kind: 'move-workflows', ids: [...sel], current: folderId })}><Icon name="folder-input" size={14} /> Move</button>
            <button className="btn sm" onClick={() => exportMany([...sel], folder ? folder.name : 'workflows')}><Icon name="download" size={14} /> Export</button>
            <button className="btn sm danger" onClick={() => removeWorkflows([...sel])}><Icon name="trash-2" size={14} /> Delete</button>
            <span className="spacer" />
            <button className="btn ghost sm" onClick={() => setSel(new Set())}>Clear</button>
          </div>
        )}

        {/* folders */}
        {subfolders.length > 0 && (
          <div className="folder-grid">
            {subfolders.map((f) => (
              <div key={f.id} className="folder-card" draggable
                onDragStart={(e) => { e.dataTransfer.setData(DRAG_FOLDER, f.id); e.dataTransfer.effectAllowed = 'move' }}
                onClick={() => navigate(`/folder/${f.id}`)} {...dropProps(f.id)}>
                <div className="folder-ico"><Icon name="folder" size={20} /></div>
                <div className="grow">
                  <div className="title">{f.name}</div>
                  <div className="muted small">
                    {searching && f.parent_id ? `in ${folderPath(folders, f.parent_id).map((x) => x.name).join(' / ')} · ` : ''}
                    {f.workflow_count} workflow{f.workflow_count === 1 ? '' : 's'}{subCount(f) ? ` · ${subCount(f)} folder${subCount(f) === 1 ? '' : 's'}` : ''}
                  </div>
                </div>
                <Menu items={folderMenu(f)} title="Folder actions" />
              </div>
            ))}
          </div>
        )}

        {/* workflows */}
        {list && shown.length === 0 && subfolders.length === 0 && (
          <div className="card empty">
            <div className="empty-art"><Icon name={searching ? 'search-x' : folderId ? 'folder-open' : 'sparkles'} size={28} /></div>
            <h3>{searching ? `Nothing matches “${q}”` : folderId ? 'This folder is empty' : 'No workflows yet'}</h3>
            {!searching && <p>{folderId ? 'Create a workflow here, add a subfolder, or drag workflows onto this folder.' : 'Start from scratch or pick a template below. It takes about a minute.'}</p>}
            {!searching && canCreateHere && (
              <div className="row" style={{ justifyContent: 'center', gap: 8 }}>
                <button className="btn primary" onClick={create}><Icon name="plus" size={16} /> Create workflow</button>
                <button className="btn" onClick={() => setDialog({ kind: 'new-folder' })}><Icon name="folder-plus" size={16} /> New folder</button>
              </div>
            )}
          </div>
        )}
        <div className="list">
          {shown.map((w, i) => (
            <div key={w.id} className={`list-item ${sel.has(w.id) ? 'selected' : ''}`} style={{ animationDelay: `${Math.min(i, 12) * 30}ms` }}
              draggable onDragStart={(e) => {
                const ids = sel.has(w.id) ? [...sel] : [w.id]
                e.dataTransfer.setData(DRAG_WF, JSON.stringify(ids))
                e.dataTransfer.effectAllowed = 'move'
              }}
              onPointerEnter={prefetchEditor} onFocus={prefetchEditor}
              onClick={(e) => (e.ctrlKey || e.metaKey ? window.open(`/workflow/${w.id}`, '_blank') : navigate(`/workflow/${w.id}`))}>
              <input type="checkbox" className="row-check" checked={sel.has(w.id)} onClick={(e) => e.stopPropagation()} onChange={() => toggle(w.id)} aria-label={`Select ${w.name}`} />
              <div className="icons-row">
                {(w.trigger_types ?? []).slice(0, 3).map((t) => byType[t] && (
                  <div key={t} className="mini-icon" style={{ '--nc': byType[t].color } as React.CSSProperties} title={byType[t].label}><Icon name={byType[t].icon} size={15} /></div>
                ))}
                {!w.trigger_types?.length && <div className="mini-icon" style={{ '--nc': '#8d92b0' } as React.CSSProperties}><Icon name="workflow" size={15} /></div>}
              </div>
              <div className="grow">
                <div className="title">{w.name}</div>
                <div className="muted small">
                  {searching && w.folder_id && <><Icon name="folder" size={12} style={{ verticalAlign: -1 }} /> {folderPath(folders, w.folder_id).map((f) => f.name).join(' / ')} · </>}
                  {w.node_count} nodes · updated {fmtDate(w.updated_at)}
                </div>
              </div>
              {w.active ? <span className="badge success">Active</span> : <span className="badge">Inactive</span>}
              <Menu items={workflowMenu(w)} title="Workflow actions" />
            </div>
          ))}
        </div>

        {!folderId && !searching && canWrite && (
          <>
            <div className="section-title" ref={tplRef}><Icon name="layout-template" size={17} /> Start from a template</div>
            <div className="grid-cards">
              {templates.map((t, i) => (
                <div key={t.id} className="card tpl" style={{ animationDelay: `${i * 40}ms` }} onPointerEnter={prefetchEditor} onClick={() => fromTemplate(t.id)}>
                  <div className="tpl-icons">
                    {t.tags.flatMap((tag) => (byType[TAG_NODE[tag]] ? [byType[TAG_NODE[tag]]] : [])).slice(0, 3).map((nt) => (
                      <div key={nt.type} className="mini-icon" style={{ '--nc': nt.color } as React.CSSProperties}><Icon name={nt.icon} size={15} /></div>
                    ))}
                  </div>
                  <Icon name="arrow-right" size={18} className="go" />
                  <b>{t.name}</b>
                  <span className="muted small" style={{ lineHeight: 1.45 }}>{t.description}</span>
                  <div className="row" style={{ flexWrap: 'wrap', marginTop: 'auto', gap: 6 }}>{t.tags.map((x) => <span key={x} className="badge plain">{x}</span>)}</div>
                </div>
              ))}
            </div>
          </>
        )}
      </div>

      {dialog?.kind === 'new-folder' && (
        <PromptDialog title={folder ? `New folder in "${folder.name}"` : 'New folder'} label="Folder name" confirm="Create" onClose={() => setDialog(null)}
          onSubmit={async (name) => { await api.createFolder(name, folderId); await load() }} />
      )}
      {dialog?.kind === 'rename-folder' && (
        <PromptDialog title="Rename folder" label="Folder name" initial={dialog.folder.name} onClose={() => setDialog(null)}
          onSubmit={async (name) => { await api.updateFolder(dialog.folder.id, { name }); await load() }} />
      )}
      {dialog?.kind === 'move-folder' && (
        <MoveDialog title={`Move folder "${dialog.folder.name}"`} folders={foldersOf(dialog.folder.owner_id)} current={dialog.folder.parent_id}
          exclude={folderSubtree(folders, dialog.folder.id)} onClose={() => setDialog(null)} onPick={(t) => moveFolder(dialog.folder, t)} />
      )}
      {dialog?.kind === 'delete-folder' && (
        <DeleteFolderDialog folder={dialog.folder} folders={folders} workflows={list ?? []} onClose={() => setDialog(null)}
          onDone={() => { load(); if (folderSubtree(folders, dialog.folder.id).has(folderId ?? '')) navigate(dialog.folder.parent_id ? `/folder/${dialog.folder.parent_id}` : '/') }} />
      )}
      {dialog?.kind === 'move-workflows' && (
        <MoveDialog title={`Move ${dialog.ids.length === 1 ? `"${list?.find((w) => w.id === dialog.ids[0])?.name}"` : `${dialog.ids.length} workflows`}`}
          folders={foldersOf(list?.find((w) => w.id === dialog.ids[0])?.owner_id)} current={dialog.current}
          onClose={() => setDialog(null)} onPick={(t) => move(dialog.ids, t)} />
      )}
      {dialog?.kind === 'duplicate' && (
        <PromptDialog title="Duplicate workflow" label="Name" initial={`${dialog.wf.name} (copy)`} confirm="Duplicate" onClose={() => setDialog(null)}
          onSubmit={async (name) => { const c = await api.duplicateWorkflow(dialog.wf.id, { name }); toast('Duplicated'); await load(); return c }} />
      )}
      {dialog?.kind === 'rename-workflow' && (
        <PromptDialog title="Rename workflow" label="Name" initial={dialog.wf.name} onClose={() => setDialog(null)}
          onSubmit={async (name) => { await api.updateWorkflow(dialog.wf.id, { name }); await load() }} />
      )}
      {dialog?.kind === 'import' && (
        <ImportDialog folderId={folderId} onClose={() => setDialog(null)}
          onImported={(wfs) => { if (wfs.length === 1) { prefetchEditor(); navigate(`/workflow/${wfs[0].id}`) } else load() }} />
      )}
    </div>
  )
}
