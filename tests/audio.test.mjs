import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import { encodeWav, microphoneError } from '../web/src/audio.js';

test('microphone WAV preserves rate, PCM signs, frame count and clipping diagnostics', async()=>{
 const result=encodeWav([new Float32Array([0,-1,1,.5])],48000);
 const buffer=await result.blob.arrayBuffer(); const v=new DataView(buffer);
 assert.equal(new TextDecoder().decode(buffer.slice(0,4)),'RIFF');
 assert.equal(v.getUint32(24,true),48000);assert.equal(v.getUint16(22,true),1);
 assert.equal(v.getUint32(40,true),8);assert.equal(v.getInt16(46,true),-32768);assert.equal(v.getInt16(48,true),32767);
 assert.equal(result.duration,4/48000);assert.equal(result.clippedFraction,.5);
});

test('capture remains bounded and rejects invalid input without producing unusable files',()=>{
 const result=encodeWav([new Float32Array(48000*26)],48000,25);
 assert.equal(result.duration,25);assert.equal(result.rms,0);
 assert.throws(()=>encodeWav([new Float32Array([NaN])],44100),/invalid samples/);
 assert.throws(()=>encodeWav([],44100),/any audio/);
 assert.match(microphoneError({name:'NotAllowedError'}),/site settings/);
 assert.match(microphoneError({name:'NotFoundError'}),/No microphone/);
});

test('worklet downmixes stereo, transfers bounded chunks and flushes the trailing samples',()=>{
 let Recorder;const messages=[];
 class Processor {constructor(){this.port={postMessage:(message)=>messages.push(message)};}}
 const sandbox={AudioWorkletProcessor:Processor,Float32Array,registerProcessor:(name,cls)=>{assert.equal(name,'studio-pcm-recorder');Recorder=cls;}};
 vm.runInNewContext(fs.readFileSync(new URL('../web/src/pcm-recorder.worklet.js',import.meta.url),'utf8'),sandbox);
 const recorder=new Recorder();
 recorder.process([[new Float32Array([.2,.4]),new Float32Array([.6,.8])]]);
 recorder.port.onmessage({data:'flush'});
 assert.equal(messages[0].samples.length,2);assert.ok(Math.abs(messages[0].samples[0]-.4)<1e-6);assert.ok(Math.abs(messages[0].samples[1]-.6)<1e-6);
 assert.equal(messages[1].flushed,true);
 for(let i=0;i<16;i++)recorder.process([[new Float32Array(128).fill(.1)]]);
 assert.equal(messages[2].samples.length,2048);
});
