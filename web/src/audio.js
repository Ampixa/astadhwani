// Encode captured mono PCM as a broadly supported WAV; no browser-specific codec upload.
export function encodeWav(chunks, sampleRate, maxSeconds = 25) {
  if (!Number.isFinite(sampleRate) || sampleRate < 8000 || sampleRate > 192000) throw new Error('Unsupported microphone sample rate.');
  const available = chunks.reduce((sum, chunk) => sum + chunk.length, 0);
  const count = Math.min(available, Math.floor(sampleRate * maxSeconds));
  if (!count) throw new Error('The microphone did not capture any audio.');
  const data = new ArrayBuffer(44 + count * 2);
  const view = new DataView(data);
  const text = (offset, value) => [...value].forEach((char, index) => view.setUint8(offset + index, char.charCodeAt(0)));
  text(0, 'RIFF'); view.setUint32(4, 36 + count * 2, true); text(8, 'WAVE'); text(12, 'fmt ');
  view.setUint32(16, 16, true); view.setUint16(20, 1, true); view.setUint16(22, 1, true);
  view.setUint32(24, sampleRate, true); view.setUint32(28, sampleRate * 2, true);
  view.setUint16(32, 2, true); view.setUint16(34, 16, true); text(36, 'data'); view.setUint32(40, count * 2, true);
  let index = 0, square = 0, peak = 0, clipped = 0;
  for (const chunk of chunks) for (const raw of chunk) {
    if (index >= count) break;
    if (!Number.isFinite(raw)) throw new Error('The microphone returned invalid samples. Please record again.');
    const sample = Math.max(-1, Math.min(1, raw));
    view.setInt16(44 + index * 2, Math.round(sample < 0 ? sample * 32768 : sample * 32767), true);
    square += sample * sample; peak = Math.max(peak, Math.abs(sample)); if (Math.abs(sample) >= 0.999) clipped++;
    index++;
  }
  return { blob: new Blob([data], { type: 'audio/wav' }), duration: count / sampleRate, rms: Math.sqrt(square / count), peak, clippedFraction: clipped / count };
}

export function microphoneError(error) {
  if (['NotAllowedError', 'PermissionDeniedError', 'SecurityError'].includes(error.name)) return 'Microphone access was blocked. Allow it in this browser’s site settings (or macOS Privacy & Security → Microphone), then try again. Upload a recording if you prefer.';
  if (['NotFoundError', 'DevicesNotFoundError'].includes(error.name)) return 'No microphone was found. Connect one, or upload an existing recording.';
  if (['NotReadableError', 'TrackStartError', 'AbortError'].includes(error.name)) return 'The microphone could not start. Another app may be using it. Check your input device and try again.';
  return error.message || 'Recording could not start. You can still upload a reference file.';
}
