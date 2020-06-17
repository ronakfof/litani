import datetime
import enum
import functools
import json
import logging
import os
import pathlib
import re
import subprocess
import sys
import tempfile

import jinja2

from lib import litani


def get_run(cache_dir):
    with open(cache_dir / litani.CACHE_FILE) as handle:
        ret = json.load(handle)
    ret["pipelines"] = {}

    for job in ret["jobs"]:
        status_file = litani.get_status_dir() / ("%s.json" % job["job_id"])
        try:
            with open(str(status_file)) as handle:
                status = json.load(handle)
        except FileNotFoundError:
            status = {
                "complete": False,
                "wrapper_arguments": job,
            }

        pipeline_name = status["wrapper_arguments"]["pipeline_name"]
        ci_stage = status["wrapper_arguments"]["ci_stage"]

        try:
            ret["pipelines"][pipeline_name]["ci_stages"][ci_stage]["jobs"].append(status)
        except KeyError:
            try:
                ret["pipelines"][pipeline_name]["ci_stages"][ci_stage]["jobs"] = [status]
            except KeyError:
                try:
                    ret["pipelines"][pipeline_name]["ci_stages"][ci_stage] = {
                        "jobs": [status]
                    }
                except KeyError:
                    try:
                        ret["pipelines"][pipeline_name]["ci_stages"] = {
                            ci_stage: {
                                "name": ci_stage,
                                "jobs": [status],
                            }
                        }
                    except KeyError:
                        ret["pipelines"][pipeline_name] = {
                            "ci_stages":  {ci_stage: {"jobs": [status]}},
                            "name": pipeline_name,
                        }
    ret.pop("jobs")
    return ret


def job_sorter(j1, j2):
    if not (j1["complete"] or j2["complete"]):
        return 0
    if not j1["complete"]:
        return -1
    if not "start_time" in j1 or "start_time" in j2:
        return 0
    if not "start_time" in j1:
        return -1
    return j1["start_time"] < j2["start_time"]


class StageStatus(enum.IntEnum):
    FAIL = 0
    FAIL_IGNORED = 1
    SUCCESS = 2


def add_stage_stats(stage, stage_name, pipeline_name):
    n_complete_jobs = len([j for j in stage["jobs"] if j["complete"]])
    stage["progress"] = int(n_complete_jobs * 100 / len(stage["jobs"]))
    stage["complete"] = n_complete_jobs == len(stage["jobs"])
    status = StageStatus.SUCCESS
    for job in stage["jobs"]:
        try:
            if not job["complete"]:
                continue
            elif job["wrapper_return_code"]:
                status = StageStatus.FAIL
            elif job["command_return_code"] and status == StageStatus.SUCCESS:
                status = StageStatus.FAIL_IGNORED
            elif job["timeout_reached"] and status == StageStatus.SUCCESS:
                status = StageStatus.FAIL_IGNORED
        except KeyError:
            print(json.dumps(stage, indent=2))
            sys.exit(1)
    stage["status"] = status.name.lower()
    stage["url"] = "artifacts/%s/%s/" % (pipeline_name, stage_name)
    stage["name"] = stage_name


class PipeStatus(enum.IntEnum):
    FAIL = 0
    IN_PROGRESS = 1
    SUCCESS = 2


def add_pipe_stats(pipe):
    pipe["url"] = "pipelines/%s/index.html" % pipe["name"]
    incomplete = [s for s in pipe["ci_stages"] if not s["complete"]]
    if incomplete:
        pipe["status"] = PipeStatus.IN_PROGRESS
    else:
        pipe["status"] = PipeStatus.SUCCESS
    for stage in pipe["ci_stages"]:
        if stage["status"] in ["fail", "fail_ignored"]:
            pipe["status"] = PipeStatus.FAIL
            break


def add_run_stats(run):
    status = PipeStatus.SUCCESS
    if [p for p in run["pipelines"] if p["status"] == PipeStatus.IN_PROGRESS]:
        status = PipeStatus.IN_PROGRESS
    if [p for p in run["pipelines"] if p["status"] == PipeStatus.FAIL]:
        status = PipeStatus.FAIL
    run["status"] = status.name.lower()
    for pipe in run["pipelines"]:
        pipe["status"] = pipe["status"].name.lower()


def add_job_stats(jobs):
    for job in jobs:
        if not ("start_time" in job and "end_time" in job):
            job["duration_str"] = None
        else:
            s = datetime.datetime.strptime(
                job["start_time"], litani.TIME_FORMAT_R)
            e = datetime.datetime.strptime(
                job["end_time"], litani.TIME_FORMAT_R)
            seconds = (e - s).seconds
            job["duration_str"] = s_to_hhmmss(seconds)
            job["duration"] = seconds


def sort_run(run):
    pipelines = []
    js = functools.cmp_to_key(job_sorter)
    for pipe in run["pipelines"].values():
        stages = []
        for stage in litani.CI_STAGES:
            try:
                pipeline_stage = pipe["ci_stages"][stage]
            except KeyError:
                pipe["ci_stages"][stage] = {"jobs"}
                pipeline_stage = pipe["ci_stages"][stage]
            jobs = sorted(pipeline_stage["jobs"], key=js)
            add_job_stats(jobs)
            pipeline_stage["jobs"] = jobs
            add_stage_stats(pipeline_stage, stage, pipe["name"])
            stages.append(pipeline_stage)

        pipe["ci_stages"] = stages
        add_pipe_stats(pipe)
        pipelines.append(pipe)
    pipelines = sorted(pipelines, key=lambda p: p["status"])
    run["pipelines"] = pipelines
    add_run_stats(run)


def get_run_data(cache_dir):
    run = get_run(cache_dir)
    sort_run(run)
    return run


def s_to_hhmmss(s):
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    if h:
        return "{h:02d}h {m:02d}m {s:02d}s".format(h=h, m=m, s=s)
    if m:
        return "{m:02d}m {s:02d}s".format(m=m, s=s)
    return "{s:02d}s".format(s=s)


def get_stats_groups(run):
    ret = {}
    for pipe in run["pipelines"]:
        for stage in pipe["ci_stages"]:
            for job in stage["jobs"]:
                if "tags" not in job["wrapper_arguments"]:
                    continue
                if not job["wrapper_arguments"]["tags"]:
                    continue
                stats_group = None
                for tag in job["wrapper_arguments"]["tags"]:
                    kv = tag.split(":")
                    if kv[0] != "stats-group":
                        continue
                    stats_group = kv[1]
                if not stats_group:
                    continue

                if "duration" not in job:
                    continue
                record = {
                    "pipeline": job["wrapper_arguments"]["pipeline_name"],
                    "duration": job["duration"]
                }
                try:
                    ret[stats_group].append(record)
                except KeyError:
                    ret[stats_group] = [record]
    return sorted([(k, v) for k, v in ret.items()])


def to_id(string):
    allowed = re.compile(r"[-a-zA-Z0-9\.]")
    return "".join([c if allowed.match(c) else "_" for c in string])


def render_runtimes(run, env, report_dir):
    stats_groups = get_stats_groups(run)
    urls = []
    gnu_templ = env.get_template("runtime-box.jinja.gnu")
    img_dir = report_dir / "runtimes"
    img_dir.mkdir(exist_ok=True, parents=True)
    for group_name, jobs in stats_groups:
        if len(jobs) < 2:
            continue
        group_id = to_id(group_name)
        url = img_dir / ("%s.svg" % group_id)
        tmp_url = "%s~" % str(url)
        gnu_file = gnu_templ.render(
            group_name=group_name, jobs=jobs, url=tmp_url)
        with tempfile.NamedTemporaryFile("w") as tmp:
            print(gnu_file, file=tmp)
            tmp.flush()
            cmd = ["gnuplot", tmp.name]
            subprocess.run(
                cmd, check=True, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL)
        os.rename(tmp_url, url)
        urls.append(str(url.relative_to(report_dir)))
    return urls


def render(run, report_dir):
    template_dir = pathlib.Path(__file__).parent.parent / "templates"
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(template_dir)))

    runtime_urls = render_runtimes(run, env, report_dir)

    dash_templ = env.get_template("dashboard.jinja.html")
    page = dash_templ.render(run=run, runtimes=runtime_urls)
    with litani.atomic_write(report_dir / "index.html") as handle:
        print(page, file=handle)

    pipe_templ = env.get_template("pipeline.jinja.html")
    for pipe in run["pipelines"]:
        page = pipe_templ.render(run=run, pipe=pipe)
        with litani.atomic_write(report_dir / pipe["url"]) as handle:
            print(page, file=handle)