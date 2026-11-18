"""Exercise the actual scrubbed credential wrapper without model execution."""
import inspect
import json
from pathlib import Path
from common import BASE,ROOT,now,write
from parallel_support import validate_inputs
from mgar_v3.quality_eval.h12_quality_runner import QualityRunner
from mgar_v3.models.deepseek_policy import deepseek_request_options
from mgar_v3.settings import Settings

before=validate_inputs(idle=True)
assert Path(inspect.getfile(QualityRunner)).resolve().is_relative_to(ROOT/'src')
assert Settings().deepseek_api_key is not None
assert deepseek_request_options()=={'thinking':{'type':'enabled'},'reasoning_effort':'max'}
after=validate_inputs(idle=True)
assert before==after
write(BASE/'parallel-launch-check.json',{'status':'PASS','recorded_at':now(),
    'actual_scrubbed_wrapper_imports_correct_source':True,'existing_credential_present':True,
    'paid_calls':0,'budget_before':before,'budget_after':after},exclusive=True)
print(json.dumps({'status':'PASS','correct_frozen_source':True,'paid_calls':0,'budget_unchanged':True}))
