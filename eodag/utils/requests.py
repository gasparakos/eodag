# -*- coding: utf-8 -*-
# Copyright 2018, CS GROUP - France, https://www.csgroup.eu/
#
# This file is part of EODAG project
#     https://www.github.com/CS-SI/EODAG
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
from __future__ import annotations

import os
from typing import Any, Tuple

import requests

from eodag.utils import uri_to_path
from eodag.utils.exceptions import RequestError


class LocalFileAdapter(requests.adapters.BaseAdapter):
    """Protocol Adapter to allow Requests to GET file:// URLs inspired
    by https://stackoverflow.com/questions/10123929/fetch-a-file-from-a-local-url-with-python-requests/27786580
    `LocalFileAdapter` class available for the moment (on the 2024-04-22)
    """

    @staticmethod
    def _chkpath(method: str, path: str) -> Tuple[int, str]:
        """Return an HTTP status for the given filesystem path.

        :param method: method of the request
        :type method: str
        :param path: path of the given file
        :type path: str
        :returns: HTTP status and its associated message
        :rtype: Tuple[int, str]
        """
        if method.lower() in ("put", "delete"):
            return 501, "Not Implemented"  # TODO
        elif method.lower() not in ("get", "head"):
            return 405, "Method Not Allowed"
        elif os.path.isdir(path):
            return 400, "Path Not A File"
        elif not os.path.isfile(path):
            return 404, "File Not Found"
        elif not os.access(path, os.R_OK):
            return 403, "Access Denied"
        else:
            return 200, "OK"

    def send(self, req: requests.PreparedRequest, **kwargs: Any) -> requests.Response:
        """Wraps a file, described in request, in a Response object.

        :param req: The PreparedRequest being "sent".
        :type req: :class:`~requests.PreparedRequest`
        :param kwargs: (not used) additionnal arguments of the request
        :type kwargs: Any
        :returns: a Response object containing the file
        :rtype: :class:`~requests.Response`
        """
        response = requests.Response()

        path_url = uri_to_path(req.url)

        if req.method is None or req.url is None:
            raise RequestError("Method or url of the request is missing")
        response.status_code, response.reason = self._chkpath(req.method, path_url)
        if response.status_code == 200 and req.method.lower() != "head":
            try:
                response.raw = open(path_url, "rb")
            except (OSError, IOError) as err:
                response.status_code = 500
                response.reason = str(err)
        response.url = req.url
        response.request = req

        return response

    def close(self):
        """Closes without cleaning up adapter specific items."""
        pass