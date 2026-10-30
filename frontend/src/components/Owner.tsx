import { useState } from 'react'
import { readsAll, useAuth } from '../auth'
import { Icon } from '../lib'
import { Select } from './Select'

export type OwnerScope = 'all' | 'mine'

function lsGet(k: string) { try { return localStorage.getItem(k) } catch { return null } }
function lsSet(k: string, v: string) { try { localStorage.setItem(k, v) } catch { /* private mode */ } }

/**
 * Per-user workspaces: editors only ever get their own items, so the filter only matters for
 * roles that can see everyone's (viewer, admin, owner). Remembered per page.
 */
export function useOwnerScope(key: string) {
  const { user } = useAuth()
  const [scope, setScope] = useState<OwnerScope>(() => (lsGet(key) === 'mine' ? 'mine' : 'all'))
  const set = (v: OwnerScope) => { setScope(v); lsSet(key, v) }
  const show = readsAll(user)
  const isMine = (o: { owner_id?: string | null }) => o.owner_id === user?.id
  /** Should this item be listed under the current scope? */
  const visible = (o: { owner_id?: string | null }) => !show || scope === 'all' || isMine(o)
  return { scope: show ? scope : 'mine' as OwnerScope, setScope: set, show, isMine, visible }
}

export function OwnerFilter({ value, onChange, className }: { value: OwnerScope; onChange: (v: OwnerScope) => void; className?: string }) {
  return (
    <Select className={className} value={value} onChange={(v) => onChange(v as OwnerScope)} title="Owner"
      options={[{ value: 'all', label: 'Everyone' }, { value: 'mine', label: 'Mine' }]} />
  )
}

/** "Owned by …" marker, only for items that aren't the viewer's own. */
export function OwnerBadge({ item }: { item: { owner_id?: string | null; owner_name?: string | null } }) {
  const { user } = useAuth()
  if (!item.owner_id || item.owner_id === user?.id) return null
  return <span className="badge plain" title={`Owned by ${item.owner_name ?? 'another user'}`}><Icon name="user" size={12} /> {item.owner_name ?? 'Other user'}</span>
}
