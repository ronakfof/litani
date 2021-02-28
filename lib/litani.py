# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License").
# You may not use this file except in compliance with the License.
# A copy of the License is located at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# or in the "license" file accompanying this file. This file is distributed
# on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either
# express or implied. See the License for the specific language governing
# permissions and limitations under the License.


import contextlib
import json
import logging
import os
import pathlib
import sys


CACHE_FILE = "cache.json"
CACHE_POINTER = ".litani_cache_dir"
CI_STAGES = ["build", "test", "report"]
JOBS_DIR = "jobs"
RUN_FILE = "run.json"
TIME_FORMAT_R = "%Y-%m-%dT%H:%M:%SZ"
TIME_FORMAT_W = "%Y-%m-%dT%H:%M:%SZ"
VERSION_MAJOR, VERSION_MINOR, VERSION_PATCH = 1, 3, 0
VERSION = "%d.%d.%d" % (VERSION_MAJOR, VERSION_MINOR, VERSION_PATCH)



class ExpireableDirectory:
    """This class is to mark directories as being safe for garbage collection"""

    def __init__(self, path: pathlib.Path):
        self._touchfile = path.resolve() / ".litani-expired"


    def expire(self):
        self._touchfile.touch()


    def is_expired(self):
        return self._touchfile.exists()



def _get_cache_dir(path=os.getcwd()):
    def cache_pointer_dirs():
        current = pathlib.Path(path).resolve(strict=True)
        yield current
        while current.parent != current:
            current = current.parent
            yield current
        current = pathlib.Path(os.getcwd()).resolve(strict=True)
        for root, _, dirs in os.walk(current):
            for dyr in dirs:
                yield pathlib.Path(os.path.join(root, dyr))

    for possible_dir in cache_pointer_dirs():
        logging.debug(
            "Searching for cache pointer in %s", possible_dir)
        possible_pointer = possible_dir / CACHE_POINTER
        try:
            if possible_pointer.exists():
                logging.debug(
                    "Found a cache pointer at %s", possible_pointer)
                with open(possible_pointer) as handle:
                    pointer = handle.read().strip()
                possible_cache = pathlib.Path(pointer)
                if possible_cache.exists():
                    logging.debug("cache is at %s", possible_cache)
                    return possible_cache
                logging.warning(
                    "Found a cache file at %s pointing to %s, but that "
                    "directory does not exist. Continuing search...",
                    possible_pointer, possible_cache)
        except PermissionError:
            pass

    logging.error(
        "Could not find a pointer to a litani cache. Did you forget "
        "to run `litani init`?")
    raise FileNotFoundError


def get_cache_dir(path=os.getcwd()):
    try:
        return _get_cache_dir(path)
    except FileNotFoundError:
        sys.exit(1)


def get_report_dir():
    return get_cache_dir() / "html"


def get_report_data_dir():
    return get_cache_dir() / "report_data"

def get_artifacts_dir():
    return get_cache_dir() / "artifacts"


def get_status_dir():
    return get_cache_dir() / "status"


@contextlib.contextmanager
def atomic_write(path, mode="w"):
    try:
        parent = pathlib.Path(path).parent
        parent.mkdir(exist_ok=True, parents=True)
        tmp = "%s~" % path
        handle = open(tmp, mode)
        yield handle
    except RuntimeError:
        try:
            os.unlink(tmp)
        except RuntimeError:
            pass
    else:
        handle.flush()
        handle.close()
        try:
            os.rename(tmp, path)
        except RuntimeError:
            os.unlink(tmp)


def add_jobs_to_cache():
    """Adds individual Litani jobs in the jobs directory to the cache file

    `litani add-job` adds jobs to individual JSON files so that it's possible to
    run multiple parallel invocations of `litani add-job`. When all jobs have
    been added, this method should be called so that all of the individual JSON
    job files get added to the single cache file, ready to be run.
    """
    jobs = []
    cache_dir = get_cache_dir()
    jobs_dir = cache_dir / JOBS_DIR
    for job_file in os.listdir(jobs_dir):
        with open(jobs_dir / job_file) as handle:
            jobs.append(json.load(handle))

    with open(cache_dir / CACHE_FILE) as handle:
        cache = json.load(handle)
    cache["jobs"] = jobs
    with atomic_write(cache_dir / CACHE_FILE) as handle:
        print(json.dumps(cache, indent=2), file=handle)
