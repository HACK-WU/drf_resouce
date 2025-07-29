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

from django.apps import AppConfig

from .management.root import setup
from django.utils.deprecation import MiddlewareMixin
from django.conf import settings
import requests

bkop_url = getattr(settings, 'BKOP_URL', '')
headers = getattr(settings, 'BKOP_HEADERS', {})


# AppConfig 相关文档：https://docs.djangoproject.com/zh-hans/5.1/ref/applications/
class DRFResourceConfig(AppConfig):
    name = "core.drf_resource"
    verbose_name = "drf_resource"
    label = "drf_resource"

    def ready(self):
        """
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
            """
        setup()
        if  getattr(settings, 'MOCK_UNIFY_QUERY', False):
            mock_unify_query()

        if getattr(settings, 'BKOP_FORWARDED_REQUEST', False):
            settings.MIDDLEWARE = tuple(
                list(settings.MIDDLEWARE) + ["core.drf_resource.apps.RequestForwardingMiddleware"])


def mock_unify_query():
    from mock import patch
    from core.drf_resource import resource, Resource

    url = bkop_url + "/query-api/rest/v2/grafana/time_series/unify_query/"

    class MockUnifyQuery(Resource):
        def perform_request(self, params):
            import json
            params = json.dumps(params)

            response = requests.request("POST", url, headers=headers, data=params)
            return response.json()["data"]

    mock_unify_query = patch.object(resource.grafana, 'graph_unify_query', new=MockUnifyQuery())
    mock_unify_query.start()


class RequestForwardingMiddleware(MiddlewareMixin):
    def process_view(self, request, view_func, view_args, view_kwargs):
        path = request.path
        if path not in self.forwarded_urls:
            return None
        response = self.handler(request)
        return response

    @property
    def handler(self):
        if not hasattr(self, "_handler"):
            from rest_framework.views import APIView
            from rest_framework.response import Response

            class InnerView(APIView):
                def get(self, request, *args, **kwargs):
                    return self.perform_request(request, *args, **kwargs)

                def post(self, request, *args, **kwargs):
                    return self.perform_request(request, *args, **kwargs)

                def perform_request(self, request, *args, **kwargs):
                    if not headers:
                        response = {
                            "result": True,
                            "code": 200,
                            "message": "error, headers is empty",
                            "data": []
                        }
                        return Response(response)

                    path = request.path
                    method = request.method.lower()

                    if method == "get":
                        params = request.GET.dict()
                    else:
                        params = request.POST.dict()
                    response = requests.request(method, bkop_url + path, params=params, headers=headers)

                    if response.status_code != 200:
                        response = {
                            "result": False,
                            "code": response.status_code,
                            "message": response.text,
                            "data": []
                        }
                        return Response(response)
                    return Response(response.json())

            self._handler = InnerView().as_view()

        return self._handler

    @property
    def forwarded_urls(self):
        if not hasattr(self, "_forwarded_urls"):
            self._forwarded_urls = getattr(settings, 'FORWARDED_URLS', [])
        return self._forwarded_urls
