# -*- coding: utf-8 -*-
"""
Tencent is pleased to support the open source community by making 蓝鲸智云 - 监控平台 (BlueKing - Monitor) available.
Copyright (C) 2017-2021 THL A29 Limited, a Tencent company. All rights reserved.
Licensed under the MIT License (the "License"); you may not use this file except in compliance with the License.
You may obtain a copy of the License at http://opensource.org/licenses/MIT
Unless required by applicable law or agreed to in writing, software distributed under the License is distributed on
an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the License for the
specific language governing permissions and limitations under the License.
"""

import inspect
import logging
from contextlib import contextmanager
from importlib import import_module
from typing import Optional

from django.conf import settings
from django.utils.module_loading import import_string

from bkmonitor.utils.text import camel_to_underscore, path_to_dotted
from core.drf_resource.base import Resource
from core.drf_resource.management.exceptions import (
    ResourceModuleConflict,
    ResourceModuleNotRegistered,
    ResourceNotRegistered,
)
from core.drf_resource.management.finder import API_DIR, ResourceFinder, ResourcePath

logger = logging.getLogger(__name__)

# 判断是否DRFResource是否已经初始化
__setup__ = False
__doc__ = """
自动发现项目下resource和adapter和api
    cc
    ├── adapter
    │   ├── default.py
    │       ├── community
    │       │       └── resources.py
    │       └── enterprise
    │           └── resources.py
    └── resources.py
    使用:
        # api: 代表基于ESB/APIGW调用的接口调用
        # api.$module.$api_name
            api.bkdata.query_data -> api/bkdata/default.py: QueryDataResource
        # resource: 基于业务逻辑的封装
        resource.plugin -> plugin/resources.py
            resource.plugin.install_plugin -> plugin/resources.py: InstallPluginResource
        # adapter: 针对不同版本的逻辑差异进行的封装
        adapter.cc -> cc/adapter/default.py -> cc/adapter/${platform}/resources.py
        # 调用adapter.cc 即可访问对应文件下的resource，
        # 如果在${platform}/resources.py里面有相同定义，会重载default.py下的resource
        
        
    补充：
        1、如果已经存在resource.py或者default.py或者resources目录，那么会自动将此文件作为最终的resource资源文件。
        此时，与resource.py同级的目录下还存在其他的resource.py 需要手动导入到当前的resource.py中才会生效。
        
        2、drf_resource要求包含resource.py或者default.py或者resources目录的目录(dir_name)名称必须是唯一的，
        因为目录名称会作为resource的快捷方式名称，而快捷方式名称必须是唯一的。
        如果存在多个相同的名称(dir_name)，可以尝试在dir_name的父目录下创建在创建一个resource.py文件，从而将dir_name父目录的名称作为快捷方式名称。
        此时就不会发生冲突。但注意一定要将当前的resource.py导入到dir_name父目录的resource.py中。
        
    """


@contextmanager
def _lazy_load(_instance):
    # _instance就是ResourceShortcut实例，确保_resource_module_instance._setup()方法被调用
    if not _instance.loaded:
        getattr(_instance, "_setup", lambda: None)()
    yield _instance


def lazy_load(func):
    def wrapper(resource_module_instance, *args, **kwargs):
        with _lazy_load(resource_module_instance):
            return func(resource_module_instance, *args, **kwargs)

    return wrapper


class ResourceShortcut(object):
    _package_pool = {}

    # 定义Resource模块的入口名称，resources指的就是resources.py或者resources包
    # 同样在API场景下，可以定义_entry = "default"
    _entry = "resources"

    def __new__(cls, module_path: str):
        def init_instance(self, m_path):
            # 确保module_path 以 resources 结尾
            if not m_path.endswith(self._entry):
                m_path += ".{}".format(self._entry)
            # resource模块所在目录的路径
            self._path = m_path
            # resource模块所在包
            self._package = None
            # Resource类转为下划线字符串后，保存在self._methods中
            # {"get_info_info":GetInfoResource}
            self._methods = {}
            self.loaded = False
            # 新增单元测试 patch 支持
            self.__deleted_methods = {}

        if module_path not in cls._package_pool:
            instance = object.__new__(cls)
            init_instance(instance, module_path)
            cls._package_pool[module_path] = instance
        return cls._package_pool[module_path]

    def __init__(self, module_path):
        # init in __new__ function
        pass

    def __getitem__(self, x):
        # delete _entry(replace once from right)
        path = "".join(self._path.rsplit(self._entry, 1)).strip(".")
        dotted_path = path.split(".")
        if not isinstance(x, slice):
            return dotted_path[x]
        rm = resource
        while dotted_path[:-1]:
            rm = getattr(rm, dotted_path.pop(0))
        return tuple(rm)

    def _setup(self):
        """
        私有方法，用于动态设置资源。
    
        本方法尝试导入指定路径下的模块，并自动检测模块中的资源类和函数。
        对于每个检测到的资源类（非抽象且继承自Resource的类），创建一个实例并作为当前实例的属性。
        对于每个函数，直接绑定为当前实例的属性。
        此方法在内部使用，主要用于初始化和动态加载资源。
        """
        try:
            # 尝试导入指定路径下的模块
            self._package = import_module(self._path)
        except ImportError as e:
            # 如果导入失败，抛出ImportError异常，提供详细的错误信息
            raise ImportError("resource called before {} setup. detail: {}".format(self._path, e))

        # 遍历模块中的所有属性和方法
        for name, obj in list(self._package.__dict__.items()):
            # 忽略以"_"开头的私有属性和方法
            if name.startswith("_"):
                continue

            # 忽略抽象类
            if inspect.isabstract(obj):
                continue

            # 如果是继承自Resource的类，创建一个实例并作为当前实例的属性
            if inspect.isclass(obj) and issubclass(obj, Resource):
                # 移除类名中的"Resource"后缀，如果存在的话
                cleaned_name = "".join(name.rsplit("Resource", 1)) if name.endswith("Resource") else name
                # 将类名转换为下划线风格，用作属性名
                property_name = camel_to_underscore(cleaned_name)
                setattr(self, property_name, obj())
                self._methods[property_name] = obj

            # 同时支持将函数直接绑定为当前实例的属性
            if inspect.isfunction(obj):
                setattr(self, name, obj)
                self._methods[name] = obj

        # 标记资源为已加载
        self.loaded = True

    def __delattr__(self, name):
        if name in self._methods:
            self.__deleted_methods[name] = self._methods.pop(name)
        super(ResourceShortcut, self).__delattr__(name)

    @lazy_load
    def __getattr__(self, item):
        if item in self._methods:
            return getattr(self, item)
        try:
            return ResourceShortcut(import_module("{}.{}".format(self._path, item)).__name__)
        except ImportError:
            try:
                return import_string("{}.{}".format(self._path, item))
            except ImportError:
                if item in self.__deleted_methods:
                    return self.__deleted_methods[item]
                raise ResourceNotRegistered("Resource {} not in [{}]".format(item, self._package.__name__))

    @lazy_load
    def list_method(self):
        return list(self._methods.keys())

    def reload_method(self, method, func):
        setattr(self, method, func)
        self._methods[method] = func


class AdapterResourceShortcut(ResourceShortcut):
    _entry = "adapter.default"
    _package_pool = {}


class APIResourceShortcut(ResourceShortcut):
    _entry = "default"
    _package_pool = {}


class ResourceManager(tuple):
    """
    ResourceManager 类继承自 tuple，用于管理资源的层级结构。
    它允许动态添加子资源，并维护资源间的父子关系。
    底层使用 tuple 作为底层数据结构。
    """
    __parent__ = None  # 父资源引用，用于维护资源层级关系

    def __contains__(self, item):
        """
        重写 __contains__ 方法，用于检查项是否是资源本身或以资源为前缀。

        参数:
        - item: 需要检查的项

        返回:
        - bool: 项是否是资源本身或以资源为前缀
        """
        return item is not None and (self is item or item[: len(self)] == self)

    def __getattr__(self, name):
        """
        重写 __getattr__ 方法，用于动态添加或获取子资源。
        如果是设置阶段，尝试从转换后的对象中获取属性，如果未找到则抛出异常。
        否则，创建新的 ResourceManager 实例作为子资源，并将其绑定到当前资源。

        参数:
        - name: 属性名

        返回:
        - ResourceManager 或其他类型: 动态添加的子资源或获取的属性值
        """
        # 快捷方式挂载完成后，如果获取不到相关Resource类，会抛出异常
        if __setup__:
            # # 如果name不存在当前的self中，尝试从self的最后一个子资源中获取。
            got = getattr(self.transform(), name, None)
            if got is None:
                raise ResourceModuleNotRegistered(
                    'module: "%s" is not registered, maybe not in `INSTALLED_APPS` ?' % name
                )
            return got

        # 创建新的子资源实例,并将当当前self的内容添加到子资源中。
        # 然后给当前self(这个元组)添加name属性，指向新创建的子资源。
        # 假设name是"a",那么self.a=("a",)
        new = ResourceManager(self + (name,))
        setattr(self, name, new)
        new.__parent__ = self
        return new

    def __repr__(self):
        """
        重写 __repr__ 方法，返回资源的字符串表示。

        返回:
        - str: 资源的字符串表示
        """
        # self本身是一个元组对象，当self为空时，返回"resource"
        # 当self不为空时，返回"resource."+ self中的内容，以"."分隔.
        # 比如当self的内容为("a", "b", "c")时，返回"resource.a.b.c"
        return "resource" + ("." if self else "") + ".".join(self)

    def __bool__(self):
        """
        重写 __bool__ 方法，根据资源长度返回布尔值。

        返回:
        - bool: 资源是否非空
        """
        return bool(len(self))

    @property
    def __root__(self):
        """
        获取资源树的根节点。

        返回:
        - ResourceManager: 资源树的根节点
        """
        target = self
        while target.__parent__ is not None:
            target = target.__parent__
        return target

    def transform(self):
        """
        获取到当前资源的最尾端的资源。

        返回:
        - Callable or None: 转换后的可调用对象或 None
        """
        func_finder = self.__root__
        # self本身是一个元组对象self[0]的值就是当前的资源名称
        # 循环self中的值，获取到当前resource的最后一个子资源的实例，然后从实例中获取属性
        for attr in self:
            func_finder = getattr(func_finder, attr)

        # 获取到的func_finder如果是ResourceManager实例，说明资源未注册，返回None。
        if isinstance(func_finder, self.__class__):
            return None
            # raise Exception("func called before resource setup."
            #                 " detail: %s" % str(self))

        return func_finder

    def __call__(self, *args, **kwargs):
        """
        重写 __call__ 方法，使 ResourceManager 实例可调用。
        调用转换后的对象，并传递参数。

        参数:
        - *args: 位置参数
        - **kwargs: 关键字参数

        返回:
        - Any: 调用结果
        """
        return self.transform()(*args, **kwargs)


def setup():
    global __setup__
    if __setup__:
        return

    finder = ResourceFinder()
    for path in finder.resource_path:  # type: ResourcePath
        # print(path)
        # >> web.plugin: unloaded  # unloaded 表示资源未注册
        install_resource(path)

    __setup__ = True
    resource.__finder__ = finder


def install_resource(rs_path: ResourcePath):
    """
    # 示例代码：展示如何使用已经注册的资源
    # 假设我们有一个资源模块路径为 'plugin.resources'
    # 并且该模块中有一个资源类 'InstallPluginResource'

    # 使用示例
    # 1. 获取资源模块
    plugin_resource = resource.plugin

    # 2. 调用资源类的方法
    # 假设 InstallPluginResource 类中有一个方法 install_plugin
    result = plugin_resource.install_plugin(param1=value1, param2=value2)


    补充说明： Resource类分为两种类型，一种是APIResource，主要用于发送API请求，一种是Resource,主要用于业务逻辑.
        - 如果dotted_path以api.开头，那么该资源类为APIResource，否则为Resource
    """
    # 获取资源路径的点分隔表示形式
    dotted_path: str = rs_path.path
    # 初始化资源和端点变量,表示当前字资源(路径上的某个点)所对应的ResourceManager实例
    # 而resource是所有_resource的根节点
    _resource: Optional[ResourceManager, ResourceShortcut] = None
    endpoint: Optional[str] = None

    # 如果路径指向的是API或适配器资源，则调用install_adapter函数处理
    if is_api(dotted_path) or is_adapter(dotted_path):
        return install_adapter(rs_path)

    # 分割资源路径，逐段处理
    for p in dotted_path.split("."):
        # 如果父资源是一个快捷方式，则无法再产生ResourceManger实例，所以直接跳过
        if isinstance(_resource, ResourceShortcut):
            logger.debug("ignored: {}".format(dotted_path))
            rs_path.ignored()
            return
        # 只有第一次获取时，是从resource中获取,此时会产生一个初始的ResourceManage实例作为字资源，而当前resource就是该子资源的父资源
        # 从第二次开始，就是从_resource中获取，里面已经记录了父资源。
        # 这里挂载完之后，每个resource及其子资源都是一个ResourceManager实例。
        # 经过后续处理之后，最后一个子资源，也就是endpoint，将会被覆盖变为一个ResourceShortcut实例，从而实现快捷方式的功能。
        _resource = getattr(_resource or resource, p)
        endpoint = p

    # 如果资源成功获取到
    if _resource:
        try:
            resource_module = ResourceShortcut(".".join(_resource))
            logger.debug("success: {}".format(dotted_path))
            rs_path.loaded()
        except ResourceNotRegistered:
            # 如果资源未注册，记录警告日志并标记路径错误
            logger.warning("failed: {}".format(dotted_path))
            rs_path.error()
            return

        # 尝试从resource中获取快捷方式,如果获取到并且也是ResourceShortcut，则抛出冲突异常，证明同名资源已经存在
        shortcut = getattr(resource, endpoint)
        if isinstance(shortcut, ResourceShortcut):
            raise ResourceModuleConflict(
                "resources conflict:\n>>> {}\n<<< {}".format(shortcut._path, ".".join(_resource))
            )
        # 设置资源的快捷方式
        # 挂载完整的路径，比如有资源"a.b.c.get_user_info",a和b都是ResourceManager实例，c是ResourceShortcut实例,
        # 所以可以使用完整路径调用，比如resource.a.b.c.get_user_info进行调用
        setattr(_resource.__parent__, endpoint, resource_module)
        # 挂载快捷方式，可以直接通过快捷方式调用，比如resource.c.get_user_info进行调用
        setattr(resource, endpoint, resource_module)


def install_adapter(rs_path):
    """
    根据给定的资源路径安装适配器。

    该函数负责根据资源路径（rs_path）来安装相应的适配器。适配器可以是API资源适配器，
    也可以是适配器资源快捷方式，具体取决于路径是否指向API目录。

    参数:
    - rs_path: 资源路径对象，包含待安装适配器的路径信息。

    返回值:
    无直接返回值，但会通过rs_path.error()记录错误，或通过rs_path.loaded()标记加载成功。
    """
    # 获取资源路径的点分隔格式
    dotted_path = rs_path.path
    # 初始化适配器类为AdapterResourceShortcut
    adapter_cls = AdapterResourceShortcut
    # adapter 和 api 代码结构一致， 唯一区别是entry不同，adapter多了一层`adapter`目录
    if is_api(dotted_path):
        # 如果是API路径，进行特殊处理
        api_root = path_to_dotted(API_DIR)
        result = dotted_path[(len(API_DIR) + 1):].split(".", 1)
        if len(result) == 2:
            rs, ada = result
        else:
            rs = result[0]
            ada = ""
        rs = "{}.{}".format(api_root, rs)
        adapter_cls = APIResourceShortcut
    else:
        # 分割非API路径，适配器路径应包含"adapter"目录
        rs, ada = [path.strip(".") for path in dotted_path.split("adapter")]

    try:
        # 创建默认适配器实例
        default_adapter = adapter_cls(rs)
        # 获取默认适配器定义的方法列表
        defined_method = default_adapter.list_method()
    except ImportError as e:
        # 如果导入失败，记录错误并返回
        logger.warning("error: {}\n{}".format(dotted_path, e))
        rs_path.error()
        return

    # 如果适配器路径以PLATFORM开头，加载平台特定的适配器
    if ada.startswith(settings.PLATFORM):
        platform_adapter = ResourceShortcut(dotted_path)
        # load method from platform adapter to default adapter
        for method in defined_method:
            if method in platform_adapter.list_method():
                default_adapter.reload_method(method, getattr(platform_adapter, method))

    # 根据路径类型确定适配器的根路径
    root = adapter
    if is_api(dotted_path):
        root = api
    # 在适配器或API的命名空间内注册加载的适配器
    setattr(root, rs.split(".")[-1], default_adapter)
    # 记录成功加载的日志
    logger.debug("success: {}".format(dotted_path))
    # 标记资源路径为已加载
    rs_path.loaded()


def is_api(dotted_path):
    return dotted_path.startswith(path_to_dotted(API_DIR))


def is_adapter(dotted_path):
    return "adapter" in dotted_path.split(".")


resource = ResourceManager()

adapter = ResourceManager()

api = ResourceManager()
