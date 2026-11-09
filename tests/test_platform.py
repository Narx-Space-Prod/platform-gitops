"""Delivery/observability contracts. Reuse CI render output to avoid duplicate Helm work."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml

ROOT=Path(__file__).resolve().parents[1]


class PlatformPolicy(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary=tempfile.TemporaryDirectory()
        folder=Path(cls.temporary.name)
        render_dir=os.environ.get('NARX_RENDER_DIR')
        if render_dir:
            folder=Path(render_dir)
        else:
            fixture=folder/'release.json'
            subprocess.run(['python3',str(ROOT/'scripts/delivery.py'),'fixtures',str(fixture)],check=True)
            for env in ['dev','qa','production']:
                args=['helm','template','commerce',str(ROOT/'charts/commerce'),'-n','commerce-'+env,'-f',str(ROOT/'environments'/env/'runtime.yaml'),'-f',str(fixture),'--set','publicOrigin=https://validation.invalid','--set','awsRegion=us-east-1','--set','externalDataCIDRs[0]=10.0.0.0/16']
                (folder/('commerce-'+env+'.yaml')).write_text(subprocess.check_output(args,text=True))
        cls.rendered={e:[d for d in yaml.safe_load_all((folder/('commerce-'+e+'.yaml')).read_text()) if d] for e in ['dev','qa','production']}
        cls.monitoring=yaml.safe_load((ROOT/'monitoring/local-values.yaml').read_text())['integration']

    @classmethod
    def tearDownClass(cls):cls.temporary.cleanup()

    def objects(self,env,kind):return [d for d in self.rendered[env] if d['kind']==kind]

    def test_catalogue_release_leaves_other_service_pod_templates_unchanged(self):
        import copy
        before=json.loads((ROOT/'environments/dev/release.json').read_text())
        after=copy.deepcopy(before)
        after['release']['id']='a'*64
        names=('frontend','catalogue','orders','cart','publisher','worker')
        for name in names:
            after[name]['podAnnotations']['delivery.narx.space/release']='a'*64
        after['catalogue']['image']['digest']='sha256:'+'b'*64
        after['catalogue']['podAnnotations']['delivery.narx.space/source-commit']='c'*40
        with tempfile.TemporaryDirectory() as directory:
            paths=[]
            for label,value in [('before',before),('after',after)]:
                path=Path(directory)/(label+'.json')
                path.write_text(json.dumps(value));paths.append(path)
            for env in ('dev','qa','production'):
                rendered=[]
                for path in paths:
                    text=subprocess.check_output(['helm','template','commerce',str(ROOT/'charts/commerce'),
                        '-n','commerce-'+env,'-f',str(ROOT/'environments'/env/'runtime.yaml'),'-f',str(path)],text=True)
                    rendered.append({d['metadata']['name']:d for d in yaml.safe_load_all(text)
                        if d and d['kind'] in ('Deployment','Rollout') and d['metadata']['name'] in {'commerce-'+n for n in names}})
                changed=[]
                for name,old in rendered[0].items():
                    new=rendered[1][name]
                    if old['spec']['template']!=new['spec']['template']:changed.append(name)
                    self.assertEqual(new['metadata']['annotations']['delivery.narx.space/release'],'a'*64)
                    annotations=new['spec']['template']['metadata'].get('annotations',{})
                    self.assertNotIn('delivery.narx.space/release',annotations)
                    self.assertIn('delivery.narx.space/source-commit',annotations)
                self.assertEqual(changed,['commerce-catalogue'],env)

    def test_public_access_updates_only_frontend_and_configmap(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture=Path(directory)/'release.json'
            fixture.write_text((ROOT/'environments/dev/release.json').read_text())
            values=[]
            for origin in ('https://first.invalid','https://second.invalid'):
                docs=list(yaml.safe_load_all(subprocess.check_output(['helm','template','commerce',str(ROOT/'charts/commerce'),
                    '-n','commerce-dev','-f',str(ROOT/'environments/dev/runtime.yaml'),'-f',str(fixture),
                    '--set-string','global.publicAccess.origin='+origin],text=True)))
                settings=next(d for d in docs if d and d['kind']=='ConfigMap' and d['metadata']['name']=='commerce-settings')
                self.assertEqual(settings['data']['PUBLIC_ORIGIN'],origin)
                self.assertEqual(settings['data']['COOKIE_SECURE'],'true')
                values.append({d['metadata']['name']:d['spec']['template'] for d in docs if d and d['kind'] in ('Deployment','Rollout')})
            self.assertEqual([name for name in values[0] if values[0][name]!=values[1][name]],['commerce-frontend'])

    def test_application_set_is_bounded_to_dedicated_local_cluster(self):
        resources=json.loads((ROOT/'applications/local/commerce.json').read_text())['items']
        self.assertEqual([r['kind'] for r in resources],['AppProject','ApplicationSet'])
        spec=resources[1]['spec']
        self.assertEqual(spec['generators'],[{'list':{'elements':[{'environment':'dev'},{'environment':'qa'},{'environment':'production'}]}}])
        self.assertEqual(spec['goTemplateOptions'],['missingkey=error'])
        self.assertTrue(spec['syncPolicy']['preserveResourcesOnDeletion'])
        self.assertEqual(spec['syncPolicy']['applicationsSync'],'create-update')
        self.assertNotIn('apiVersion',spec['template'])
        self.assertEqual(spec['template']['spec']['destination'],{'name':'narx-local','namespace':'commerce-{{.environment}}'})
        self.assertEqual(resources[0]['spec']['destinations'],[{'name':'narx-local','namespace':'commerce-'+e} for e in ['dev','qa','production']])
        self.assertIn('RespectIgnoreDifferences=true',spec['template']['spec']['syncPolicy']['syncOptions'])
        self.assertEqual(resources[0]['spec']['clusterResourceWhitelist'],[])
        self.assertFalse((ROOT/'applications/aws/commerce.json').exists())

    def test_repository_review_policy_exempts_only_dev_qa_release_files(self):
        policy=json.loads((ROOT/'policy/review-settings.json').read_text())
        self.assertEqual(policy['required_approving_review_count'], 0)
        self.assertTrue(policy['require_code_owner_reviews'])
        self.assertFalse(policy['require_last_push_approval'])
        self.assertTrue(policy['dismiss_stale_reviews'])
        rules=[line.strip() for line in (ROOT/'.github/CODEOWNERS').read_text().splitlines()
               if line.strip() and not line.lstrip().startswith('#')]
        self.assertEqual(rules, ['* @Narx-Space-Prod/code-reviewers', '/environments/dev/release.json', '/environments/qa/release.json'])
        ruleset=json.loads((ROOT/'policy/review-ruleset.json').read_text())
        self.assertEqual(ruleset['enforcement'],'active')
        self.assertEqual(ruleset['bypass_actors'],[])
        self.assertEqual(ruleset['conditions']['ref_name'],{'include':['refs/heads/main'],'exclude':[]})
        reviewer=ruleset['rules'][0]['parameters']['required_reviewers'][0]
        self.assertEqual(reviewer,{'minimum_approvals':1,'file_patterns':['**','!environments/dev/release.json','!environments/qa/release.json'],
                                  'reviewer':{'id':20254182,'type':'Team'}})


    def test_container_ports_declare_protocol_to_avoid_crd_default_drift(self):
        for env in ('dev', 'qa', 'production'):
            workloads=self.objects(env, 'Deployment')+self.objects(env, 'Rollout')
            for workload in workloads:
                if workload['metadata']['name'] not in {'commerce-'+name for name in ('frontend','catalogue','cart','orders','publisher','worker')}:
                    continue
                for container in workload['spec']['template']['spec']['containers']:
                    for port in container.get('ports', []):
                        self.assertEqual(port.get('protocol'), 'TCP', workload['metadata']['name'])

    def test_probes_allow_startup_and_brief_host_latency_without_disabling_health(self):
        for env in ('dev','qa','production'):
            for workload in self.objects(env,'Deployment')+self.objects(env,'Rollout')+self.objects(env,'StatefulSet'):
                for container in workload['spec']['template']['spec']['containers']:
                    for kind in ('readinessProbe','livenessProbe'):
                        if kind in container:
                            self.assertEqual(container[kind]['timeoutSeconds'], 5)
                    if 'startupProbe' in container:
                        probe=container['startupProbe']
                        self.assertGreaterEqual(probe['failureThreshold']*probe['periodSeconds'],300)
                        self.assertEqual(probe['timeoutSeconds'],5)

    def test_service_updates_bound_surge_and_keep_java_roles_sequential(self):
        expected={'orders':0,'cart':1,'catalogue':1,'publisher':3,'worker':4,'frontend':5}
        for env in ('dev','qa','production'):
            workloads=self.objects(env,'Deployment')+self.objects(env,'Rollout')
            for role,wave in expected.items():
                workload=next(w for w in workloads if w['metadata']['name']=='commerce-'+role)
                self.assertEqual(int(workload['metadata']['annotations']['argocd.argoproj.io/sync-wave']),wave)
                self.assertGreater(workload['spec']['progressDeadlineSeconds'],300)
                if role in {'orders','publisher','worker'}:
                    resources=workload['spec']['template']['spec']['containers'][0]['resources']
                    self.assertEqual(str(resources['limits']['cpu']),'1000m')
                    self.assertEqual(resources['limits']['memory'],'448Mi' if role=='orders' else '320Mi')
                if workload['kind']=='Deployment':
                    self.assertEqual(workload['spec']['strategy']['rollingUpdate'],{'maxSurge':1,'maxUnavailable':0})

    def test_each_ordered_surge_fits_local_memory_quota_at_hpa_maximum(self):
        def mib(value):
            return int(value[:-2]) * (1024 if value.endswith('Gi') else 1)
        for env in ('dev','qa','production'):
            workloads=self.objects(env,'Deployment')+self.objects(env,'Rollout')+self.objects(env,'StatefulSet')
            frontend_max=next(iter(self.objects(env,'HorizontalPodAutoscaler')),{}).get('spec',{}).get('maxReplicas',1)
            costs={}
            replicas={}
            for w in workloads:
                name=w['metadata']['name']
                costs[name]={k:sum(mib(c['resources'][k]['memory']) for c in w['spec']['template']['spec']['containers']) for k in ('requests','limits')}
                replicas[name]=frontend_max if name=='commerce-frontend' else w['spec'].get('replicas',1)
            for category,quota in [('requests',1536),('limits',3072)]:
                steady=sum(costs[name][category]*replicas[name] for name in costs)
                waves={}
                for workload in workloads:
                    name=workload['metadata']['name']
                    if name not in {'commerce-'+r for r in ('orders','cart','catalogue','publisher','worker','frontend')}:
                        continue
                    wave=int(workload['metadata']['annotations']['argocd.argoproj.io/sync-wave'])
                    extra=costs[name][category]*(frontend_max if name=='commerce-frontend' else 1)
                    if name=='commerce-frontend' and env!='dev':extra+=32 if category=='requests' else 96
                    waves[wave]=waves.get(wave,0)+extra
                for wave,extra in waves.items():
                    self.assertLessEqual(steady+extra,quota,(env,category,wave,steady+extra,quota))

    def test_hpa_is_bounded_and_argo_leaves_only_frontend_replicas_to_it(self):
        self.assertEqual(self.objects('dev', 'HorizontalPodAutoscaler'), [])
        for env, minimum, maximum in [('qa',1,2), ('production',2,3)]:
            hpas=self.objects(env, 'HorizontalPodAutoscaler')
            self.assertEqual(len(hpas), 1)
            hpa=hpas[0]['spec']
            self.assertEqual(hpa['scaleTargetRef'], {'apiVersion':'argoproj.io/v1alpha1','kind':'Rollout','name':'commerce-frontend'})
            self.assertEqual((hpa['minReplicas'],hpa['maxReplicas']),(minimum,maximum))
            self.assertEqual(hpa['metrics'][0]['resource']['target']['averageUtilization'],70)
            self.assertEqual(hpa['behavior']['scaleDown']['stabilizationWindowSeconds'],300)
            self.assertEqual(hpa['behavior']['scaleUp']['policies'],[{'type':'Pods','value':1,'periodSeconds':60}])
            self.assertNotIn('replicas',self.objects(env,'Rollout')[0]['spec'])
        items=json.loads((ROOT/'applications/local/commerce.json').read_text())['items']
        ignores=items[1]['spec']['template']['spec']['ignoreDifferences']
        replica_rules=[i for i in ignores if '/spec/replicas' in i.get('jsonPointers',[])]
        self.assertEqual(replica_rules,[{'group':'argoproj.io','kind':'Rollout','name':'commerce-frontend','jsonPointers':['/spec/replicas']}])

    def test_cilium_api_access_is_limited_to_rollouts_controller(self):
        self.assertEqual(self.objects('dev', 'CiliumNetworkPolicy'), [])
        for env in ('qa', 'production'):
            policies=self.objects(env, 'CiliumNetworkPolicy')
            self.assertEqual(len(policies), 1)
            self.assertEqual(policies[0]['metadata']['annotations']['argocd.argoproj.io/sync-wave'], '-2')
            spec=policies[0]['spec']
            self.assertEqual(spec['endpointSelector']['matchLabels'],
                             {'app.kubernetes.io/name':'narx-rollouts'})
            self.assertEqual(spec['egress'], [{'toEntities':['kube-apiserver'],
                              'toPorts':[{'ports':[{'port':'443','protocol':'TCP'},
                                                   {'port':'6443','protocol':'TCP'}]}]}])
            self.assertFalse([p for p in self.objects(env, 'NetworkPolicy')
                              if p['metadata']['name']=='commerce-rollouts-api'])

    def test_dev_stays_simple_and_only_frontend_uses_progressive_delivery(self):
        self.assertFalse(self.objects('dev','Rollout'))
        self.assertFalse(self.objects('dev','AnalysisTemplate'))
        self.assertFalse([d for d in self.objects('dev','Deployment') if d['metadata']['name']=='narx-rollouts'])
        for env in ['qa','production']:
            rollouts=self.objects(env,'Rollout')
            self.assertEqual([r['metadata']['name'] for r in rollouts],['commerce-frontend'])
            self.assertFalse([d for d in self.objects(env,'Deployment') if d['metadata']['name']=='commerce-frontend'])
            bg=rollouts[0]['spec']['strategy']['blueGreen']
            self.assertEqual(bg['activeService'],'commerce-frontend')
            self.assertEqual(bg['previewService'],'commerce-frontend-preview')
            self.assertTrue(bg['autoPromotionEnabled'])
            self.assertIn('prePromotionAnalysis',bg)
            self.assertIn('postPromotionAnalysis',bg)
            job=self.objects(env,'AnalysisTemplate')[0]['spec']['metrics'][0]['provider']['job']['spec']
            self.assertGreaterEqual(bg['scaleDownDelaySeconds'],job['activeDeadlineSeconds']+30)
            self.assertEqual(job['backoffLimit'],0)
            self.assertFalse(job['template']['spec']['automountServiceAccountToken'])

    def test_local_production_uses_its_own_data_without_cloud_identity(self):
        prod=yaml.safe_load((ROOT/'environments/production/runtime.yaml').read_text())
        self.assertTrue(prod['localData']['enabled'])
        self.assertFalse(prod['podIdentity']['enabled'])
        self.assertEqual(prod['frontend']['replicaCount'],2)
        claims=[claim for resource in self.objects('production','StatefulSet')
                for claim in resource['spec'].get('volumeClaimTemplates',[])]
        self.assertTrue(claims)
        for resource in self.objects('production','ServiceAccount'):
            self.assertNotIn('eks.amazonaws.com/role-arn',resource['metadata'].get('annotations',{}))

    def test_managed_data_still_requires_secure_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture=Path(directory)/'release.json'
            subprocess.run(['python3',str(ROOT/'scripts/delivery.py'),'fixtures',str(fixture)],check=True)
            result=subprocess.run(['helm','template','commerce',str(ROOT/'charts/commerce'),
                '-f',str(ROOT/'environments/production/runtime.yaml'),'-f',str(fixture),
                '--set','localData.enabled=false'],text=True,capture_output=True)
        self.assertNotEqual(result.returncode,0)
        self.assertIn('managed-data deployments require HTTPS',result.stderr)

    def test_application_and_analysis_images_remain_the_verified_bundle(self):
        for env in self.rendered:
            workloads=self.objects(env,'Deployment')+self.objects(env,'Rollout')
            app={x['metadata']['name']:x['spec']['template']['spec']['containers'][0]['image'] for x in workloads if x['metadata']['name']!='narx-rollouts'}
            self.assertEqual(app['commerce-orders'],app['commerce-publisher'])
            self.assertEqual(app['commerce-orders'],app['commerce-worker'])
            self.assertTrue(all('@sha256:' in image for image in app.values()))
            if env!='dev':
                template=self.objects(env,'AnalysisTemplate')[0]
                image=template['spec']['metrics'][0]['provider']['job']['spec']['template']['spec']['containers'][0]['image']
                self.assertEqual(image,app['commerce-frontend'])

    def test_controller_access_is_namespaced_and_has_no_wildcards(self):
        for env in ['qa','production']:
            self.assertFalse(self.objects(env,'ClusterRole'))
            roles=self.objects(env,'Role')
            self.assertEqual(len(roles),1)
            for rule in roles[0]['rules']:
                for field in ['apiGroups','resources','verbs']:self.assertNotIn('*',rule[field])
                self.assertNotIn('clusteranalysistemplates',rule['resources'])
            binding=self.objects(env,'RoleBinding')[0]
            self.assertEqual(binding['subjects'][0]['namespace'],'commerce-'+env)
            controller=next(d for d in self.objects(env,'Deployment') if d['metadata']['name']=='narx-rollouts')
            self.assertEqual(controller['spec']['template']['spec']['containers'][0]['args'],['--namespaced'])

    def test_monitoring_discovers_only_narx_and_keeps_project_identity(self):
        self.assertEqual(self.monitoring['namespaces'],['commerce-dev','commerce-qa','commerce-production'])
        jobs=self.monitoring['scrapeConfigs']
        self.assertEqual(len({j['job_name'] for j in jobs}),len(jobs))
        for job in jobs:
            for config in job.get('kubernetes_sd_configs',[]):
                self.assertEqual(config['namespaces']['names'],self.monitoring['namespaces'])
                self.assertIn('narx-commerce',config['selectors'][0]['label'])
            if job.get('kubernetes_sd_configs'):
                self.assertIn({'target_label':'project','replacement':'narx-commerce'},job['relabel_configs'])
        self.assertIn('narx-argocd',{j['job_name'] for j in jobs})

    def test_slack_summaries_render_valid_payloads_without_exposing_credentials(self):
        config=json.loads((ROOT/'applications/notifications.json').read_text())['data']
        self.assertIn('url: $narx-slack-webhook',config['service.webhook.slack'])
        for event in ['deployed','failed']:
            trigger=yaml.safe_load(config['trigger.narx-slack-'+event])[0]
            self.assertEqual(trigger['oncePer'],'app.status.operationState.startedAt')
            if event=='deployed':
                self.assertIn("#.hookPhase == 'Succeeded'",trigger['when'])
                self.assertIn("app.status.sync.status == 'Synced'",trigger['when'])
                self.assertNotIn('syncResult.revision == app.status.sync.revision',trigger['when'])
            body=yaml.safe_load(config['template.narx-slack-'+event])['webhook']['slack']['body']
            with tempfile.TemporaryDirectory() as directory:
                chart=Path(directory);(chart/'templates').mkdir()
                (chart/'Chart.yaml').write_text('apiVersion: v2\nname: notification-check\nversion: 0.1.0\n')
                (chart/'body.txt').write_text(body)
                (chart/'templates/config.yaml').write_text('apiVersion: v1\nkind: ConfigMap\nmetadata: {name: notification-check}\ndata:\n  message: {{ tpl (.Files.Get "body.txt") .Values | quote }}\n')
                (chart/'values.yaml').write_text(yaml.safe_dump({'app':{'metadata':{'name':'narx-commerce-qa','labels':{'delivery.narx.space/environment':'qa'}},'status':{'sync':{'revision':'b'*40},'operationState':{'syncResult':{'revision':'a'*40}}}}}))
                rendered=yaml.safe_load(subprocess.check_output(['helm','template','message',str(chart)],text=True))
                message=json.loads(rendered['data']['message'])['text']
                self.assertIn('Narx qa',message)
                self.assertIn('a'*40,message)
                self.assertNotIn('b'*40,message)
                self.assertNotIn('webhook',message)

    def test_deployment_events_use_trusted_observer_and_named_slack_webhook(self):
        appset=json.loads((ROOT/'applications/local/commerce.json').read_text())['items'][1]
        annotations=appset['spec']['template']['metadata']['annotations']
        for event in ('deployed','failed'):
            self.assertEqual(annotations['notifications.argoproj.io/subscribe.narx-slack-'+event+'.slack'],'')
            self.assertNotIn('notifications.argoproj.io/subscribe.narx-slack-'+event+'.webhook',annotations)
        workflow=yaml.load((ROOT/'.github/workflows/commerce.yml').read_text(),Loader=yaml.BaseLoader)
        self.assertNotIn('status',workflow['on'])
        self.assertNotIn('concurrency',workflow)
        recovery=yaml.load((ROOT/'.github/workflows/recovery.yml').read_text(),Loader=yaml.BaseLoader)
        self.assertNotIn('status',recovery['on'])
        self.assertEqual(recovery['on']['repository_dispatch']['types'],['recovery-requested'])
        self.assertNotIn('concurrency',recovery)
        self.assertNotIn('concurrency',recovery['jobs']['recovery'])
        guard=recovery['jobs']['recovery']['if']
        for clause in ("github.event.sender.login == 'narx-local-commerce-release[bot]'",
                       'github.event.sender.id == 352511280', 'github.event.client_payload.environment',
                       "github.event_name == 'repository_dispatch'", 'refs/heads/main',
                       'qa', 'production'):
            self.assertIn(clause,guard)
        config=json.loads((ROOT/'applications/notifications.json').read_text())['data']
        for event in ('healthy','failed'):
            template=yaml.safe_load(config['template.narx-'+event])['github']
            self.assertIn('.app.status.operationState.syncResult.revision',template['revisionPath'])
            self.assertNotIn('.app.status.sync.revision',template['revisionPath'])

    def test_dashboard_environment_filter_covers_every_monitored_environment(self):
        expected={name.removeprefix('commerce-') for name in self.monitoring['namespaces']}
        for dashboard in self.monitoring['dashboards'].values():
            variables=dashboard.get('templating',{}).get('list',[])
            environment=next(variable for variable in variables if variable['name']=='environment')
            self.assertEqual(set(environment['query'].split(',')),expected)
            self.assertIn(environment['current']['value'],expected)
            self.assertTrue(environment['includeAll'])

    def test_dashboard_and_alert_queries_are_project_scoped(self):
        for dashboard in self.monitoring['dashboards'].values():
            for panel in dashboard['panels']:
                for target in panel['targets']:self.assertIn('project="narx-commerce"',target['expr'])
        rules=self.monitoring['ruleGroups'][0]['rules']
        for rule in rules:
            self.assertIn('project="narx-commerce"',rule['expr'])
            self.assertEqual(rule['labels']['project'],'narx-commerce')
            self.assertTrue(rule['annotations']['runbook_url'].endswith('#local-monitoring-and-progressive-delivery'))
        self.assertIn('NarxOutboxStalled',{r['alert'] for r in rules})
        self.assertIn('NarxRolloutDegraded',{r['alert'] for r in rules})
        self.assertIn('NarxArgoApplicationDegraded',{r['alert'] for r in rules})


if __name__=='__main__':unittest.main()
