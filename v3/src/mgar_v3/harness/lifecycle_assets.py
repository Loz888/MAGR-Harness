"""Fixed controller assets for the original, scoped container execution path."""

import json
from pathlib import Path
from typing import Any

from mgar_v3.domain import DomainError, digest
from mgar_v3.harness.lifecycle_types import RuntimeControl

PYTHON_LOADER = r"""
import importlib,json,os,sys
spec=json.loads(sys.argv[1])
os.chdir('/workspace'+('/'+spec['cwd'] if spec['cwd'] else ''))
for root in reversed(spec['import_roots']):
 sys.path.insert(0,'/workspace'+('/'+root if root else ''))
relative=spec['path']
for root in sorted(spec['import_roots'],key=len,reverse=True):
 if root and relative.startswith(root+'/'): relative=relative[len(root)+1:]; break
name=relative[:-3].replace('/','.')
if name.endswith('.__init__'): name=name[:-9]
importlib.import_module(name)
inodes=set()
for fd in os.listdir('/proc/self/fd'):
 try:
  link=os.readlink('/proc/self/fd/'+fd)
  if link.startswith('socket:['): inodes.add(link[8:-1])
 except OSError: pass
ports=[]
for source in ('tcp','tcp6'):
 for line in open('/proc/net/'+source).read().splitlines()[1:]:
  fields=line.split()
  if len(fields)>9 and fields[3]=='0A' and fields[9] in inodes:
   ports.append(int(fields[1].split(':')[1],16))
with open(os.environ['HARNESS_R04_AUDIT'],'a') as out:
 out.write(json.dumps({'kind':'entry','path':spec['path'],'loaded':True,
                      'pid':os.getpid(),'ports':ports})+'\n')
"""

NODE_LOADER = r"""
var fs=require('fs'),spec=JSON.parse(process.argv[1]);
process.chdir('/workspace'+(spec.cwd?'/'+spec.cwd:''));
function done(){setImmediate(function(){
 var servers=process._getActiveHandles().filter(function(h){return h.listening===true});
 fs.appendFileSync(process.env.HARNESS_R04_AUDIT,JSON.stringify({kind:'entry',path:spec.path,
 loaded:true,pid:process.pid,ports:servers.map(function(s){return s.address().port})})+'\n');
 process.exit(0);
})}
try {require('/workspace/'+spec.path);done()} catch(error){
 if(error.code!=='ERR_REQUIRE_ESM'&&error.code!=='ERR_REQUIRE_ASYNC_MODULE')throw error;
 var load=new Function('path','return import(path)');
 load(require('url').pathToFileURL('/workspace/'+spec.path).href).then(done,function(e){throw e});
}
"""

PROCESS_PROBE = r"""
import errno,hashlib,json,os,signal,socket,sys,time
from datetime import datetime,timezone
expected,action,port=sys.argv[1],sys.argv[2],int(sys.argv[3])
def process(pid):
 try:
  fields=open('/proc/'+str(pid)+'/stat').read().rsplit(')',1)[1].split()
  return {'pid':pid,'parent_pid':int(fields[1]),'group_id':int(fields[2]),
   'session_id':int(fields[3]),'start_ticks':int(fields[19]),'state':fields[0],
   'exit_code':os.waitstatus_to_exitcode(int(fields[49])) if fields[0]=='Z' else None}
 except (OSError,ValueError,IndexError): return None
values={}
supervisors=[]
for name in os.listdir('/proc'):
 if not name.isdigit(): continue
 pid=int(name); value=process(pid)
 if value is None: continue
 values[pid]=value
 try:
  args=open('/proc/'+name+'/cmdline','rb').read().split(b'\0')
  if b'-c' in args and hashlib.sha256(args[args.index(b'-c')+1]).hexdigest()==expected:
   supervisors.append(pid)
 except (OSError,IndexError): pass
if len(supervisors)!=1: raise RuntimeError('supervisor identity unavailable')
supervisor=values[supervisors[0]]
descendants={supervisor['pid']}
while True:
 more={pid for pid,value in values.items() if value['parent_pid'] in descendants}
 if more.issubset(descendants): break
 descendants.update(more)
descendants.discard(supervisor['pid']);descendants.discard(os.getpid())
if action.startswith('release:'):
 pid=int(action.split(':')[1])
 if pid not in descendants or values[pid]['state']=='Z': raise RuntimeError('release owner missing')
 os.kill(pid,signal.SIGUSR2)
if action=='kill':
 for pid in sorted(descendants,reverse=True):
  current=process(pid)
  if current and current['start_ticks']==values[pid]['start_ticks'] and current['state']!='Z':
   try: os.kill(pid,signal.SIGKILL)
   except ProcessLookupError: pass
owners={}
for pid in descendants:
 try:
  for fd in os.listdir('/proc/'+str(pid)+'/fd'):
   try: link=os.readlink('/proc/'+str(pid)+'/fd/'+fd)
   except OSError: continue
   if link.startswith('socket:['): owners.setdefault(link[8:-1],[]).append(pid)
 except OSError: pass
listeners=[]
for name in ('tcp','tcp6'):
 for line in open('/proc/net/'+name).read().splitlines()[1:]:
  fields=line.split()
  if len(fields)>9 and fields[3]=='0A':
   address,number=fields[1].split(':')
   listeners.append({'family':name,'address':address,'port':int(number,16),
                     'pids':owners.get(fields[9],[])})
available=None
if port:
 available=True
 for family,host in ((socket.AF_INET,'127.0.0.1'),(socket.AF_INET6,'::1')):
  try:
   with socket.socket(family,socket.SOCK_STREAM) as probe:
    probe.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
    probe.bind((host,port));probe.listen(1)
  except OSError as error:
   if family!=socket.AF_INET6 or error.errno not in (errno.EAFNOSUPPORT,errno.EADDRNOTAVAIL):
    available=False
print(json.dumps({'observed_at':datetime.now(timezone.utc).isoformat(),
 'monotonic':time.monotonic(),'supervisor':supervisor,
 'processes':[values[pid] for pid in sorted(descendants)],'listeners':listeners,
 'bind_available':available}))
"""


def asset_sources() -> dict[str, str]:
    root = Path(__file__).parent
    return {
        "supervisor": (root / "lifecycle_supervisor.py").read_text(),
        "python_hook": (root / "lifecycle_python_hook.py").read_text(),
        "node_hook": (root / "lifecycle_node_hook.mjs").read_text(),
        "python_loader": PYTHON_LOADER,
        "node_loader": NODE_LOADER,
        "process_probe": PROCESS_PROBE,
    }


def assets_hash() -> str:
    return digest(asset_sources())


def control_command(control: RuntimeControl, argv: tuple[str, ...]) -> tuple[str, ...]:
    sources = asset_sources()
    if digest(sources) != control.assets_hash:
        raise DomainError("HARNESS_LIFECYCLE_ASSET_CHANGED", "Frozen control assets differ", 409)
    config: dict[str, Any] = {
        **sources,
        "mode": control.mode,
        "entry": control.entry.model_dump(mode="json") if control.entry else None,
    }
    return (
        "python3",
        "-I",
        "-u",
        "-c",
        sources["supervisor"],
        json.dumps(config, separators=(",", ":")),
        json.dumps(argv),
    )
