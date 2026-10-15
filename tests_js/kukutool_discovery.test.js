"use strict";
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../xhs_download/services/platforms/kukutool_provider.js'), 'utf8');
const profile = {id:'fixture-current',authRoute:'auth-current',authResponseFields:{key:'key',seed:'seed'},
  parseRequestFields:{key:'key',payload:'payload',iv:'iv',version:'revision'}};

function fixture({found=true, cached=false, offsite=false}={}) {
  const calls=[];
  const context=vm.createContext({require, URL, Buffer, AbortSignal,
    process:{argv:['node','helper','https://source.example/work/1'],env:cached?{KUKUTOOL_PROFILE_JSON:JSON.stringify({...profile,id:'old',authRoute:'auth-old'})}:{},stdout:{write(){} }},
    fetch:async (url, options={})=>{
      const p=new URL(url).pathname; calls.push(p);
      if(p==='/')return new Response(Array.from({length:30},(_,i)=>`<script src="${offsite?'https://other.example':''}/script-${i}.js"></script>`).join(''));
      if(p.endsWith('.js'))return new Response(found && p==='/script-13.js'?JSON.stringify({activeProfileId:profile.id,profiles:[profile]}):'/* unrelated */');
      if(p==='/api/auth-current')return new Response(JSON.stringify({key:'fixture-key',seed:'fixture-seed'}));
      if(p==='/api/parse')return new Response(JSON.stringify({status:0,data:{images:[]}}));
      return new Response('{"status":404}',{status:404});
    }});
  vm.runInContext(source.slice(0,source.indexOf('main().catch(')),context);
  return {context,calls};
}

test('current profile after sixth script is discovered without hardcoded filenames',async()=>{
  const {context,calls}=fixture(); await context.main();
  assert.ok(calls.includes('/script-13.js'));
  assert.deepEqual(calls.filter(p=>p.startsWith('/api/')),['/api/auth-current','/api/parse']);
});
test('failed cached profile discovers a new profile and does not retry old authorization',async()=>{
  const {context,calls}=fixture({cached:true}); await context.main();
  assert.deepEqual(calls.filter(p=>p.startsWith('/api/')),['/api/auth-old','/api/auth-current','/api/parse']);
});
test('bounded discovery never doubles failed fallback attempts',async()=>{
  const {context,calls}=fixture({found:false}); await assert.rejects(context.main());
  assert.equal(calls.filter(p=>p.endsWith('.js')).length,24);
  const auth=calls.filter(p=>p.startsWith('/api/'));
  assert.equal(auth.length,2); assert.equal(new Set(auth).size,2);
});
test('off-origin scripts are not fetched',async()=>{
  const {context,calls}=fixture({offsite:true}); await assert.rejects(context.main());
  assert.equal(calls.filter(p=>p.endsWith('.js')).length,0);
});

test('response decoding follows the public SHA256 key derivation',()=>{
  const crypto=require('node:crypto');
  const iv=Buffer.alloc(16,7);
  const key=crypto.createHash('sha256').update('12345678901234567890123456789013').digest();
  const cipher=crypto.createCipheriv('aes-256-cbc',key,iv);
  const payload={images:[{width:1920,height:1080}],title:'offline fixture'};
  const ciphertext=Buffer.concat([cipher.update(JSON.stringify(payload)),cipher.final()]);
  function encode(bytes) {
    const standard='ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/';
    const custom='ZYXABCDEFGHIJKLMNOPQRSTUVWzyxabcdefghijklmnopqrstuvw9876543210-_';
    const mapped=Array.from(bytes.toString('base64'),c=>standard.includes(c)?custom[standard.indexOf(c)]:c).join('');
    let reversed=''; for(let i=0;i<mapped.length;i+=8)reversed+=Array.from(mapped.slice(i,i+8)).reverse().join('');
    return Array.from(reversed,c=>String.fromCharCode(c.charCodeAt(0)^0x5a)).join('');
  }
  const {context}=fixture();
  assert.equal(JSON.stringify(context.decryptResponse(encode(ciphertext),encode(iv))),JSON.stringify(payload));
  assert.throws(()=>context.decryptResponse('invalid','invalid'));
});

test('diagnostics classify parse HTTP failure without emitting source or credential values',async()=>{
  const {context}=fixture();
  let output='';
  context.process.env.KUKUTOOL_DIAGNOSTICS='1';
  context.process.stderr={write(value){output+=value;}};
  const original=context.fetch;
  context.fetch=async(url,options)=>new URL(url).pathname==='/api/parse'
    ?new Response(JSON.stringify({message:'secret source https://private.example/token'}),{status:500,headers:{'content-type':'application/json'}})
    :original(url,options);
  await assert.rejects(context.main());
  context.emitDiagnostics();
  const data=JSON.parse(output.split('KUKUTOOL_DIAGNOSTICS:')[1]);
  assert.ok(data.some(row=>row.role==='parse' && row.status===500));
  assert.ok(data.some(row=>row.role==='auth' && row.status===200));
  assert.ok(!output.includes('private.example'));
  assert.ok(!output.includes('fixture-key'));
});
