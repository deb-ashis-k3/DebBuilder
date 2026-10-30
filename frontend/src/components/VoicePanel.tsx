import '@livekit/components-styles'
import {
  BarVisualizer, LiveKitRoom, RoomAudioRenderer, VoiceAssistantControlBar, useRoomContext, useTranscriptions, useVoiceAssistant,
} from '@livekit/components-react'
import { useState } from 'react'
import { api } from '../api'
import { Icon, toast } from '../lib'

const KEYS = ['1', '2', '3', '4', '5', '6', '7', '8', '9', '*', '0', '#']
const DTMF_CODE: Record<string, number> = { '*': 10, '#': 11 }

/** Phone keypad: sends real DTMF tones, so IVR menus ("press 1 for sales") can be tested in the browser. */
function Keypad() {
  const room = useRoomContext()
  const [sent, setSent] = useState('')
  const press = async (k: string) => {
    try {
      await room.localParticipant.publishDtmf(DTMF_CODE[k] ?? Number(k), k)
      setSent((s) => (s + k).slice(-16))
    } catch (e) { toast(String(e), 'error') }
  }
  return (
    <div className="keypad">
      <div className="keypad-sent">{sent || 'Keypad'}</div>
      <div className="keypad-grid">{KEYS.map((k) => <button key={k} className="btn" onClick={() => press(k)}>{k}</button>)}</div>
    </div>
  )
}

function Stage() {
  const { state, audioTrack } = useVoiceAssistant()
  const transcripts = useTranscriptions()
  const [pad, setPad] = useState(false)
  return (
    <div className="voice-stage">
      <div className="voice-viz">
        <BarVisualizer state={state} barCount={5} trackRef={audioTrack} options={{ minHeight: 18 }} />
      </div>
      <div className="voice-state">{state}</div>
      <VoiceAssistantControlBar controls={{ leave: false }} />
      <button className="btn sm ghost" onClick={() => setPad(!pad)}><Icon name="grid-3x3" size={14} /> {pad ? 'Hide keypad' : 'Keypad (DTMF)'}</button>
      {pad && <Keypad />}
      <div className="chat-log" style={{ width: '100%', maxHeight: 340 }}>
        {transcripts.slice(-30).map((t) => {
          const isAgent = t.participantInfo.identity.startsWith('agent')
          return <div key={t.streamInfo.id} className={`msg ${isAgent ? 'bot' : 'user'}`}>{t.text}</div>
        })}
      </div>
    </div>
  )
}

/** Browser voice session against a workflow's Voice Agent trigger (LiveKit). */
export function VoicePanel({ workflowId, ensureSaved, onClose }: { workflowId: string; ensureSaved: () => Promise<void>; onClose: () => void }) {
  const [conn, setConn] = useState<{ url: string; token: string } | null>(null)
  const [busy, setBusy] = useState(false)

  const start = async () => {
    setBusy(true)
    try {
      await ensureSaved()
      setConn(await api.voiceSession(workflowId))
    } catch (e) {
      toast(String(e), 'error')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="side-panel">
      <div className="side-head">
        <span className="h-icon"><Icon name="mic" size={15} /></span> Voice agent
        <span className="spacer" />
        {conn && <button className="btn sm danger" onClick={() => setConn(null)}><Icon name="phone-off" size={14} /> End</button>}
        <button className="btn ghost sm icon" onClick={onClose}><Icon name="x" size={16} /></button>
      </div>
      {conn ? (
        <LiveKitRoom serverUrl={conn.url} token={conn.token} connect audio video={false}
          onDisconnected={() => setConn(null)} onError={(e) => toast(e.message, 'error')}>
          <RoomAudioRenderer />
          <Stage />
        </LiveKitRoom>
      ) : (
        <div className="voice-stage">
          <div className="voice-orb"><Icon name="audio-lines" size={48} /></div>
          <p className="muted small" style={{ textAlign: 'center' }}>
            Starts a LiveKit room and dispatches the DebBuilder voice worker. Every turn runs this workflow; its output is spoken back.
          </p>
          <button className="btn primary" disabled={busy} onClick={start}><Icon name="phone" size={16} /> {busy ? 'Connecting…' : 'Start call'}</button>
          <p className="muted small" style={{ textAlign: 'center' }}>Needs the voice worker running:<br /><code>uv run --extra voice python voice/worker.py dev</code></p>
        </div>
      )}
    </div>
  )
}
