# Copyright 2020 The JAX Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import contextlib
import dataclasses
import functools
import itertools
import os.path
import threading
import types
from typing import Optional, Iterator, NamedTuple, Union, Tuple

import jax.version
from jax._src.lib import xla_client

from jax._src import traceback_util
traceback_util.register_exclusion(__file__)


Traceback = xla_client.Traceback

class Frame(NamedTuple):
  file_name: str
  function_name: str
  line_num: int


_exclude_paths = [os.path.dirname(jax.version.__file__)]

def register_exclusion(path):
  _exclude_paths.append(path)

class Scope(NamedTuple):
  name: str

  def wrap(self, stack: Tuple[str, ...]) -> Tuple[str, ...]:
    return (self.name, *stack)

class Transform(NamedTuple):
  name: str

  def wrap(self, stack: Tuple[str, ...]) -> Tuple[str, ...]:
    if stack:
      return (f'{self.name}({stack[0]})', *stack[1:])
    else:
      return ()

@dataclasses.dataclass(frozen=True)
class NameStack:
  stack: Tuple[Union[Scope, Transform], ...] = ()

  def extend(self, name: Union[Tuple[str, ...], str]) -> 'NameStack':
    if not isinstance(name, tuple):
      name = (name,)
    scopes = tuple(map(Scope, name))
    return NameStack(self.stack + scopes)

  def wrap_name(self, name: str) -> str:
    if not self.stack:
      return name
    return f'{str(self)}/{name}'

  def transform(self, transform_name: str) -> 'NameStack':
    return NameStack((*self.stack, Transform(transform_name)))

  def __getitem__(self, idx) -> 'NameStack':
    return NameStack(self.stack[idx])

  def __len__(self):
    return len(self.stack)

  def __add__(self, other: 'NameStack') -> 'NameStack':
    return NameStack(self.stack + other.stack)

  def __radd__(self, other: 'NameStack') -> 'NameStack':
    return NameStack(other.stack + self.stack)

  def __str__(self) -> str:
    scope: Tuple[str, ...] = ()
    for elem in self.stack[::-1]:
      scope = elem.wrap(scope)
    return '/'.join(scope)

class SourceInfo(NamedTuple):
  traceback: Optional[Traceback]
  name_stack: NameStack

  def replace(self, *, traceback: Optional[Traceback] = None,
      name_stack: Optional[NameStack] = None) -> 'SourceInfo':
    traceback = traceback or self.traceback
    name_stack = self.name_stack if name_stack is None else name_stack
    return self._replace(traceback=traceback, name_stack=name_stack)

def new_source_info() -> SourceInfo:
  return SourceInfo(None, NameStack())

def is_user_filename(filename: str) -> bool:
  """Heuristic that guesses the identity of the user's code in a stack trace."""
  return (filename.endswith("_test.py") or
          not any(filename.startswith(p) for p in _exclude_paths))

def _raw_frame_to_frame(code: types.CodeType, lasti: int) -> Frame:
  return Frame(file_name=code.co_filename,
               function_name=code.co_name,
               line_num=xla_client.Traceback.code_addr2line(code, lasti))

def user_frames(source_info: SourceInfo) -> Iterator[Frame]:
  """Iterator over the user's frames, filtering jax-internal frames."""
  # Guess the user's frame is the innermost frame not in the jax source tree
  # We don't use traceback_util.path_starts_with because that incurs filesystem
  # access, which may be slow; we call this function when e.g. adding source
  # provenance annotations to XLA lowerings, so we don't want to incur the cost.
  # We consider files that end with _test.py as user frames, to allow testing
  # this mechanism from tests.
  traceback = source_info.traceback
  code, lasti = traceback.raw_frames() if traceback else ([], [])
  return (_raw_frame_to_frame(code[i], lasti[i]) for i in range(len(code))  # type: ignore
          if is_user_filename(code[i].co_filename))

@functools.lru_cache(maxsize=64)
def user_frame(source_info: SourceInfo) -> Optional[Frame]:
  return next(user_frames(source_info), None)

def summarize(source_info: SourceInfo, num_frames=1) -> str:
  frames = itertools.islice(user_frames(source_info), num_frames)
  frame_strs = [f"{frame.file_name}:{frame.line_num} ({frame.function_name})"
                if frame else "unknown" for frame in frames]
  return '\n'.join(reversed(frame_strs))

class _SourceInfoContext(threading.local):
  context: SourceInfo

  def __init__(self):
    self.context = new_source_info()

_source_info_context = _SourceInfoContext()

def current() -> SourceInfo:
  source_info = _source_info_context.context
  if not source_info.traceback:
    source_info = source_info.replace(traceback=xla_client.Traceback.get_traceback())
  return source_info

class JaxStackTraceBeforeTransformation(Exception): pass

_message = (
    'The preceding stack trace is the source of the JAX operation that, once '
    'transformed by JAX, triggered the following exception.\n'
    '\n--------------------')

def has_user_context(e):
  while e is not None:
    if isinstance(e, JaxStackTraceBeforeTransformation):
      return True
    e = e.__cause__
  return False

@contextlib.contextmanager
def user_context(c: Optional[Traceback], *, name_stack: Optional[NameStack] = None):
  prev = _source_info_context.context
  _source_info_context.context = _source_info_context.context.replace(
      traceback=c, name_stack=name_stack)
  filtered_tb = None
  try:
    yield
  except Exception as e:
    if c is None or has_user_context(e):
      raise
    filtered_tb = traceback_util.filter_traceback(c.as_python_traceback())
    if filtered_tb:
      msg = traceback_util.format_exception_only(e)
      msg = f'{msg}\n\n{_message}'
      exp = JaxStackTraceBeforeTransformation(msg).with_traceback(filtered_tb)
      exp.__context__ = e.__context__
      exp.__cause__ = e.__cause__
      exp.__suppress_context__ = e.__suppress_context__
      e.__context__ = None
      e.__cause__ = exp
    raise
  finally:
    _source_info_context.context = prev
    del filtered_tb

def current_name_stack() -> NameStack:
  return _source_info_context.context.name_stack

@contextlib.contextmanager
def extend_name_stack(name: str) -> Iterator[NameStack]:
  prev_context = _source_info_context.context
  curr_name_stack = prev_context.name_stack
  new_context = prev_context.replace(name_stack=curr_name_stack.extend(name))
  _source_info_context.context = new_context
  try:
    yield _source_info_context.context.name_stack
  finally:
    _source_info_context.context = prev_context

@contextlib.contextmanager
def set_name_stack(name_stack: NameStack) -> Iterator[None]:
  prev_context = _source_info_context.context
  new_context = prev_context.replace(name_stack=name_stack)
  _source_info_context.context = new_context
  try:
    yield
  finally:
    _source_info_context.context = prev_context

@contextlib.contextmanager
def reset_name_stack() -> Iterator[None]:
  with set_name_stack(NameStack()):
    yield

@contextlib.contextmanager
def transform_name_stack(name: str) -> Iterator[NameStack]:
  prev_context = _source_info_context.context
  curr_name_stack = prev_context.name_stack
  new_context = prev_context.replace(name_stack=curr_name_stack.transform(name))
  _source_info_context.context = new_context
  try:
    yield _source_info_context.context.name_stack
  finally:
    _source_info_context.context = prev_context
