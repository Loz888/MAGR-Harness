"""Public pflag contract: a non-interspersed parser stops at unknown short flags."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

PLAN_ID = "public-pflag-stop-unknown-v1"
CASE_IDS = ("unknown-before-known", "known-prefix-before-unknown", "unknown-before-value", "interspersed-control")
GO_SOURCE = r'''
package main
import (
 "encoding/json"
 "io"
 "os"
 flag "github.com/spf13/pflag"
)
type Sample struct { Args []string; Verbose bool; Name string; Error string }
func observe(interspersed bool, arguments []string) Sample {
 f := flag.NewFlagSet("public", flag.ContinueOnError)
 f.SetOutput(io.Discard)
 f.SetInterspersed(interspersed)
 f.ParseErrorsAllowlist.UnknownFlagsHandling = flag.PassUnknownFlagToArgs
 verbose := f.BoolP("verbose", "v", false, "public flag")
 name := f.StringP("name", "n", "default", "public value")
 err := f.Parse(arguments)
 problem := ""
 if err != nil { problem = err.Error() }
 return Sample{f.Args(), *verbose, *name, problem}
}
func main() {
 values := map[string]Sample{
  "unknown-before-known": observe(false, []string{"-x", "-v", "tail"}),
  "known-prefix-before-unknown": observe(false, []string{"-n", "before", "-x", "-n", "after", "tail"}),
  "unknown-before-value": observe(false, []string{"-x", "tail", "-v"}),
  "interspersed-control": observe(true, []string{"-x", "-v", "tail"}),
 }
 if err := json.NewEncoder(os.Stdout).Encode(values); err != nil { os.Exit(2) }
}
'''
EXPECTED = {
 "unknown-before-known": {"Args": ["-x", "-v", "tail"], "Verbose": False, "Name": "default", "Error": ""},
 "known-prefix-before-unknown": {"Args": ["-x", "-n", "after", "tail"], "Verbose": False, "Name": "before", "Error": ""},
 "unknown-before-value": {"Args": ["-x", "tail", "-v"], "Verbose": False, "Name": "default", "Error": ""},
 "interspersed-control": {"Args": ["-x", "tail"], "Verbose": True, "Name": "default", "Error": ""},
}
cases = []
try:
 with tempfile.TemporaryDirectory(prefix="h12-public-pflag-") as directory:
  source = Path(directory) / "public.go"
  source.write_text(GO_SOURCE)
  environment = dict(os.environ, GOTOOLCHAIN="local", GOPROXY="off", GOSUMDB="off")
  process = subprocess.run(["go", "run", str(source)], capture_output=True, text=True,
                           timeout=120, check=True, env=environment)
  if process.stderr:
   raise RuntimeError("Go setup/runtime wrote stderr: " + process.stderr[:200])
  observed = json.loads(process.stdout)
  if not isinstance(observed, dict) or set(observed) != set(CASE_IDS):
   raise RuntimeError("Go runner returned an invalid observation set")
except Exception as problem:
 cases = [{"id": name, "status": "error", "detail": type(problem).__name__ + ": " + str(problem)[:300]} for name in CASE_IDS]
else:
 for name in CASE_IDS:
  value = observed[name]
  if not isinstance(value, dict) or set(value) != {"Args", "Verbose", "Name", "Error"} or value["Error"]:
   cases.append({"id": name, "status": "error", "detail": "Target API observation failed"})
   continue
  try:
   assert value == EXPECTED[name], "expected=" + repr(EXPECTED[name]) + "; observed=" + repr(value)
  except AssertionError as problem:
   cases.append({"id": name, "status": "assertion_failed", "detail": str(problem)[:800]})
  else:
   cases.append({"id": name, "status": "passed"})
print("HARNESS_PUBLIC_PROBE_V1 " + json.dumps({"plan_id": PLAN_ID,
 "asset_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), "cases": cases}))
sys.exit(2 if any(c["status"] == "error" for c in cases) else
         1 if any(c["status"] == "assertion_failed" for c in cases) else 0)
