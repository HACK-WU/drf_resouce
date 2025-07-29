"""
Tencent is pleased to support the open source community by making 蓝鲸智云 - 监控平台 (BlueKing - Monitor) available.
Copyright (C) 2017-2021 THL A29 Limited, a Tencent company. All rights reserved.
Licensed under the MIT License (the "License"); you may not use this file except in compliance with the License.
You may obtain a copy of the License at http://opensource.org/licenses/MIT
Unless required by applicable law or agreed to in writing, software distributed under the License is distributed on
an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the License for the
specific language governing permissions and limitations under the License.
"""

import logging
from functools import wraps

from celery import shared_task
from celery.result import AsyncResult

from bkmonitor.utils.tenant import set_local_tenant_id
from bkmonitor.utils.user import set_local_username
from core.drf_resource.exceptions import CustomException

logger = logging.getLogger(__name__)


@shared_task(bind=True, queue="celery_resource")
def run_perform_request(self, resource_obj, username: str, bk_tenant_id: str, request_data):
    """
    将resource作为异步任务执行
    :param self: 任务对象
    :param resource_obj: Resource实例
    :param request: 请求
    :param request_data: 请求数据
    :return: resource处理后的返回数据
    """
    set_local_username(username)
    set_local_tenant_id(bk_tenant_id)

    resource_obj._task_manager = self
    validated_request_data = resource_obj.validate_request_data(request_data)
    response_data = resource_obj.perform_request(validated_request_data)
    validated_response_data = resource_obj.validate_response_data(response_data)
    return validated_response_data


def _fetch_data_from_result(async_result):
    """
    从异步任务结果中提取步骤信息
    :param async_result: AsyncResult 对象
    :return: message, data: 步骤信息，步骤数据
    """
    info = async_result.info
    try:
        message = info.get("message")
        data = info.get("data")
    except Exception:
        message = None
        data = None
    return message, data


def query_task_result(task_id):
    """
    查询任务结果
    """
    result = AsyncResult(task_id)

    # 任务是否完成
    is_completed = False

    message, data = _fetch_data_from_result(result)

    if result.successful() or result.failed():
        is_completed = True
        try:
            # 任务执行完成，则读取结果数据
            message = None
            data = result.get()
        except CustomException as e:
            message = e.message
            data = e.data
        except Exception as e:
            logger.exception("Caught exception when running async resource task : %s" % e)
            message = "%s" % e
            data = None

    return {
        "task_id": task_id,
        "is_completed": is_completed,
        "state": result.state,
        "message": message,
        "data": data,
        "traceback": result.traceback,
    }


def step(state=None, message=None, data=None):
    """
    步骤装饰器工厂函数，用于创建状态跟踪装饰器

    参数说明：
    state  : str | callable | None  步骤状态标识。当直接装饰函数时自动转为None
    message: str | None             步骤描述信息
    data   : Any | None             步骤关联的附加数据

    返回值：
    callable: 装饰器函数或直接返回装饰后的函数（当不带参数调用时）

    功能特点：
    1. 支持带参数和不带参数两种调用方式
    2. 自动维护被装饰对象的状态跟踪
    3. 默认使用被装饰函数的__name__.upper()作为步骤标识
    """

    # 处理不带括号的装饰器用法：@step 等价于 @step()
    if callable(state):
        real_func = state  # 保存被装饰的原始函数
        real_state = None  # 重置状态标识为默认值
    else:
        real_func = None
        real_state = state  # 显式指定的状态标识

    def decorate(func):
        """实际装饰器实现，负责包装原始函数"""

        @wraps(func)
        def wrapper(self, *args, **kwargs):
            """包装器函数，实现状态更新逻辑"""
            # 自动生成步骤标识：优先使用显式state，其次用函数名的大写形式
            _state = real_state or func.__name__.upper()

            # 在调用目标函数前更新任务状态
            self.update_state(state=_state, message=message, data=data)

            # 执行原始函数并返回结果
            return func(self, *args, **kwargs)

        return wrapper

    # 处理两种装饰器调用方式的统一返回
    if real_func:
        return decorate(real_func)  # 处理不带参数调用的装饰器
    return decorate  # 处理带参数调用的装饰器
