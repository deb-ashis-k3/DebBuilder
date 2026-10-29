import { useEffect, useRef, useState } from 'react'
import { api, outputText } from '../api'
import { Icon, toast } from '../lib'
import type { ExecEvent, Item } from '../types'

interface Msg { role: 'user' | 'bot'; text: string; error?: boolean; pending?: boolean; execId?: string }

/** Editor chat for workflows with a Chat Trigger. Tokens stream in over the workflow socket. */
export function ChatPanel({ workflowId, welcome, ensureSaved, subscribe, onClose }: {
  workflowId: string
  welcome?: string
  ensureSaved: () => Promise<void>
  subscribe: (fn: (ev: ExecEvent) => void) => () => void
  onClose: () => void
}) {
  const [msgs, setMsgs] = useState<Msg[]>(welcome ? [{ role: 'bot', text: welcome }] : [])
  const [input, setInput] = useState('')
  const [session, setSession] = useState(() => 'editor-' + Math.random().toString(36).slice(2, 8))
  const logRef = useRef<HTMLDivElement>(null)

  useEffect(() => subscribe((ev) => {
    setMsgs((cur) => {
      const i = cur.findIndex((m) => m.execId === ev.execution_id && m.pending)
      if (i < 0) return cur
      const next = [...cur]
      const m = { ...next[i] }
      if (ev.type === 'token' && ev.token) m.text += ev.token
      if (ev.type === 'execution_finished') {
        m.pending = false
        if (ev.status === 'error') { m.error = true; m.text = ev.error ?? 'Execution failed' }
        else if (ev.status === 'waiting') m.text = `⏸ Waiting: ${ev.waiting_for?.message ?? 'approval needed'}`
        else m.text = outputText(ev.output as Item[]) || m.text || '(no output)'
      }
      next[i] = m
      return next
    })
  }), [subscribe])

  useEffect(() => { logRef.current?.scrollTo(0, logRef.current.scrollHeight) }, [msgs])

  const send = async () => {
    const text = input.trim()
    if (!text) return
    setInput('')
    setMsgs((m) => [...m, { role: 'user', text }])
    try {
      await ensureSaved()
      const { execution_id } = await api.chat(workflowId, text, session)
      setMsgs((m) => [...m, { role: 'bot', text: '', pending: true, execId: execution_id }])
    } catch (e) {
      toast(String(e), 'error')
      setMsgs((m) => [...m, { role: 'bot', text: String(e), error: true }])
    }
  }

  return (
    <div className="side-panel">
      <div className="side-head">
        <span className="h-icon"><Icon name="message-circle" size={15} /></span> Chat
        <span className="badge plain small">session {session.slice(-6)}</span>
        <span className="spacer" />
        <button className="btn ghost sm" title="New session (clears memory context)" onClick={() => { setSession('editor-' + Math.random().toString(36).slice(2, 8)); setMsgs(welcome ? [{ role: 'bot', text: welcome }] : []) }}>
          <Icon name="rotate-ccw" size={14} /> New
        </button>
        <button className="btn ghost sm icon" onClick={onClose}><Icon name="x" size={16} /></button>
      </div>
      <div className="chat-log" ref={logRef}>
        {msgs.length === 0 && (
          <div className="empty small">
            <div className="empty-art"><Icon name="messages-square" size={26} /></div>
            Send a message to run the workflow.<br />Replies stream in token by token.
          </div>
        )}
        {msgs.map((m, i) => (
          <div key={i} className={`msg ${m.role} ${m.error ? 'err' : ''}`}>
            {m.text || (m.pending ? <span className="typing"><i /><i /><i /></span> : '')}
          </div>
        ))}
      </div>
      <div className="chat-input">
        <textarea className="textarea" rows={1} style={{ minHeight: 38 }} placeholder="Type a message…" value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send() } }} />
        <button className="btn primary icon" style={{ height: 38, width: 42 }} onClick={send}><Icon name="send" size={16} /></button>
      </div>
    </div>
  )
}
