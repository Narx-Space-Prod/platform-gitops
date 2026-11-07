"""Release policy and recovery invariants, without registry or cloud access."""
import copy
import base64
import json
import os
import subprocess
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import delivery as d


class DeliveryPolicy(unittest.TestCase):
    def setUp(self):
        self.facts = {name: {'component': name, 'repository': 'Narx-Space-Prod/' + repo,
            'image': 'ghcr.io/narx-space-prod/commerce-' + name,
            'digest': 'sha256:' + 'a' * 64, 'commit': 'b' * 40, 'runId': 12}
            for name, repo in d.COMPONENTS.items()}

    def test_attestation_reuse_is_process_local_and_bound_to_source_digest_and_policy(self):
        fact=self.facts['frontend']
        predicate={k:fact[k] for k in ('repository','commit','component','runId')}
        predicate.update(workflow='.github/workflows/ci.yml',ref='refs/heads/main')
        statement={'subject':[{'digest':{'sha256':fact['digest'].removeprefix('sha256:')}}],
                   'predicateType':d.PREDICATE,'predicate':predicate}
        envelope=json.dumps({'payload':base64.b64encode(json.dumps(statement).encode()).decode()})
        builders={'workflowCommits':['c'*40]}
        d._VERIFIED_IMAGES.clear()
        self.addCleanup(d._VERIFIED_IMAGES.clear)
        with patch.object(d,'read',return_value=builders),patch.object(d,'run',return_value=envelope) as run:
            d.verify_image(fact);d.verify_image(copy.deepcopy(fact))
            self.assertEqual(run.call_count,2)  # provenance + SBOM, once each
            for change in ({'digest':'sha256:'+'d'*64},{'commit':'e'*40},{'runId':13}):
                with self.assertRaises(ValueError):d.verify_image({**fact,**change})
            self.assertEqual(len(d._VERIFIED_IMAGES),1)  # failed results never enter the cache
            calls=run.call_count
            builders['workflowCommits']=['f'*40]
            d.verify_image(fact)
            self.assertEqual(run.call_count,calls+2)  # new trust policy forces both checks
            d._VERIFIED_IMAGES.clear()
            calls=run.call_count;d.verify_image(fact)
            self.assertEqual(run.call_count,calls+2)  # new invocation verifies again

    def test_automatic_release_review_scope_rejects_human_forks_and_renames(self):
        author={'login':'narx-local-commerce-release[bot]','id':352511280,'type':'Bot'}
        files=[{'filename':'environments/dev/release.json','status':'modified'}]
        repo='Narx-Space-Prod/platform-gitops'
        d.validate_review_scope(files, author, repo)
        for changed_author,changed_repo,changed_files in [
            ({**author,'id':1},repo,files), (author,'other/fork',files),
            (author,repo,[{**files[0],'status':'removed'}]),
            (author,repo,[{**files[0],'previous_filename':'environments/production/release.json'}]),
            (author,repo,files+[{'filename':'environments/qa/release.json','status':'modified'}])]:
            with self.assertRaises(ValueError):
                d.validate_review_scope(changed_files,changed_author,changed_repo)
        # Owned files retain GitHub's code-owner review requirement.
        d.validate_review_scope([{'filename':'environments/production/release.json'}], {}, repo)

    def test_trusted_verifier_fetches_history_for_deployment_evidence(self):
        import yaml
        workflow=yaml.safe_load((d.ROOT/'.github/workflows/release-verification.yml').read_text())
        checkout=next(step for step in workflow['jobs']['verify']['steps'] if step.get('uses','').startswith('actions/checkout@'))
        self.assertEqual(checkout['with']['ref'],'${{ github.event.pull_request.base.sha }}')
        self.assertEqual(checkout['with']['fetch-depth'],0)
        self.assertFalse(checkout['with']['persist-credentials'])

    def test_all_components_and_background_roles_share_verified_images(self):
        value = d.bundle(self.facts)
        self.assertTrue(d.validate(value))
        self.assertEqual(value['orders']['image'], value['publisher']['image'])
        self.assertEqual(value['orders']['image'], value['worker']['image'])
        with self.assertRaises(ValueError):
            d.bundle({'frontend': self.facts['frontend']})

    def test_mutable_unrelated_and_tampered_image_identities_are_rejected(self):
        for field, value in [('digest', 'latest'), ('image', 'docker.io/attacker/orders'),
                             ('image','ghcr.io/attacker/commerce-orders'),
                             ('image','ghcr.io/narx-space-prod/commerce-frontend'),
                             ('image','123456789012.dkr.ecr.us-east-1.amazonaws.com/narx/commerce-orders'),
                             ('repository', 'other/commerce-orders'), ('commit', 'main'), ('runId', True)]:
            fact = dict(self.facts['orders'], **{field: value})
            with self.subTest(field=field), self.assertRaises(ValueError):
                d.facts_valid(fact)
        candidate = d.bundle(self.facts)
        candidate['worker']['image']['digest'] = 'sha256:' + '0' * 64
        with self.assertRaises(ValueError):
            d.validate(candidate)

    def test_promotion_preserves_every_digest_and_records_previous_healthy_state(self):
        current = d.bundle(self.facts)
        next_facts = copy.deepcopy(self.facts)
        next_facts['frontend']['digest'] = 'sha256:' + 'c' * 64
        revision = 'd' * 40
        with patch.object(d, 'healthy', return_value=True):
            value = d.next_bundle(current, next_facts, revision, 'qa')
            self.assertEqual(value['release']['components'], next_facts)
            previous = value['release']['previous']
            self.assertEqual(previous['components'], current['release']['components'])
            self.assertEqual(previous['gitopsCommit'], revision)
            restored = d.rollback(value, 'qa')
            self.assertEqual(restored['release']['components'], current['release']['components'])
            self.assertIsNone(restored['release']['previous'], 'Rollback must not create oscillating recovery loops')
        with patch.object(d, 'healthy', return_value=False), self.assertRaises(ValueError):
            d.next_bundle(current, next_facts, revision, 'qa')

    def test_rollback_requires_correct_environment_and_trusted_previous_evidence(self):
        previous = {'components': copy.deepcopy(self.facts), 'gitopsCommit': 'd' * 40, 'environment': 'qa'}
        value = d.bundle(self.facts, previous)
        with self.assertRaises(ValueError):
            d.rollback(value, 'production')
        with patch.object(d, 'healthy', return_value=False), self.assertRaises(ValueError):
            d.rollback(value, 'qa')

    def test_forged_and_superseded_rollout_statuses_cannot_authorize_promotion(self):
        good = {'context': 'narx/deploy-qa', 'state': 'success', 'creator': {'login': 'observer[bot]'}}
        bad = dict(good, state='failure')
        with patch.dict('os.environ', {'NARX_DEPLOYMENT_BOT_LOGIN': 'observer[bot]'}), patch.object(d, 'deployment_revisions', return_value=['d'*40]):
            with patch.object(d, 'api', return_value=[good]):
                self.assertTrue(d.healthy('d' * 40, 'qa'))
            with patch.object(d, 'api', return_value=[bad, good]):
                self.assertFalse(d.healthy('d' * 40, 'qa'))
            with patch.object(d, 'api', return_value=[dict(good, creator={'login': 'developer'})]):
                self.assertFalse(d.healthy('d' * 40, 'qa'))

    def test_newer_pending_or_failed_deployment_blocks_historical_success(self):
        good={'context':'narx/deploy-qa','state':'success','creator':{'login':'observer[bot]'}}
        with patch.dict('os.environ', {'NARX_DEPLOYMENT_BOT_LOGIN':'observer[bot]'}), patch.object(d, 'deployment_revisions', return_value=['d'*40,'c'*40]):
            for state in ('pending','failure','error'):
                with self.subTest(state=state), patch.object(d, 'api', side_effect=[[dict(good,state=state)],[good]]) as api:
                    self.assertFalse(d.healthy('d'*40, 'qa'))
                    self.assertEqual(api.call_count, 1)
            with patch.object(d, 'api', side_effect=[[],[good]]):
                self.assertTrue(d.healthy('d'*40, 'qa'))

    def test_deployment_history_stops_at_latest_environment_configuration_change(self):
        with tempfile.TemporaryDirectory() as directory:
            previous=Path.cwd()
            try:
                os.chdir(directory)
                subprocess.run(['git','init','--initial-branch=main'],check=True,stdout=subprocess.DEVNULL)
                d.run('git','config','user.name','Test');d.run('git','config','user.email','test@example.invalid')
                Path('charts').mkdir();Path('charts/chart.yaml').write_text('initial')
                d.run('git','add','.');d.run('git','commit','-m','initial')
                anchor=d.run('git','rev-parse','HEAD')
                Path('environments/production').mkdir(parents=True)
                Path('environments/production/release.json').write_text('{}')
                d.run('git','add','.');d.run('git','commit','-m','unrelated production promotion')
                head=d.run('git','rev-parse','HEAD')
                self.assertEqual(d.deployment_revisions(head,'qa'),[head,anchor])
                self.assertEqual(d.deployment_revisions(head,'production'),[head])
                Path('charts/chart.yaml').write_text('new runtime')
                d.run('git','add','.');d.run('git','commit','-m','runtime change')
                changed=d.run('git','rev-parse','HEAD')
                self.assertEqual(d.deployment_revisions(changed,'qa'),[changed])
            finally:
                os.chdir(previous)

    def test_only_completed_protected_main_ci_counts_as_source_evidence(self):
        fact = self.facts['frontend']
        run = {'conclusion': 'success', 'event': 'push', 'head_branch': 'main', 'head_sha': fact['commit'],
               'path': '.github/workflows/ci.yml', 'head_repository': {'full_name': fact['repository']}}
        with patch.object(d, 'api', return_value=run):
            self.assertTrue(d.successful_run(fact))
        for mutation in [{'event': 'pull_request'}, {'conclusion': 'failure'}, {'head_branch': 'feature'}, {'head_sha': '0'*40}]:
            with self.subTest(mutation=mutation), patch.object(d, 'api', return_value=dict(run, **mutation)):
                self.assertFalse(d.successful_run(fact))

    def test_candidate_cannot_skip_upstream_or_invent_rollback_evidence(self):
        active = d.bundle(self.facts)
        states = {e: copy.deepcopy(active) for e in d.ENVIRONMENTS}
        changed = copy.deepcopy(self.facts)
        changed['frontend']['digest'] = 'sha256:' + 'e' * 64
        revision = 'd' * 40
        with patch.object(d, 'healthy', return_value=True), patch.object(d, 'successful_run', return_value=True), patch.object(d, 'verify_image') as verify:
            for environment in ['qa', 'production']:
                with self.subTest(environment=environment), self.assertRaises(ValueError):
                    d.verify_candidate(d.next_bundle(active, changed, revision, environment), environment, revision, states)
            candidate = d.next_bundle(active, changed, revision, 'dev')
            d.verify_candidate(candidate, 'dev', revision, states)
            self.assertEqual(verify.call_count, 4)
            candidate['release']['previous']['gitopsCommit'] = 'f' * 40
            with self.assertRaises(ValueError):
                d.verify_candidate(candidate, 'dev', revision, states)

    def test_candidate_requires_healthy_upstream_and_successful_source_ci(self):
        bootstrap = {'release': {'ready': False, 'id': '', 'components': {}, 'previous': None},
                     **{alias: {'enabled': False} for alias in d.ALIASES}}
        active = d.bundle(self.facts)
        states = {'dev': active, 'qa': bootstrap, 'production': bootstrap}
        with patch.object(d, 'healthy', return_value=False), self.assertRaises(ValueError):
            d.verify_candidate(active, 'qa', 'd' * 40, states)
        with patch.object(d, 'healthy', return_value=True), patch.object(d, 'successful_run', return_value=False), self.assertRaises(ValueError):
            d.verify_candidate(active, 'qa', 'd' * 40, states)
        states['qa'] = active
        with self.assertRaises(ValueError):
            d.verify_candidate(bootstrap, 'qa', 'd' * 40, states)

    def test_out_of_order_build_completion_does_not_select_an_older_commit(self):
        old = dict(self.facts['frontend'], commit='1'*40)
        new = dict(self.facts['frontend'], commit='2'*40)
        records = [{'tag_name': 'frontend-'+f['commit'], 'assets': [{'name':'release.json','size':1024,'id':i}]} for i,f in enumerate([old,new])]
        def api(path):
            if '/compare/' in path:
                return {'status':'ahead'}
            return records if '/commerce-frontend/' in path else []
        def run(*args, **kwargs):
            if '--paginate' in args:
                return json.dumps([records if '/commerce-frontend/' in args[-1] else []])
            return json.dumps([old,new][int(args[-1].rsplit('/',1)[1])])
        with patch.object(d, 'api', side_effect=api), patch.object(d, 'run', side_effect=run), patch.object(d, 'successful_run', return_value=True), patch.object(d, 'verify_image') as verify:
            self.assertEqual(d.released_facts()['frontend'], new)
            verify.assert_called_once_with(new)

    def test_open_release_pr_refreshes_main_without_force_push_or_duplicate_pr(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            remote, work = root/'remote.git', root/'work'
            subprocess.run(['git','init','--bare',str(remote)],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            subprocess.run(['git','init','--initial-branch=main',str(work)],check=True,stdout=subprocess.DEVNULL)
            previous_directory = Path.cwd()
            original_run = d.run
            existing, created = [], []
            def command(*args, **kwargs):
                if args[0] != 'gh':
                    return original_run(*args, **kwargs)
                if args[1:3] == ('pr','list'):
                    return json.dumps(existing)
                if args[1:3] == ('pr','view'):
                    return json.dumps({'state':'OPEN','headRefOid':original_run('git','rev-parse','HEAD'),'autoMergeRequest':None})
                if args[1:3] == ('pr','create'):
                    created.append(args)
                return ''
            try:
                os.chdir(work)
                original_run('git','config','user.name','Policy Test')
                original_run('git','config','user.email','policy@example.invalid')
                original_run('git','remote','add','origin',str(remote))
                Path('README.md').write_text('initial')
                original_run('git','add','README.md')
                original_run('git','commit','-m','initial')
                original_run('git','push','-u','origin','main')
                value = d.bundle(self.facts)
                branch = 'release/dev-' + value['release']['id'][:16]
                with patch.object(d, 'ROOT', work), patch.object(d, 'run', side_effect=command):
                    d.open_pr('dev', value, 'release')
                    self.assertEqual(original_run('git','log','-1','--format=%an <%ae>'),
                                     'narx-local-commerce-release[bot] <352511280+narx-local-commerce-release[bot]@users.noreply.github.com>')
                    original_run('git','switch','main')
                    original_run('git','branch','-D',branch)
                    Path('README.md').write_text('unrelated main change')
                    original_run('git','add','README.md')
                    original_run('git','commit','-m','advance main')
                    original_run('git','push','origin','main')
                    existing.append({'state':'OPEN','number':1})
                    d.open_pr('dev', value, 'release')
                    original_run('git','merge-base','--is-ancestor','origin/main','HEAD')
                    self.assertEqual(json.loads((work/'environments/dev/release.json').read_text()),value)
                    self.assertEqual(len(created),1)
            finally:
                os.chdir(previous_directory)

    def test_auto_merge_is_limited_to_dev_qa_and_pins_the_commit(self):
        for environment in ('dev', 'qa', 'production'):
            with self.subTest(environment=environment), patch.object(d, 'run', side_effect=lambda *a: 'a'*40 if a[0]=='git' else json.dumps({'state':'OPEN','headRefOid':'a'*40,'autoMergeRequest':None})) as command:
                d.enable_release_auto_merge(environment, 'release/example')
                if environment == 'production':
                    command.assert_not_called()
                else:
                    command.assert_any_call('gh', 'pr', 'merge', 'release/example', '--repo',
                                            'Narx-Space-Prod/platform-gitops', '--auto', '--squash',
                                            '--match-head-commit', 'a'*40)
        with self.assertRaises(ValueError):
            d.enable_release_auto_merge('unknown', 'release/example')

    def test_reconcile_waits_for_pending_target_without_bypassing_health(self):
        facts=copy.deepcopy(self.facts)
        facts['catalogue']['digest']='sha256:'+'c'*64
        states={e:d.bundle(self.facts) for e in d.ENVIRONMENTS}
        states['dev']=d.bundle(facts)
        def read(path):return states[Path(path).parent.name]
        with patch.object(d,'run',side_effect=lambda *args,**kwargs: json.dumps(states[args[2].split('environments/')[1].split('/')[0]]) if args[:2]==('git','show') else 'd'*40), patch.object(d,'read',side_effect=read), \
             patch.object(d,'released_facts',return_value=facts), \
             patch.object(d,'healthy',side_effect=lambda revision,env,state='success': env=='dev'), \
             patch.object(d,'open_pr') as propose:
            d.reconcile('reconcile')
            propose.assert_not_called()

    def test_auto_merge_waits_for_exact_head_and_does_not_merge_a_different_head(self):
        def state(head, **extra):
            return json.dumps(dict(state='OPEN',headRefOid=head,autoMergeRequest=None,**extra))
        with patch.object(d,'run',side_effect=['a'*40,state('b'*40),state('a'*40),'']) as run, patch.object(d.time,'sleep'):
            d.enable_release_auto_merge('qa','release/test')
            self.assertEqual(run.call_args.args[-1],'a'*40)
        with patch.object(d,'run',side_effect=['a'*40]+[state('b'*40)]*5) as run, patch.object(d.time,'sleep'):
            with self.assertRaisesRegex(ValueError,'expected commit'):
                d.enable_release_auto_merge('dev','release/test')
            self.assertFalse(any(c.args[1:3]==('pr','merge') for c in run.call_args_list))

    def test_auto_merge_is_idempotent_and_recovers_from_github_mutation_race(self):
        enabled=json.dumps({'state':'OPEN','headRefOid':'a'*40,'autoMergeRequest':{'enabledAt':'now'}})
        with patch.object(d,'run',side_effect=['a'*40,enabled]) as run:
            d.enable_release_auto_merge('dev','release/test')
            self.assertEqual(run.call_count,2)
        merged=json.dumps({'state':'MERGED','headRefOid':'a'*40,'autoMergeRequest':None})
        opened=json.dumps({'state':'OPEN','headRefOid':'a'*40,'autoMergeRequest':None})
        with patch.object(d,'run',side_effect=['a'*40,opened,subprocess.CalledProcessError(1,['gh']),merged]), patch.object(d.time,'sleep'):
            d.enable_release_auto_merge('qa','release/test')

    def test_bootstrap_is_non_deployable_and_has_no_fake_images(self):
        value = {'release': {'ready': False, 'id': '', 'components': {}, 'previous': None},
                 **{alias: {'enabled': False} for alias in d.ALIASES}}
        self.assertFalse(d.validate(value))
        self.assertEqual(value['release']['components'], {})
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)/'fixture.json'
            d.fixtures(target)
            self.assertTrue(d.validate(d.read(target)))


if __name__ == '__main__':
    unittest.main()
