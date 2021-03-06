from collections import defaultdict
import json
from osbs.utils import strip_registry_from_image
import sys


class BuildTree(object):
    def __init__(self, builds):
        self.deps = defaultdict(set)
        self.seen = set()
        self.when = {}
        builds = [build for build in builds
                  if ('status' in build and
                      'startTimestamp' in build['status'])]
        builds.sort(key=lambda x: x['status']['startTimestamp'],
                    reverse=True)
        for build in builds:
            self.add(build)

    def add(self, build):
        try:
            annotations = build['metadata']['annotations']
            base_image_name = annotations['base-image-name']
            repositories = json.loads(annotations['repositories'])
            when = build['status']['startTimestamp']
        except KeyError:
            return

        repos = set([strip_registry_from_image(repo)
                     for repo in repositories['primary']])
        duplicates = self.seen.intersection(repos)
        repos -= duplicates
        self.seen.update(repos)
        self.deps[strip_registry_from_image(base_image_name)].update(repos)
        for repo in repos:
            self.when[repo] = when

    def _trim_layers(self, base):
        layers = self.deps[base]
        excess = set()
        for layer in layers:
            name, version = layer.split(':', 1)
            if version == 'latest':
                pass
            elif not self.deps.get(layer):
                # Leaf node
                excess.add(layer)

        self.deps[base] -= excess
        return excess

    def trim_excess_tags(self):
        while True:
            images = [image for image in self.deps.keys()]
            any_trimmed = False
            for base in images:
                if self.deps.get(base):
                    if self._trim_layers(base):
                        any_trimmed = True

            if not any_trimmed:
                break

    def __repr__(self):
        return repr(self.deps)

    def as_graph_easy_txt(self, include_datestamp=False):
        txt = ''
        def formatwhen(name):
            if include_datestamp:
                try:
                    return "\\n{when}".format(when=self.when[name][:10])
                except KeyError:
                    return ""
            else:
                return ""

        for base, layers in self.deps.items():
            for layer in layers:
                txt += "[ {base}{when} ]".format(base=base,
                                                 when=formatwhen(base))
                txt += " --> "
                txt += "[ {layer}{when} ]".format(layer=layer,
                                                  when=formatwhen(layer))
                txt += "\n"

        return txt


def run(inputfile=None):
    if inputfile is not None:
        with open(inputfile) as fp:
            builds = json.load(fp)
    else:
        builds = json.load(sys.stdin)

    tree = BuildTree(builds)
    tree.trim_excess_tags()
    print(tree.as_graph_easy_txt(include_datestamp=True))


if __name__ == '__main__':
    try:
        run(sys.argv[1])
    except IndexError:
        run()
