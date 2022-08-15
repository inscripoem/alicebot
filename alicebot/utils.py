import json
import asyncio
import inspect
import os.path
import pkgutil
import importlib
import collections
import dataclasses
from abc import ABC
from types import ModuleType
from importlib.abc import MetaPathFinder
from importlib.machinery import PathFinder
from typing import List, Type, Generic, TypeVar, Callable, Iterable, Optional

from pydantic import BaseModel

from alicebot.exceptions import LoadModuleError

__all__ = [
    "Condition",
    "ModulePathFinder",
    "ModuleInfo",
    "load_module",
    "load_module_from_name",
    "load_module_form_file",
    "load_modules_from_dir",
    "DataclassEncoder",
]

_T = TypeVar("_T")


class Condition(Generic[_T]):
    """类似于 asyncio.Condition ，但允许在 notify() 时传递值，并由 wait() 返回。"""

    def __init__(self):
        self._loop = asyncio.get_running_loop()
        lock = asyncio.Lock()
        self._lock = lock
        # Export the lock's locked(), acquire() and release() methods.
        self.locked = lock.locked
        self.acquire = lock.acquire
        self.release = lock.release

        self._waiters = collections.deque()

    async def __aenter__(self):
        await self.acquire()
        # We have no use for the "as ..."  clause in the with
        # statement for locks.
        return None

    async def __aexit__(self, exc_type, exc, tb):
        self.release()

    def __repr__(self):
        res = super().__repr__()
        extra = "locked" if self.locked() else "unlocked"
        if self._waiters:
            extra = f"{extra}, waiters:{len(self._waiters)}"
        return f"<{res[1:-1]} [{extra}]>"

    async def wait(self) -> _T:
        if not self.locked():
            raise RuntimeError("cannot wait on un-acquired lock")

        self.release()
        try:
            fut = self._loop.create_future()
            self._waiters.append(fut)
            try:
                return await fut
            finally:
                self._waiters.remove(fut)

        finally:
            # Must reacquire lock even if wait is cancelled
            cancelled = False
            while True:
                try:
                    await self.acquire()
                    break
                except asyncio.CancelledError:
                    cancelled = True

            if cancelled:
                raise asyncio.CancelledError

    async def wait_for(self, predicate: Callable[..., bool]) -> bool:
        result = predicate()
        while not result:
            await self.wait()
            result = predicate()
        return result

    def notify(self, value: _T = None, n: int = 1):
        if not self.locked():
            raise RuntimeError("cannot notify on un-acquired lock")

        idx = 0
        for fut in self._waiters:
            if idx >= n:
                break

            if not fut.done():
                idx += 1
                fut.set_result(value)

    def notify_all(self, value: _T = None):
        self.notify(value, len(self._waiters))


class ModulePathFinder(MetaPathFinder):
    """用于查找 AliceBot 组件的元路径查找器。"""

    path: List[str] = []

    def find_spec(self, fullname, path=None, target=None):
        if path is None:
            path = []
        return PathFinder.find_spec(fullname, self.path + list(path), target)


class ModuleInfo(Generic[_T]):
    """模块信息。

    Attributes
        module_class: 模块类。
        config_class: 配置类。
        module: 模块。
    """

    __slots__ = ("module_class", "config_class", "module")

    def __init__(
        self,
        module_class: Type[_T],
        config_class: Optional[Type[BaseModel]],
        module: ModuleType,
    ):
        self.module_class = module_class
        self.config_class = config_class
        self.module = module

    def reload(self, class_type: Type[_T]):
        importlib.reload(self.module)
        module_class, config_class = load_module(self.module, class_type)
        self.module_class = module_class
        self.config_class = config_class


def load_module(module: ModuleType, class_type: Type[_T]) -> ModuleInfo[_T]:
    """从模块中查找指定类型的类和 `Config` 。若模块中存在多个符合条件的类，则抛出错误。

    Args:
        module: Python 模块。
        class_type: 要查找的类型。

    Returns:
        返回符合条件的类和配置类组成的元组。

    Raises:
        LoadModuleError: 当找不到符合条件的类或者模块中存在多个符合条件的类。
    """

    module_class = []
    for module_attr in dir(module):
        module_attr = getattr(module, module_attr)
        if (
            inspect.isclass(module_attr)
            and (inspect.getmodule(module_attr) or module) is module
            and issubclass(module_attr, class_type)
            and module_attr != class_type
            and ABC not in module_attr.__bases__
            and not inspect.isabstract(module_attr)
        ):
            module_class.append(module_attr)

    if not module_class:
        raise LoadModuleError(
            f"Can not find {class_type!r} class in the {module.__name__} module"
        )
    elif len(module_class) > 1:
        print(module_class)
        raise LoadModuleError(
            f"More then one {class_type!r} class in the {module.__name__} module"
        )

    if "Config" in dir(module):
        module_attr = getattr(module, "Config")
        if (
            inspect.isclass(module_attr)
            and issubclass(module_attr, BaseModel)
            and isinstance(getattr(module_attr, "__config_name__", None), str)
        ):
            return ModuleInfo(module_class[0], module_attr, module)
    return ModuleInfo(module_class[0], None, module)


def load_module_from_name(name: str, class_type: Type[_T]) -> ModuleInfo[_T]:
    """从模块中查找指定类型的类和 `Config` 。若模块中存在多个符合条件的类，则抛出错误。

    Args:
        name: 模块名称，格式和 Python `import` 语句相同。
        class_type: 要查找的类型。

    Returns:
        返回符合条件的 `ModuleInfo`。

    Raises:
        LoadModuleError: 当找不到符合条件的类或者模块中存在多个符合条件的类。
    """
    importlib.invalidate_caches()
    module = importlib.import_module(name)
    importlib.reload(module)
    return load_module(module, class_type)


def load_module_form_file(
    module_path_finder: ModulePathFinder, path: str, class_type: Type[_T]
) -> ModuleInfo[_T]:
    """从指定文件中导入模块。

    Args:
        module_path_finder: 用于查找 AliceBot 组件的元路径查找器。
        path: 由储存模块的路径文本组成的列表。 例如 `['path/of/plugins/', '/home/xxx/alicebot/plugins']` 。
        class_type: 要查找的类型。

    Returns:
        返回符合条件的 `ModuleInfo`。
    """
    dirname, basename = os.path.split(path)
    name, ext = os.path.splitext(basename)
    if ext != ".py":
        raise LoadModuleError("The extension of path must be .py")
    module_path_finder.path = [dirname]
    return load_module_from_name(name, class_type)


def load_modules_from_dir(
    module_path_finder: ModulePathFinder, path: Iterable[str], class_type: Type[_T]
) -> List[ModuleInfo[_T]]:
    """从指定路径列表中的所有模块中查找指定类型的类和 `Config` ，以 `_` 开头的插件不会被导入。路径可以是相对路径或绝对路径。

    Args:
        module_path_finder: 用于查找 AliceBot 组件的元路径查找器。
        path: 由储存模块的路径文本组成的列表。 例如 `['path/of/plugins/', '/home/xxx/alicebot/plugins']` 。
        class_type: 要查找的类型。

    Returns:
        返回符合条件的 `ModuleInfo` 的列表。
    """
    modules = []
    module_path_finder.path = list(path)
    for module_info in pkgutil.iter_modules(path):
        if not module_info.name.startswith("_"):
            try:
                modules.append(load_module_from_name(module_info.name, class_type))
            except LoadModuleError:
                continue
    return modules


class DataclassEncoder(json.JSONEncoder):
    """用于解析 MessageSegment 的 JSONEncoder 类。"""

    def default(self, o):
        if dataclasses.is_dataclass(o):
            return o.as_dict()
        return super().default(o)
