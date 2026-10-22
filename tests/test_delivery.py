"""Release policy and recovery invariants, without registry or cloud access."""
import copy
import json
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
            'image': '123456789012.dkr.ecr.us-east-1.amazonaws.com/narx/commerce-' + name,
            'digest': 'sha256:' + 'a' * 64, 'commit': 'b' * 40, 'runId': 12}
            for name, repo in d.COMPONENTS.items()}

    def test_all_components_and_background_roles_share_verified_images(self):
        value = d.bundle(self.facts)
        self.assertTrue(d.validate(value))
        self.assertEqual(value['orders']['image'], value['publisher']['image'])
        self.assertEqual(value['orders']['image'], value['worker']['image'])
        with self.assertRaises(ValueError):
            d.bundle({'frontend': self.facts['frontend']})

    def test_mutable_unrelated_and_tampered_image_identities_are_rejected(self):
        for field, value in [('digest', 'latest'), ('image', 'docker.io/attacker/orders'),
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
        with patch.dict('os.environ', {'NARX_DEPLOYMENT_BOT_LOGIN': 'observer[bot]'}):
            with patch.object(d, 'api', return_value=[good]):
                self.assertTrue(d.healthy('d' * 40, 'qa'))
            with patch.object(d, 'api', return_value=[bad, good]):
                self.assertFalse(d.healthy('d' * 40, 'qa'))
            with patch.object(d, 'api', return_value=[dict(good, creator={'login': 'developer'})]):
                self.assertFalse(d.healthy('d' * 40, 'qa'))

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
