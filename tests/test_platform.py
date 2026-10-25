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

    def test_application_set_is_bounded_to_two_local_environments(self):
        resources=json.loads((ROOT/'applications/local/commerce.json').read_text())['items']
        self.assertEqual([r['kind'] for r in resources],['AppProject','ApplicationSet'])
        spec=resources[1]['spec']
        self.assertEqual(spec['generators'],[{'list':{'elements':[{'environment':'dev'},{'environment':'qa'}]}}])
        self.assertEqual(spec['goTemplateOptions'],['missingkey=error'])
        self.assertTrue(spec['syncPolicy']['preserveResourcesOnDeletion'])
        self.assertEqual(spec['syncPolicy']['applicationsSync'],'create-update')
        self.assertNotIn('apiVersion',spec['template'])
        self.assertEqual(spec['template']['spec']['destination']['server'],'https://kubernetes.default.svc')
        self.assertIn('RespectIgnoreDifferences=true',spec['template']['spec']['syncPolicy']['syncOptions'])
        self.assertEqual(resources[0]['spec']['clusterResourceWhitelist'],[])
        cloud=json.loads((ROOT/'applications/aws/commerce.json').read_text())['items'][1]
        self.assertEqual(cloud['spec']['destination'],{'name':'narx-production','namespace':'commerce-production'})

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
        self.assertEqual(self.monitoring['namespaces'],['commerce-dev','commerce-qa'])
        jobs=self.monitoring['scrapeConfigs']
        self.assertEqual(len({j['job_name'] for j in jobs}),len(jobs))
        for job in jobs:
            for config in job.get('kubernetes_sd_configs',[]):
                self.assertEqual(config['namespaces']['names'],self.monitoring['namespaces'])
                self.assertIn('narx-commerce',config['selectors'][0]['label'])
            if job.get('kubernetes_sd_configs'):
                self.assertIn({'target_label':'project','replacement':'narx-commerce'},job['relabel_configs'])
        self.assertIn('narx-argocd',{j['job_name'] for j in jobs})

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
