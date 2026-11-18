import fs from 'node:fs';
import http from 'node:http';
import crypto from 'node:crypto';
import {syncBuiltinESMExports} from 'node:module';

const report = value => {
  fs.appendFileSync(process.env.HARNESS_R04_AUDIT, JSON.stringify({...value, pid: process.pid})+'\n');
};
const write = process.stdout.write;
process.stdout.write = function(chunk, ...args) {
  report({kind:'print', text:chunk.toString().slice(0,2048)});
  return write.call(this, chunk, ...args);
};
const emit = http.ClientRequest.prototype.emit;
const observed = Symbol('harness-http-observation');
http.ClientRequest.prototype.emit = function(name, ...args) {
  if (name === 'response') {
    const res = args[0];
    res[observed] = {chunks:[],size:0,host:res.socket?.remoteAddress,
      port:res.socket?.remotePort,path:this.path};
  }
  return emit.call(this, name, ...args);
};
const push = http.IncomingMessage.prototype.push;
http.IncomingMessage.prototype.push = function(value, ...args) {
  const state=this[observed];
  if (state && value!==null) {
    const chunk=Buffer.isBuffer(value)?value:Buffer.from(value);
    state.size+=chunk.length;if(state.size<=1048576)state.chunks.push(chunk);
  }
  if (state && value===null) {
    report({kind:'http',path:state.path,host:state.host,port:state.port,
      status:this.statusCode,type:this.headers['content-type']||'',bytes:state.size,
      body_hash:crypto.createHash('sha256').update(Buffer.concat(state.chunks)).digest('hex')});
    delete this[observed];
  }
  return push.call(this,value,...args);
};
const close = http.Server.prototype.close;
let pending = [];
http.Server.prototype.close = function(...args) {
  if (!this.listening) return close.apply(this, args);
  const address = this.address();
  report({kind:'closing', host:address.address, port:address.port});
  pending.push(() => close.apply(this, args));
  return this;
};
process.on('SIGUSR2', () => {const work=pending; pending=[]; for (const done of work) done();});
syncBuiltinESMExports();
