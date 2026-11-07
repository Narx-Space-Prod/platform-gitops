#!/usr/bin/env python3
"""Read-only CI evidence bridge for the central release graph; never runs source code."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import time

REPOS = {'Narx-Space-Prod/commerce-'+name for name in ('frontend','catalogue','orders')}
PREFIX = 'Application / '

def api(path):
    return json.loads(subprocess.check_output(['gh','api',path],text=True))

def identity(event):
    source=event.get('client_payload',{})
    repo,commit,run_id=(source.get(k) for k in ('repository','commit','runId'))
    if repo not in REPOS or not isinstance(commit,str) or not re.fullmatch('[a-f0-9]{40}',commit) or type(run_id) is not int or run_id<=0:
        raise ValueError('Invalid source CI identity')
    return repo,commit,run_id

def validate(record,repo,commit):
    if not (record.get('head_sha')==commit and record.get('event')=='push' and record.get('head_branch')=='main'
            and record.get('path')=='.github/workflows/ci.yml' and record.get('head_repository',{}).get('full_name')==repo):
        raise ValueError('Evidence is not protected-main application CI')

def required(gate,repo):
    if gate=='tests':
        names=[PREFIX+'01 - Source policy / Workflow lint',PREFIX+'02 - Build test and scan / Build and test']
        if repo.endswith('/commerce-orders'): names.append(PREFIX+'02 - Cart build test and scan / Build and test')
        return names
    if gate=='quality': return [PREFIX+'03 - Sonar quality gate / Source quality gate']
    return []

def observe(gate,event,timeout=1800):
    repo,commit,run_id=identity(event)
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        record=api(f'repos/{repo}/actions/runs/{run_id}')
        validate(record,repo,commit)
        # Fetch all attempts' current jobs with pagination (Orders adds companion jobs).
        jobs=json.loads(subprocess.check_output(['gh','api','--paginate','--slurp',f'repos/{repo}/actions/runs/{run_id}/jobs?per_page=100'],text=True))
        by_name={j['name']:j for page in jobs for j in page['jobs']}
        selected=[by_name.get(name) for name in required(gate,repo)]
        if any(j and j.get('status')=='completed' and j.get('conclusion')!='success' for j in selected):
            raise ValueError('Required CI gate failed: '+gate)
        if record.get('status')=='completed' and record.get('conclusion')!='success':
            raise ValueError('Application CI failed or was cancelled')
        if gate != 'publication' and record.get('status')=='completed' and any(j is None for j in selected):
            raise ValueError('Completed source CI is missing required jobs')
        ready=bool(selected) and all(j and j.get('conclusion')=='success' for j in selected)
        if gate=='publication':
            ready=record.get('status')=='completed' and record.get('conclusion')=='success'
            # A workflow-only/no-image run cannot claim publication.
            if ready:
                published=any(j['name'].startswith(PREFIX+'04 - ') and j.get('conclusion')=='success' for j in by_name.values())
                with open(os.environ['GITHUB_OUTPUT'],'a') as output:
                    output.write('deploy='+str(published).lower()+'\n')
                if not published:
                    with open(os.environ['GITHUB_STEP_SUMMARY'],'a') as output:
                        output.write('No runtime image changed: CI passed; deployment stages are intentionally skipped.\n')
                    return False
        if ready:
            with open(os.environ['GITHUB_STEP_SUMMARY'],'a') as output:
                output.write(f'### Verified {gate}\n\n[Source CI run {run_id}](https://github.com/{repo}/actions/runs/{run_id}) · `{commit}`\n\n')
                for j in selected:
                    output.write(f"- [{j['name']}]({j['html_url']}): success\n")
            return
        time.sleep(15)
    raise ValueError('Source gate timed out: '+gate)

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('gate',choices=['tests','quality','publication'])
    args=parser.parse_args()
    observe(args.gate,json.loads(Path(os.environ['GITHUB_EVENT_PATH']).read_text()))
