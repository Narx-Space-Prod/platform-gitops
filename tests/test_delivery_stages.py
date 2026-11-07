"""Named pipeline stages preserve the existing release and deployment trust policy."""
import copy
import json
import os
from pathlib import Path
import sys
import unittest
import yaml
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import delivery as d

class DeliveryStages(unittest.TestCase):
 def setUp(self):
  self.facts={name:{'component':name,'repository':'Narx-Space-Prod/'+repo,
   'image':'ghcr.io/narx-space-prod/commerce-'+name,'digest':'sha256:'+'a'*64,
   'commit':'b'*40,'runId':12} for name,repo in d.COMPONENTS.items()}
  self.bundle=d.bundle(self.facts)
  self.old=copy.deepcopy(self.facts)
  for fact in self.old.values():fact['commit']='e'*40
  self.states={'dev':self.bundle,'qa':d.bundle(self.old),'production':d.bundle(self.old)}
 def command(self,*args,**kwargs):
  if args[:3]==('git','rev-parse','origin/main'):return 'c'*40
  if args[:2]==('git','show'):return json.dumps(self.bundle)
  if args[:3]==('gh','pr','list'):return json.dumps([{'state':'OPEN'}])
  return ''
 def state(self,path):return self.states[Path(path).parent.name]
 def test_qa_stage_verifies_the_exact_upstream_and_preserves_previous_state(self):
  with patch.object(d,'run',side_effect=self.command),patch.object(d,'read',side_effect=self.state),patch.object(d,'healthy',return_value=True),patch.object(d,'successful_run',return_value=True),patch.object(d,'verify_image') as verify,patch.object(d,'open_pr') as propose,patch.object(d,'stage_output'):
   d.advance_stage('qa',self.bundle['release']['id'])
   self.assertEqual(verify.call_count,4)
   env,value,reason=propose.call_args.args
   self.assertEqual((env,reason),('qa','promote'))
   self.assertEqual(value['release']['components'],self.facts)
   self.assertEqual(value['release']['previous'],{'components':self.old,'gitopsCommit':'c'*40,'environment':'qa'})
 def test_superseded_or_unhealthy_upstream_cannot_be_promoted(self):
  with patch.object(d,'run',side_effect=self.command),patch.object(d,'read',side_effect=self.state),patch.object(d,'healthy',return_value=False),patch.object(d,'open_pr') as propose:
   for wanted in ['f'*64,self.bundle['release']['id']]:
    with self.assertRaises(ValueError):d.advance_stage('qa',wanted)
   propose.assert_not_called()
 def test_wait_requires_matching_bundle_and_trusted_success(self):
  with patch.object(d,'run',side_effect=self.command),patch.object(d,'healthy',side_effect=lambda revision,env,state='success':state=='success'),patch.object(d,'stage_output') as output:
   d.wait_for_deployment('dev',self.bundle['release']['id'])
   output.assert_called_once()
  with patch.object(d,'run',side_effect=self.command),patch.object(d,'healthy',return_value=True),patch.object(d,'stage_output') as output:
   with self.assertRaisesRegex(ValueError,'failure'):d.wait_for_deployment('qa',self.bundle['release']['id'])
   output.assert_not_called()
 def test_wait_timeout_is_not_reported_as_deployment_success(self):
  with patch.object(d.time,'monotonic',return_value=1),patch.object(d,'stage_output') as output:
   with self.assertRaisesRegex(ValueError,'timed out'):d.wait_for_deployment('qa',self.bundle['release']['id'],timeout=0)
   output.assert_not_called()
 def test_dispatch_waits_for_source_ci_and_rejects_failed_or_untrusted_runs(self):
  source={'repository':'Narx-Space-Prod/commerce-orders','commit':'b'*40,'runId':12}
  record={'head_sha':'b'*40,'event':'push','head_branch':'main','path':'.github/workflows/ci.yml','head_repository':{'full_name':source['repository']}}
  with patch.dict(os.environ,{'GITHUB_EVENT_NAME':'repository_dispatch','GITHUB_EVENT_PATH':'fixture'}),patch.object(d,'read',return_value={'client_payload':source}),patch.object(d.time,'sleep'):
   with patch.object(d,'api',side_effect=[{**record,'status':'in_progress'},{**record,'status':'completed','conclusion':'success'}]) as api:
    d.wait_for_source();self.assertEqual(api.call_count,2)
   for change in [{'status':'completed','conclusion':'failure'},{'head_branch':'feature'}]:
    with patch.object(d,'api',return_value={**record,**change}):
     with self.assertRaises(ValueError):d.wait_for_source()
 def test_production_continuation_rejects_unmerged_and_fork_events(self):
  for event in [{'pull_request':{'merged':False}}, {'pull_request':{'merged':True,'base':{'ref':'main'},'head':{'repo':{'full_name':'attacker/fork'}}}}]:
   with patch.dict(os.environ,{'GITHUB_EVENT_PATH':'fixture'}),patch.object(d,'read',return_value=event),patch.object(d,'run') as run:
    with self.assertRaises(ValueError):d.observe_production()
    run.assert_not_called()

class DeliveryGraph(unittest.TestCase):
 def test_graph_orders_real_smoke_gates_and_keeps_wait_jobs_read_only(self):
  root=Path(__file__).resolve().parents[1]
  workflow=yaml.safe_load((root/'.github/workflows/delivery.yml').read_text())
  jobs=workflow['jobs']
  self.assertEqual(jobs['verify-dev']['needs'],'dev')
  self.assertEqual(jobs['qa']['needs'],['dev','verify-dev'])
  self.assertEqual(jobs['verify-qa']['needs'],['dev','qa'])
  self.assertEqual(jobs['production-pr']['needs'],['dev','verify-qa'])
  for name in ['verify-dev','verify-qa','production']:
   self.assertNotIn('secrets',jobs[name])
   self.assertNotIn('environment',jobs[name])
   self.assertEqual(jobs[name]['permissions']['statuses'],'read')
   self.assertEqual(jobs[name]['permissions']['contents'],'read')
  observer=yaml.safe_load((root/'.github/workflows/delivery-observe.yml').read_text())['jobs']['observe']
  self.assertNotIn('environment',observer)
  self.assertNotIn('secrets',observer)
  self.assertEqual(observer['permissions'],{'contents':'read','statuses':'read','pull-requests':'read'})
  self.assertIn('github.event.pull_request.merged == true',jobs['production']['if'])
  self.assertIn('github.event.pull_request.head.repo.full_name == github.repository',jobs['production']['if'])

class ReleaseSecretContract(unittest.TestCase):
 def test_only_release_stages_receive_named_secret(self):
  import yaml
  root=Path(__file__).resolve().parents[1]
  for filename in ('delivery.yml','recovery.yml'):
   workflow=yaml.safe_load((root/'.github/workflows'/filename).read_text())
   for job in workflow['jobs'].values():
    if job.get('uses','').endswith('/delivery-stage.yml'):
     self.assertEqual(set(job['secrets']),{'NARX_RELEASE_APP_PRIVATE_KEY'})
    else:
     self.assertNotIn('secrets',job)
