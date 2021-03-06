from calendar import timegm
from collections import defaultdict, namedtuple
import json
import os
import re
import subprocess
import sys
from time import ctime, gmtime, strftime, strptime


FIELDS = [('name', 'name'),
          ('image', 'image'),
          ('completion', 'completion'),
          ('state', 'state'),
          ('throughput', 'throughput'),
          ('pending', 'pending'),
          ('running', 'running'),
          ('pull_base_image', 'plugin_pull_base_image'),
          ('distgit_fetch_artefacts', 'plugin_distgit_fetch_artefacts'),
          ('dockerfile_content', 'docker_build'),
          ('squash', 'plugin_squash'),
          ('compress', 'plugin_compress'),
          ('pulp_push', 'plugin_pulp_push'),
          ('upload_size_mb', 'upload_size_mb'),
          ('failed_plugin', 'failed_plugin'),
          ('exception', 'exception')]
Metrics = namedtuple('Metrics', [field[0] for field in FIELDS])


def rfc3339_time(rfc3339):
    time_tuple = strptime(rfc3339, '%Y-%m-%dT%H:%M:%SZ')
    return timegm(time_tuple)


class ThroughputModel(object):
    def __init__(self, window):
        self.builds = []
        self.start_time = None  # start of window
        self.window = window

    def append(self, timestamp):
        if not self.builds:
            self.start_time = timestamp

        self.builds.append(timestamp)
        while self.builds[-1] - self.builds[0] >= self.window:
            del self.builds[0]

        return len(self.builds)


class ConcurrentModel(object):
    def __init__(self):
        self.start_finish = []
        self.finish_times = []

    def append(self, start, finish):
        self.start_finish.append((start, finish))

    def get_nbuilds(self):
        for start, finish in self.start_finish:
            while self.finish_times:
                if start < self.finish_times[0]:
                    break

                yield (self.finish_times[0], len(self.finish_times) - 1)
                del self.finish_times[0]

            self.finish_times.append(finish)
            self.finish_times.sort()
            yield (start, len(self.finish_times))


class MissingLog(Exception):
    pass


class BuildLog(object):
    def __init__(self, logfile):
        self.logfile = logfile
        self.data = None
        self._trawl()

    def _trawl(self):
        if self.data is not None:
            return

        cache = self.logfile + '.cache'
        try:
            with open(cache) as cf:
                self.data = json.load(cf)
                return
        except (IOError, ValueError):
            pass

        self.data = {
            'upload_size_mb': 'nan',
        }
        name_re = re.compile(r'selflink.: u./oapi/v1/namespaces/default/builds/([^,]*).,')
        size_re = re.compile(r' - dockpulp - INFO - uploading a (.*)M image')
        plugin_re = re.compile(r'([0-9 :-]*),[0-9]+ - atomic_reactor.plugin - DEBUG - running plugin \'(.*)\'')
        error_re = re.compile(r'ERROR - .*plugin \'(.*)\' raised an exception: ([^(]*)')
        image_re = re.compile(r'pulp_push - INFO - image names: \[.*\'([^\']*):latest')
        buildfail_re = re.compile(r'INFO - build was unsuccess?ful')
        with open(self.logfile) as lf:
            log = lf.read()
            if len(log) < 50:
                raise MissingLog

            name = name_re.search(log)
            if name:
                self.data['name'] = name.groups()[0]

            size = size_re.search(log)
            if size:
                self.data['upload_size_mb'] = size.groups()[0]

            image = image_re.search(log)
            if image:
                self.data['image'] = image.groups()[0]

            last_plugin = None
            plugins = plugin_re.findall(log)
            for timestamp, plugin_name in plugins:
                t = timegm(strptime(timestamp, "%Y-%m-%d %H:%M:%S"))
                if last_plugin is not None:
                    self.data[last_plugin[1]] = t - last_plugin[0]

                last_plugin = (t, plugin_name)

            error = error_re.search(log)
            if error:
                (self.data['failed_plugin'],
                 self.data['exception']) = error.groups()
            else:
                buildfail = buildfail_re.search(log)
                if buildfail:
                    self.data['failed_plugin'] = 'build'
                    self.data['exception'] = ''

        with open(cache, 'w') as cf:
            json.dump(self.data, cf, indent=2)


class Builds(object):
    def __init__(self, builds):
        self.builds = builds
        self.metrics_require_logs = int(os.environ.get('METRICS_REQUIRE_LOGS',
                                                       1))

    def fetch_log(self, name):
        if not self.metrics_require_logs:
            raise MissingLog

        logfile = "{name}.log".format(name=name)
        if not os.access(logfile, os.R_OK):
            cmd = ['osbs',
                   'build-logs',
                   name]
            with open(logfile, 'w') as fp:
                print(' '.join(cmd))
                p = subprocess.Popen(cmd, stdout=fp)
                p.communicate()

        return BuildLog(logfile).data

    def get_stats(self):
        builds_examined = 0
        earliest_completion = None
        latest_completion = None
        states = defaultdict(int)
        tputmodel = ThroughputModel(60 * 60)
        missing = []
        results = {
            'archived': [],
            'current': [],
            'concurrent': [],
        }

        # Sort by time created
        builds = [build for build in self.builds
                  if 'completionTimestamp' in build['status']]
        builds.sort(key=lambda x: x['status']['completionTimestamp'])

        tput = 0
        for build in builds:
            name = build['metadata']['name']
            completionTimestamp = build['status']['completionTimestamp']
            completion = rfc3339_time(completionTimestamp)
            timestamp = strftime("%Y-%m-%d %H:%M:%S", gmtime(completion))
            if earliest_completion is None:
                earliest_completion = latest_completion = completion

            latest_completion = completion

            state = build['status']['phase']
            states[state] += 1
            creationTimestamp = build['metadata']['creationTimestamp']
            creation = rfc3339_time(creationTimestamp)
            startTimestamp = build['status'].get('startTimestamp')
            if startTimestamp is None:
                continue

            start = rfc3339_time(startTimestamp)
            pending = start - creation
            build_log = {}
            if pending < 0:
                which = 'archived'
                pending = upload_size_mb = 'nan'
            else:
                which = 'current'
                try:
                    build_log = self.fetch_log(name)
                except MissingLog:
                    missing.append(name)

            duration = build['status'].get('duration', 0) / 1000000000
            plugins = {name: 'nan'
                       for name in ['pull_base_image',
                                    'distgit_fetch_artefacts',
                                    'dockerfile_content',
                                    'squash',
                                    'compress',
                                    'pulp_push']}
            plugins.update({name: build_log.get(name, '')
                            for name in ['image',
                                         'failed_plugin',
                                         'exception']})

            if state == 'Complete':
                # Count this towards throughput
                tput = tputmodel.append(completion)

                if which == 'current':
                    annotations = build['metadata'].get('annotations', {})
                    tar_metadata = annotations.get('tar_metadata')
                    if tar_metadata:
                        md = json.loads(tar_metadata)
                        upload_size_mb = md['size'] / (1024 * 1024)
                    else:
                        upload_size_mb = build_log.get('upload_size_mb', 'nan')

                    for plugin in plugins.keys():
                        try:
                            plugins[plugin] = build_log[plugin]
                        except KeyError:
                            pass

                metrics = Metrics(name=name,
                                  completion=timestamp,
                                  state=state,
                                  throughput=tput,
                                  pending=pending,
                                  running=duration,
                                  upload_size_mb=upload_size_mb,
                                  **plugins)
                results[which].append(metrics)
            elif state == 'Failed':
                metrics = Metrics(name=name,
                                  completion=timestamp,
                                  state=state,
                                  throughput=tput,
                                  pending=pending,
                                  running=duration,
                                  upload_size_mb='nan',
                                  **plugins)
                results[which].append(metrics)

            builds_examined += 1

        # Now sort by time started
        builds = [build for build in self.builds
                  if ('startTimestamp' in build['status'] and
                      'completionTimestamp' in build['status'])]
        builds.sort(key=lambda x: x['status']['startTimestamp'])
        cmodel = ConcurrentModel()
        for build in builds:
            startTimestamp = build['status']['startTimestamp']
            start = rfc3339_time(startTimestamp)
            completionTimestamp = build['status']['completionTimestamp']
            completion = rfc3339_time(completionTimestamp)
            cmodel.append(start, completion)

        results['concurrent'].extend(
                [(strftime("%Y-%m-%d %H:%M:%S", gmtime(timestamp)), nbuilds)
                 for (timestamp, nbuilds) in cmodel.get_nbuilds()])

        for which, data in results.items():
            if which == 'concurrent':
                with open("metrics-concurrent.csv", "w") as fp:
                    fp.write("timestamp,nbuilds\n")
                    for result in data:
                        fp.write(",".join([str(m) for m in result]) + '\n')
            else:
                with open("metrics-{which}.csv".format(which=which), "w") as fp:
                    fp.write(",".join([field[1] for field in FIELDS]) + '\n')
                    for result in data:
                        fp.write(",".join([str(m) for m in result]) + '\n')

        return {
            'builds examined': builds_examined,
            'earliest_completion': ctime(earliest_completion),
            'latest_completion': ctime(latest_completion),
            'states': states,
            'missing-log': missing,
        }

        
def run(inputfile=None):
    if inputfile is not None:
        with open(inputfile) as fp:
            builds = json.load(fp)
    else:
        builds = json.load(sys.stdin)

    print(json.dumps(Builds(builds).get_stats(), sort_keys=True, indent=2))


if __name__ == '__main__':
    try:
        run(sys.argv[1])
    except IndexError:
        run()
