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

import importlib
import json
import os
import socket
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Dict, List, Optional, Union
from unittest.mock import MagicMock, Mock

import geojson
import httpx
import responses
from fastapi.testclient import TestClient
from shapely.geometry import box

from eodag.config import PluginConfig
from eodag.plugins.authentication.base import Authentication
from eodag.plugins.download.base import Download
from eodag.rest.config import Settings
from eodag.rest.types.queryables import StacQueryables
from eodag.utils import USER_AGENT, MockResponse, StreamResponse
from eodag.utils.exceptions import NotAvailableError, TimeOutError
from tests import mock, temporary_environment
from tests.context import (
    DEFAULT_ITEMS_PER_PAGE,
    HTTP_REQ_TIMEOUT,
    NOT_AVAILABLE,
    OFFLINE_STATUS,
    ONLINE_STATUS,
    STAGING_STATUS,
    TEST_RESOURCES_PATH,
    AuthenticationError,
    SearchResult,
    parse_header,
)


# AF_UNIX socket not supported on windows yet, see https://github.com/python/cpython/issues/77589
@unittest.skipIf(
    not hasattr(socket, "AF_UNIX"), "AF_UNIX socket not supported on this OS (windows)"
)
class RequestTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        super(RequestTestCase, cls).setUpClass()

        cls.tested_product_type = "S2_MSI_L1C"

        # Mock home and eodag conf directory to tmp dir
        cls.tmp_home_dir = TemporaryDirectory()
        cls.expanduser_mock = mock.patch(
            "os.path.expanduser", autospec=True, return_value=cls.tmp_home_dir.name
        )
        cls.expanduser_mock.start()

        # mock os.environ to empty env
        cls.mock_os_environ = mock.patch.dict(os.environ, {}, clear=True)
        cls.mock_os_environ.start()

        # disable product types fetch
        os.environ["EODAG_EXT_PRODUCT_TYPES_CFG_FILE"] = ""

        # load fake credentials to prevent providers needing auth for search to be pruned
        os.environ["EODAG_CFG_FILE"] = os.path.join(
            TEST_RESOURCES_PATH, "wrong_credentials_conf.yml"
        )

        # import after having mocked home_dir because it launches http server (and EODataAccessGateway)
        # reload eodag.rest.core to prevent eodag_api cache conflicts
        import eodag.rest.core

        importlib.reload(eodag.rest.core)
        from eodag.rest import server as eodag_http_server

        cls.eodag_http_server = eodag_http_server

    @classmethod
    def tearDownClass(cls):
        super(RequestTestCase, cls).tearDownClass()
        # stop os.environ
        cls.mock_os_environ.stop()

        # stop Mock and remove tmp config dir
        cls.expanduser_mock.stop()
        cls.tmp_home_dir.cleanup()

    def setUp(self):
        self.app = TestClient(self.eodag_http_server.app)

    def test_route(self):
        result = self._request_valid("/", check_links=False)

        # check links (root specfic)
        self.assertIsInstance(result, dict)
        self.assertIn("links", result, f"links not found in {str(result)}")
        self.assertIsInstance(result["links"], list)
        links = result["links"]

        known_rel = [
            "self",
            "root",
            "parent",
            "child",
            "items",
            "service-desc",
            "service-doc",
            "conformance",
            "search",
            "data",
        ]
        required_links_rel = ["self"]

        for link in links:
            # known relations
            self.assertIn(link["rel"], known_rel)
            # must start with app base-url
            assert link["href"].startswith(str(self.app.base_url))
            if link["rel"] != "search":
                # must be valid
                self._request_valid_raw(link["href"])
            else:
                # missing collection
                self._request_not_valid(link["href"])

            if link["rel"] in required_links_rel:
                required_links_rel.remove(link["rel"])

        # required relations
        self.assertEqual(
            len(required_links_rel),
            0,
            f"missing {required_links_rel} relation(s) in {links}",
        )

    def test_forward(self):
        response = self.app.get("/", follow_redirects=True)
        self.assertEqual(200, response.status_code)
        resp_json = json.loads(response.content.decode("utf-8"))
        self.assertEqual(resp_json["links"][0]["href"], "http://testserver/")

        response = self.app.get(
            "/", follow_redirects=True, headers={"Forwarded": "host=foo;proto=https"}
        )
        self.assertEqual(200, response.status_code)
        resp_json = json.loads(response.content.decode("utf-8"))
        self.assertEqual(resp_json["links"][0]["href"], "https://foo/")

        response = self.app.get(
            "/",
            follow_redirects=True,
            headers={"X-Forwarded-Host": "bar", "X-Forwarded-Proto": "httpz"},
        )
        self.assertEqual(200, response.status_code)
        resp_json = json.loads(response.content.decode("utf-8"))
        self.assertEqual(resp_json["links"][0]["href"], "httpz://bar/")

    def mock_search_result(self):
        """generate eodag_api.search mock results"""
        search_result = SearchResult.from_geojson(
            {
                "features": [
                    {
                        "properties": {
                            "snowCover": None,
                            "resolution": None,
                            "completionTimeFromAscendingNode": "2018-02-16T00:12:14"
                            ".035Z",
                            "keyword": {},
                            "productType": "OCN",
                            "downloadLink": (
                                "https://peps.cnes.fr/resto/collections/S1/"
                                "578f1768-e66e-5b86-9363-b19f8931cc7b/download"
                            ),
                            "eodag_provider": "peps",
                            "eodag_product_type": "S1_SAR_OCN",
                            "platformSerialIdentifier": "S1A",
                            "cloudCover": 0,
                            "title": "S1A_WV_OCN__2SSV_20180215T235323_"
                            "20180216T001213_020624_023501_0FD3",
                            "orbitNumber": 20624,
                            "instrument": "SAR-C SAR",
                            "abstract": None,
                            "eodag_search_intersection": {
                                "coordinates": [
                                    [
                                        [89.590721, 2.614019],
                                        [89.771805, 2.575546],
                                        [89.809341, 2.756323],
                                        [89.628258, 2.794767],
                                        [89.590721, 2.614019],
                                    ]
                                ],
                                "type": "Polygon",
                            },
                            "organisationName": None,
                            "startTimeFromAscendingNode": "2018-02-15T23:53:22" ".871Z",
                            "platform": None,
                            "sensorType": None,
                            "processingLevel": None,
                            "orbitType": None,
                            "topicCategory": None,
                            "orbitDirection": None,
                            "parentIdentifier": None,
                            "sensorMode": None,
                            "quicklook": None,
                            "storageStatus": ONLINE_STATUS,
                            "providerProperty": "foo",
                        },
                        "id": "578f1768-e66e-5b86-9363-b19f8931cc7b",
                        "type": "Feature",
                        "geometry": {
                            "coordinates": [
                                [
                                    [89.590721, 2.614019],
                                    [89.771805, 2.575546],
                                    [89.809341, 2.756323],
                                    [89.628258, 2.794767],
                                    [89.590721, 2.614019],
                                ]
                            ],
                            "type": "Polygon",
                        },
                    },
                    {
                        "properties": {
                            "snowCover": None,
                            "resolution": None,
                            "completionTimeFromAscendingNode": "2018-02-17T00:12:14"
                            ".035Z",
                            "keyword": {},
                            "productType": "OCN",
                            "downloadLink": (
                                "https://peps.cnes.fr/resto/collections/S1/"
                                "578f1768-e66e-5b86-9363-b19f8931cc7c/download"
                            ),
                            "eodag_provider": "peps",
                            "eodag_product_type": "S1_SAR_OCN",
                            "platformSerialIdentifier": "S1A",
                            "cloudCover": 0,
                            "title": "S1A_WV_OCN__2SSV_20180216T235323_"
                            "20180217T001213_020624_023501_0FD3",
                            "orbitNumber": 20624,
                            "instrument": "SAR-C SAR",
                            "abstract": None,
                            "eodag_search_intersection": {
                                "coordinates": [
                                    [
                                        [89.590721, 2.614019],
                                        [89.771805, 2.575546],
                                        [89.809341, 2.756323],
                                        [89.628258, 2.794767],
                                        [89.590721, 2.614019],
                                    ]
                                ],
                                "type": "Polygon",
                            },
                            "organisationName": None,
                            "startTimeFromAscendingNode": "2018-02-16T23:53:22" ".871Z",
                            "platform": None,
                            "sensorType": None,
                            "processingLevel": None,
                            "orbitType": None,
                            "topicCategory": None,
                            "orbitDirection": None,
                            "parentIdentifier": None,
                            "sensorMode": None,
                            "quicklook": None,
                            "storageStatus": OFFLINE_STATUS,
                        },
                        "id": "578f1768-e66e-5b86-9363-b19f8931cc7c",
                        "type": "Feature",
                        "geometry": {
                            "coordinates": [
                                [
                                    [89.590721, 2.614019],
                                    [89.771805, 2.575546],
                                    [89.809341, 2.756323],
                                    [89.628258, 2.794767],
                                    [89.590721, 2.614019],
                                ]
                            ],
                            "type": "Polygon",
                        },
                    },
                ],
                "type": "FeatureCollection",
            }
        )
        config = PluginConfig()
        config.priority = 0
        for p in search_result:
            p.downloader = Download("peps", config)
            p.downloader_auth = Authentication("peps", config)
        search_result.number_matched = len(search_result)
        return search_result

    @mock.patch("eodag.rest.core.eodag_api.search", autospec=True)
    def _request_valid_raw(
        self,
        url: str,
        mock_search: Mock,
        expected_search_kwargs: Union[
            List[Dict[str, Any]], Dict[str, Any], None
        ] = None,
        method: str = "GET",
        post_data: Optional[Any] = None,
        search_call_count: Optional[int] = None,
    ) -> httpx.Response:
        mock_search.return_value = self.mock_search_result()
        response = self.app.request(
            method,
            url,
            json=post_data,
            follow_redirects=True,
            headers={"Content-Type": "application/json"} if method == "POST" else {},
        )

        if search_call_count is not None:
            self.assertEqual(mock_search.call_count, search_call_count)

        if (
            expected_search_kwargs is not None
            and search_call_count is not None
            and search_call_count > 1
        ):
            self.assertIsInstance(
                expected_search_kwargs,
                list,
                "expected_search_kwargs must be a list if search_call_count > 1",
            )
            for single_search_kwargs in expected_search_kwargs:
                mock_search.assert_any_call(**single_search_kwargs)
        elif expected_search_kwargs is not None:
            mock_search.assert_called_once_with(**expected_search_kwargs)

        self.assertEqual(200, response.status_code, response.text)

        return response

    def _request_valid(
        self,
        url: str,
        expected_search_kwargs: Union[
            List[Dict[str, Any]], Dict[str, Any], None
        ] = None,
        method: str = "GET",
        post_data: Optional[Any] = None,
        search_call_count: Optional[int] = None,
        check_links: bool = True,
    ) -> Any:
        response = self._request_valid_raw(
            url,
            expected_search_kwargs=expected_search_kwargs,
            method=method,
            post_data=post_data,
            search_call_count=search_call_count,
        )

        # Assert response format is GeoJSON
        result = geojson.loads(response.content.decode("utf-8"))

        if check_links:
            self.assert_links_valid(result)

        return result

    def assert_links_valid(self, element: Any):
        """Checks that element links are valid"""
        self.assertIsInstance(element, dict)
        self.assertIn("links", element, f"links not found in {str(element)}")
        self.assertIsInstance(element["links"], list)
        links = element["links"]

        known_rel = [
            "self",
            "root",
            "parent",
            "child",
            "items",
            "service-desc",
            "service-doc",
            "conformance",
            "search",
            "data",
            "collection",
        ]
        required_links_rel = ["self", "root"]

        for link in links:
            # known relations
            self.assertIn(link["rel"], known_rel)
            # must start with app base-url
            assert link["href"].startswith(str(self.app.base_url))
            # HEAD must be valid
            self._request_valid_raw(link["href"], method="HEAD")
            # GET must be valid
            self._request_valid_raw(link["href"])

            if link["rel"] in required_links_rel:
                required_links_rel.remove(link["rel"])

        # required relations
        self.assertEqual(
            len(required_links_rel),
            0,
            f"missing {required_links_rel} relation(s) in {links}",
        )

    def _request_not_valid(
        self, url: str, method: str = "GET", post_data: Optional[Any] = None
    ) -> None:
        response = self.app.request(
            method,
            url,
            json=post_data,
            follow_redirects=True,
            headers={"Content-Type": "application/json"} if method == "POST" else {},
        )
        response_content = json.loads(response.content.decode("utf-8"))

        self.assertEqual(400, response.status_code)
        self.assertIn("description", response_content)

    def _request_not_found(self, url: str):
        response = self.app.get(url, follow_redirects=True)
        response_content = json.loads(response.content.decode("utf-8"))

        self.assertEqual(404, response.status_code)
        self.assertIn("description", response_content)
        self.assertIn("NotAvailableError", response_content["description"])

    def _request_accepted(self, url: str):
        response = self.app.get(url, follow_redirects=True)
        response_content = json.loads(response.content.decode("utf-8"))
        self.assertEqual(202, response.status_code)
        self.assertIn("description", response_content)
        self.assertIn("location", response_content)
        return response_content

    def test_request_params(self):
        self._request_not_valid(f"search?collections={self.tested_product_type}&bbox=1")
        self._request_not_valid(
            f"search?collections={self.tested_product_type}&bbox=0,43,1"
        )
        self._request_not_valid(
            f"search?collections={self.tested_product_type}&bbox=0,,1"
        )
        self._request_not_valid(
            f"search?collections={self.tested_product_type}&bbox=a,43,1,44"
        )

        self._request_valid(
            f"search?collections={self.tested_product_type}",
            expected_search_kwargs=dict(
                productType=self.tested_product_type,
                page=1,
                items_per_page=DEFAULT_ITEMS_PER_PAGE,
                raise_errors=False,
                count=True,
            ),
        )
        self._request_valid(
            f"search?collections={self.tested_product_type}&bbox=0,43,1,44",
            expected_search_kwargs=dict(
                productType=self.tested_product_type,
                page=1,
                items_per_page=DEFAULT_ITEMS_PER_PAGE,
                geom=box(0, 43, 1, 44, ccw=False),
                raise_errors=False,
                count=True,
            ),
        )

    def test_items_response(self):
        """Returned items properties must be mapped as expected"""
        resp_json = self._request_valid(
            f"search?collections={self.tested_product_type}",
        )
        res = resp_json.features
        self.assertEqual(len(res), 2)
        first_props = res[0]["properties"]
        self.assertCountEqual(
            res[0].keys(),
            [
                "type",
                "stac_version",
                "stac_extensions",
                "bbox",
                "collection",
                "links",
                "assets",
                "id",
                "geometry",
                "properties",
            ],
        )
        self.assertEqual(len(first_props["providers"]), 1)
        self.assertCountEqual(
            first_props["providers"][0].keys(),
            ["name", "description", "roles", "url", "priority"],
        )
        self.assertEqual(first_props["providers"][0]["name"], "peps")
        self.assertEqual(first_props["providers"][0]["roles"], ["host"])
        self.assertEqual(first_props["providers"][0]["url"], "https://peps.cnes.fr")
        self.assertEqual(first_props["datetime"], "2018-02-15T23:53:22.871Z")
        self.assertEqual(first_props["start_datetime"], "2018-02-15T23:53:22.871Z")
        self.assertEqual(first_props["end_datetime"], "2018-02-16T00:12:14.035Z")
        self.assertEqual(first_props["license"], "proprietary")
        self.assertEqual(first_props["platform"], "S1A")
        self.assertEqual(first_props["instruments"], ["SAR-C SAR"])
        self.assertEqual(first_props["eo:cloud_cover"], 0)
        self.assertEqual(first_props["sat:absolute_orbit"], 20624)
        self.assertEqual(first_props["sar:product_type"], "OCN")
        self.assertEqual(first_props["order:status"], "succeeded")
        self.assertEqual(res[0]["assets"]["downloadLink"]["storage:tier"], "ONLINE")
        self.assertEqual(res[1]["assets"]["downloadLink"]["storage:tier"], "OFFLINE")
        self.assertEqual(res[1]["properties"]["order:status"], "orderable")

    def test_not_found(self):
        """A request to eodag server with a not supported product type must return a 404 HTTP error code"""
        self._request_not_found("search?collections=ZZZ&bbox=0,43,1,44")

    @mock.patch(
        "eodag.rest.core.eodag_api.search",
        autospec=True,
        side_effect=AuthenticationError("you are not authorized"),
    )
    def test_auth_error(self, mock_search: Mock):
        """A request to eodag server raising a Authentication error must return a 500 HTTP error code"""

        with self.assertLogs(level="ERROR") as cm_logs:
            response = self.app.get(
                f"search?collections={self.tested_product_type}", follow_redirects=True
            )
            response_content = json.loads(response.content.decode("utf-8"))

            self.assertIn("description", response_content)
            self.assertIn("AuthenticationError", str(cm_logs.output))
            self.assertIn("you are not authorized", str(cm_logs.output))

        self.assertEqual(500, response.status_code)

    @mock.patch(
        "eodag.rest.core.eodag_api.search",
        autospec=True,
        side_effect=TimeOutError("too long"),
    )
    def test_timeout_error(self, mock_search: Mock):
        """A request to eodag server raising a Authentication error must return a 500 HTTP error code"""
        with self.assertLogs(level="ERROR") as cm_logs:
            response = self.app.get(
                f"search?collections={self.tested_product_type}", follow_redirects=True
            )
            response_content = json.loads(response.content.decode("utf-8"))

            self.assertIn("description", response_content)
            self.assertIn("TimeOutError", str(cm_logs.output))
            self.assertIn("too long", str(cm_logs.output))

        self.assertEqual(504, response.status_code)

    def test_filter(self):
        """latestIntersect filter should only keep the latest products once search area is fully covered"""
        result1 = self._request_valid(
            f"search?collections={self.tested_product_type}&bbox=89.65,2.65,89.7,2.7",
            expected_search_kwargs=dict(
                productType=self.tested_product_type,
                geom=box(89.65, 2.65, 89.7, 2.7, ccw=False),
                page=1,
                items_per_page=DEFAULT_ITEMS_PER_PAGE,
                raise_errors=False,
                count=True,
            ),
        )
        self.assertEqual(len(result1.features), 2)
        result2 = self._request_valid(
            f"search?collections={self.tested_product_type}&bbox=89.65,2.65,89.7,2.7&crunch=filterLatestIntersect",
            expected_search_kwargs=dict(
                productType=self.tested_product_type,
                page=1,
                items_per_page=DEFAULT_ITEMS_PER_PAGE,
                geom=box(89.65, 2.65, 89.7, 2.7, ccw=False),
                raise_errors=False,
                count=True,
            ),
        )
        # only one product is returned with filter=latestIntersect
        self.assertEqual(len(result2.features), 1)

    def test_date_search(self):
        """Search through eodag server /search endpoint using dates filering should return a valid response"""
        self._request_valid(
            f"search?collections={self.tested_product_type}&bbox=0,43,1,44&datetime=2018-01-20/2018-01-25",
            expected_search_kwargs=dict(
                productType=self.tested_product_type,
                page=1,
                items_per_page=DEFAULT_ITEMS_PER_PAGE,
                start="2018-01-20T00:00:00.000Z",
                end="2018-01-25T00:00:00.000Z",
                geom=box(0, 43, 1, 44, ccw=False),
                raise_errors=False,
                count=True,
            ),
        )
        self._request_valid(
            f"search?collections={self.tested_product_type}&bbox=0,43,1,44&datetime=2018-01-20/..",
            expected_search_kwargs=dict(
                productType=self.tested_product_type,
                page=1,
                items_per_page=DEFAULT_ITEMS_PER_PAGE,
                start="2018-01-20T00:00:00.000Z",
                geom=box(0, 43, 1, 44, ccw=False),
                raise_errors=False,
                count=True,
            ),
        )
        self._request_valid(
            f"search?collections={self.tested_product_type}&bbox=0,43,1,44&datetime=../2018-01-25",
            expected_search_kwargs=dict(
                productType=self.tested_product_type,
                page=1,
                items_per_page=DEFAULT_ITEMS_PER_PAGE,
                end="2018-01-25T00:00:00.000Z",
                geom=box(0, 43, 1, 44, ccw=False),
                raise_errors=False,
                count=True,
            ),
        )
        self._request_valid(
            f"search?collections={self.tested_product_type}&bbox=0,43,1,44&datetime=2018-01-20",
            expected_search_kwargs=dict(
                productType=self.tested_product_type,
                page=1,
                items_per_page=DEFAULT_ITEMS_PER_PAGE,
                start="2018-01-20T00:00:00.000Z",
                end="2018-01-20T00:00:00.000Z",
                geom=box(0, 43, 1, 44, ccw=False),
                raise_errors=False,
                count=True,
            ),
        )

    def test_date_search_from_items(self):
        """Search through eodag server collection/items endpoint using dates filering should return a valid response"""
        self._request_valid(
            f"collections/{self.tested_product_type}/items?bbox=0,43,1,44",
            expected_search_kwargs=dict(
                productType=self.tested_product_type,
                page=1,
                items_per_page=DEFAULT_ITEMS_PER_PAGE,
                geom=box(0, 43, 1, 44, ccw=False),
                raise_errors=False,
                count=True,
            ),
        )
        self._request_valid(
            f"collections/{self.tested_product_type}/items?bbox=0,43,1,44&datetime=2018-01-20/2018-01-25",
            expected_search_kwargs=dict(
                productType=self.tested_product_type,
                page=1,
                items_per_page=DEFAULT_ITEMS_PER_PAGE,
                start="2018-01-20T00:00:00.000Z",
                end="2018-01-25T00:00:00.000Z",
                geom=box(0, 43, 1, 44, ccw=False),
                raise_errors=False,
                count=True,
            ),
        )

    def test_date_search_from_catalog_items(self):
        """Search through eodag server catalog/items endpoint using dates filering should return a valid response"""
        results = self._request_valid(
            f"catalogs/{self.tested_product_type}/year/2018/month/01/items?bbox=0,43,1,44",
            expected_search_kwargs=dict(
                productType=self.tested_product_type,
                page=1,
                items_per_page=DEFAULT_ITEMS_PER_PAGE,
                start="2018-01-01T00:00:00Z",
                end="2018-02-01T00:00:00Z",
                geom=box(0, 43, 1, 44, ccw=False),
                raise_errors=False,
                count=True,
            ),
        )
        self.assertEqual(len(results.features), 2)

        results = self._request_valid(
            f"catalogs/{self.tested_product_type}/year/2018/month/01/items"
            "?bbox=0,43,1,44&datetime=2018-01-20/2018-01-25",
            expected_search_kwargs=dict(
                productType=self.tested_product_type,
                page=1,
                items_per_page=DEFAULT_ITEMS_PER_PAGE,
                start="2018-01-20T00:00:00Z",
                end="2018-01-25T00:00:00Z",
                geom=box(0, 43, 1, 44, ccw=False),
                raise_errors=False,
                count=True,
            ),
        )
        self.assertEqual(len(results.features), 2)

        results = self._request_valid(
            f"catalogs/{self.tested_product_type}/year/2018/month/01/items"
            "?bbox=0,43,1,44&datetime=2018-01-20/2019-01-01",
            expected_search_kwargs=dict(
                productType=self.tested_product_type,
                page=1,
                items_per_page=DEFAULT_ITEMS_PER_PAGE,
                start="2018-01-20T00:00:00Z",
                end="2018-02-01T00:00:00Z",
                geom=box(0, 43, 1, 44, ccw=False),
                raise_errors=False,
                count=True,
            ),
        )
        self.assertEqual(len(results.features), 2)

        results = self._request_valid(
            f"catalogs/{self.tested_product_type}/year/2018/month/01/items"
            "?bbox=0,43,1,44&datetime=2019-01-01/2019-01-31",
        )
        self.assertEqual(len(results.features), 0)

    def test_catalog_browse(self):
        """Browsing catalogs through eodag server should return a valid response"""
        result = self._request_valid(
            f"catalogs/{self.tested_product_type}/year/2018/month/01/day"
        )
        self.assertListEqual(
            [str(i) for i in range(1, 32)],
            [it["title"] for it in result.get("links", []) if it["rel"] == "child"],
        )

    def test_catalog_browse_date_search(self):
        """
        Browsing catalogs with date filtering through eodag server should return a valid response
        """
        self._request_valid(
            f"catalogs/{self.tested_product_type}/year/2018/month/01/items",
            expected_search_kwargs=dict(
                productType=self.tested_product_type,
                page=1,
                items_per_page=DEFAULT_ITEMS_PER_PAGE,
                start="2018-01-01T00:00:00Z",
                end="2018-02-01T00:00:00Z",
                raise_errors=False,
                count=True,
            ),
        )
        # args & catalog intersection
        self._request_valid(
            f"catalogs/{self.tested_product_type}/year/2018/month/01/items?datetime=2018-01-20/2018-02-15",
            expected_search_kwargs=dict(
                productType=self.tested_product_type,
                page=1,
                items_per_page=DEFAULT_ITEMS_PER_PAGE,
                start="2018-01-20T00:00:00Z",
                end="2018-02-01T00:00:00Z",
                raise_errors=False,
                count=True,
            ),
        )
        self._request_valid(
            f"catalogs/{self.tested_product_type}/year/2018/month/01/items?datetime=2018-01-20/..",
            expected_search_kwargs=dict(
                productType=self.tested_product_type,
                page=1,
                items_per_page=DEFAULT_ITEMS_PER_PAGE,
                start="2018-01-20T00:00:00Z",
                end="2018-02-01T00:00:00Z",
                raise_errors=False,
                count=True,
            ),
        )
        self._request_valid(
            f"catalogs/{self.tested_product_type}/year/2018/month/01/items?datetime=../2018-01-05",
            expected_search_kwargs=dict(
                productType=self.tested_product_type,
                page=1,
                items_per_page=DEFAULT_ITEMS_PER_PAGE,
                start="2018-01-01T00:00:00Z",
                end="2018-01-05T00:00:00Z",
                raise_errors=False,
                count=True,
            ),
        )
        self._request_valid(
            f"catalogs/{self.tested_product_type}/year/2018/month/01/items?datetime=2018-01-05",
            expected_search_kwargs=dict(
                productType=self.tested_product_type,
                page=1,
                items_per_page=DEFAULT_ITEMS_PER_PAGE,
                start="2018-01-05T00:00:00Z",
                end="2018-01-05T00:00:00Z",
                raise_errors=False,
                count=True,
            ),
        )
        result = self._request_valid(
            f"catalogs/{self.tested_product_type}/year/2018/month/01/items?datetime=../2017-08-01",
        )
        self.assertEqual(len(result["features"]), 0)

    def test_date_search_from_catalog_items_with_provider(self):
        """Search through eodag server catalog/items endpoint using dates filtering should return a valid response
        and the provider should be added to the item link
        """
        results = self._request_valid(
            f"catalogs/{self.tested_product_type}/year/2018/month/01/items?bbox=0,43,1,44&provider=peps",
            expected_search_kwargs=dict(
                productType=self.tested_product_type,
                page=1,
                items_per_page=DEFAULT_ITEMS_PER_PAGE,
                start="2018-01-01T00:00:00Z",
                end="2018-02-01T00:00:00Z",
                provider="peps",
                geom=box(0, 43, 1, 44, ccw=False),
                raise_errors=True,
                count=True,
            ),
        )
        self.assertEqual(len(results.features), 2)
        links = results.features[0]["links"]
        self_link = None
        for link in links:
            if link["rel"] == "self":
                self_link = link
        self.assertIsNotNone(self_link)
        self.assertIn("?provider=peps", self_link["href"])
        self.assertEqual(
            results["features"][0]["properties"]["peps:providerProperty"], "foo"
        )

    def test_search_item_id_from_catalog(self):
        """Search by id through eodag server /catalog endpoint should return a valid response"""
        self._request_valid(
            f"catalogs/{self.tested_product_type}/items/foo",
            expected_search_kwargs={
                "id": "foo",
                "productType": self.tested_product_type,
                "provider": None,
            },
        )

    def test_search_item_id_from_collection(self):
        """Search by id through eodag server /collection endpoint should return a valid response"""
        self._request_valid(
            f"collections/{self.tested_product_type}/items/foo",
            expected_search_kwargs={
                "id": "foo",
                "productType": self.tested_product_type,
                "provider": None,
            },
        )

    def test_collection(self):
        """Requesting a collection through eodag server should return a valid response"""
        result = self._request_valid(f"collections/{self.tested_product_type}")
        self.assertEqual(result["id"], self.tested_product_type)
        for link in result["links"]:
            self.assertIn(link["rel"], ["self", "root", "items"])

    def test_cloud_cover_post_search(self):
        """POST search with cloudCover filtering through eodag server should return a valid response"""
        self._request_valid(
            "search",
            method="POST",
            post_data={
                "collections": [self.tested_product_type],
                "bbox": [0, 43, 1, 44],
                "query": {"eo:cloud_cover": {"lte": 10}},
            },
            expected_search_kwargs=dict(
                productType=self.tested_product_type,
                page=1,
                items_per_page=DEFAULT_ITEMS_PER_PAGE,
                cloudCover=10,
                geom=box(0, 43, 1, 44, ccw=False),
                raise_errors=False,
                count=True,
            ),
        )

    def test_intersects_post_search(self):
        """POST search with intersects filtering through eodag server should return a valid response"""
        self._request_valid(
            "search",
            method="POST",
            post_data={
                "collections": [self.tested_product_type],
                "intersects": {
                    "type": "Polygon",
                    "coordinates": [[[0, 43], [0, 44], [1, 44], [1, 43], [0, 43]]],
                },
            },
            expected_search_kwargs=dict(
                productType=self.tested_product_type,
                page=1,
                items_per_page=DEFAULT_ITEMS_PER_PAGE,
                geom=box(0, 43, 1, 44, ccw=False),
                raise_errors=False,
                count=True,
            ),
        )

    def test_date_post_search(self):
        """POST search with datetime filtering through eodag server should return a valid response"""
        self._request_valid(
            "search",
            method="POST",
            post_data={
                "collections": [self.tested_product_type],
                "datetime": "2018-01-20/2018-01-25",
            },
            expected_search_kwargs=dict(
                productType=self.tested_product_type,
                page=1,
                items_per_page=DEFAULT_ITEMS_PER_PAGE,
                start="2018-01-20T00:00:00.000Z",
                end="2018-01-25T00:00:00.000Z",
                raise_errors=False,
                count=True,
            ),
        )
        self._request_valid(
            "search",
            method="POST",
            post_data={
                "collections": [self.tested_product_type],
                "datetime": "2018-01-20/..",
            },
            expected_search_kwargs=dict(
                productType=self.tested_product_type,
                page=1,
                items_per_page=DEFAULT_ITEMS_PER_PAGE,
                start="2018-01-20T00:00:00.000Z",
                raise_errors=False,
                count=True,
            ),
        )
        self._request_valid(
            "search",
            method="POST",
            post_data={
                "collections": [self.tested_product_type],
                "datetime": "../2018-01-25",
            },
            expected_search_kwargs=dict(
                productType=self.tested_product_type,
                page=1,
                items_per_page=DEFAULT_ITEMS_PER_PAGE,
                end="2018-01-25T00:00:00.000Z",
                raise_errors=False,
                count=True,
            ),
        )
        self._request_valid(
            "search",
            method="POST",
            post_data={
                "collections": [self.tested_product_type],
                "datetime": "2018-01-20",
            },
            expected_search_kwargs=dict(
                productType=self.tested_product_type,
                page=1,
                items_per_page=DEFAULT_ITEMS_PER_PAGE,
                start="2018-01-20T00:00:00.000Z",
                end="2018-01-20T00:00:00.000Z",
                raise_errors=False,
                count=True,
            ),
        )

    def test_ids_post_search(self):
        """POST search with ids filtering through eodag server should return a valid response"""
        self._request_valid(
            "search",
            method="POST",
            post_data={
                "collections": [self.tested_product_type],
                "ids": ["foo", "bar"],
            },
            search_call_count=2,
            expected_search_kwargs=[
                {
                    "provider": None,
                    "id": "foo",
                    "productType": self.tested_product_type,
                },
                {
                    "provider": None,
                    "id": "bar",
                    "productType": self.tested_product_type,
                },
            ],
        )

    @mock.patch("eodag.rest.core.eodag_api.search", autospec=True)
    def test_provider_prefix_post_search(self, mock_search):
        """provider prefixes should be removed from query parameters"""
        post_data = {
            "collections": ["ERA5_SL"],
            "provider": "cop_cds",
            "query": {
                "cop_cds:month": {"eq": "10"},
                "cop_cds:year": {"eq": "2010"},
                "cop_cds:day": {"eq": "10"},
            },
        }
        mock_search.return_value = SearchResult.from_geojson(
            {"features": [], "type": "FeatureCollection"}
        )
        self.app.request(
            method="POST",
            url="search",
            json=post_data,
            follow_redirects=True,
            headers={"Content-Type": "application/json"},
        )
        expected_search_kwargs = dict(
            productType="ERA5_SL",
            page=1,
            items_per_page=DEFAULT_ITEMS_PER_PAGE,
            month="10",
            year="2010",
            day="10",
            raise_errors=True,
            count=True,
            provider="cop_cds",
        )
        mock_search.assert_called_once_with(**expected_search_kwargs)

    def test_search_response_contains_pagination_info(self):
        """Responses to valid search requests must return a geojson with pagination info in properties"""
        response = self._request_valid(f"search?collections={self.tested_product_type}")
        self.assertIn("numberMatched", response)
        self.assertIn("numberReturned", response)

    def test_search_provider_in_downloadlink(self):
        """Search through eodag server and check that specified provider appears in downloadLink"""
        # with provider (get)
        response = self._request_valid(
            f"search?collections={self.tested_product_type}&provider=peps"
        )
        response_items = [f for f in response["features"]]
        self.assertTrue(
            all(
                [
                    i["assets"]["downloadLink"]["href"].endswith(
                        "download?provider=peps"
                    )
                    for i in response_items
                ]
            )
        )
        # with provider (post)
        response = self._request_valid(
            "search",
            method="POST",
            post_data={"collections": [self.tested_product_type], "provider": "peps"},
        )
        response_items = [f for f in response["features"]]
        self.assertTrue(
            all(
                [
                    i["assets"]["downloadLink"]["href"].endswith(
                        "download?provider=peps"
                    )
                    for i in response_items
                ]
            )
        )
        # without provider
        response = self._request_valid(f"search?collections={self.tested_product_type}")
        response_items = [f for f in response["features"]]
        self.assertTrue(
            all(
                [
                    i["assets"]["downloadLink"]["href"].endswith(
                        "download?provider=peps"
                    )
                    for i in response_items
                ]
            )
        )

    def test_assets_alt_url_blacklist(self):
        """Search through eodag server must not have alternate link if in blacklist"""
        # no blacklist
        response = self._request_valid(f"search?collections={self.tested_product_type}")
        response_items = [f for f in response["features"]]
        self.assertTrue(
            all(["alternate" in i["assets"]["downloadLink"] for i in response_items]),
            "alternate links are missing",
        )

        # with blacklist
        try:
            Settings.from_environment.cache_clear()
            with temporary_environment(
                EODAG_ORIGIN_URL_BLACKLIST="https://peps.cnes.fr"
            ):
                response = self._request_valid(
                    f"search?collections={self.tested_product_type}"
                )
                response_items = [f for f in response["features"]]
                self.assertTrue(
                    all(
                        [
                            "alternate" not in i["assets"]["downloadLink"]
                            for i in response_items
                        ]
                    ),
                    "alternate links were not removed",
                )
        finally:
            Settings.from_environment.cache_clear()

    @mock.patch(
        "eodag.rest.core.eodag_api.guess_product_type", autospec=True, return_value=[]
    )
    @mock.patch(
        "eodag.rest.core.eodag_api.list_product_types",
        autospec=True,
        return_value=[
            {"_id": "S2_MSI_L1C", "ID": "S2_MSI_L1C", "title": "SENTINEL2 Level-1C"},
            {"_id": "S2_MSI_L2A", "ID": "S2_MSI_L2A"},
        ],
    )
    def test_list_product_types_ok(self, list_pt: Mock, guess_pt: Mock):
        """A simple request for product types with(out) a provider must succeed"""
        for url in ("/collections",):
            r = self.app.get(url)
            self.assertTrue(list_pt.called)
            self.assertEqual(200, r.status_code)
            self.assertListEqual(
                ["S2_MSI_L1C", "S2_MSI_L2A"],
                [
                    col["id"]
                    for col in json.loads(r.content.decode("utf-8")).get(
                        "collections", []
                    )
                ],
            )

        guess_pt.return_value = ["S2_MSI_L1C"]
        url = "/collections?instrument=MSI"
        r = self.app.get(url)
        self.assertTrue(guess_pt.called)
        self.assertTrue(list_pt.called)
        self.assertEqual(200, r.status_code)
        resp_json = json.loads(r.content.decode("utf-8"))
        self.assertListEqual(
            ["S2_MSI_L1C"],
            [col["id"] for col in resp_json.get("collections", [])],
        )
        self.assertEqual(resp_json["collections"][0]["title"], "SENTINEL2 Level-1C")

    @mock.patch(
        "eodag.rest.core.eodag_api.list_product_types",
        autospec=True,
        return_value=[
            {"_id": "S2_MSI_L1C", "ID": "S2_MSI_L1C"},
            {"_id": "S2_MSI_L2A", "ID": "S2_MSI_L2A"},
        ],
    )
    def test_list_product_types_nok(self, list_pt: Mock):
        """A request for product types with a not supported filter must return all product types"""
        url = "/collections?gibberish=gibberish"
        r = self.app.get(url)
        self.assertTrue(list_pt.called)
        self.assertEqual(200, r.status_code)
        self.assertListEqual(
            ["S2_MSI_L1C", "S2_MSI_L2A"],
            [
                col["id"]
                for col in json.loads(r.content.decode("utf-8")).get("collections", [])
            ],
        )

    @mock.patch(
        "eodag.plugins.authentication.base.Authentication.authenticate",
        autospec=True,
    )
    @mock.patch(
        "eodag.plugins.download.base.Download._stream_download_dict",
        autospec=True,
    )
    def test_download_item_from_catalog_stream(
        self, mock_download: Mock, mock_auth: Mock
    ):
        """Download through eodag server catalog should return a valid response"""

        expected_file = "somewhere.zip"

        mock_download.return_value = StreamResponse(
            content=iter(bytes(i) for i in range(0)),
            headers={
                "content-disposition": f"attachment; filename={expected_file}",
            },
        )

        response = self._request_valid_raw(
            f"catalogs/{self.tested_product_type}/items/foo/download?provider=peps"
        )
        mock_download.assert_called_once()

        header_content_disposition = parse_header(
            response.headers["content-disposition"]
        )
        response_filename = header_content_disposition.get_param("filename", None)
        self.assertEqual(response_filename, expected_file)

    @mock.patch(
        "eodag.plugins.authentication.base.Authentication.authenticate",
        autospec=True,
    )
    @mock.patch(
        "eodag.plugins.download.base.Download._stream_download_dict",
        autospec=True,
    )
    @mock.patch(
        "eodag.rest.core.eodag_api.download",
        autospec=True,
    )
    def test_download_item_from_collection_no_stream(
        self, mock_download: Mock, mock_stream_download: Mock, mock_auth: Mock
    ):
        """Download through eodag server catalog should return a valid response"""
        # download should be performed locally then deleted if streaming is not available
        tmp_dl_dir = TemporaryDirectory()
        expected_file = f"{tmp_dl_dir.name}.tar"
        Path(expected_file).touch()
        mock_download.return_value = expected_file
        mock_stream_download.side_effect = NotImplementedError()

        self._request_valid_raw(
            f"collections/{self.tested_product_type}/items/foo/download?provider=peps"
        )
        mock_download.assert_called_once()
        # downloaded file should have been immediatly deleted from the server
        assert not os.path.exists(
            expected_file
        ), f"File {expected_file} should have been deleted"

    @mock.patch(
        "eodag.rest.core.eodag_api.search",
        autospec=True,
    )
    def test_download_offline_item_from_catalog(self, mock_search):
        """Download an offline item through eodag server catalog should return a
        response with HTTP Status 202"""
        # mock_search_result returns 2 search results, only keep one
        two_results = self.mock_search_result()
        product = two_results[0]
        mock_search.return_value = SearchResult([product], 1)
        product.downloader_auth = MagicMock()
        product.downloader.order_download = MagicMock(return_value={"status": "foo"})
        product.downloader.order_download_status = MagicMock()
        product.downloader.order_response_process = MagicMock()
        product.downloader._stream_download_dict = MagicMock(
            side_effect=NotAvailableError("Product offline. Try again later.")
        )
        product.properties["orderLink"] = "http://somewhere?order=foo"
        product.properties["orderStatusLink"] = f"{NOT_AVAILABLE}?foo=bar"

        # ONLINE product with error
        product.properties["storageStatus"] = ONLINE_STATUS
        # status 404 and no order try
        self._request_not_found(
            f"catalogs/{self.tested_product_type}/items/foo/download"
        )
        product.downloader.order_download.assert_not_called()
        product.downloader.order_download_status.assert_not_called()
        product.downloader.order_response_process.assert_not_called()
        product.downloader._stream_download_dict.assert_called_once()
        product.downloader._stream_download_dict.reset_mock()

        # OFFLINE product with error
        product.properties["storageStatus"] = OFFLINE_STATUS
        # status 202 and order once and no status check
        resp_json = self._request_accepted(
            f"catalogs/{self.tested_product_type}/items/foo/download"
        )
        product.downloader.order_download.assert_called_once()
        product.downloader.order_download.reset_mock()
        product.downloader.order_download_status.assert_not_called()
        product.downloader.order_response_process.assert_called()
        product.downloader.order_response_process.reset_mock()
        product.downloader._stream_download_dict.assert_not_called()
        self.assertIn("status=foo", resp_json["location"])

        # OFFLINE product with error and no orderLink
        product.properties["storageStatus"] = OFFLINE_STATUS
        order_link = product.properties.pop("orderLink")
        # status 202 and no order try
        resp_json = self._request_accepted(
            f"catalogs/{self.tested_product_type}/items/foo/download"
        )
        product.downloader.order_download.assert_not_called()
        product.downloader.order_download_status.assert_not_called()
        product.downloader.order_response_process.assert_not_called()
        product.downloader._stream_download_dict.assert_called_once()
        product.downloader._stream_download_dict.reset_mock()

        # STAGING product and available orderStatusLink
        product.properties["storageStatus"] = STAGING_STATUS
        product.properties["orderLink"] = order_link
        product.properties["orderStatusLink"] = "http://somewhere?foo=bar"
        # status 202 and no order but status checked and no download try
        self._request_accepted(
            f"catalogs/{self.tested_product_type}/items/foo/download"
        )
        product.downloader.order_download.assert_not_called()
        product.downloader.order_download_status.assert_called_once()
        product.downloader.order_download_status.reset_mock()
        product.downloader.order_response_process.assert_called()
        product.downloader.order_response_process.reset_mock()
        product.downloader._stream_download_dict.assert_not_called()

    def test_root(self):
        """Request to / should return a valid response"""
        resp_json = self._request_valid("", check_links=False)
        self.assertEqual(resp_json["id"], "eodag-stac-api")
        self.assertEqual(resp_json["title"], "eodag-stac-api")
        self.assertEqual(resp_json["description"], "STAC API provided by EODAG")

        # customize root info
        try:
            Settings.from_environment.cache_clear()
            with temporary_environment(
                EODAG_STAC_API_LANDING_ID="foo-id",
                EODAG_STAC_API_TITLE="foo title",
                EODAG_STAC_API_DESCRIPTION="foo description",
            ):
                resp_json = self._request_valid("", check_links=False)
                self.assertEqual(resp_json["id"], "foo-id")
                self.assertEqual(resp_json["title"], "foo title")
                self.assertEqual(resp_json["description"], "foo description")
        finally:
            Settings.from_environment.cache_clear()

    def test_conformance(self):
        """Request to /conformance should return a valid response"""
        self._request_valid("conformance", check_links=False)

    def test_service_desc(self):
        """Request to service_desc should return a valid response"""
        service_desc = self._request_valid("api", check_links=False)
        self.assertIn("openapi", service_desc.keys())
        self.assertIn("eodag", service_desc["info"]["title"].lower())
        self.assertGreater(len(service_desc["paths"].keys()), 0)
        # test a 2nd call (ending slash must be ignored)
        self._request_valid("api/", check_links=False)

    def test_service_doc(self):
        """Request to service_doc should return a valid response"""
        response = self.app.get("api.html", follow_redirects=True)
        self.assertEqual(200, response.status_code)

    def test_stac_extension_oseo(self):
        """Request to oseo extension should return a valid response"""
        response = self._request_valid(
            "/extensions/oseo/json-schema/schema.json", check_links=False
        )
        self.assertEqual(response["title"], "OpenSearch for Earth Observation")
        self.assertEqual(response["allOf"][0]["$ref"], "#/definitions/oseo")

    def test_queryables(self):
        """Request to /queryables without parameter should return a valid response."""
        stac_common_queryables = list(StacQueryables.default_properties.keys())

        # neither provider nor product type are specified
        res_no_product_type_no_provider = self._request_valid(
            "queryables", check_links=False
        )

        # the response is in StacQueryables class format
        self.assertListEqual(
            list(res_no_product_type_no_provider.keys()),
            [
                "$schema",
                "$id",
                "type",
                "title",
                "description",
                "properties",
                "additionalProperties",
            ],
        )
        self.assertTrue(res_no_product_type_no_provider["additionalProperties"])

        # properties from stac common queryables are added and are the only ones of the response
        self.assertListEqual(
            list(res_no_product_type_no_provider["properties"].keys()),
            stac_common_queryables,
        )

    @mock.patch("eodag.plugins.search.qssearch.requests.get", autospec=True)
    def test_queryables_with_provider(self, mock_requests_get: Mock):
        """Request to /queryables with a valid provider as parameter should return a valid response."""
        queryables_path = os.path.join(
            TEST_RESOURCES_PATH, "stac/provider_queryables.json"
        )
        with open(queryables_path) as f:
            provider_queryables = json.load(f)
        mock_requests_get.return_value = MockResponse(
            provider_queryables, status_code=200
        )

        stac_common_queryables = list(StacQueryables.default_properties.keys())
        provider_stac_queryables_from_queryables_file = [
            "id",
            "gsd",
            "title",
            "s3:gsd",
            "datetime",
            "geometry",
            "platform",
            "processing:level",
            "s1:processing_level",
            "landsat:processing_level",
        ]

        # provider is specified without product type
        res_no_product_type_with_provider = self._request_valid(
            "queryables?provider=planetary_computer", check_links=False
        )

        mock_requests_get.assert_called_once_with(
            url="https://planetarycomputer.microsoft.com/api/stac/v1/search/../queryables",
            timeout=HTTP_REQ_TIMEOUT,
            headers=USER_AGENT,
            verify=True,
        )

        # the response is in StacQueryables class format
        self.assertListEqual(
            list(res_no_product_type_with_provider.keys()),
            [
                "$schema",
                "$id",
                "type",
                "title",
                "description",
                "properties",
                "additionalProperties",
            ],
        )
        self.assertTrue(res_no_product_type_with_provider["additionalProperties"])

        # properties from stac common queryables are added
        for p in stac_common_queryables:
            self.assertIn(
                p, list(res_no_product_type_with_provider["properties"].keys())
            )

        # properties from provider queryables are added (here the ones of planetary_computer)
        for provider_stac_queryable in provider_stac_queryables_from_queryables_file:
            self.assertIn(
                provider_stac_queryable, res_no_product_type_with_provider["properties"]
            )

        # properties from eodag general provider metadata mapping may be added (here an example with orbitDirection)
        stac_od_property = "sat:orbit_state"
        self.assertNotIn(
            stac_od_property, provider_stac_queryables_from_queryables_file
        )
        self.assertIn(stac_od_property, res_no_product_type_with_provider["properties"])

    def test_queryables_with_provider_error(self):
        """Request to /queryables with a wrong provider as parameter should return a UnsupportedProvider error."""
        response = self.app.get(
            "queryables?provider=not_supported_provider", follow_redirects=True
        )
        response_content = json.loads(response.content.decode("utf-8"))

        self.assertIn("description", response_content)
        self.assertIn("UnsupportedProvider", response_content["description"])

        self.assertEqual(400, response.status_code)

    @mock.patch("eodag.plugins.manager.PluginManager.get_auth_plugin", autospec=True)
    def test_product_type_queryables(self, mock_requests_session_post):
        """Request to /collections/{collection_id}/queryables should return a valid response."""

        @responses.activate(registry=responses.registries.OrderedRegistry)
        def run():
            queryables_path = os.path.join(
                TEST_RESOURCES_PATH, "stac/product_type_queryables.json"
            )
            with open(queryables_path) as f:
                provider_queryables = json.load(f)
            constraints_path = os.path.join(TEST_RESOURCES_PATH, "constraints.json")
            with open(constraints_path) as f:
                constraints = json.load(f)
            wekeo_main_constraints = {"constraints": constraints}

            planetary_computer_queryables_url = (
                "https://planetarycomputer.microsoft.com/api/stac/v1/search/../collections/"
                "sentinel-1-grd/queryables"
            )
            norm_planetary_computer_queryables_url = os.path.normpath(
                planetary_computer_queryables_url
            ).replace("https:/", "https://")
            wekeo_main_constraints_url = (
                "https://gateway.prod.wekeo2.eu/hda-broker/api/v1/dataaccess/queryable/"
                "EO:ESA:DAT:SENTINEL-1"
            )

            stac_common_queryables = list(StacQueryables.default_properties.keys())
            # when product type is given, "collection" item is not used
            stac_common_queryables.remove("collection")

            responses.add(
                responses.GET,
                planetary_computer_queryables_url,
                status=200,
                json=provider_queryables,
            )
            responses.add(
                responses.GET,
                wekeo_main_constraints_url,
                status=200,
                json=wekeo_main_constraints,
            )

            # no provider is specified with the product type (2 providers get a queryables or constraints file
            # among available providers for S1_SAR_GRD for the moment): queryables intersection returned
            res_product_type_no_provider = self._request_valid(
                "collections/S1_SAR_GRD/queryables",
                check_links=False,
            )
            self.assertEqual(len(responses.calls), 2)

            # check the mock call on planetary_computer
            self.assertEqual(
                norm_planetary_computer_queryables_url, responses.calls[0].request.url
            )
            self.assertIn(("timeout", 5), responses.calls[0].request.req_kwargs.items())
            self.assertIn(
                list(USER_AGENT.items())[0], responses.calls[0].request.headers.items()
            )
            self.assertIn(
                ("verify", True), responses.calls[0].request.req_kwargs.items()
            )
            # check the mock call on wekeo_main
            self.assertEqual(wekeo_main_constraints_url, responses.calls[1].request.url)
            self.assertIn(
                ("timeout", 60), responses.calls[1].request.req_kwargs.items()
            )
            self.assertIn(
                list(USER_AGENT.items())[0], responses.calls[1].request.headers.items()
            )
            self.assertIn(
                ("verify", True), responses.calls[1].request.req_kwargs.items()
            )

            # the response is in StacQueryables class format
            self.assertListEqual(
                list(res_product_type_no_provider.keys()),
                [
                    "$schema",
                    "$id",
                    "type",
                    "title",
                    "description",
                    "properties",
                    "additionalProperties",
                ],
            )
            self.assertFalse(res_product_type_no_provider["additionalProperties"])

            # properties from stac common queryables are added and are the only ones of the response
            self.assertListEqual(
                list(res_product_type_no_provider["properties"].keys()),
                stac_common_queryables,
            )
            # no property are added from providers queryables because none of them
            # is shared with all providers for this product type
            pl_s1_sar_grd_planetary_computer_queryable = "s1:processing_level"
            pl_s1_sar_grd_wekeo_main_queryable = "processingLevel"
            stac_pl_property = "processing:level"
            self.assertIn(
                pl_s1_sar_grd_planetary_computer_queryable,
                provider_queryables["properties"],
            )
            for constraint in wekeo_main_constraints["constraints"]:
                self.assertNotIn(pl_s1_sar_grd_wekeo_main_queryable, constraint)
            self.assertNotIn(
                stac_pl_property, res_product_type_no_provider["properties"]
            )

        run()

    def test_product_type_queryables_error(self):
        """Request to /collections/{collection_id}/queryables with a wrong collection_id
        should return a UnsupportedProductType error."""
        response = self.app.get(
            "collections/not_supported_product_type/queryables", follow_redirects=True
        )
        response_content = json.loads(response.content.decode("utf-8"))

        self.assertIn("description", response_content)
        self.assertIn("UnsupportedProductType", response_content["description"])

        self.assertEqual(400, response.status_code)

    @mock.patch("eodag.plugins.search.qssearch.requests.get", autospec=True)
    def test_product_type_queryables_with_provider(self, mock_requests_get):
        """Request a collection-specific list of queryables for a given provider
        using a queryables file should return a valid response."""
        queryables_path = os.path.join(
            TEST_RESOURCES_PATH, "stac/product_type_queryables.json"
        )
        with open(queryables_path) as f:
            provider_queryables = json.load(f)
        mock_requests_get.return_value = MockResponse(
            provider_queryables, status_code=200
        )

        planetary_computer_queryables_url = (
            "https://planetarycomputer.microsoft.com/api/stac/v1/search/../collections/"
            "sentinel-1-grd/queryables"
        )

        stac_common_queryables = list(StacQueryables.default_properties.keys())
        # when product type is given, "collection" item is not used
        stac_common_queryables.remove("collection")
        provider_stac_queryables_from_queryables_file = [
            "id",
            "datetime",
            "geometry",
            "platform",
            "processing:level",
        ]

        # provider and product type are specified
        res_product_type_with_provider = self._request_valid(
            "collections/S1_SAR_GRD/queryables?provider=planetary_computer",
            check_links=False,
        )

        mock_requests_get.assert_called_once_with(
            url=planetary_computer_queryables_url,
            timeout=HTTP_REQ_TIMEOUT,
            headers=USER_AGENT,
            verify=True,
        )

        # the response is in StacQueryables class format
        self.assertListEqual(
            list(res_product_type_with_provider.keys()),
            [
                "$schema",
                "$id",
                "type",
                "title",
                "description",
                "properties",
                "additionalProperties",
            ],
        )
        self.assertFalse(res_product_type_with_provider["additionalProperties"])

        # properties from stac common queryables are added
        for p in stac_common_queryables:
            self.assertIn(p, list(res_product_type_with_provider["properties"].keys()))

        # properties from provider product type queryables are added
        # (here the ones of S1_SAR_GRD for planetary_computer)
        for provider_stac_queryable in provider_stac_queryables_from_queryables_file:
            self.assertIn(
                provider_stac_queryable, res_product_type_with_provider["properties"]
            )

        # properties may be updated with info from provider queryables if
        # info exist (here an example with platformSerialIdentifier)
        stac_psi_property = "platform"
        self.assertEqual(
            "string", provider_queryables["properties"][stac_psi_property]["type"]
        )
        self.assertIn(
            "string",
            res_product_type_with_provider["properties"][stac_psi_property]["type"],
        )

        # properties from eodag provider metadata mapping may be added (here an example with orbitDirection)
        stac_od_property = "sat:orbit_state"
        self.assertNotIn(
            stac_od_property, provider_stac_queryables_from_queryables_file
        )
        self.assertIn(stac_od_property, res_product_type_with_provider["properties"])

    def test_stac_queryables_type(self):
        res = self._request_valid(
            "collections/S2_MSI_L2A/queryables?provider=creodias",
            check_links=False,
        )
        self.assertIn("eo:cloud_cover", res["properties"])
        cloud_cover = res["properties"]["eo:cloud_cover"]
        self.assertIn("type", cloud_cover)
        self.assertListEqual(["integer", "null"], cloud_cover["type"])
        self.assertIn("min", cloud_cover)
        self.assertListEqual([0, None], cloud_cover["min"])
        self.assertIn("max", cloud_cover)
        self.assertListEqual([100, None], cloud_cover["max"])
        self.assertIn("processing:level", res["properties"])
        processing_level = res["properties"]["processing:level"]
        self.assertListEqual(["string", "null"], processing_level["type"])
        self.assertNotIn(
            "min", processing_level
        )  # none values are left out in serialization

    @mock.patch("eodag.utils.requests.requests.Session.get", autospec=True)
    def test_product_type_queryables_from_constraints(
        self, mock_requests_session_constraints: Mock
    ):
        """Request a collection-specific list of queryables for a given provider
        using a constraints file should return a valid response."""
        constraints_path = os.path.join(TEST_RESOURCES_PATH, "constraints.json")
        with open(constraints_path) as f:
            constraints = json.load(f)
        for const in constraints:
            const["variable"].append("10m_u_component_of_wind")
        mock_requests_session_constraints.return_value = MockResponse(
            constraints, status_code=200
        )

        stac_common_queryables = list(StacQueryables.default_properties.keys())
        # when product type is given, "collection" item is not used
        stac_common_queryables.remove("collection")
        provider_queryables_from_constraints_file = [
            "year",
            "month",
            "day",
            "time",
            "variable",
            "leadtime_hour",
            "type",
            "api_product_type",
        ]
        # queryables properties not shared by all constraints must be removed
        not_shared_properties = ["leadtime_hour", "type"]
        provider_queryables_from_constraints_file = [
            f"cop_cds:{properties}"
            for properties in provider_queryables_from_constraints_file
            if properties not in not_shared_properties
        ]
        default_provider_stac_properties = [
            "cop_cds:api_product_type",
            "cop_cds:format",
        ]

        res = self._request_valid(
            "collections/ERA5_SL/queryables?provider=cop_cds",
            check_links=False,
        )

        mock_requests_session_constraints.assert_called_once_with(
            mock.ANY,
            "https://cds-beta.climate.copernicus.eu/api/catalogue/v1/collections/"
            "reanalysis-era5-single-levels/constraints.json",
            headers=USER_AGENT,
            auth=None,
            timeout=5,
        )

        # the response is in StacQueryables class format
        self.assertListEqual(
            list(res.keys()),
            [
                "$schema",
                "$id",
                "type",
                "title",
                "description",
                "properties",
                "additionalProperties",
            ],
        )
        self.assertFalse(res["additionalProperties"])

        # properties from stac common queryables are added
        for p in stac_common_queryables:
            self.assertIn(p, list(res["properties"].keys()))

        # properties from provider product type queryables and default properties are added
        # (here the ones of ERA5_SL for cop_cds)
        for provider_stac_queryable in list(
            set(
                provider_queryables_from_constraints_file
                + default_provider_stac_properties
            )
        ):
            self.assertIn(provider_stac_queryable, res["properties"])

    def test_cql_post_search(self):
        self._request_valid(
            "search",
            method="POST",
            post_data={
                "filter": {
                    "op": "and",
                    "args": [
                        {
                            "op": "in",
                            "args": [{"property": "id"}, ["foo", "bar"]],
                        },
                        {
                            "op": "=",
                            "args": [
                                {"property": "collection"},
                                self.tested_product_type,
                            ],
                        },
                    ],
                }
            },
            search_call_count=2,
            expected_search_kwargs=[
                {
                    "provider": None,
                    "id": "foo",
                    "productType": self.tested_product_type,
                },
                {
                    "provider": None,
                    "id": "bar",
                    "productType": self.tested_product_type,
                },
            ],
        )

        self._request_valid(
            "search",
            method="POST",
            post_data={
                "filter-lang": "cql2-json",
                "filter": {
                    "op": "and",
                    "args": [
                        {
                            "op": "=",
                            "args": [
                                {"property": "collection"},
                                self.tested_product_type,
                            ],
                        },
                        {"op": "=", "args": [{"property": "eo:cloud_cover"}, 10]},
                        {
                            "op": "t_intersects",
                            "args": [
                                {"property": "datetime"},
                                {
                                    "interval": [
                                        "2018-01-20T00:00:00Z",
                                        "2018-01-25T00:00:00Z",
                                    ]
                                },
                            ],
                        },
                        {
                            "op": "s_intersects",
                            "args": [
                                {"property": "geometry"},
                                {
                                    "type": "Polygon",
                                    "coordinates": [
                                        [[0, 43], [0, 44], [1, 44], [1, 43], [0, 43]]
                                    ],
                                },
                            ],
                        },
                    ],
                },
            },
            expected_search_kwargs={
                "productType": "S2_MSI_L1C",
                "geom": {
                    "type": "Polygon",
                    "coordinates": [[[0, 43], [0, 44], [1, 44], [1, 43], [0, 43]]],
                },
                "start": "2018-01-20T00:00:00Z",
                "end": "2018-01-25T00:00:00Z",
                "cloudCover": 10,
                "page": 1,
                "items_per_page": 20,
                "raise_errors": False,
                "count": True,
            },
        )

        self._request_not_valid(
            "search",
            method="POST",
            post_data={
                "filter": {
                    "op": "and",
                    "args": [
                        {
                            "op": "in",
                            "args": [{"property": "id"}, "foo", "bar"],
                        },
                        {
                            "op": "=",
                            "args": [
                                {"property": "collections"},
                                self.tested_product_type,
                            ],
                        },
                    ],
                }
            },
        )

    @mock.patch("eodag.rest.core.eodag_api.list_product_types", autospec=True)
    @mock.patch("eodag.rest.core.eodag_api.guess_product_type", autospec=True)
    def test_collection_free_text_search(self, guess_pt: Mock, list_pt: Mock):
        """Test STAC Collection free-text search"""

        url = "/collections?q=TERM1,TERM2"
        r = self.app.get(url)
        list_pt.assert_called_once_with(provider=None, fetch_providers=False)
        guess_pt.assert_called_once_with(
            free_text="TERM1,TERM2",
            platformSerialIdentifier=None,
            instrument=None,
            platform=None,
            missionStartDate=None,
            missionEndDate=None,
            productType=None,
        )
        self.assertEqual(200, r.status_code)
