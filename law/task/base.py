# -*- coding: utf-8 -*-

"""
Custom luigi base task definitions.
"""


__all__ = ["Task", "WrapperTask", "ExternalTask"]


import sys
import socket
import logging
import warnings
from collections import OrderedDict
from contextlib import contextmanager
from abc import abstractmethod

import luigi
import luigi.util
import six

from law.parameter import NO_STR, TaskInstanceParameter, CSVParameter
from law.parser import global_cmdline_values
from law.util import (
    abort, colored, uncolored, make_list, query_choice, multi_match, flatten, check_bool_flag,
    BaseStream,
)


logger = logging.getLogger(__name__)


class BaseRegister(luigi.task_register.Register):

    def __new__(metacls, classname, bases, classdict):
        # default attributes
        classdict.setdefault("exclude_db", False)

        # union "exclude_params_*" sets with those of all base classes
        for base in bases:
            for attr, base_params in vars(base).items():
                if isinstance(base_params, set) and attr.startswith("exclude_params_"):
                    params = classdict.setdefault(attr, set())
                    params |= base_params

        return super(BaseRegister, metacls).__new__(metacls, classname, bases, classdict)


@six.add_metaclass(BaseRegister)
class BaseTask(luigi.Task):

    exclude_db = True
    exclude_params_db = set()
    exclude_params_req = set()
    exclude_params_req_pass = set()
    exclude_params_req_get = set()

    @staticmethod
    def resource_name(name, host=None):
        if host is None:
            host = socket.gethostname().partition(".")[0]
        return "{}_{}".format(host, name)

    @classmethod
    def get_param_values(cls, *args, **kwargs):
        values = super(BaseTask, cls).get_param_values(*args, **kwargs)
        if six.callable(cls.modify_param_values):
            return cls.modify_param_values(OrderedDict(values)).items()
        else:
            return values

    @classmethod
    def modify_param_values(cls, params):
        return params

    @classmethod
    def req(cls, *args, **kwargs):
        return cls(**cls.req_params(*args, **kwargs))

    @classmethod
    def req_params(cls, inst, _exclude=None, _prefer_cli=None, **kwargs):
        # common/intersection params
        params = luigi.util.common_params(inst, cls)

        # determine parameters to exclude
        _exclude = set() if _exclude is None else set(make_list(_exclude))

        # also use this class' req and req_get sets
        # and the req and req_pass sets of the instance's class
        _exclude.update(cls.exclude_params_req, cls.exclude_params_req_get)
        _exclude.update(inst.exclude_params_req, inst.exclude_params_req_pass)

        # remove excluded parameters
        for name in list(params.keys()):
            if multi_match(name, _exclude, any):
                del params[name]

        # add kwargs
        params.update(kwargs)

        # remove params that are preferably set via cli class arguments
        if _prefer_cli:
            cls_args = []
            prefix = cls.task_family + "_"
            if luigi.cmdline_parser.CmdlineParser.get_instance():
                for key in global_cmdline_values().keys():
                    if key.startswith(prefix):
                        cls_args.append(key[len(prefix):])
            for name in make_list(_prefer_cli):
                if name in params and name in cls_args:
                    del params[name]

        return params

    def complete(self):
        outputs = [t for t in flatten(self.output()) if not t.optional]

        if len(outputs) == 0:
            msg = "task {!r} has either no non-optional outputs or no custom complete() method"
            warnings.warn(msg.format(self), stacklevel=2)
            return False

        return all(t.exists() for t in outputs)

    def walk_deps(self, max_depth=-1, order="level"):
        # see https://en.wikipedia.org/wiki/Tree_traversal
        if order not in ("level", "pre"):
            raise ValueError("unknown traversal order '{}', use 'level' or 'pre'".format(order))

        tasks = [(self, 0)]
        while len(tasks):
            task, depth = tasks.pop(0)
            if max_depth >= 0 and depth > max_depth:
                continue
            deps = luigi.task.flatten(task.requires())

            yield (task, deps, depth)

            deps = ((d, depth + 1) for d in deps)
            if order == "level":
                tasks[len(tasks):] = deps
            elif order == "pre":
                tasks[:0] = deps

    def cli_args(self, exclude=None, replace=None):
        if exclude is None:
            exclude = set()
        if replace is None:
            replace = {}

        args = []
        for name, param in self.get_params():
            if multi_match(name, exclude, any):
                continue
            raw = replace.get(name, getattr(self, name))
            val = param.serialize(raw)
            arg = "--{}".format(name.replace("_", "-"))
            if isinstance(param, luigi.BoolParameter):
                if raw:
                    args.extend([arg])
            elif isinstance(param, (luigi.IntParameter, luigi.FloatParameter)):
                args.extend([arg, str(val)])
            else:
                args.extend([arg, "\"{}\"".format(val)])

        return args

    @abstractmethod
    def run(self):
        return


class Register(BaseRegister):

    def __call__(cls, *args, **kwargs):
        inst = super(Register, cls).__call__(*args, **kwargs)

        # check for interactive parameters
        for param in inst.interactive_params:
            value = getattr(inst, param)
            if value:
                try:
                    logger.debug("evaluating interactive parameter '{}' with value '{}'".format(
                        param, value))
                    getattr(inst, "_" + param)(*value)
                except KeyboardInterrupt:
                    print("\naborted")
                abort("", exitcode=0)

        return inst


@six.add_metaclass(Register)
class Task(BaseTask):

    log_file = luigi.Parameter(default=NO_STR, significant=False, description="a custom log file, "
        "default: <task.default_log_file>")
    print_deps = CSVParameter(default=[], significant=False, description="print task dependencies, "
        "do not run any task, the passed numbers set the recursion depth (0 means non-recursive)")
    print_status = CSVParameter(default=[], significant=False, description="print the task status, "
        "do not run any task, the passed numbers set the recursion depth (0 means non-recursive) "
        "and optionally the collection depth")
    remove_output = CSVParameter(default=[], significant=False, description="remove all outputs, "
        "do not run any task, the passed number sets the recursion depth (0 means non-recursive)")

    interactive_params = ["print_deps", "print_status", "remove_output"]

    message_cache_size = 10

    exclude_db = True
    exclude_params_req = set(interactive_params)

    def __init__(self, *args, **kwargs):
        super(Task, self).__init__(*args, **kwargs)

        # cache for messages published to the scheduler
        self._message_cache = []

        # cache for the last progress published to the scheduler
        self._last_progress_percentage = None

    @property
    def default_log_file(self):
        return "-"

    def publish_message(self, *args):
        msg = " ".join(str(arg) for arg in args)
        print(msg)
        sys.stdout.flush()

        self._publish_message(*args)

    def _publish_message(self, *args):
        msg = " ".join(str(arg) for arg in args)

        # add to message cache and handle overflow
        msg = uncolored(msg)
        self._message_cache.append(msg)
        if self.message_cache_size >= 0:
            end = max(len(self._message_cache) - self.message_cache_size, 0)
            del self._message_cache[:end]

        # set status message using the current message cache
        self.set_status_message("\n".join(self._message_cache))

    def create_message_stream(self, *args, **kwargs):
        return TaskMessageStream(self, *args, **kwargs)

    @contextmanager
    def publish_step(self, msg, success_message="done", fail_message="failed"):
        self.publish_message(msg)
        success = False
        try:
            yield
            success = True
        finally:
            self.publish_message(success_message if success else fail_message)

    def publish_progress(self, percentage, precision=0):
        percentage = round(percentage, precision)
        if percentage != self._last_progress_percentage:
            self._last_progress_percentage = percentage
            self.set_progress_percentage(percentage)

    def colored_repr(self, color=True):
        family = self._repr_family(self.task_family, color=color)

        parts = [self._repr_param(*pair, color=color) for pair in self._repr_params(color=color)]
        parts += [self._repr_flag(flag, color=color) for flag in self._repr_flags(color=color)]

        return "{}({})".format(family, ", ".join(parts))

    def _repr_params(self, color=True):
        # build key value pairs of all significant parameters
        params = self.get_params()
        param_values = self.get_param_values(params, [], self.param_kwargs)
        param_objs = dict(params)

        pairs = []
        for param_name, param_value in param_values:
            if param_objs[param_name].significant:
                pairs.append((param_name, param_objs[param_name].serialize(param_value)))

        return pairs

    def _repr_flags(self, color=True):
        return []

    @classmethod
    def _repr_family(cls, family, color=True):
        return colored(family, "green") if color else family

    @classmethod
    def _repr_param(cls, name, value, color=True):
        return "{}={}".format(colored(name, color="blue", style="bright") if color else name, value)

    @classmethod
    def _repr_flag(cls, name, color=True):
        return colored(name, color="magenta") if color else name

    def create_progress_callback(self, n_total, reach=(0, 100)):
        def make_callback(n, start, end):
            def callback(i):
                self.publish_progress(start + (i + 1) / float(n) * (end - start))
            return callback

        if isinstance(n_total, (list, tuple)):
            width = 100. / len(n_total)
            reaches = [(width * i, width * (i + 1)) for i in range(len(n_total))]
            return n_total.__class__(make_callback(n, *r) for n, r in zip(n_total, reaches))
        else:
            return make_callback(n_total, *reach)

    def _print_deps(self, *args, **kwargs):
        return print_task_deps(self, *args, **kwargs)

    def _print_status(self, *args, **kwargs):
        return print_task_status(self, *args, **kwargs)

    def _remove_output(self, *args, **kwargs):
        return remove_task_output(self, *args, **kwargs)


class WrapperTask(Task):
    """
    Use for tasks that only wrap other tasks and that by definition are done
    if all their requirements exist.
    """
    exclude_db = True

    def _repr_flags(self, color=True):
        return super(WrapperTask, self)._repr_flags(color=color) + ["wrapper"]

    def complete(self):
        return all(task.complete() for task in flatten(self.requires()))

    def run(self):
        return


class ExternalTask(Task):

    exclude_db = True

    run = None

    def _repr_flags(self, color=True):
        return super(ExternalTask, self)._repr_flags(color=color) + ["external"]


class ProxyTask(BaseTask):

    task = TaskInstanceParameter()

    exclude_params_req = {"task"}


class TaskMessageStream(BaseStream):

    def __init__(self, task, stdout=True):
        super(TaskMessageStream, self).__init__()
        self.task = task
        self.stdout = stdout

    def _write(self, *args):
        if self.stdout:
            self.task.publish_message(*args)
        else:
            self.task._publish_message(*args)


def getreqs(struct):
    # same as luigi.task.getpaths but for requires()
    if isinstance(struct, Task):
        return struct.requires()
    elif isinstance(struct, dict):
        r = struct.__class__()
        for k, v in six.iteritems(struct):
            r[k] = getreqs(v)
        return r
    else:
        try:
            s = list(struct)
        except TypeError:
            raise Exception("Cannot map {} to Task/dict/list".format(struct))

        return struct.__class__(getreqs(r) for r in s)


def print_task_deps(task, max_depth=1):
    max_depth = int(max_depth)

    print("print task dependencies with max_depth {}\n".format(max_depth))

    ind = "|   "
    for dep, _, depth in task.walk_deps(max_depth=max_depth, order="pre"):
        print(depth * ind + "> " + dep.colored_repr())


def print_task_status(task, max_depth=0, target_depth=0, flags=None):
    max_depth = int(max_depth)
    target_depth = int(target_depth)
    if flags:
        flags = tuple(flags.lower().split("-"))

    print("print task status with max_depth {} and target_depth {}".format(
        max_depth, target_depth))

    done = []
    ind = "|   "
    for dep, _, depth in task.walk_deps(max_depth=max_depth, order="pre"):
        offset = depth * ind
        print(offset)
        print("{}> check status of {}".format(offset, dep.colored_repr()))
        offset += ind

        if dep in done:
            print(offset + "- " + colored("outputs already checked", "yellow"))
        else:
            done.append(dep)

            for outp in luigi.task.flatten(dep.output()):
                print("{}- check {}".format(offset, outp.colored_repr()))

                status_lines = outp.status_text(max_depth=target_depth, flags=flags).split("\n")
                status_text = status_lines[0]
                for line in status_lines[1:]:
                    status_text += "\n" + offset + "     " + line
                print("{}  -> {}".format(offset, status_text))


def remove_task_output(task, max_depth=0, mode=None, include_external=False):
    max_depth = int(max_depth)

    print("remove task output with max_depth {}".format(max_depth))

    include_external = check_bool_flag(include_external)
    if include_external:
        print("include external tasks")

    # determine the mode, i.e., all, dry, interactive
    modes = ["i", "a", "d"]
    mode_names = ["interactive", "all", "dry"]
    if mode is None:
        mode = query_choice("removal mode?", modes, default="i", descriptions=mode_names)
    elif isinstance(mode, int):
        mode = modes[mode]
    else:
        mode = mode[0].lower()
    if mode not in modes:
        raise Exception("unknown removal mode '{}'".format(mode))
    mode_name = mode_names[modes.index(mode)]
    print("selected " + colored(mode_name + " mode", "blue", style="bright"))

    done = []
    ind = "|   "
    for dep, _, depth in task.walk_deps(max_depth=max_depth, order="pre"):
        offset = depth * ind
        print(offset)
        print("{}> remove output of {}".format(offset, dep.colored_repr()))
        offset += ind

        if not include_external and isinstance(dep, ExternalTask):
            print(offset + "- " + colored("task is external, skip", "yellow"))
            continue

        if mode == "i":
            task_mode = query_choice(offset + "  walk through outputs?", ("y", "n"), default="y")
            if task_mode == "n":
                continue

        if dep in done:
            print(offset + "- " + colored("outputs already removed", "yellow"))
            continue

        done.append(dep)

        for outp in luigi.task.flatten(dep.output()):
            print("{}- remove {}".format(offset, outp.colored_repr()))

            if mode == "d":
                continue
            elif mode == "i":
                if query_choice(offset + "  remove?", ("y", "n"), default="n") == "n":
                    print(offset + colored("  skipped", "yellow"))
                    continue

            outp.remove()
            print(offset + "  " + colored("removed", "red", style="bright"))
