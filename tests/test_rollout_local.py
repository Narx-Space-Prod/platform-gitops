"""Opt-in real Rollouts recovery test. Only a fresh disposable namespace in kind."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
NAMESPACE = 'narx-rollout-validation'
IMAGE = 'narx-local/frontend:dev'


@unittest.skipUnless(os.environ.get('NARX_ROLLOUT_TEST') == '1', 'Opt-in kind Rollouts exercise')
class RolloutRecovery(unittest.TestCase):
    def test_preview_rejection_and_post_switch_traffic_recovery(self):
        context = os.environ.get('NARX_KUBE_CONTEXT', '')
        self.assertTrue(context.startswith('kind-'), 'An explicit kind context is required')
        base = ['kubectl', '--context', context]
        def kubectl(*args, input=None):
            return subprocess.check_output([*base, *args], input=input, text=True).strip()
        def get(kind, name):
            return json.loads(kubectl('-n', NAMESPACE, 'get', kind, name, '-o', 'json'))
        def wait(condition, seconds=210):
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                try:
                    if condition(): return
                except (subprocess.CalledProcessError, OSError, KeyError): pass
                time.sleep(2)
            self.fail('Timed out: ' + json.dumps(get('rollout', 'commerce-frontend').get('status', {})))
        self.assertFalse(kubectl('get', 'namespace', NAMESPACE, '--ignore-not-found', '-o', 'name'), 'Refusing to reuse an existing namespace')
        subprocess.run(['kind','load','docker-image',IMAGE,'--name',context.removeprefix('kind-')],check=True)
        kubectl('create','namespace',NAMESPACE)
        try:
            with tempfile.TemporaryDirectory() as directory:
                fixture=Path(directory)/'release.json'
                subprocess.run(['python3',str(ROOT/'scripts/delivery.py'),'fixtures',str(fixture)],check=True)
                rendered=subprocess.check_output(['helm','template','commerce',str(ROOT/'charts/commerce'),'-n',NAMESPACE,'-f',str(ROOT/'environments/qa/runtime.yaml'),'-f',str(fixture),'--set-json','global.imagePullSecrets=[]'],text=True)
            keep = {('ServiceAccount','narx-rollouts'),('Role','narx-rollouts'),('RoleBinding','narx-rollouts'),('Deployment','narx-rollouts'),('ServiceAccount','commerce-frontend'),('Service','commerce-frontend'),('Service','commerce-frontend-preview'),('Rollout','commerce-frontend'),('AnalysisTemplate','commerce-frontend-verify')}
            docs=[d for d in yaml.safe_load_all(rendered) if d and (d['kind'],d['metadata']['name']) in keep]
            rollout=next(d for d in docs if d['kind']=='Rollout')
            container=rollout['spec']['template']['spec']['containers'][0]
            container['image']=IMAGE
            container['imagePullPolicy']='IfNotPresent'
            container['resources']={'requests':{'cpu':'25m','memory':'32Mi'},'limits':{'cpu':'250m','memory':'96Mi'}}
            container['env']=[{'name':'REVISION','value':'stable'},{'name':'FAIL_MODE','value':'none'}]
            container['command']=['node','--input-type=module','-e']
            container['args']=["""
              import http from 'node:http';
              http.createServer((req,res)=>{
                const active=!req.headers.host.includes('-preview');
                const fail=process.env.FAIL_MODE==='pre'||(process.env.FAIL_MODE==='post'&&active);
                if(req.url==='/api/products'){
                  res.writeHead(fail?500:200,{'Content-Type':'application/json'});
                  res.end(JSON.stringify([{id:'fixture',price:100,currency:'INR'}]));
                } else if(req.url==='/revision') res.end(process.env.REVISION);
                else if(req.url==='/') res.end('<html>isolated rollout fixture</html>');
                else res.end('ok');
              }).listen(8080,'0.0.0.0');
            """]
            template=next(d for d in docs if d['kind']=='AnalysisTemplate')
            jobcontainer=template['spec']['metrics'][0]['provider']['job']['spec']['template']['spec']['containers'][0]
            jobcontainer['image']=IMAGE
            jobcontainer['imagePullPolicy']='IfNotPresent'
            kubectl('-n',NAMESPACE,'apply','-f','-',input=yaml.safe_dump_all(docs))
            wait(lambda:get('rollout','commerce-frontend').get('status',{}).get('phase')=='Healthy')
            stable=get('service','commerce-frontend')['spec']['selector']['rollouts-pod-template-hash']
            def revision():
                return kubectl('get','--raw','/api/v1/namespaces/'+NAMESPACE+'/services/http:commerce-frontend:80/proxy/revision')
            wait(lambda:revision()=='stable')
            for mode in ['pre','post']:
                container['env']=[{'name':'REVISION','value':'bad-'+mode},{'name':'FAIL_MODE','value':mode}]
                kubectl('-n',NAMESPACE,'apply','-f','-',input=json.dumps(rollout))
                wait(lambda:get('rollout','commerce-frontend').get('status',{}).get('abort') is True)
                wait(lambda:get('service','commerce-frontend')['spec']['selector']['rollouts-pod-template-hash']==stable)
                wait(lambda:revision()=='stable')
                print(mode+'-promotion rejection retained/restored stable traffic',flush=True)
            container['env']=[{'name':'REVISION','value':'recovered'},{'name':'FAIL_MODE','value':'none'}]
            kubectl('-n',NAMESPACE,'apply','-f','-',input=json.dumps(rollout))
            wait(lambda:get('rollout','commerce-frontend').get('status',{}).get('phase')=='Healthy' and not get('rollout','commerce-frontend').get('status',{}).get('abort'))
            wait(lambda:revision()=='recovered')
            print('Healthy candidate passed preview and active analysis; traffic promoted',flush=True)
        finally:
            kubectl('delete','namespace',NAMESPACE,'--wait=false')


if __name__=='__main__':unittest.main()
