import React, { useEffect, useRef, useState } from 'react';
import { Check, Headphones, LoaderCircle, Mic2, RefreshCw, ShieldCheck, Square, Upload, X } from 'lucide-react';
import { encodeWav, microphoneError } from './audio.js';

const READING = 'नमस्कार। म अहिले आफ्नो आवाजको नमुना रेकर्ड गर्दै छु। आजको दिन शान्त छ। आउनुहोस्, सँगै बसेर केही कुरा गरौँ।';

export default function VoiceDialog({ request, onClose, onSaved, existingNames = [] }) {
  const [mode, setMode] = useState('record');
  const [name, setName] = useState(() => { const names = new Set(existingNames); let candidate = 'My voice', take = 2; while (names.has(candidate)) candidate = `My voice ${take++}`; return candidate; });
  const [transcript, setTranscript] = useState(READING);
  const [file, setFile] = useState(null), [url, setUrl] = useState('');
  const [consent, setConsent] = useState(false), [verified, setVerified] = useState(false);
  const [state, setState] = useState('idle'), [duration, setDuration] = useState(0), [level, setLevel] = useState(0);
  const [device, setDevice] = useState(''), [warning, setWarning] = useState(''), [error, setError] = useState(''), [saving, setSaving] = useState(false);
  const nameRef = useRef(null), capture = useRef(null), epoch = useRef(0), alive = useRef(true);
  const active = state === 'requesting' || state === 'recording' || state === 'stopping';

  const release = (session) => {
    if (!session) return;
    clearTimeout(session.limitTimer); clearTimeout(session.flushTimer);
    session.stream?.getTracks().forEach(track => { track.onended = null; track.stop(); });
    try { session.source?.disconnect(); session.node?.disconnect(); session.silent?.disconnect(); } catch { /* Already disconnected. */ }
    session.context?.close().catch(() => {});
  };
  useEffect(() => { nameRef.current?.focus(); return () => { alive.current = false; epoch.current++; release(capture.current); capture.current = null; }; }, []);
  useEffect(() => { if (!file) {setUrl(''); return;} const next = URL.createObjectURL(file); setUrl(next); return () => URL.revokeObjectURL(next); }, [file]);

  const finish = (session) => {
    if (capture.current !== session || session.done) return;
    session.done = true;
    try {
      const result = encodeWav(session.chunks, session.context.sampleRate);
      if (result.duration < 3) throw new Error('That was a little short. Record at least 3 seconds; 10–20 seconds works well for this preview.');
      if (result.rms < 0.0001) throw new Error('We could not hear your voice. Check the microphone input and record again.');
      if (alive.current) {
        setFile(new File([result.blob], 'my-voice-reference.wav', {type:'audio/wav'}));
        setDuration(result.duration); setState('review'); setVerified(false);
        setWarning(result.clippedFraction > 0.005 ? 'Some audio is clipping. Move a little farther from the microphone and consider recording again.' : result.rms < 0.004 ? 'Your recording is quiet. Listen back; move closer and record again if needed.' : '');
      }
    } catch (e) { if (alive.current) { setError(e.message); setState('idle'); setDuration(0); } }
    finally { release(session); if(capture.current===session)capture.current=null; if(alive.current)setLevel(0); }
  };

  const stop = () => {
    const session = capture.current;
    if (!session || session.stopping) return;
    if (state === 'requesting') { epoch.current++; release(session); capture.current = null; setState('idle'); return; }
    session.stopping = true; setState('stopping');
    clearTimeout(session.limitTimer);
    // Flush the last partial PCM block before disconnecting the microphone.
    session.node?.port.postMessage('flush');
    session.flushTimer = setTimeout(() => finish(session), 700);
  };
  const stopRef = useRef(stop); stopRef.current = stop;
  useEffect(() => {
    const hidden = () => { if(document.hidden && capture.current)stopRef.current(); };
    const leaving = () => { epoch.current++; release(capture.current); capture.current=null; };
    document.addEventListener('visibilitychange', hidden); window.addEventListener('pagehide', leaving);
    return () => {document.removeEventListener('visibilitychange',hidden);window.removeEventListener('pagehide',leaving);};
  }, []);

  const start = async () => {
    if (capture.current) return;
    if (!navigator.mediaDevices?.getUserMedia || !window.AudioContext || !window.AudioWorkletNode) {setError('Recording is unavailable in this browser. Try a current browser or upload a reference file.');return;}
    setError(''); setWarning(''); setFile(null); setVerified(false); setDuration(0); setLevel(0); setState('requesting');
    const id = ++epoch.current;
    const session = {chunks:[], count:0, done:false, stopping:false}; capture.current = session;
    const current = () => alive.current && epoch.current===id && capture.current===session;
    try {
      session.context = new AudioContext();
      await session.context.resume();
      if(!current()){release(session);return;}
      session.stream = await navigator.mediaDevices.getUserMedia({audio:{channelCount:{ideal:1},echoCancellation:false,noiseSuppression:false,autoGainControl:false},video:false});
      if(!current()){release(session);return;}
      const track = session.stream.getAudioTracks()[0];
      setDevice(track.label || 'Default microphone');
      await session.context.audioWorklet.addModule(new URL('./pcm-recorder.worklet.js',import.meta.url));
      if(!current()){release(session);return;}
      session.source = session.context.createMediaStreamSource(session.stream);
      session.node = new AudioWorkletNode(session.context,'studio-pcm-recorder');
      session.silent = session.context.createGain(); session.silent.gain.value=0;
      session.node.port.onmessage = ({data}) => {
        if(!current() || session.done)return;
        if(data.samples){
          session.chunks.push(data.samples); session.count+=data.samples.length;
          let peak=0; for(const sample of data.samples)peak=Math.max(peak,Math.abs(sample));
          setLevel(Math.min(1,peak*2));setDuration(Math.min(25,session.count/session.context.sampleRate));
          if(session.count>=session.context.sampleRate*25&&!session.stopping)stopRef.current();
        }
        if(data.flushed&&session.stopping)finish(session);
      };
      session.source.connect(session.node);session.node.connect(session.silent);session.silent.connect(session.context.destination);
      track.onended=()=>{if(current()){setWarning('The microphone disconnected. Review the audio captured so far.');stopRef.current();}};
      session.limitTimer=setTimeout(()=>{if(current())stopRef.current();},25000);
      setState('recording');
    } catch(e){ release(session);if(current()){capture.current=null;setError(microphoneError(e));setState('idle');} }
  };

  const close = () => {
    if(saving)return;
    if((active||file)&&!confirm('Close without saving this voice? The unsaved recording will be discarded.'))return;
    epoch.current++;release(capture.current);capture.current=null;onClose();
  };
  const changeMode = (next) => {
    if(next===mode||active||saving)return;
    if(file&&!confirm('Switch input method? Your unsaved reference will be discarded.'))return;
    setMode(next);setFile(null);setDuration(0);setVerified(false);setWarning('');setError('');setState('idle');setTranscript(next==='record'?READING:'');
  };
  const save = async (event) => {
    event.preventDefault();if(active||!file||!verified||!consent)return;
    setError('');setSaving(true);
    try{
      const form=new FormData();form.set('name',name.trim());form.set('transcript',transcript.trim());form.set('consent','true');form.set('audio',file);
      const voice=await request('voices',{method:'POST',body:form});
      await onSaved(voice);
    }catch(e){if(alive.current)setError(e.message);}finally{if(alive.current)setSaving(false);}
  };

  return <div className="modal-backdrop"><section role="dialog" aria-modal="true" aria-labelledby="voice-title" className="modal voice-dialog" onKeyDown={event=>{
    if(event.key==='Escape'){event.stopPropagation();close();}
    if(event.key==='Tab'){const nodes=[...event.currentTarget.querySelectorAll('button:not(:disabled),input:not(:disabled),textarea:not(:disabled),select:not(:disabled),audio[controls]')];const first=nodes[0],last=nodes[nodes.length-1];if(event.shiftKey&&document.activeElement===first){event.preventDefault();last.focus();}else if(!event.shiftKey&&document.activeElement===last){event.preventDefault();first.focus();}}
  }}><div className="modal-heading"><div><div className="eyebrow">A FAMILIAR VOICE</div><h2 id="voice-title">Make it your voice.</h2></div><button className="icon-button" disabled={saving} aria-label="Close voice dialog" onClick={close}><X size={20}/></button></div>
  <p className="dialog-intro">A short sample, a few familiar words. Record here or bring your own file.</p>
  <div className="voice-input-tabs" role="tablist" aria-label="Reference input method"><button id="record-tab" role="tab" aria-selected={mode==='record'} aria-controls="record-panel" disabled={active||saving} onClick={()=>changeMode('record')}><Mic2 size={16}/> Record my voice</button><button id="upload-tab" role="tab" aria-selected={mode==='upload'} aria-controls="upload-panel" disabled={active||saving} onClick={()=>changeMode('upload')}><Upload size={16}/> Upload a file</button></div>
  {error&&<div className="error-banner" role="alert">{error}</div>}
  <form onSubmit={save}><label className="field">Voice name<input ref={nameRef} required maxLength={60} disabled={saving} value={name} onChange={e=>setName(e.target.value)} placeholder="e.g. Yash’s voice"/></label>
   {mode==='record'?<div id="record-panel" role="tabpanel" aria-labelledby="record-tab"><div className="reading-card"><div className="reading-label">READ THIS ALOUD <span>नेपाली</span></div><p lang="ne">{READING}</p><small>Speak naturally in a quiet room. Aim for 10–20 seconds; recording stops at 25 seconds.</small></div>
    <div className={`recorder-box ${state==='recording'?'is-recording':''}`}><div className="recorder-status"><span className="recorder-mic"><Mic2 size={21}/></span><div><b>{state==='requesting'?'Waiting for your microphone…':state==='recording'?'Listening to you':state==='stopping'?'Preparing your recording…':file?'Your sample is ready':'Ready when you are'}</b><small>{state==='requesting'?'Allow access in the microphone prompt.':state==='recording'?device:state==='stopping'?'Finishing the WAV file locally.':file?`${duration.toFixed(1)} seconds · WAV · ready to save`:'Microphone turns on only when you press Record.'}</small></div><span className="record-clock" aria-label="Recording duration">{String(Math.floor(duration)).padStart(2,'0')}s <span>/ 25s</span></span></div>
     {state==='recording'&&<div className="input-meter" role="meter" aria-label="Microphone input level" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(level*100)}><span style={{width:`${Math.max(1,level*100)}%`}}/></div>}
     <div className="recorder-controls">{active?<button type="button" className="button stop-recording" onClick={stop} disabled={state==='stopping'}>{state==='stopping'?<LoaderCircle className="spin" size={15}/>:<Square size={14}/>} {state==='requesting'?'Cancel microphone request':'Stop recording'}</button>:<button type="button" className="button primary" onClick={start} disabled={saving}>{file?<RefreshCw size={15}/>:<Mic2 size={15}/>} {file?'Record again':'Start recording'}</button>}<span>{state==='recording'?'Stop when you finish reading.':'Recorded in this window until you save.'}</span></div>
    </div>
   </div>:<div id="upload-panel" role="tabpanel" aria-labelledby="upload-tab"><label className="field upload-field">Reference recording<input type="file" accept="audio/*,.wav,.flac,.aiff" required disabled={saving} onChange={e=>{setFile(e.target.files[0]||null);setVerified(false);}}/><small>A clean 1–30 second WAV, FLAC or AIFF. Up to 25 MiB.</small></label></div>}
   {file&&<div className="recording-review"><div><Headphones size={15}/><b>Listen before saving</b><span>{mode==='record'?'Microphone is off':'Local file preview'}</span></div><audio controls src={url} preload="metadata"/></div>}
   {warning&&<div className="warning" role="status">{warning}</div>}
   <label className="field transcript-field">{mode==='record'?'Check the words you actually said':'Exact transcript'}<textarea required maxLength={1200} disabled={active||saving} value={transcript} onChange={e=>{setTranscript(e.target.value);setVerified(false);}} placeholder="Write exactly what the recording says…"/><small>{mode==='record'?'The reading prompt is filled in. If you said something different, edit it to match your recording.':'Cloning works best when the transcript matches the recording word for word.'}</small></label>
   <div className="voice-confirmations"><label className="consent-row"><input type="checkbox" checked={verified} disabled={!file||active||saving} onChange={e=>setVerified(e.target.checked)}/><span>I listened back; the transcript matches my recording.</span></label><label className="consent-row"><input type="checkbox" checked={consent} disabled={saving} onChange={e=>setConsent(e.target.checked)}/><span>This is my voice, or I have permission to use it for speech generation.</span></label></div>
   <div className="recording-privacy"><ShieldCheck size={13}/> Unsaved audio stays in this window. Save voice sends the recording and transcript to the studio engine: on your Mac for the local app, or on the server for a hosted studio.</div>
   <div className="modal-actions"><button type="button" className="button secondary" disabled={saving} onClick={close}>Cancel</button><button type="submit" className="button primary" disabled={saving||active||!file||!verified||!consent||!name.trim()||!transcript.trim()}>{saving?<LoaderCircle className="spin" size={16}/>:<Check size={16}/>} {saving?'Saving your voice…':'Save voice & use it'}</button></div>
  </form></section></div>;
}
